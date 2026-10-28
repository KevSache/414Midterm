"""Is there a better clock time to buy an NBA contract?

The profit of buying one share at a five-minute mid and holding to
settlement is settlement minus that mid. Settlement is 1 or 0 for the
whole life of the market, so the best time to buy inside one market is
simply the five-minute bin where the mid was lowest.

That best time is saved for every market. It is not what the line is fit
to. A list of best times has no second variable, so it has no beta. The
line is fit to every bin:

    profit = market effect + beta * hours_from_tip

beta is the change in expected profit per share from waiting one more
hour, after each market's own average profit is removed. A beta near
zero means waiting longer does not raise or lower profit in a straight
line. A profit hump in the middle of the window can still hide inside a
near-zero slope, so the script also writes the mean profit in each half
hour.

The same full sample, not the top-10 chart sample, is scored for
accuracy. At each five-minute mark the average price is compared with
the share of markets that won. Relative accuracy is 1 when those match.
Mean profit at that mark is the win rate minus the average price, so a
mark priced exactly right earns about zero and a cheap mark scores
lower on accuracy. The marked time maximizes the average of that
accuracy and a 0-to-1 rescaling of mean profit.

The hourly files supply the mid. Posted size is not used. Postgres has
no price history. The window is the six hours before tip through the
three hours after, for every completed game winner and for every points,
assists, and rebounds line with matched volume above zero.

    python examples/optimal_time.py

A full pass reads each distinct hour once. There are a few hundred of
those hours. The host logs the bytes.
"""

import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import nba_analysis as nba
import prediction_score as predict

OUTPUT = Path("outputs/nba/optimal_time")
CACHE = OUTPUT / "cache"
# A slope inside this band, in profit per share per hour, is too small
# to matter across the nine-hour window. Two cents an hour is 18 cents
# from the first bin to the last.
PRACTICAL_BETA = 0.02
MIN_BINS = 3
# A five-minute mark needs this many markets before it can be the joint
# best time. Thinner samples use the fullest mark in the table instead.
MIN_SLOT_MARKETS = 8


def as_utc(value):
    """Listed tip as UTC. Naive timestamps from Postgres are already UTC."""
    tip = pd.Timestamp(value)
    if tip.tzinfo is None:
        return tip.tz_localize("UTC")
    return tip.tz_convert("UTC")


def load_markets(pg):
    """Every completed game winner, and every traded points, assists, or rebounds line."""
    games = nba.load_games(pg)
    props = nba.load_props(pg, ["points", "assists", "rebounds"])
    props = props[props["volume_clob"] > 0].copy()
    records = []
    for _, row in games.iterrows():
        asset_id, _first = nba.token_for(row, row["away"])
        records.append(
            {
                "sample": "games",
                "market_id": row["slug"],
                "cluster": row["slug"],
                "condition_id": row["condition_id"],
                "asset_id": asset_id,
                "tip": as_utc(row["tip"]),
                "label": row["title"],
                "volume_clob": float(row["volume_clob"]) if pd.notna(row["volume_clob"]) else 0.0,
                "resolved_yes": row["resolved_yes"],
                "away": row["away"],
                "home": row["home"],
                "game_slug": row["slug"],
                "stat": "",
                "player_slug": "",
                "line": np.nan,
            }
        )
    for _, row in props.iterrows():
        asset_id, first = nba.yes_token(row)
        records.append(
            {
                "sample": "props",
                "market_id": row["slug"],
                "cluster": row["game_slug"],
                "condition_id": row["condition_id"],
                "asset_id": asset_id,
                "tip": as_utc(row["tip"]),
                "label": nba.prop_short_name(row["player"], row["stat"], row["line"]),
                "volume_clob": float(row["volume_clob"]),
                "resolved_yes": row["resolved_yes"],
                "yes_is_first": first,
                "away": row["away"],
                "home": row["home"],
                "game_slug": row["game_slug"],
                "stat": row["stat"],
                "player_slug": row["player_slug"],
                "line": float(row["line"]),
            }
        )
    return records


def settle_games(records):
    """Away-team win from Polymarket, or from the ESPN score when that row is missing."""
    cache, boards, plays = {}, {}, {}
    for record in records:
        if record["sample"] != "games":
            continue
        game = {
            "slug": record["game_slug"],
            "game_slug": record["game_slug"],
            "away": record["away"],
            "home": record["home"],
        }
        result = nba.fetch_result(game, cache, boards, plays)
        resolved = record["resolved_yes"]
        if pd.notna(resolved):
            record["won"] = bool(resolved)
            record["settle_source"] = "polymarket"
        elif result is None or result["away_score"] == result["home_score"]:
            record["won"] = None
            record["settle_source"] = ""
        else:
            record["won"] = result["away_score"] > result["home_score"]
            record["settle_source"] = "espn"
    return records


