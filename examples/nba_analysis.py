"""Price history for the top 10 NBA teams in the Week 2 power rankings.

NBA.com's Week 2 power rankings, published October 26, 2026, are the ranking.
Each team contributes one game-winner market: the market whose slug equals its
event slug, so the two outcomes are the two teams. The hourly Parquet files
supply the bid and ask path. The markets table only stores the latest quote.

The script keeps the highest-volume completed game that actually appears in
those files. A game already confirmed for another team is reused, so both
clubs in that game get a price path. It then compares the price variance in
the six hours before the listed tip with the variance in the two and a half
hours after tip, checks that pregame price against the resolution, and
compares it with the team's main point spread.

    python examples/nba_analysis.py

Graphs and a summary table are written to outputs/nba/.

A full run downloads whole hourly files from the class CloudFront host, on
the order of a few gigabytes. That host logs each file and the bytes sent.
"""

import base64
from datetime import timedelta
from pathlib import Path

import duckdb
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
import psycopg2

CONNECTION = (
    "postgresql://polymarketdatabase.merbqatlxbrcvtbnuaqe:PolymarketData2026!"
    "@aws-0-us-west-2.pooler.supabase.com:6543/postgres?sslmode=require"
)
CLOUD_USER = "polymarketdatabase"
CLOUD_PASSWORD = "PolymarketData2026!"
BUCKET = "https://d1yyqbluk0oz0t.cloudfront.net"
OUTPUT = Path("outputs/nba")
HOURS_BEFORE = 6
HOURS_AFTER = 3
IN_GAME_HOURS = 2.5

# NBA.com Power Rankings, Week 2, October 26, 2026.
TOP_TEAMS = [
    {"rank": 1, "code": "okc", "name": "Thunder", "record": "3-0"},
    {"rank": 2, "code": "det", "name": "Pistons", "record": "3-0"},
    {"rank": 3, "code": "min", "name": "Timberwolves", "record": "3-0"},
    {"rank": 4, "code": "hou", "name": "Rockets", "record": "3-0"},
    {"rank": 5, "code": "phi", "name": "76ers", "record": "3-1"},
    {"rank": 6, "code": "sas", "name": "Spurs", "record": "1-2"},
    {"rank": 7, "code": "lal", "name": "Lakers", "record": "3-0"},
    {"rank": 8, "code": "bos", "name": "Celtics", "record": "1-1"},
    {"rank": 9, "code": "tor", "name": "Raptors", "record": "2-1"},
    {"rank": 10, "code": "nyk", "name": "Knicks", "record": "0-3"},
]


def connect_postgres():
    con = psycopg2.connect(CONNECTION)
    con.set_session(readonly=True, autocommit=True)
    return con


def connect_duckdb():
    token = base64.b64encode(f"{CLOUD_USER}:{CLOUD_PASSWORD}".encode()).decode()
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute(f"""
        CREATE SECRET cf (
            TYPE HTTP,
            EXTRA_HTTP_HEADERS MAP {{'Authorization': 'Basic {token}'}}
        );
    """)
    # Busy hours are hundreds of megabytes. Drop the file cache and cap memory
    # so a long scan does not keep every hour resident.
    con.execute("SET memory_limit='6GB';")
    con.execute("SET threads=2;")
    con.execute("SET preserve_insertion_order=false;")
    con.execute("SET enable_external_file_cache=false;")
    return con


def load_games(pg):
    frame = pd.read_sql(
        """
        SELECT m.slug, m.title, m.outcomes, m.token_ids, m.condition_id,
               m.end_date AS tip, m.volume_clob, m.best_bid, m.best_ask,
               m.last_trade_price, r.resolved_yes, r.resolved_at
        FROM markets m
        LEFT JOIN resolutions r ON r.market_slug = m.slug
        WHERE m.slug = m.event_slug
          AND m.slug ~ '^nba-[a-z]{3}-[a-z]{3}-[0-9]{4}-[0-9]{2}-[0-9]{2}$'
          AND m.end_date < now()
        """,
        pg,
    )
    parts = frame["slug"].str.extract(r"^nba-([a-z]{3})-([a-z]{3})-")
    frame["away"] = parts[0]
    frame["home"] = parts[1]
    frame["volume_clob"] = frame["volume_clob"].astype(float)
    return frame


