"""Score five-minute mids against a 1-or-0 settlement.

The mid is the market's probability that the charted outcome happens.
Above 0.5 that is a win call. Below 0.5 it is a loss call. A price band
such as 0.50–0.60 collects every market that was quoted in that range.
The share of those markets that resolved positive should sit near the
average price in the band.
"""

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


def outcome_flag(value):
    """1 when the charted outcome happened, 0 when it did not, otherwise unknown."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "1"}:
            return 1.0
        if text in {"false", "0"}:
            return 0.0
        if text in {"", "nan", "none"}:
            return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        return None
    return 1.0 if bool(value) else 0.0


def score_predictions(paths, summary):
    """Attach the win chance, the 0.5 call, and the squared error on every bin."""
    if paths is None or paths.empty:
        return paths
    frame = paths.copy()
    won_by_rank = {}
    if summary is not None and not summary.empty and "won" in summary.columns:
        for _, row in summary.iterrows():
            try:
                won_by_rank[int(row["rank"])] = outcome_flag(row["won"])
            except (TypeError, ValueError):
                continue
    frame["outcome"] = [won_by_rank.get(int(rank)) if pd.notna(rank) else None for rank in frame["rank"]]
    frame["outcome"] = pd.to_numeric(frame["outcome"], errors="coerce")
    chance = pd.to_numeric(frame["mid"], errors="coerce")
    frame["win_chance"] = chance
    known = frame["outcome"].notna() & chance.notna()
    called = pd.Series(np.nan, index=frame.index)
    called = called.mask(known & chance.gt(0.5), 1.0)
    called = called.mask(known & chance.lt(0.5), 0.0)
    frame["called_win"] = called
    made = called.notna()
    agree = ((called == 1) & (frame["outcome"] == 1)) | ((called == 0) & (frame["outcome"] == 0))
    correct = pd.Series(np.nan, index=frame.index)
    correct = correct.mask(made & agree, 1.0)
    correct = correct.mask(made & ~agree, 0.0)
    frame["call_correct"] = correct
    frame["brier"] = (chance - frame["outcome"]) ** 2
    return frame


def market_key(frame):
    if "slug" in frame.columns and frame["slug"].notna().any():
        return frame["slug"].astype(str)
    return frame["rank"].astype(str)


def _counted(n, subject):
    n = int(n)
    word = subject if n == 1 else f"{subject}s"
    return f"{n} {word}"


def accuracy_tables(frame, clock="hours_from_end"):
    """One row per five-minute mark, and one row per 10-point price band.

    A market that sits in one band for an hour is one market, not twelve
    bins. The bin count is still saved so that overweighting is visible.
    """
    minute_column = clock.replace("hours_", "minutes_")
    time_columns = [
        minute_column, clock, "markets", "mean_win_chance",
        "actual_win_rate", "calls", "call_hit_rate", "brier",
    ]
    price_columns = [
        "band_low", "band_high", "band", "markets", "bins",
        "mean_win_chance", "actual_win_rate", "brier",
    ]
    empty_time = pd.DataFrame(columns=time_columns)
    empty_price = pd.DataFrame(columns=price_columns)
    if frame is None or frame.empty or "win_chance" not in frame.columns:
        return empty_time, empty_price
    usable = frame.dropna(subset=["win_chance", "outcome"]).copy()
    if usable.empty or clock not in usable.columns:
        return empty_time, empty_price
    usable["market_id"] = market_key(usable)
    minutes = (pd.to_numeric(usable[clock], errors="coerce") * 60).round()
    usable[minute_column] = (minutes / 5).round() * 5

    per_slot = usable.groupby([minute_column, "market_id"], as_index=False).agg(
        win_chance=("win_chance", "mean"),
        outcome=("outcome", "first"),
        call_correct=("call_correct", "mean"),
        brier=("brier", "mean"),
    )
    time_rows = []
    for minute, part in per_slot.groupby(minute_column):
        calls = part["call_correct"].dropna()
        time_rows.append(
            {
                minute_column: int(minute),
                clock: float(minute) / 60,
                "markets": int(part["market_id"].nunique()),
                "mean_win_chance": float(part["win_chance"].mean()),
                "actual_win_rate": float(part["outcome"].mean()),
                "calls": int(len(calls)),
                "call_hit_rate": float(calls.mean()) if len(calls) else np.nan,
                "brier": float(part["brier"].mean()),
            }
        )
    by_time = pd.DataFrame(time_rows, columns=time_columns)
    if not by_time.empty:
        by_time = by_time.sort_values(minute_column).reset_index(drop=True)

    clipped = usable["win_chance"].clip(lower=0, upper=0.999999)
    usable["band_low"] = np.floor(clipped * 10) / 10
    per_band = usable.groupby(["band_low", "market_id"], as_index=False).agg(
        win_chance=("win_chance", "mean"),
        outcome=("outcome", "first"),
        brier=("brier", "mean"),
        bins=("win_chance", "size"),
    )
    price_rows = []
    for band, part in per_band.groupby("band_low"):
        low = float(band)
        price_rows.append(
            {
                "band_low": low,
                "band_high": low + 0.1,
                "band": f"{low:.2f}–{low + 0.1:.2f}",
                "markets": int(part["market_id"].nunique()),
                "bins": int(part["bins"].sum()),
                "mean_win_chance": float(part["win_chance"].mean()),
                "actual_win_rate": float(part["outcome"].mean()),
                "brier": float(part["brier"].mean()),
            }
        )
    by_price = pd.DataFrame(price_rows, columns=price_columns)
    if not by_price.empty:
        by_price = by_price.sort_values("band_low").reset_index(drop=True)
    return by_time, by_price


def prediction_report(scored, by_time, by_price, subject="market"):
    lines = ["How often the price called the outcome", ""]
    lines.append(
        "The win chance on a five-minute row is that row's mid. A mid of 0.70 is a 70% chance "
        "the charted outcome happens. A call predicts it above 0.5 and predicts it does not "
        "happen below 0.5. A mid of exactly 0.5 is not a call."
    )
    lines.append(
        f"One {subject} has one result, so an hour spent at the same price is twelve rows and still one {subject}. "
        f"The tables count each {subject} once inside a five-minute mark or a price band."
    )
    lines.append("")
    if scored is None or scored.empty or "call_correct" not in getattr(scored, "columns", []):
        lines.append(f"No settled {subject} had a five-minute mid.")
        return "\n".join(lines) + "\n"
    calls = scored["call_correct"].dropna()
    brier = scored["brier"].dropna()
    settled = scored.dropna(subset=["outcome"]) if "outcome" in scored.columns else scored.iloc[0:0]
    count = market_key(settled).nunique() if not settled.empty else 0
    if calls.empty:
        lines.append("No settled quote was away from 0.5, so there is no call to score.")
    else:
        lines.append(
            f"Bin-level calls: {calls.mean():.1%} of {len(calls)} quotes matched the final result "
            f"({_counted(count, subject)}). This counts a long quote path more than once."
        )
    if not brier.empty:
        lines.append(
            f"Bin-level Brier score: {brier.mean():.3f}. "
            "This is the average of (win chance − outcome) squared. A coin flip scores 0.250."
        )
    if by_time is not None and not by_time.empty and "call_hit_rate" in by_time.columns:
        rated = by_time.dropna(subset=["call_hit_rate"])
        if not rated.empty:
            lines.append(
                f"Across five-minute marks, the average call hit rate is {rated['call_hit_rate'].mean():.1%}. "
                f"Each mark counts once, so one {subject} quoted all night does not dominate this average."
            )
    lines.append("")
    lines.append("Price bands. A calibrated market resolves positive about as often as its price.")
    if by_price is None or by_price.empty:
        lines.append("No band had a settled quote.")
    else:
        for _, row in by_price.iterrows():
            bins = int(row["bins"])
            bin_word = "bin" if bins == 1 else "bins"
            lines.append(
                f"  {row['band']}: price {row['mean_win_chance']:.2f}, "
                f"resolved positive for {row['actual_win_rate']:.0%} of {_counted(row['markets'], subject)} "
                f"({bins} {bin_word})."
            )
    return "\n".join(lines) + "\n"


def write_bundle(paths, summary, output, clock="hours_from_tip", subject="market"):
    """Score every bin and write the by-time table, the price-band table, and the report."""
    folder = Path(output)
    folder.mkdir(parents=True, exist_ok=True)
    scored = score_predictions(paths, summary)
    by_time, by_price = accuracy_tables(scored, clock=clock)
    by_time.to_csv(folder / "accuracy_by_time.csv", index=False)
    by_price.to_csv(folder / "accuracy_by_price.csv", index=False)
    text = prediction_report(scored, by_time, by_price, subject=subject)
    (folder / "prediction_report.txt").write_text(text, encoding="utf-8")
    print(text, flush=True)
    return scored


def plot_prediction(paths, output, clock="hours_from_tip", xlabel="Hours from listed tip", outcome_label="Charted outcome happened"):
    """Price against the result at each five-minute mark, and the same check by price band."""
    by_time, by_price = accuracy_tables(paths, clock=clock)
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.4))
    if by_time.empty:
        axes[0].text(0.5, 0.5, "No settled quotes", ha="center", va="center")
    else:
        axes[0].plot(by_time[clock], by_time["mean_win_chance"], color="#1f4e79", marker="o", linewidth=1.3, label="Average win chance")
        axes[0].plot(by_time[clock], by_time["actual_win_rate"], color="#d9762c", marker="o", linewidth=1.3, label=outcome_label)
        axes[0].plot(by_time[clock], by_time["call_hit_rate"], color="#2f6f4e", marker="o", linewidth=1.2, label="Favorite call was right")
        axes[0].legend(fontsize=8)
    axes[0].axvline(0, color="#b0b0b0", linewidth=0.8)
    axes[0].set_ylim(-0.05, 1.05)
    axes[0].set_xlabel(xlabel)
    axes[0].set_ylabel("Share")
    axes[0].set_title("At each five-minute mark")
    axes[0].grid(axis="y", color="#eeeeee")

    axes[1].plot([0, 1], [0, 1], color="#b0b0b0", linewidth=1)
    if by_price.empty:
        axes[1].text(0.5, 0.5, "No settled quotes", ha="center", va="center")
    else:
        axes[1].scatter(
            by_price["mean_win_chance"],
            by_price["actual_win_rate"],
            s=40 + by_price["markets"] * 6,
            color="#1f4e79",
            zorder=3,
        )
        for _, row in by_price.iterrows():
            above = row["actual_win_rate"] > 0.85
            axes[1].annotate(
                f"{row['band']}  n={int(row['markets'])}",
                (row["mean_win_chance"], row["actual_win_rate"]),
                textcoords="offset points",
                xytext=(6, -12 if above else 4),
                fontsize=7,
            )
    axes[1].set_xlim(0, 1)
    axes[1].set_ylim(0, 1)
    axes[1].set_xlabel("Average win chance in the band")
    axes[1].set_ylabel("Share resolved positive")
    axes[1].set_title("Does a 70% price happen 70% of the time?")
    fig.tight_layout()
    fig.savefig(Path(output) / "prediction_accuracy.png", dpi=140)
    plt.close(fig)