def settle_props(records):
    """Over wins from Polymarket, or from the ESPN box score when that row is missing."""
    cache, boards, plays = {}, {}, {}
    for record in records:
        if record["sample"] != "props":
            continue
        game = {
            "slug": record["game_slug"],
            "game_slug": record["game_slug"],
            "away": record["away"],
            "home": record["home"],
        }
        result = nba.fetch_result(game, cache, boards, plays)
        ticks = nba.stat_ticks(
            None if result is None else result["plays"],
            record["player_slug"],
            record["stat"],
        )
        final = nba.prop_final(result, record["player_slug"], record["stat"], ticks)
        resolved = record["resolved_yes"]
        if pd.notna(resolved):
            yes_won = bool(resolved)
            record["won"] = yes_won if record["yes_is_first"] else (not yes_won)
            record["settle_source"] = "polymarket"
        elif final is None or final == record["line"]:
            record["won"] = None
            record["settle_source"] = ""
        else:
            record["won"] = final > record["line"]
            record["settle_source"] = "espn"
    return records


def read_mids(records):
    """Five-minute mids. Each distinct hour file is read once."""
    needed = {}
    for record in records:
        tip = record["tip"].to_pydatetime()
        for moment in nba.hours_around(tip):
            needed.setdefault(moment, set()).add((record["condition_id"], record["asset_id"]))
    print(f"{len(needed)} distinct hours to read.", flush=True)
    CACHE.mkdir(parents=True, exist_ok=True)
    frames = []
    duck = None
    for index, moment in enumerate(sorted(needed), start=1):
        key = nba.hour_key(moment)
        cached = CACHE / f"{key}.csv"
        if cached.exists():
            frame = pd.read_csv(cached)
            if not frame.empty:
                frames.append(frame)
            continue
        if duck is None:
            duck = nba.connect_duckdb()
        print(f"hour {index}/{len(needed)} {key}", flush=True)
        frame = nba.read_hour(duck, moment, list(needed[moment]))
        if frame is None:
            continue
        frame.to_csv(cached, index=False)
        if not frame.empty:
            frames.append(frame)
    if duck is not None:
        duck.close()
    if not frames:
        return pd.DataFrame(columns=["market", "asset_id", "bin", "best_bid", "best_ask"])
    quotes = pd.concat(frames, ignore_index=True)
    return quotes.drop_duplicates(["market", "asset_id", "bin"])


def build_panel(records, quotes):
    """One row per market per five-minute bin, with profit of a buy held to settlement."""
    rows = []
    for record in records:
        won = record.get("won")
        settle = nba.settlement_price(won)
        if settle is None:
            continue
        mine = quotes[
            (quotes["market"] == record["condition_id"]) & (quotes["asset_id"] == record["asset_id"])
        ]
        path = nba.series_from(mine, record["tip"])
        if path.empty:
            continue
        for hour, mid in zip(path["hours_from_tip"], path["mid"]):
            if pd.isna(mid):
                continue
            rows.append(
                {
                    "sample": record["sample"],
                    "market_id": record["market_id"],
                    "cluster": record["cluster"],
                    "label": record["label"],
                    "hours_from_tip": float(hour),
                    "mid": float(mid),
                    "profit": settle - float(mid),
                    "won": bool(won),
                    "volume_clob": record["volume_clob"],
                    "settle_source": record["settle_source"],
                }
            )
    return pd.DataFrame(rows)


def best_times(panel):
    """The bin with the highest buy-and-hold profit. Ties keep the earliest bin."""
    if panel.empty:
        return pd.DataFrame()
    ordered = panel.sort_values(["market_id", "hours_from_tip"])
    picked = ordered.loc[ordered.groupby("market_id")["profit"].idxmax()]
    return picked.sort_values("hours_from_tip").reset_index(drop=True)


def demean(values, groups):
    frame = pd.DataFrame({"value": values, "group": groups})
    return frame["value"] - frame.groupby("group")["value"].transform("mean")