def load_spreads(pg):
    frame = pd.read_sql(
        """
        SELECT event_slug, slug, title, outcomes, token_ids, condition_id, volume_clob
        FROM markets
        WHERE event_slug ~ '^nba-[a-z]{3}-[a-z]{3}-[0-9]{4}-[0-9]{2}-[0-9]{2}$'
          AND slug LIKE '%%spread%%'
          AND slug NOT LIKE '%%1h-spread%%'
        """,
        pg,
    )
    frame["volume_clob"] = frame["volume_clob"].astype(float)
    return frame


def team_games(games, code):
    mine = games[(games["away"] == code) | (games["home"] == code)].copy()
    mine["volume_clob"] = mine["volume_clob"].fillna(0)
    mine["liquid"] = mine["volume_clob"] >= 50
    return mine.sort_values(["liquid", "tip"], ascending=[False, False])


def token_for(row, code):
    tokens = list(row["token_ids"])
    if row["away"] == code:
        return tokens[0], True
    return tokens[1], False


def spread_for(spreads, game_slug, team_name):
    mine = spreads[
        (spreads["event_slug"] == game_slug)
        & spreads["title"].str.contains(team_name, case=False, na=False)
    ].copy()
    if mine.empty:
        return None
    mine = mine.sort_values("volume_clob", ascending=False)
    row = mine.iloc[0]
    outcomes = list(row["outcomes"])
    tokens = list(row["token_ids"])
    index = 0 if team_name.lower() in str(outcomes[0]).lower() else 1
    return {
        "slug": row["slug"],
        "title": row["title"],
        "condition_id": row["condition_id"],
        "asset_id": tokens[index],
        "volume_clob": row["volume_clob"],
    }


def hours_around(tip):
    start = (tip - timedelta(hours=HOURS_BEFORE)).replace(minute=0, second=0, microsecond=0)
    end = tip + timedelta(hours=HOURS_AFTER)
    cursor = start
    hours = []
    while cursor <= end:
        hours.append(cursor)
        cursor += timedelta(hours=1)
    return hours


def hour_key(moment):
    return moment.strftime("%Y-%m-%dT%H")


def read_hour(duck, moment, pairs):
    if not pairs:
        return pd.DataFrame()
    url = f"{BUCKET}/events-{hour_key(moment)}.parquet"
    values = ", ".join(f"('{market}', '{asset}')" for market, asset in pairs)
    sql = f"""
        WITH want(market, asset_id) AS (VALUES {values})
        SELECT w.market, w.asset_id,
               CAST(to_timestamp(floor(p.ts_ms / 1000.0 / 300) * 300) AS VARCHAR) AS bin,
               max(p.price) FILTER (WHERE p.side = 'BUY' AND p.size > 0) AS best_bid,
               min(p.price) FILTER (WHERE p.side = 'SELL' AND p.size > 0) AS best_ask
        FROM read_parquet('{url}') p
        JOIN want w ON p.market = w.market AND p.asset_id = w.asset_id
        GROUP BY 1, 2, 3
    """
    try:
        rows = duck.execute(sql).fetchall()
    except Exception as error:
        print(f"  skip {hour_key(moment)}: {error.__class__.__name__}")
        return pd.DataFrame()
    frame = pd.DataFrame(rows, columns=["market", "asset_id", "bin", "best_bid", "best_ask"])
    return frame


def has_book(duck, tip, condition_id, asset_id):
    for hours_back in (0, 1, 3):
        probe = (tip - timedelta(hours=hours_back)).replace(minute=0, second=0, microsecond=0)
        frame = read_hour(duck, probe, [(condition_id, asset_id)])
        if not frame.empty:
            return True
    return False