def clustered_slope(y, x, clusters):
    """OLS slope of y on x with an intercept. Standard error clustered by game.

    Returns beta on x, its standard error, the number of rows, and the
    number of clusters. Returns None when the clock does not vary or
    there is only one cluster.
    """
    y = np.asarray(y, dtype=float)
    x = np.asarray(x, dtype=float)
    clusters = np.asarray(clusters)
    design = np.column_stack([np.ones(len(y)), x])
    gram = design.T @ design
    if np.linalg.matrix_rank(gram) < 2:
        return None
    beta, *_ = np.linalg.lstsq(design, y, rcond=None)
    residual = y - design @ beta
    bread = np.linalg.inv(gram)
    meat = np.zeros((2, 2))
    unique = pd.unique(clusters)
    for group in unique:
        mask = clusters == group
        score = design[mask].T @ residual[mask]
        meat += np.outer(score, score)
    groups = len(unique)
    if groups < 2:
        return None
    scale = groups / (groups - 1)
    variance = scale * bread @ meat @ bread
    se = float(np.sqrt(max(variance[1, 1], 0.0)))
    return float(beta[1]), se, int(len(y)), int(groups)


def within_slope(panel):
    """beta from profit and hours after each market's own average is removed.

    The standard error is clustered by game. Two prop lines from one game
    are one cluster, and each line still has its own average removed.
    """
    usable = panel.groupby("market_id")["hours_from_tip"].transform("nunique") >= 2
    frame = panel.loc[usable].copy()
    if frame.empty or frame["cluster"].nunique() < 3:
        return None
    y = demean(frame["profit"], frame["market_id"]).to_numpy()
    x = demean(frame["hours_from_tip"], frame["market_id"]).to_numpy()
    clusters = frame["cluster"].to_numpy()
    design_scale = np.sum(x ** 2)
    if design_scale <= 0:
        return None
    beta = float(np.sum(x * y) / design_scale)
    residual = y - beta * x
    meat = 0.0
    unique = pd.unique(clusters)
    for group in unique:
        mask = clusters == group
        meat += float(np.sum(x[mask] * residual[mask]) ** 2)
    groups = len(unique)
    scale = groups / (groups - 1) if groups > 1 else 1.0
    se = math.sqrt(scale * meat / (design_scale ** 2))
    return beta, se, int(len(frame)), int(groups)


def half_hour_means(panel):
    if panel.empty:
        return pd.DataFrame(columns=["hours_from_tip", "mean_profit", "markets"])
    frame = panel.copy()
    frame["hours_from_tip"] = (np.floor(frame["hours_from_tip"] * 2) / 2).round(1)
    grouped = frame.groupby("hours_from_tip", as_index=False).agg(
        mean_profit=("profit", "mean"),
        markets=("cluster", "nunique"),
    )
    return grouped


def normal_tail(z):
    return math.erfc(abs(z) / math.sqrt(2))


def describe_fit(name, fit):
    if fit is None:
        return f"{name}: not enough markets with a price that moves.\n"
    beta, se, rows, groups = fit
    if se == 0:
        z = 0.0 if beta == 0 else float("inf")
    else:
        z = beta / se
    low, high = beta - 1.96 * se, beta + 1.96 * se
    practical = abs(beta) < PRACTICAL_BETA and abs(low) < PRACTICAL_BETA and abs(high) < PRACTICAL_BETA
    if practical:
        verdict = "The 95% interval sits inside 2 cents per hour. No practical linear relationship."
    elif low <= 0 <= high:
        verdict = "The 95% interval includes zero. A linear relationship is not distinguishable from noise."
    else:
        direction = "later" if beta > 0 else "earlier"
        verdict = f"The slope is away from zero. Buying {direction} raises profit in this sample."
    return (
        f"{name}\n"
        f"  beta = {beta:+.4f} profit per share per hour\n"
        f"  se = {se:.4f}   95% interval [{low:+.4f}, {high:+.4f}]\n"
        f"  two-sided normal p = {normal_tail(z):.3f}\n"
        f"  rows = {rows}   clusters = {groups}\n"
        f"  {verdict}\n"
    )


def write_report(sample, panel, best, within, pooled, means):
    lines = [sample, ""]
    lines.append(
        "Best time to buy inside one market is the five-minute bin with the lowest mid. "
        "Settlement does not depend on the clock, so that bin is the highest profit by construction. "
        "If several bins share that price, the earliest one is kept."
    )
    lines.append("")
    if best.empty:
        lines.append("No settled market had a mid in the window.")
    else:
        span = panel.groupby("market_id")["hours_from_tip"].agg(["min", "max"])
        joined = best.join(span, on="market_id")
        at_own_edge = np.isclose(joined["hours_from_tip"], joined["min"]) | np.isclose(
            joined["hours_from_tip"], joined["max"]
        )
        lines.append(
            f"Markets with a price path: {best['market_id'].nunique()}. "
            f"Median best time: {best['hours_from_tip'].median():+.2f} hours from tip. "
            f"Share whose best bin is the first or last quote of that market: {at_own_edge.mean():.0%}."
        )
    lines.append("")
    lines.append("Within-market line: profit minus the market's average, on hours minus the market's average.")
    lines.append(describe_fit("within", within).rstrip())
    lines.append("")
    lines.append("Pooled line: one intercept for every market. A market that won sits higher for the whole window.")
    lines.append(describe_fit("pooled", pooled).rstrip())
    lines.append("")
    lines.append(
        "A near-zero beta does not by itself prove there is no best time. "
        "It proves there is no straight-line gain from betting later rather than earlier. "
        "The half-hour means below are the check for a hump."
    )
    text = "\n".join(lines) + "\n"
    path = OUTPUT / f"{sample}_report.txt"
    path.write_text(text, encoding="utf-8")
    print(text, flush=True)
    return means


def plot_sample(sample, best, means, within):
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2))
    if best.empty:
        axes[0].text(0.5, 0.5, "No prices", ha="center")
    else:
        axes[0].hist(best["hours_from_tip"], bins=18, color="#1f4e79")
    axes[0].axvline(0, color="#b0b0b0", linewidth=0.8)
    axes[0].set_xlabel("Hours from listed tip")
    axes[0].set_ylabel("Markets")
    axes[0].set_title("When the mid was lowest")
    if means.empty:
        axes[1].text(0.5, 0.5, "No prices", ha="center")
    else:
        axes[1].plot(means["hours_from_tip"], means["mean_profit"], color="#1f4e79", marker="o", linewidth=1.4)
        if within is not None:
            beta = within[0]
            center_x = float(means["hours_from_tip"].mean())
            center_y = float(means["mean_profit"].mean())
            grid = np.linspace(float(means["hours_from_tip"].min()), float(means["hours_from_tip"].max()), 20)
            axes[1].plot(grid, center_y + beta * (grid - center_x), color="#d9762c", linewidth=1.2, label="Within-market slope")
            axes[1].legend(fontsize=8)
    axes[1].axvline(0, color="#b0b0b0", linewidth=0.8)
    axes[1].axhline(0, color="#b0b0b0", linewidth=0.8)
    axes[1].set_xlabel("Hours from listed tip")
    axes[1].set_ylabel("Mean profit per share")
    axes[1].set_title("Average profit of buying then")
    fig.suptitle(sample)
    fig.tight_layout()
    fig.savefig(OUTPUT / f"{sample}.png", dpi=140)
    plt.close(fig)


def scored_panel(panel):
    """Win chance, outcome, and call columns on the optimal-time panel."""
    if panel is None or panel.empty:
        return panel
    frame = panel.copy()
    frame["slug"] = frame["market_id"].astype(str)
    codes = {market: index for index, market in enumerate(frame["market_id"].drop_duplicates(), start=1)}
    frame["rank"] = frame["market_id"].map(codes)
    summary = frame.groupby("rank", as_index=False)["won"].first()
    return predict.score_predictions(frame, summary)


def profit_by_minute(scored):
    """Mean buy-and-hold profit at each five-minute mark, one vote per market."""
    columns = ["minutes_from_tip", "mean_profit"]
    if scored is None or scored.empty or "profit" not in scored.columns:
        return pd.DataFrame(columns=columns)
    frame = scored.dropna(subset=["profit", "hours_from_tip", "slug"]).copy()
    if frame.empty:
        return pd.DataFrame(columns=columns)
    minutes = (pd.to_numeric(frame["hours_from_tip"], errors="coerce") * 60).round()
    frame["minutes_from_tip"] = (minutes / 5).round() * 5
    per_market = frame.groupby(["minutes_from_tip", "slug"], as_index=False)["profit"].mean()
    grouped = per_market.groupby("minutes_from_tip", as_index=False)["profit"].mean()
    grouped = grouped.rename(columns={"profit": "mean_profit"})
    grouped["minutes_from_tip"] = grouped["minutes_from_tip"].astype(int)
    return grouped