def choose_game(duck, games, code, confirmed):
    if games.empty:
        return None, None, None
    known = games[games["slug"].isin(confirmed)]
    if not known.empty:
        row = known.iloc[0]
        asset_id, first = token_for(row, code)
        print(f"  reuse {row['slug']}", flush=True)
        return row, asset_id, first
    for _, row in games.iterrows():
        asset_id, first = token_for(row, code)
        print(f"  probe {row['slug']}", flush=True)
        if has_book(duck, row["tip"], row["condition_id"], asset_id):
            confirmed.add(row["slug"])
            return row, asset_id, first
    return None, None, None


def mid_price(bid, ask):
    if pd.notna(bid) and pd.notna(ask):
        return (float(bid) + float(ask)) / 2
    if pd.notna(bid):
        return float(bid)
    if pd.notna(ask):
        return float(ask)
    return None


def series_from(quotes, tip):
    empty = pd.DataFrame(columns=["bin", "mid", "hours_from_tip", "best_bid", "best_ask"])
    if quotes is None or quotes.empty:
        return empty
    frame = quotes.copy()
    frame["bin"] = pd.to_datetime(frame["bin"], utc=True)
    frame["mid"] = [mid_price(bid, ask) for bid, ask in zip(frame["best_bid"], frame["best_ask"])]
    frame = frame.dropna(subset=["mid"]).sort_values("bin")
    frame["hours_from_tip"] = (frame["bin"] - tip).dt.total_seconds() / 3600
    return frame


def window_std(frame, start, end):
    if frame.empty:
        return None, 0
    sample = frame[(frame["hours_from_tip"] >= start) & (frame["hours_from_tip"] < end)]
    if len(sample) < 3:
        return None, len(sample)
    return float(sample["mid"].std()), len(sample)


def last_before_tip(frame):
    if frame.empty:
        return None
    sample = frame[frame["hours_from_tip"] < 0]
    if sample.empty:
        return None
    return float(sample.iloc[-1]["mid"])