def slot_floor(table):
    if table is None or table.empty:
        return MIN_SLOT_MARKETS
    fullest = int(table["markets"].max())
    return min(MIN_SLOT_MARKETS, max(fullest, 1))


def add_joint_score(table):
    """Scale profit onto 0–1 and average it with relative accuracy.

    Relative accuracy is 1 when the win rate equals the average price,
    and falls by the absolute gap. A price of 0.54 that wins 54% of the
    time scores 1. A price of 0.54 that wins 4% scores 0.50.
    """
    if table is None or table.empty:
        return table
    frame = table.copy()
    gap = (frame["actual_win_rate"] - frame["mean_win_chance"]).abs()
    frame["relative_accuracy"] = (1 - gap).clip(lower=0, upper=1)
    floor = slot_floor(frame)
    frame["eligible"] = frame["markets"] >= floor
    frame["profit_score"] = np.nan
    frame["joint"] = np.nan
    eligible = frame["eligible"] & frame["mean_profit"].notna() & frame["relative_accuracy"].notna()
    if not eligible.any():
        return frame
    profits = frame.loc[eligible, "mean_profit"]
    low = float(profits.min())
    high = float(profits.max())
    span = high - low
    if span <= 1e-12:
        frame.loc[eligible, "profit_score"] = 1.0
    else:
        frame.loc[eligible, "profit_score"] = (frame.loc[eligible, "mean_profit"] - low) / span
    frame.loc[eligible, "joint"] = 0.5 * frame.loc[eligible, "profit_score"] + 0.5 * frame.loc[eligible, "relative_accuracy"]
    return frame


def choose_joint_time(table):
    """Highest joint score. Ties prefer higher profit, then the earlier mark."""
    if table is None or table.empty or "joint" not in table.columns:
        return None
    eligible = table.dropna(subset=["joint"])
    if eligible.empty:
        return None
    ordered = eligible.sort_values(
        ["joint", "mean_profit", "minutes_from_tip"],
        ascending=[False, False, True],
    )
    return ordered.iloc[0]


def accuracy_text(sample, table, best, by_price):
    lines = [f"{sample} accuracy", ""]
    lines.append(
        "This uses every settled market in the optimal-time sample, not the top-10 chart sample. "
        "At each five-minute mark, mean win chance is the average mid and actual win rate is the share "
        "of those markets whose charted outcome happened. Each market votes once."
    )
    lines.append(
        "Relative accuracy is 1 minus the absolute gap between those two. "
        "Mean profit at a mark is that win rate minus the average price, so a mark priced exactly right earns about zero. "
        "Profit score rescales mean profit from 0 at the worst eligible mark to 1 at the best. "
        "The joint score is half profit score and half relative accuracy. "
        f"A mark needs at least {slot_floor(table)} markets before it can win."
    )
    lines.append("")
    if best is None:
        lines.append("No five-minute mark had a settled price.")
    else:
        lines.append(
            f"Best joint time: {best['hours_from_tip']:+.2f} hours from tip. "
            f"Mean profit {best['mean_profit']:+.3f} per share. "
            f"Price {best['mean_win_chance']:.2f}, won {best['actual_win_rate']:.0%}. "
            f"Relative accuracy {best['relative_accuracy']:.2f}. "
            f"Joint {best['joint']:.2f}. "
            f"Markets {int(best['markets'])}."
        )
    lines.append("")
    lines.append("Price bands. A calibrated band resolves positive about as often as its price.")
    if by_price is None or by_price.empty:
        lines.append("No band had a settled quote.")
    else:
        for _, row in by_price.iterrows():
            lines.append(
                f"  {row['band']}: price {row['mean_win_chance']:.2f}, "
                f"resolved positive for {row['actual_win_rate']:.0%} of {int(row['markets'])} markets "
                f"({int(row['bins'])} bins)."
            )
    return "\n".join(lines) + "\n"


def plot_joint(sample, table, best):
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.4))
    if table is None or table.empty:
        axes[0].text(0.5, 0.5, "No settled quotes", ha="center", va="center")
        axes[1].text(0.5, 0.5, "No settled quotes", ha="center", va="center")
    else:
        hours = table["hours_from_tip"]
        left = axes[0]
        left.plot(hours, table["mean_profit"], color="#1f4e79", marker="o", linewidth=1.3, label="Mean profit per share")
        left.set_ylabel("Mean profit per share")
        right_axis = left.twinx()
        right_axis.plot(hours, table["relative_accuracy"], color="#d9762c", marker="o", linewidth=1.3, label="Relative accuracy")
        right_axis.set_ylabel("Relative accuracy")
        right_axis.set_ylim(-0.05, 1.05)
        handles, labels = left.get_legend_handles_labels()
        more_handles, more_labels = right_axis.get_legend_handles_labels()
        left.legend(handles + more_handles, labels + more_labels, fontsize=8, loc="best")
        axes[1].plot(hours, table["joint"], color="#2f6f4e", marker="o", linewidth=1.3)
        axes[1].set_ylabel("Joint score")
        axes[1].set_ylim(-0.05, 1.05)
        if best is not None:
            for axis in (left, axes[1]):
                axis.axvline(best["hours_from_tip"], color="#2f6f4e", linewidth=1.1)
            axes[1].scatter([best["hours_from_tip"]], [best["joint"]], color="#2f6f4e", s=40, zorder=3)
    for axis in axes:
        axis.axvline(0, color="#b0b0b0", linewidth=0.8)
        axis.set_xlabel("Hours from listed tip")
        axis.grid(axis="y", color="#eeeeee")
    axes[0].set_title("Profit and accuracy")
    axes[1].set_title("Half profit score, half accuracy")
    fig.suptitle(sample)
    fig.tight_layout()
    fig.savefig(OUTPUT / f"{sample}_profit_vs_accuracy.png", dpi=140)
    plt.close(fig)


def write_accuracy(sample, panel):
    """Accuracy by time and by price, then the clock time that balances both with profit."""
    scored = scored_panel(panel)
    by_time, by_price = predict.accuracy_tables(scored, clock="hours_from_tip")
    if not by_time.empty:
        profits = profit_by_minute(scored)
        by_time = by_time.merge(profits, on="minutes_from_tip", how="left")
        by_time = add_joint_score(by_time)
    best = choose_joint_time(by_time)
    if best is not None and not by_time.empty:
        by_time["chosen"] = by_time["minutes_from_tip"] == int(best["minutes_from_tip"])
    by_time.to_csv(OUTPUT / f"{sample}_accuracy_by_time.csv", index=False)
    by_price.to_csv(OUTPUT / f"{sample}_accuracy_by_price.csv", index=False)
    text = accuracy_text(sample, by_time, best, by_price)
    (OUTPUT / f"{sample}_accuracy.txt").write_text(text, encoding="utf-8")
    print(text, flush=True)
    if scored is not None and not scored.empty:
        predict.plot_prediction(
            scored,
            OUTPUT,
            clock="hours_from_tip",
            xlabel="Hours from listed tip",
            outcome_label="Charted outcome happened",
        )
        saved = OUTPUT / "prediction_accuracy.png"
        target = OUTPUT / f"{sample}_prediction_accuracy.png"
        if saved.exists():
            saved.replace(target)
    plot_joint(sample, by_time, best)


def run_sample(sample, panel):
    frame = panel[panel["sample"] == sample].copy()
    frame = frame.groupby("market_id").filter(lambda part: len(part) >= MIN_BINS)
    best = best_times(frame)
    within = within_slope(frame)
    if frame.empty or frame["cluster"].nunique() < 3:
        pooled = None
    else:
        pooled = clustered_slope(frame["profit"], frame["hours_from_tip"], frame["cluster"])
    means = half_hour_means(frame)
    frame.to_csv(OUTPUT / f"{sample}_bins.csv", index=False)
    best.to_csv(OUTPUT / f"{sample}_best_times.csv", index=False)
    means.to_csv(OUTPUT / f"{sample}_half_hours.csv", index=False)
    write_report(sample, frame, best, within, pooled, means)
    plot_sample(sample, best, means, within)
    write_accuracy(sample, frame)


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    pg = nba.connect_postgres()
    records = load_markets(pg)
    pg.close()
    print(f"{sum(r['sample']=='games' for r in records)} game winners, "
          f"{sum(r['sample']=='props' for r in records)} prop lines.", flush=True)
    settle_games(records)
    settle_props(records)
    settled = [record for record in records if record.get("won") is not None]
    print(f"{len(settled)} markets have a settlement.", flush=True)
    quotes = read_mids(settled)
    panel = build_panel(settled, quotes)
    if panel.empty:
        raise SystemExit("No settled market had a five-minute mid in the window.")
    panel.to_csv(OUTPUT / "bins.csv", index=False)
    for sample in ("games", "props"):
        run_sample(sample, panel)
    print(f"Wrote {OUTPUT}")


if __name__ == "__main__":
    main()