def analyze():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    pg = connect_postgres()
    games = load_games(pg)
    spreads = load_spreads(pg)
    pg.close()
    duck = connect_duckdb()

    selected = []
    confirmed = set()
    for team in TOP_TEAMS:
        print(f"{team['rank']}. {team['name']}", flush=True)
        candidates = team_games(games, team["code"])
        row, asset_id, first = choose_game(duck, candidates, team["code"], confirmed)
        if row is None:
            selected.append({**team, "game": None})
            print("  no order-book game", flush=True)
            continue
        spread = spread_for(spreads, row["slug"], team["name"])
        selected.append({**team, "game": row, "asset_id": asset_id, "first": first, "spread": spread})
        print(f"  using {row['slug']}", flush=True)
    duck.close()
    duck = connect_duckdb()

    needed = {}
    for item in selected:
        if item["game"] is None:
            continue
        game = item["game"]
        pairs = [(game["condition_id"], item["asset_id"])]
        if item["spread"]:
            pairs.append((item["spread"]["condition_id"], item["spread"]["asset_id"]))
        for moment in hours_around(game["tip"]):
            needed.setdefault(moment, [])
            for pair in pairs:
                if pair not in needed[moment]:
                    needed[moment].append(pair)

    quotes = []
    moments = sorted(needed)
    for index, moment in enumerate(moments, start=1):
        if index > 1 and index % 6 == 1:
            duck.close()
            duck = connect_duckdb()
        print(f"hour {index}/{len(moments)} {hour_key(moment)}", flush=True)
        frame = read_hour(duck, moment, needed[moment])
        if not frame.empty:
            quotes.append(frame)
    duck.close()
    book = pd.concat(quotes, ignore_index=True) if quotes else pd.DataFrame()

    summaries = []
    paths = []
    for item in selected:
        summary = {
            "rank": item["rank"],
            "team": item["name"],
            "code": item["code"],
            "record": item["record"],
            "game": None,
            "opponent": None,
            "tip_utc": None,
            "pregame_std": None,
            "in_game_std": None,
            "pregame_price": None,
            "won": None,
            "spread": None,
            "spread_pregame": None,
            "note": "Every completed game-winner market for this team was checked. None of those hours contain its token.",
        }
        if item["game"] is None:
            summaries.append(summary)
            continue
        game = item["game"]
        opponent_code = game["home"] if game["away"] == item["code"] else game["away"]
        opponent = str(game["outcomes"][1] if item["first"] else game["outcomes"][0])
        mine = book[(book["market"] == game["condition_id"]) & (book["asset_id"] == item["asset_id"])]
        path = series_from(mine, game["tip"])
        pre_std, pre_n = window_std(path, -HOURS_BEFORE, 0)
        live_std, live_n = window_std(path, 0, IN_GAME_HOURS)
        pregame = last_before_tip(path)
        won = None
        if pd.notna(game["resolved_yes"]):
            won = bool(game["resolved_yes"]) if item["first"] else not bool(game["resolved_yes"])
        spread_price = None
        spread_title = None
        if item["spread"] is not None and not book.empty:
            spread_rows = book[
                (book["market"] == item["spread"]["condition_id"])
                & (book["asset_id"] == item["spread"]["asset_id"])
            ]
            spread_path = series_from(spread_rows, game["tip"])
            spread_price = last_before_tip(spread_path)
            spread_title = item["spread"]["title"]
        summary.update(
            {
                "game": game["slug"],
                "opponent": opponent,
                "opponent_code": opponent_code,
                "tip_utc": game["tip"],
                "pregame_std": pre_std,
                "in_game_std": live_std,
                "pregame_n": pre_n,
                "in_game_n": live_n,
                "pregame_price": pregame,
                "won": won,
                "spread": spread_title,
                "spread_pregame": spread_price,
                "volume_clob": None if pd.isna(game["volume_clob"]) else game["volume_clob"],
                "note": "",
            }
        )
        summaries.append(summary)
        if not path.empty:
            plot_path = path.copy()
            plot_path["team"] = item["name"]
            plot_path["rank"] = item["rank"]
            plot_path["label"] = f"{item['name']} vs {opponent}"
            paths.append(plot_path)

    summary_frame = pd.DataFrame(summaries)
    summary_frame.to_csv(OUTPUT / "summary.csv", index=False)
    if paths:
        pd.concat(paths, ignore_index=True).to_csv(OUTPUT / "price_paths.csv", index=False)
    plot_paths(paths)
    plot_variance(summary_frame)
    plot_calibration(summary_frame)
    plot_spread(summary_frame)
    print(summary_frame[["rank", "team", "game", "pregame_price", "pregame_std", "in_game_std", "won"]].to_string(index=False))
    print(f"Wrote {OUTPUT}")


def plot_paths(paths):
    fig, axes = plt.subplots(5, 2, figsize=(11, 14), sharex=True, sharey=True)
    axes = axes.ravel()
    by_rank = {frame["rank"].iloc[0]: frame for frame in paths}
    for axis, team in zip(axes, TOP_TEAMS):
        frame = by_rank.get(team["rank"])
        if frame is None:
            axis.text(0.5, 0.5, "No order-book path", ha="center", va="center")
            axis.set_xlim(-HOURS_BEFORE, HOURS_AFTER)
            axis.set_ylim(0, 1)
            title = f"{team['rank']}. {team['name']} ({team['record']})"
        else:
            axis.plot(frame["hours_from_tip"], frame["mid"], color="#1f4e79", linewidth=1.4)
            axis.axvline(0, color="#b0b0b0", linewidth=0.8)
            axis.set_ylim(0, 1)
            title = f"{team['rank']}. {frame['label'].iloc[0]}"
        axis.set_title(title, loc="left", fontsize=10)
        axis.grid(axis="y", color="#eeeeee")
    for axis in axes[8:]:
        axis.set_xlabel("Hours from listed tip")
    for axis in axes[::2]:
        axis.set_ylabel("Price of that team")
    fig.suptitle("Win-market price around tip, top 10 NBA.com power rankings", fontsize=13)
    fig.tight_layout()
    fig.savefig(OUTPUT / "price_paths.png", dpi=140)
    plt.close(fig)


def plot_variance(summary):
    frame = summary.dropna(subset=["pregame_std", "in_game_std"]).copy()
    fig, axis = plt.subplots(figsize=(10, 5))
    if frame.empty:
        axis.text(0.5, 0.5, "Not enough quotes to measure variance", ha="center")
    else:
        positions = range(len(frame))
        width = 0.38
        axis.bar([p - width / 2 for p in positions], frame["pregame_std"], width=width, label="6 hours before tip", color="#1f4e79")
        axis.bar([p + width / 2 for p in positions], frame["in_game_std"], width=width, label="First 2.5 hours after tip", color="#d9762c")
        axis.set_xticks(list(positions))
        axis.set_xticklabels(frame["team"], rotation=30, ha="right")
        axis.set_ylabel("Standard deviation of the 5-minute mid price")
        axis.legend()
    axis.set_title("Price variance before tip and during the game")
    fig.tight_layout()
    fig.savefig(OUTPUT / "variance.png", dpi=140)
    plt.close(fig)


def plot_calibration(summary):
    frame = summary.dropna(subset=["pregame_price", "won"]).copy()
    fig, axis = plt.subplots(figsize=(6.5, 6))
    axis.plot([0, 1], [0, 1], color="#b0b0b0", linewidth=1)
    if frame.empty:
        axis.text(0.5, 0.5, "No resolved games with a pregame price", ha="center")
    else:
        wins = frame[frame["won"] == True]  # noqa: E712
        losses = frame[frame["won"] == False]  # noqa: E712
        axis.scatter(wins["pregame_price"], [1] * len(wins), color="#1f4e79", label="Team won", zorder=3)
        axis.scatter(losses["pregame_price"], [0] * len(losses), color="#d9762c", label="Team lost", zorder=3)
        for _, row in frame.iterrows():
            axis.annotate(row["team"], (row["pregame_price"], 1 if row["won"] else 0), textcoords="offset points", xytext=(4, 4), fontsize=8)
        axis.legend()
    axis.set_xlim(0, 1)
    axis.set_ylim(-0.1, 1.1)
    axis.set_xlabel("Last mid price before tip")
    axis.set_ylabel("Game result")
    axis.set_title("Pregame price against the resolution")
    fig.tight_layout()
    fig.savefig(OUTPUT / "calibration.png", dpi=140)
    plt.close(fig)


def plot_spread(summary):
    frame = summary.dropna(subset=["pregame_price", "spread_pregame"]).copy()
    fig, axis = plt.subplots(figsize=(6.5, 6))
    axis.plot([0, 1], [0, 1], color="#b0b0b0", linewidth=1)
    if frame.empty:
        axis.text(0.5, 0.5, "No spread quote in the same pregame window", ha="center")
    else:
        axis.scatter(frame["pregame_price"], frame["spread_pregame"], color="#1f4e79")
        for _, row in frame.iterrows():
            axis.annotate(row["team"], (row["pregame_price"], row["spread_pregame"]), textcoords="offset points", xytext=(4, 4), fontsize=8)
    axis.set_xlim(0, 1)
    axis.set_ylim(0, 1)
    axis.set_xlabel("Moneyline mid before tip")
    axis.set_ylabel("Main spread mid before tip")
    axis.set_title("Win price versus the highest-volume spread")
    fig.tight_layout()
    fig.savefig(OUTPUT / "moneyline_vs_spread.png", dpi=140)
    plt.close(fig)


if __name__ == "__main__":
    print(
        "Reading whole hourly Parquet files from the class CloudFront host. "
        "A full run is on the order of a few GB, and that host logs each file.",
        flush=True,
    )
    analyze()
