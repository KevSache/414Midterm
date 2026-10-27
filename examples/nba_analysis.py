"""Price history for NBA game-winner markets.

Two selections are built in select_games:

    python examples/nba_analysis.py --find power
    python examples/nba_analysis.py --find top20

`power` is the NBA.com Week 2 power rankings from October 26, 2026, one
completed game per team. `top20` (or any `topN`) sorts completed game-winner
markets by volume_clob and keeps the first N that actually appear in the
hourly order-book files. A large stored volume does not by itself mean those
files contain the game, so empty slugs are skipped.

Each kept market has a slug equal to its event slug, so the two outcomes are
the two teams. The hourly Parquet files supply the bid and ask path. The
markets table only stores the latest quote.

From the same hourly files the script also keeps
the main spread, the next most traded alternate spreads, and the main game
total. It compares the price variance in the six hours before the listed tip
with the variance in the two and a half hours after tip, and it writes a
variance score on every five-minute bin: the standard deviation of the mid
over the trailing 30 minutes.

Final scores come from ESPN's public scoreboard, not from this database.
The score margin is lined up with the price path on the clock.

    python examples/nba_analysis.py

Graphs and a summary table are written to outputs/nba/.

A full run downloads whole hourly files from the class CloudFront host, on
the order of a few gigabytes. That host logs each file and the bytes sent.
"""

import base64
import json
import re
import urllib.request
from datetime import datetime, timedelta
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
VARIANCE_BINS = 6
MAX_SPREADS = 4
MAX_FIND = 40
LARGE_SIZE_MULT = 10
FOLLOW_MOVE = 0.03
ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={day}"
ESPN_SUMMARY = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event={event_id}"
LINE_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")

# Polymarket slug codes that do not match ESPN's abbreviation.
ESPN_ABBREV = {
    "gsw": "GS",
    "nop": "NO",
    "nyk": "NY",
    "sas": "SA",
    "uta": "UTAH",
    "was": "WSH",
    "wsh": "WSH",
}

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
ACTIVE_PANELS = TOP_TEAMS
CHART_SUBJECT = "NBA.com Week 2 power rankings"


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


def load_side_markets(pg):
    frame = pd.read_sql(
        """
        SELECT event_slug, slug, title, outcomes, token_ids, condition_id, volume_clob
        FROM markets
        WHERE event_slug ~ '^nba-[a-z]{3}-[a-z]{3}-[0-9]{4}-[0-9]{2}-[0-9]{2}$'
          AND slug NOT LIKE '%%1h%%'
          AND (slug LIKE '%%-spread-%%' OR slug LIKE '%%-total-%%')
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


def signed_line(title, team_name):
    numbers = LINE_RE.findall(str(title))
    if not numbers:
        return None
    number = float(numbers[-1])
    if team_name.lower() in str(title).lower():
        return number
    return -number


def token_matching(row, team_name):
    outcomes = list(row["outcomes"])
    tokens = list(row["token_ids"])
    for index, name in enumerate(outcomes):
        if team_name.lower() in str(name).lower():
            return tokens[index]
    return tokens[0]


def market_record(row, team_name, kind):
    line = signed_line(row["title"], team_name) if kind == "spread" else None
    if kind == "total":
        numbers = LINE_RE.findall(str(row["title"]))
        line = float(numbers[-1]) if numbers else None
        asset_id = token_matching(row, "Over")
    else:
        asset_id = token_matching(row, team_name)
    volume = row["volume_clob"]
    return {
        "slug": row["slug"],
        "title": row["title"],
        "condition_id": row["condition_id"],
        "asset_id": asset_id,
        "volume_clob": None if pd.isna(volume) else float(volume),
        "line": line,
        "kind": kind,
    }


def side_markets_for(children, game_slug, team_name):
    mine = children[children["event_slug"] == game_slug].copy()
    if mine.empty:
        return [], None
    mine = mine.sort_values("volume_clob", ascending=False)
    spreads = [
        market_record(row, team_name, "spread")
        for _, row in mine[mine["slug"].str.contains("-spread-", na=False)].head(MAX_SPREADS).iterrows()
    ]
    totals = mine[mine["slug"].str.contains("-total-", na=False)]
    total = market_record(totals.iloc[0], team_name, "total") if not totals.empty else None
    return spreads, total


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


def read_hour_scan(duck, moment, pairs):
    """One read of an hourly file: five-minute quotes, plus unusually large sizes.

    A large size is at least 10 times the median size on that token and side.
    Size 0 is a removed level, so those rows are left out. The file is read once.
    """
    empty_bins = pd.DataFrame(columns=["market", "asset_id", "bin", "best_bid", "best_ask"])
    empty_large = pd.DataFrame(columns=["ts_ms", "market", "asset_id", "side", "price", "size", "med_size"])
    if not pairs:
        return empty_bins, empty_large
    url = f"{BUCKET}/events-{hour_key(moment)}.parquet"
    values = ", ".join(
        f"('{market}', '{asset}', {1 if focus else 0})" for market, asset, focus in pairs
    )
    try:
        duck.execute(f"""
            CREATE OR REPLACE TEMP TABLE hour_hits AS
            WITH want(market, asset_id, focus) AS (VALUES {values})
            SELECT p.ts_ms, w.market, w.asset_id, w.focus, p.side, p.price, p.size
            FROM read_parquet('{url}') p
            JOIN want w ON p.market = w.market AND p.asset_id = w.asset_id
            WHERE p.size > 0
        """)
        bin_rows = duck.execute("""
            SELECT market, asset_id,
                   CAST(to_timestamp(floor(ts_ms / 1000.0 / 300) * 300) AS VARCHAR) AS bin,
                   max(price) FILTER (WHERE side = 'BUY') AS best_bid,
                   min(price) FILTER (WHERE side = 'SELL') AS best_ask
            FROM hour_hits
            GROUP BY 1, 2, 3
        """).fetchall()
        large_rows = duck.execute(f"""
            WITH med AS (
                SELECT market, asset_id, side, median(size) AS med_size
                FROM hour_hits
                WHERE focus = 1
                GROUP BY 1, 2, 3
                HAVING count(*) >= 20
            )
            SELECT h.ts_ms, h.market, h.asset_id, h.side, h.price, h.size, m.med_size
            FROM hour_hits h
            JOIN med m USING (market, asset_id, side)
            WHERE h.focus = 1 AND h.size >= m.med_size * {LARGE_SIZE_MULT}
        """).fetchall()
    except Exception as error:
        print(f"  skip {hour_key(moment)}: {error.__class__.__name__}", flush=True)
        return empty_bins, empty_large
    bins = pd.DataFrame(bin_rows, columns=["market", "asset_id", "bin", "best_bid", "best_ask"])
    larges = pd.DataFrame(large_rows, columns=["ts_ms", "market", "asset_id", "side", "price", "size", "med_size"])
    return bins, larges


def has_book(duck, tip, condition_id, asset_id, checks=(0, 1, 3)):
    for hours_back in checks:
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


def parse_find(text):
    """Turn --find power or --find top20 into a selection kind and a count."""
    value = (text or "power").strip().lower()
    if value == "power":
        return "power", len(TOP_TEAMS)
    match = re.fullmatch(r"top(\d+)", value)
    if match is None:
        raise SystemExit("--find must be 'power' or 'topN', for example --find top20")
    count = int(match.group(1))
    if not 1 <= count <= MAX_FIND:
        raise SystemExit(f"--find topN must be from 1 to {MAX_FIND}")
    return "volume", count


def pack_game(rank, name, code, record, row, asset_id, first, spreads, total):
    return {
        "rank": rank,
        "name": name,
        "code": code,
        "record": record,
        "game": row,
        "asset_id": asset_id,
        "first": first,
        "spreads": spreads,
        "total": total,
    }


def select_power(duck, games, side_markets):
    """One completed game for each team in the Week 2 power rankings."""
    selected = []
    confirmed = set()
    for team in TOP_TEAMS:
        print(f"{team['rank']}. {team['name']}", flush=True)
        candidates = team_games(games, team["code"])
        row, asset_id, first = choose_game(duck, candidates, team["code"], confirmed)
        if row is None:
            selected.append({**team, "game": None, "spreads": [], "total": None})
            print("  no order-book game", flush=True)
            continue
        spreads, total = side_markets_for(side_markets, row["slug"], team["name"])
        selected.append(pack_game(team["rank"], team["name"], team["code"], team["record"], row, asset_id, first, spreads, total))
        print(f"  using {row['slug']}", flush=True)
    return selected


def select_volume(duck, games, count, side_markets, probe=has_book):
    """Highest volume_clob games whose hourly files contain the away-team token.

    Games with no order-book rows are skipped, so a raw top-N by volume is not
    enough. The walk stops at `count` hits or after three times that many probes.
    """
    ordered = games.copy()
    ordered["volume_clob"] = ordered["volume_clob"].fillna(0)
    ordered = ordered[ordered["volume_clob"] > 0].sort_values(["volume_clob", "tip"], ascending=[False, False])
    probe_limit = min(len(ordered), max(count * 3, count))
    print(
        f"Walking volume_clob from the top. Keeping {count} games with order-book rows, "
        f"probing at most {probe_limit}.",
        flush=True,
    )
    selected = []
    probes = 0
    for _, row in ordered.iterrows():
        if len(selected) >= count or probes >= probe_limit:
            break
        probes += 1
        asset_id, first = token_for(row, row["away"])
        print(f"  probe {row['slug']}  volume_clob={float(row['volume_clob']):,.0f}", flush=True)
        if not probe(duck, row["tip"], row["condition_id"], asset_id, checks=(0, 1)):
            print("  no order book", flush=True)
            continue
        name = str(list(row["outcomes"])[0])
        spreads, total = side_markets_for(side_markets, row["slug"], name)
        rank = len(selected) + 1
        selected.append(pack_game(rank, name, row["away"], "", row, asset_id, first, spreads, total))
        print(f"  kept {rank}/{count} {row['slug']}", flush=True)
    if len(selected) < count:
        print(f"Found {len(selected)} games with an order book after {probes} probes.", flush=True)
    return selected


def select_games(duck, games, side_markets, find_text):
    """Choose the games for this run.

    find_text is 'power' or 'topN'. Returns the selected rows and a short
    chart subtitle.
    """
    kind, count = parse_find(find_text)
    if kind == "power":
        return select_power(duck, games, side_markets), "NBA.com Week 2 power rankings"
    selected = select_volume(duck, games, count, side_markets)
    return selected, f"top {len(selected)} by volume_clob with an order book"


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


def add_path_features(frame):
    if frame.empty:
        frame = frame.copy()
        frame["variance_score"] = pd.Series(dtype=float)
        frame["spread_width"] = pd.Series(dtype=float)
        return frame
    frame = frame.sort_values("bin").copy()
    frame["variance_score"] = frame["mid"].rolling(VARIANCE_BINS, min_periods=3).std()
    frame["spread_width"] = frame["best_ask"] - frame["best_bid"]
    return frame


def window_mean(frame, column, start, end):
    if frame.empty or column not in frame:
        return None
    sample = frame[(frame["hours_from_tip"] >= start) & (frame["hours_from_tip"] < end)][column].dropna()
    if sample.empty:
        return None
    return float(sample.mean())


def minutes_until_decided(frame):
    if frame.empty:
        return None
    live = frame[frame["hours_from_tip"] >= 0]
    hit = live[(live["mid"] >= 0.90) | (live["mid"] <= 0.10)]
    if hit.empty:
        return None
    return float(hit.iloc[0]["hours_from_tip"] * 60)


def espn_abbrev(code):
    return ESPN_ABBREV.get(code, str(code).upper())


def slug_day(slug):
    year, month, day = slug.split("-")[-3:]
    return datetime(int(year), int(month), int(day))


def fetch_json(url):
    request = urllib.request.Request(url, headers={"User-Agent": "poly-feed-class/1.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def find_espn_event(away, home, day, boards):
    wanted = {espn_abbrev(away), espn_abbrev(home)}
    for shift in (0, 1, -1):
        stamp = (day + timedelta(days=shift)).strftime("%Y%m%d")
        if stamp not in boards:
            try:
                boards[stamp] = fetch_json(ESPN_SCOREBOARD.format(day=stamp)).get("events", [])
            except Exception as error:
                print(f"  scoreboard {stamp}: {error.__class__.__name__}", flush=True)
                boards[stamp] = []
        for event in boards[stamp]:
            competitors = event["competitions"][0]["competitors"]
            abbreviations = {item["team"]["abbreviation"] for item in competitors}
            if wanted <= abbreviations:
                return event
    return None


def plays_frame(event_id, cache):
    if event_id in cache:
        return cache[event_id]
    try:
        payload = fetch_json(ESPN_SUMMARY.format(event_id=event_id))
        rows = []
        for play in payload.get("plays") or []:
            wall = play.get("wallclock")
            if not wall:
                continue
            rows.append(
                {
                    "wallclock": wall,
                    "home_score": int(play.get("homeScore") or 0),
                    "away_score": int(play.get("awayScore") or 0),
                }
            )
        frame = pd.DataFrame(rows)
        if not frame.empty:
            frame["wallclock"] = pd.to_datetime(frame["wallclock"], utc=True)
            frame = frame.sort_values("wallclock")
    except Exception as error:
        print(f"  plays {event_id}: {error.__class__.__name__}", flush=True)
        frame = pd.DataFrame()
    cache[event_id] = frame
    return frame


def quarter_list(competitor):
    return [int(float(item.get("value") or 0)) for item in (competitor.get("linescores") or [])]


def game_bucket(margin):
    if margin is None:
        return None
    gap = abs(margin)
    if gap <= 5:
        return "close"
    if gap >= 15:
        return "blowout"
    return "competitive"


def fetch_result(game, cache, boards, play_cache):
    slug = game["slug"]
    if slug in cache:
        return cache[slug]
    try:
        event = find_espn_event(game["away"], game["home"], slug_day(slug), boards)
        if event is None:
            cache[slug] = None
            return None
        competitors = {item["team"]["abbreviation"]: item for item in event["competitions"][0]["competitors"]}
        home = competitors[espn_abbrev(game["home"])]
        away = competitors[espn_abbrev(game["away"])]
        result = {
            "event_id": event["id"],
            "home_score": int(home.get("score") or 0),
            "away_score": int(away.get("score") or 0),
            "home_quarters": quarter_list(home),
            "away_quarters": quarter_list(away),
            "plays": plays_frame(event["id"], play_cache),
        }
    except Exception as error:
        print(f"  score {slug}: {error.__class__.__name__}", flush=True)
        cache[slug] = None
        return None
    cache[slug] = result
    return result


def attach_scores(path, plays, team_is_home):
    path = path.copy()
    path["score_margin"] = pd.NA
    path["team_score"] = pd.NA
    path["opponent_score"] = pd.NA
    if path.empty or plays is None or plays.empty:
        return path
    merged = pd.merge_asof(
        path.sort_values("bin"),
        plays.sort_values("wallclock"),
        left_on="bin",
        right_on="wallclock",
        direction="backward",
    )
    live = merged["hours_from_tip"] >= 0
    if team_is_home:
        merged.loc[live, "score_margin"] = merged.loc[live, "home_score"] - merged.loc[live, "away_score"]
        merged.loc[live, "team_score"] = merged.loc[live, "home_score"]
        merged.loc[live, "opponent_score"] = merged.loc[live, "away_score"]
    else:
        merged.loc[live, "score_margin"] = merged.loc[live, "away_score"] - merged.loc[live, "home_score"]
        merged.loc[live, "team_score"] = merged.loc[live, "away_score"]
        merged.loc[live, "opponent_score"] = merged.loc[live, "home_score"]
    return merged.drop(columns=["wallclock", "home_score", "away_score"], errors="ignore")


def dedupe_orders(frame):
    """Collapse repeated prints of the same resting size."""
    if frame is None or frame.empty:
        return frame
    frame = frame.sort_values("time").copy()
    frame["price_key"] = frame["price"].round(3)
    frame["size_key"] = frame["size"].round(1)
    keys = ["market", "asset_id", "side", "price_key", "size_key"]
    gap = frame.groupby(keys, sort=False)["time"].diff().dt.total_seconds()
    return frame.loc[gap.isna() | (gap > 10)].drop(columns=["price_key", "size_key"])


def scoring_times(plays):
    empty = pd.DataFrame(columns=["wallclock"])
    if plays is None or plays.empty:
        return empty
    frame = plays.sort_values("wallclock").copy()
    changed = (frame["home_score"] + frame["away_score"]).diff().fillna(0) != 0
    times = frame.loc[changed, ["wallclock"]]
    return times if not times.empty else empty


def classify_orders(orders, path, plays, tip):
    """Mark large sizes whose price then moved, and which of those led a scoring play.

    There is no wallet on these rows. A size that is removed cannot be told from a
    cancel, so only posted sizes are kept. 'Led a score' means the previous basket
    was at least 15 seconds earlier and the next basket came within two minutes.
    """
    columns = [
        "time", "hours_from_tip", "side", "price", "size", "med_size", "size_ratio",
        "mid_at", "mid_later", "price_followed", "led_score", "stands_out",
    ]
    if orders is None or orders.empty:
        return pd.DataFrame(columns=columns)
    frame = orders.copy()
    frame["time"] = pd.to_datetime(frame["ts_ms"], unit="ms", utc=True)
    frame = dedupe_orders(frame).sort_values("time").reset_index(drop=True)
    frame["hours_from_tip"] = (frame["time"] - pd.Timestamp(tip)) / pd.Timedelta(hours=1)
    frame["size_ratio"] = frame["size"] / frame["med_size"]
    mids = pd.DataFrame(columns=["bin", "mid"])
    if path is not None and not path.empty and "mid" in path:
        mids = path.loc[path["mid"].notna(), ["bin", "mid"]].sort_values("bin")
    if not mids.empty:
        frame = pd.merge_asof(frame, mids, left_on="time", right_on="bin", direction="backward")
        frame = frame.rename(columns={"mid": "mid_at"}).drop(columns=["bin"], errors="ignore")
        frame["lookup"] = frame["time"] + pd.Timedelta(minutes=30)
        later = pd.merge_asof(
            frame[["lookup"]],
            mids.rename(columns={"bin": "later_bin", "mid": "mid_later"}),
            left_on="lookup",
            right_on="later_bin",
            direction="forward",
        )
        frame["mid_later"] = later["mid_later"].to_numpy()
        frame = frame.drop(columns=["lookup"])
    else:
        frame["mid_at"] = pd.NA
        frame["mid_later"] = pd.NA
    move = pd.to_numeric(frame["mid_later"], errors="coerce") - pd.to_numeric(frame["mid_at"], errors="coerce")
    buy = frame["side"].str.upper() == "BUY"
    frame["price_followed"] = (buy & (move >= FOLLOW_MOVE)) | (~buy & (move <= -FOLLOW_MOVE))
    frame["price_followed"] = frame["price_followed"].fillna(False)
    times = scoring_times(plays)
    frame["led_score"] = False
    if not times.empty:
        previous = pd.merge_asof(
            frame[["time"]],
            times.rename(columns={"wallclock": "prev_score"}),
            left_on="time",
            right_on="prev_score",
            direction="backward",
        )
        nxt = pd.merge_asof(
            frame[["time"]],
            times.rename(columns={"wallclock": "next_score"}),
            left_on="time",
            right_on="next_score",
            direction="forward",
        )
        since_s = (frame["time"] - previous["prev_score"]).dt.total_seconds()
        until_s = (nxt["next_score"] - frame["time"]).dt.total_seconds()
        not_reaction = since_s.isna() | (since_s >= 15)
        frame["led_score"] = (not_reaction & until_s.notna() & (until_s <= 120)).fillna(False)
    frame["stands_out"] = frame["price_followed"] & frame["led_score"]
    return frame[columns]


def quote_path(book, market, tip):
    if market is None or book.empty:
        return add_path_features(series_from(pd.DataFrame(), tip))
    rows = book[(book["market"] == market["condition_id"]) & (book["asset_id"] == market["asset_id"])]
    return add_path_features(series_from(rows, tip))


def analyze(find_text="power"):
    global ACTIVE_PANELS, CHART_SUBJECT
    OUTPUT.mkdir(parents=True, exist_ok=True)
    pg = connect_postgres()
    games = load_games(pg)
    side_markets = load_side_markets(pg)
    pg.close()
    duck = connect_duckdb()

    selected, CHART_SUBJECT = select_games(duck, games, side_markets, find_text)
    ACTIVE_PANELS = [
        {"rank": item["rank"], "name": item["name"], "code": item["code"], "record": item.get("record") or ""}
        for item in selected
    ]
    duck.close()
    duck = connect_duckdb()

    needed = {}
    for item in selected:
        if item["game"] is None:
            continue
        game = item["game"]
        pairs = [(game["condition_id"], item["asset_id"], True)]
        for market in item["spreads"]:
            pairs.append((market["condition_id"], market["asset_id"], False))
        if item["total"]:
            pairs.append((item["total"]["condition_id"], item["total"]["asset_id"], False))
        for moment in hours_around(game["tip"]):
            bucket = needed.setdefault(moment, {})
            for market, asset, focus in pairs:
                key = (market, asset)
                bucket[key] = bucket.get(key, False) or focus

    quotes = []
    large_hits = []
    moments = sorted(needed)
    for index, moment in enumerate(moments, start=1):
        if index > 1 and index % 6 == 1:
            duck.close()
            duck = connect_duckdb()
        print(f"hour {index}/{len(moments)} {hour_key(moment)}", flush=True)
        pairs = [(market, asset, focus) for (market, asset), focus in needed[moment].items()]
        frame, larges = read_hour_scan(duck, moment, pairs)
        if not frame.empty:
            quotes.append(frame)
        if not larges.empty:
            large_hits.append(larges)
    duck.close()
    book = pd.concat(quotes, ignore_index=True) if quotes else pd.DataFrame()
    large_book = pd.concat(large_hits, ignore_index=True) if large_hits else pd.DataFrame()

    summaries = []
    paths = []
    alternates = []
    order_frames = []
    results = []
    score_cache = {}
    boards = {}
    play_cache = {}
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
            "variance_score_pregame": None,
            "variance_score_early": None,
            "variance_score_late": None,
            "variance_score_live": None,
            "minutes_to_decided": None,
            "large_n": 0,
            "followed_n": 0,
            "led_score_n": 0,
            "espn_team_score": None,
            "espn_opponent_score": None,
            "espn_margin": None,
            "game_bucket": None,
            "final_points": None,
            "total_line": None,
            "total_pregame": None,
            "note": "Every completed game-winner market for this team was checked. None of those hours contain its token.",
        }
        if item["game"] is None:
            summaries.append(summary)
            continue
        game = item["game"]
        opponent_code = game["home"] if game["away"] == item["code"] else game["away"]
        opponent = str(game["outcomes"][1] if item["first"] else game["outcomes"][0])
        team_is_home = game["home"] == item["code"]
        moneyline = {
            "condition_id": game["condition_id"],
            "asset_id": item["asset_id"],
        }
        path = quote_path(book, moneyline, game["tip"])
        main_spread = item["spreads"][0] if item["spreads"] else None
        spread_path = quote_path(book, main_spread, game["tip"])
        total_path = quote_path(book, item["total"], game["tip"])
        result = fetch_result(game, score_cache, boards, play_cache)
        if result is None:
            print(f"  no ESPN score for {game['slug']}", flush=True)
            path = attach_scores(path, None, team_is_home)
        else:
            path = attach_scores(path, result["plays"], team_is_home)
            print(
                f"  ESPN {result['away_score']}-{result['home_score']} ({game['away']} at {game['home']})",
                flush=True,
            )
        if not spread_path.empty:
            path = path.merge(
                spread_path[["bin", "mid"]].rename(columns={"mid": "spread_mid"}),
                on="bin",
                how="left",
            )
        else:
            path["spread_mid"] = pd.NA
        if not total_path.empty:
            path = path.merge(
                total_path[["bin", "mid"]].rename(columns={"mid": "total_mid"}),
                on="bin",
                how="left",
            )
        else:
            path["total_mid"] = pd.NA

        pre_std, pre_n = window_std(path, -HOURS_BEFORE, 0)
        live_std, live_n = window_std(path, 0, IN_GAME_HOURS)
        pregame = last_before_tip(path)
        won = None
        if pd.notna(game["resolved_yes"]):
            won = bool(game["resolved_yes"]) if item["first"] else not bool(game["resolved_yes"])
        team_score = opponent_score = margin = None
        quarters = ""
        if result is not None:
            if team_is_home:
                team_score = result["home_score"]
                opponent_score = result["away_score"]
                team_quarters = result["home_quarters"]
                opponent_quarters = result["away_quarters"]
            else:
                team_score = result["away_score"]
                opponent_score = result["home_score"]
                team_quarters = result["away_quarters"]
                opponent_quarters = result["home_quarters"]
            margin = team_score - opponent_score
            quarters = " ".join(
                f"Q{index}:{left}-{right}"
                for index, (left, right) in enumerate(zip(team_quarters, opponent_quarters), start=1)
            )
        total_line = item["total"]["line"] if item["total"] else None
        total_pregame = last_before_tip(total_path)
        final_points = None if team_score is None else team_score + opponent_score
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
                "spread": None if main_spread is None else main_spread["title"],
                "spread_line": None if main_spread is None else main_spread["line"],
                "spread_pregame": last_before_tip(spread_path),
                "variance_score_pregame": window_mean(path, "variance_score", -HOURS_BEFORE, 0),
                "variance_score_early": window_mean(path, "variance_score", 0, IN_GAME_HOURS / 2),
                "variance_score_late": window_mean(path, "variance_score", IN_GAME_HOURS / 2, IN_GAME_HOURS),
                "variance_score_live": window_mean(path, "variance_score", 0, IN_GAME_HOURS),
                "minutes_to_decided": minutes_until_decided(path),
                "bid_ask_pregame": window_mean(path, "spread_width", -1, 0),
                "bid_ask_live": window_mean(path, "spread_width", 0, IN_GAME_HOURS),
                "espn_team_score": team_score,
                "espn_opponent_score": opponent_score,
                "espn_margin": margin,
                "game_bucket": game_bucket(margin),
                "quarters": quarters,
                "final_points": final_points,
                "total_line": total_line,
                "total_pregame": total_pregame,
                "total_hit": None if final_points is None or total_line is None else final_points > total_line,
                "large_n": 0,
                "followed_n": 0,
                "led_score_n": 0,
                "volume_clob": None if pd.isna(game["volume_clob"]) else game["volume_clob"],
                "score_source": None if result is None else f"espn:{result['event_id']}",
                "note": "" if result is not None else "Order book found. ESPN had no matching final score.",
            }
        )
        summaries.append(summary)
        results.append(summary)
        for market in item["spreads"]:
            series = quote_path(book, market, game["tip"])
            alternates.append(
                {
                    "rank": item["rank"],
                    "team": item["name"],
                    "line": market["line"],
                    "pregame_price": last_before_tip(series),
                    "title": market["title"],
                    "volume_clob": market["volume_clob"],
                }
            )
        if not path.empty:
            score_label = ""
            if team_score is not None:
                score_label = f" {team_score}-{opponent_score}"
            plot_path = path.copy()
            plot_path["team"] = item["name"]
            plot_path["rank"] = item["rank"]
            plot_path["label"] = f"{item['name']} vs {opponent}{score_label}"
            plot_path["bucket"] = summary["game_bucket"]
            paths.append(plot_path)
        if not large_book.empty:
            mine_orders = large_book[
                (large_book["market"] == game["condition_id"]) & (large_book["asset_id"] == item["asset_id"])
            ]
            tagged = classify_orders(
                mine_orders,
                path,
                None if result is None else result["plays"],
                game["tip"],
            )
            if not tagged.empty:
                tagged.insert(0, "rank", item["rank"])
                tagged.insert(1, "team", item["name"])
                tagged.insert(2, "label", f"{item['name']} vs {opponent}")
                order_frames.append(tagged)
                summary["large_n"] = int(len(tagged))
                summary["followed_n"] = int(tagged["price_followed"].sum())
                summary["led_score_n"] = int(tagged["stands_out"].sum())

    summary_frame = pd.DataFrame(summaries)
    alternate_frame = pd.DataFrame(alternates)
    summary_frame.to_csv(OUTPUT / "summary.csv", index=False)
    if paths:
        pd.concat(paths, ignore_index=True).to_csv(OUTPUT / "price_paths.csv", index=False)
    if not alternate_frame.empty:
        alternate_frame.to_csv(OUTPUT / "alternate_spreads.csv", index=False)
    pd.DataFrame(results).to_csv(OUTPUT / "game_results.csv", index=False)
    orders = pd.concat(order_frames, ignore_index=True) if order_frames else pd.DataFrame()
    if not orders.empty:
        orders.to_csv(OUTPUT / "large_orders.csv", index=False)
    plot_paths(paths)
    plot_variance(summary_frame)
    plot_variance_paths(paths)
    plot_variance_vs_margin(summary_frame)
    plot_price_vs_score(paths)
    plot_market_paths(paths)
    plot_bid_ask(paths)
    plot_alternate_spreads(alternate_frame)
    plot_calibration(summary_frame)
    plot_spread(summary_frame)
    plot_large_orders(paths, orders)
    plot_large_order_counts(summary_frame)
    columns = [
        "rank",
        "team",
        "game",
        "variance_score_live",
        "variance_score_early",
        "variance_score_late",
        "espn_margin",
        "game_bucket",
        "large_n",
        "followed_n",
        "led_score_n",
        "won",
    ]
    print(summary_frame[columns].to_string(index=False))
    print(f"Wrote {OUTPUT}")


def panels_from_summary(summary):
    panels = []
    for _, row in summary.sort_values("rank").iterrows():
        record = ""
        if "record" in summary.columns and pd.notna(row["record"]):
            record = str(row["record"])
        panels.append({"rank": int(row["rank"]), "name": str(row["team"]), "record": record})
    return panels


def redraw():
    global ACTIVE_PANELS, CHART_SUBJECT
    summary_path = OUTPUT / "summary.csv"
    if not summary_path.exists():
        raise SystemExit(f"No saved table at {summary_path}. Run the script once without --plots-only.")
    summary = pd.read_csv(summary_path)
    ACTIVE_PANELS = panels_from_summary(summary)
    CHART_SUBJECT = f"{len(ACTIVE_PANELS)} saved games"
    if "won" in summary:
        summary["won"] = summary["won"].map(
            lambda value: value if isinstance(value, bool) or pd.isna(value) else str(value).strip().lower() == "true"
        )
    price_path = OUTPUT / "price_paths.csv"
    paths = []
    if price_path.exists():
        prices = pd.read_csv(price_path)
        for _, part in prices.groupby("rank", sort=True):
            paths.append(part.reset_index(drop=True))
    alternate_path = OUTPUT / "alternate_spreads.csv"
    alternates = pd.read_csv(alternate_path) if alternate_path.exists() else pd.DataFrame()
    plot_paths(paths)
    plot_variance(summary)
    plot_variance_paths(paths)
    plot_variance_vs_margin(summary)
    plot_price_vs_score(paths)
    plot_market_paths(paths)
    plot_bid_ask(paths)
    plot_alternate_spreads(alternates)
    plot_calibration(summary)
    plot_spread(summary)
    orders_path = OUTPUT / "large_orders.csv"
    orders = pd.read_csv(orders_path) if orders_path.exists() else pd.DataFrame()
    plot_large_orders(paths, orders)
    plot_large_order_counts(summary)
    print(f"Rewrote charts in {OUTPUT}")


def plot_paths(paths):
    fig, axes, count, cols = grid_for(sharey=True)
    by_rank = paths_by_rank(paths)
    for axis, panel in zip(axes, ACTIVE_PANELS):
        frame = by_rank.get(panel["rank"])
        if frame is None:
            draw_empty(axis, panel)
            axis.set_xlim(-HOURS_BEFORE, HOURS_AFTER)
            axis.set_ylim(0, 1)
        else:
            axis.plot(frame["hours_from_tip"], frame["mid"], color="#1f4e79", linewidth=1.4)
            axis.axvline(0, color="#b0b0b0", linewidth=0.8)
            axis.set_ylim(0, 1)
            axis.set_title(f"{panel['rank']}. {frame['label'].iloc[0]}", loc="left", fontsize=9)
        axis.grid(axis="y", color="#eeeeee")
    finish_grid(fig, axes, count, cols, "Hours from listed tip", "Price of that team", f"Win price around tip, {CHART_SUBJECT}", "price_paths.png")


def plot_variance(summary):
    frame = summary.dropna(subset=["pregame_std", "in_game_std"]).copy()
    fig, axis = plt.subplots(figsize=(max(10, 0.55 * max(len(frame), 1)), 5))
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


def panel_heading(panel):
    record = panel.get("record") or ""
    if record:
        return f"{panel['rank']}. {panel['name']} ({record})"
    return f"{panel['rank']}. {panel['name']}"


def grid_for(sharey=False):
    count = max(len(ACTIVE_PANELS), 1)
    cols = 2 if count <= 10 else 4
    rows = (count + cols - 1) // cols
    fig, axes = plt.subplots(
        rows,
        cols,
        figsize=(5.5 * cols, 2.55 * rows),
        sharex=True,
        sharey=sharey,
        squeeze=False,
    )
    flat = list(axes.ravel())
    for axis in flat[count:]:
        axis.axis("off")
    return fig, flat, count, cols


def finish_grid(fig, axes, count, cols, xlabel, ylabel, title, filename):
    for index in range(count):
        if index + cols >= count:
            axes[index].set_xlabel(xlabel)
        if index % cols == 0:
            axes[index].set_ylabel(ylabel)
    fig.suptitle(title, fontsize=13)
    fig.tight_layout()
    fig.savefig(OUTPUT / filename, dpi=140)
    plt.close(fig)


def draw_empty(axis, panel):
    axis.text(0.5, 0.5, "No order-book path", ha="center", va="center", transform=axis.transAxes)
    axis.set_title(panel_heading(panel), loc="left", fontsize=9)


def paths_by_rank(paths):
    grouped = {}
    for frame in paths:
        if frame is None or len(frame) == 0:
            continue
        frame = frame.copy()
        for column in (
            "hours_from_tip",
            "mid",
            "variance_score",
            "score_margin",
            "spread_mid",
            "total_mid",
            "spread_width",
            "best_bid",
            "best_ask",
        ):
            if column in frame:
                frame[column] = pd.to_numeric(frame[column], errors="coerce")
        grouped[int(frame["rank"].iloc[0])] = frame
    return grouped


def plot_variance_paths(paths):
    fig, axes, count, cols = grid_for(sharey=True)
    by_rank = paths_by_rank(paths)
    for axis, panel in zip(axes, ACTIVE_PANELS):
        frame = by_rank.get(panel["rank"])
        if frame is None or "variance_score" not in frame or frame["variance_score"].dropna().empty:
            draw_empty(axis, panel)
            continue
        axis.plot(frame["hours_from_tip"], frame["variance_score"], color="#6b3fa0", linewidth=1.4)
        axis.axvline(0, color="#b0b0b0", linewidth=0.8)
        axis.set_title(f"{panel['rank']}. {frame['label'].iloc[0]}", loc="left", fontsize=9)
        axis.grid(axis="y", color="#eeeeee")
    finish_grid(fig, axes, count, cols, "Hours from listed tip", "Trailing 30-minute std", "Variance score every five minutes", "variance_score.png")


def plot_variance_vs_margin(summary):
    frame = summary.dropna(subset=["variance_score_live", "espn_margin"]).copy()
    fig, axis = plt.subplots(figsize=(7.5, 5.5))
    colors = {"close": "#1f4e79", "competitive": "#d9762c", "blowout": "#6b6b6b"}
    if frame.empty:
        axis.text(0.5, 0.5, "No game has both a variance score and an ESPN score", ha="center")
    else:
        frame["abs_margin"] = frame["espn_margin"].abs()
        for bucket, color in colors.items():
            part = frame[frame["game_bucket"] == bucket]
            axis.scatter(part["abs_margin"], part["variance_score_live"], color=color, label=bucket, zorder=3)
        for _, row in frame.iterrows():
            axis.annotate(row["team"], (abs(row["espn_margin"]), row["variance_score_live"]), textcoords="offset points", xytext=(4, 4), fontsize=8)
        axis.legend()
    axis.set_xlabel("Absolute final margin")
    axis.set_ylabel("Mean in-game variance score")
    axis.set_title("Variance score against the final margin")
    fig.tight_layout()
    fig.savefig(OUTPUT / "variance_vs_margin.png", dpi=140)
    plt.close(fig)


def plot_price_vs_score(paths):
    fig, axes, count, cols = grid_for()
    by_rank = paths_by_rank(paths)
    for index, (axis, panel) in enumerate(zip(axes, ACTIVE_PANELS)):
        frame = by_rank.get(panel["rank"])
        if frame is None:
            draw_empty(axis, panel)
            continue
        axis.plot(frame["hours_from_tip"], frame["mid"], color="#1f4e79", linewidth=1.4, label="Win price")
        axis.axvline(0, color="#b0b0b0", linewidth=0.8)
        axis.set_ylim(0, 1)
        scores = frame["score_margin"].dropna() if "score_margin" in frame else pd.Series(dtype=float)
        if not scores.empty:
            twin = axis.twinx()
            twin.plot(frame["hours_from_tip"], frame["score_margin"], color="#b04a4a", linewidth=1.2, label="Score margin")
            if (index + 1) % cols == 0:
                twin.set_ylabel("Margin")
            limit = max(15, float(scores.abs().max()) + 2)
            twin.set_ylim(-limit, limit)
        axis.set_title(f"{panel['rank']}. {frame['label'].iloc[0]}", loc="left", fontsize=9)
        axis.grid(axis="y", color="#eeeeee")
    finish_grid(fig, axes, count, cols, "Hours from listed tip", "Win price", "Win price and ESPN score margin", "price_vs_score.png")


def plot_market_paths(paths):
    fig, axes, count, cols = grid_for(sharey=True)
    by_rank = paths_by_rank(paths)
    labeled = False
    for axis, panel in zip(axes, ACTIVE_PANELS):
        frame = by_rank.get(panel["rank"])
        if frame is None:
            draw_empty(axis, panel)
            continue
        axis.plot(frame["hours_from_tip"], frame["mid"], color="#1f4e79", linewidth=1.4, label="Win")
        if "spread_mid" in frame and frame["spread_mid"].notna().any():
            axis.plot(frame["hours_from_tip"], frame["spread_mid"], color="#d9762c", linewidth=1.2, label="Main spread")
        if "total_mid" in frame and frame["total_mid"].notna().any():
            axis.plot(frame["hours_from_tip"], frame["total_mid"], color="#2f7d4a", linewidth=1.2, label="Game total Over")
        axis.axvline(0, color="#b0b0b0", linewidth=0.8)
        axis.set_ylim(0, 1)
        axis.set_title(f"{panel['rank']}. {frame['label'].iloc[0]}", loc="left", fontsize=9)
        axis.grid(axis="y", color="#eeeeee")
        if not labeled:
            axis.legend(fontsize=8, loc="lower left")
            labeled = True
    finish_grid(fig, axes, count, cols, "Hours from listed tip", "Price", "Win price, main spread, and game total", "market_paths.png")


def plot_bid_ask(paths):
    fig, axes, count, cols = grid_for(sharey=True)
    by_rank = paths_by_rank(paths)
    for axis, panel in zip(axes, ACTIVE_PANELS):
        frame = by_rank.get(panel["rank"])
        if frame is None or "spread_width" not in frame or frame["spread_width"].dropna().empty:
            draw_empty(axis, panel)
            continue
        axis.plot(frame["hours_from_tip"], frame["spread_width"], color="#1f4e79", linewidth=1.3)
        axis.axvline(0, color="#b0b0b0", linewidth=0.8)
        axis.set_title(f"{panel['rank']}. {frame['label'].iloc[0]}", loc="left", fontsize=9)
        axis.grid(axis="y", color="#eeeeee")
    finish_grid(fig, axes, count, cols, "Hours from listed tip", "Ask minus bid", "Bid-ask gap every five minutes", "bid_ask_width.png")


def plot_alternate_spreads(frame):
    fig, axes, count, cols = grid_for()
    if frame is None or frame.empty:
        grouped = {}
    else:
        frame = frame.copy()
        frame["line"] = pd.to_numeric(frame["line"], errors="coerce")
        frame["pregame_price"] = pd.to_numeric(frame["pregame_price"], errors="coerce")
        grouped = {int(rank): part.dropna(subset=["line", "pregame_price"]) for rank, part in frame.groupby("rank")}
    for axis, panel in zip(axes, ACTIVE_PANELS):
        part = grouped.get(panel["rank"])
        if part is None or part.empty:
            draw_empty(axis, panel)
            continue
        part = part.sort_values("line")
        axis.plot(part["line"], part["pregame_price"], color="#1f4e79", marker="o")
        axis.set_ylim(0, 1)
        axis.axhline(0.5, color="#b0b0b0", linewidth=0.8)
        axis.set_title(panel_heading(panel), loc="left", fontsize=9)
        axis.grid(axis="y", color="#eeeeee")
    finish_grid(fig, axes, count, cols, "Points, from that team's side", "Pregame cover price", "Alternate spreads before tip", "alternate_spreads.png")


def plot_large_orders(paths, orders):
    fig, axes, count, cols = grid_for()
    by_rank = paths_by_rank(paths)
    grouped = {}
    if orders is not None and not orders.empty:
        orders = orders.copy()
        for column in ("hours_from_tip", "price", "size_ratio"):
            if column in orders:
                orders[column] = pd.to_numeric(orders[column], errors="coerce")
        if "stands_out" in orders:
            orders["stands_out"] = orders["stands_out"].map(lambda value: str(value).strip().lower() in {"true", "1"} or value is True)
        grouped = {int(rank): part for rank, part in orders.groupby("rank")}
    labeled = False
    legend_axis = None
    for axis, panel in zip(axes, ACTIVE_PANELS):
        frame = by_rank.get(panel["rank"])
        if frame is None:
            draw_empty(axis, panel)
            continue
        axis.plot(frame["hours_from_tip"], frame["mid"], color="#d9d9d9", linewidth=1.2)
        axis.axvline(0, color="#b0b0b0", linewidth=0.8)
        axis.set_ylim(0, 1)
        part = grouped.get(panel["rank"])
        if part is not None and not part.empty:
            if len(part) > 60 and "size_ratio" in part:
                part = part.nlargest(60, "size_ratio")
            quiet = part[~part["stands_out"]] if "stands_out" in part else part
            loud = part[part["stands_out"]] if "stands_out" in part else part.iloc[0:0]
            if legend_axis is None:
                legend_axis = axis
            if not quiet.empty:
                buys = quiet["side"].str.upper() == "BUY"
                axis.scatter(quiet.loc[buys, "hours_from_tip"], quiet.loc[buys, "price"], marker="^", s=18, color="#9aa0a6", label="Large buy" if not labeled else None)
                axis.scatter(quiet.loc[~buys, "hours_from_tip"], quiet.loc[~buys, "price"], marker="v", s=18, color="#9aa0a6", label="Large sell" if not labeled else None)
            if not loud.empty:
                buys = loud["side"].str.upper() == "BUY"
                axis.scatter(loud.loc[buys, "hours_from_tip"], loud.loc[buys, "price"], marker="^", s=36, color="#1f4e79", label="Led a score" if not labeled else None, zorder=3)
                axis.scatter(loud.loc[~buys, "hours_from_tip"], loud.loc[~buys, "price"], marker="v", s=36, color="#d9762c", label="Led a score, sell" if not labeled else None, zorder=3)
            labeled = True
        title = frame["label"].iloc[0] if "label" in frame else panel["name"]
        axis.set_title(f"{panel['rank']}. {title}", loc="left", fontsize=9)
        axis.grid(axis="y", color="#eeeeee")
    if legend_axis is not None:
        legend_axis.legend(fontsize=7, loc="lower left")
    finish_grid(
        fig, axes, count, cols,
        "Hours from listed tip", "Price",
        "Large orders. Dark marks led the next score and the price followed.",
        "large_orders.png",
    )


def plot_large_order_counts(summary):
    columns = ["team", "large_n", "followed_n", "led_score_n"]
    if summary is None or summary.empty or not set(columns).issubset(summary.columns):
        fig, axis = plt.subplots(figsize=(8, 4))
        axis.text(0.5, 0.5, "No large-order counts in this run", ha="center")
        fig.tight_layout()
        fig.savefig(OUTPUT / "large_order_counts.png", dpi=140)
        plt.close(fig)
        return
    frame = summary.copy()
    for column in ("large_n", "followed_n", "led_score_n"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce").fillna(0)
    frame = frame[frame["large_n"] > 0]
    fig, axis = plt.subplots(figsize=(max(10, 0.55 * max(len(frame), 1)), 5))
    if frame.empty:
        axis.text(0.5, 0.5, "No order was at least 10 times the median size", ha="center")
    else:
        positions = list(range(len(frame)))
        width = 0.25
        axis.bar([p - width for p in positions], frame["large_n"], width=width, color="#c5c5c5", label="At least 10x median size")
        axis.bar(positions, frame["followed_n"], width=width, color="#d9762c", label="Price followed for 30 minutes")
        axis.bar([p + width for p in positions], frame["led_score_n"], width=width, color="#1f4e79", label="Also led the next score")
        axis.set_xticks(positions)
        axis.set_xticklabels(frame["team"], rotation=30, ha="right")
        axis.legend()
    axis.set_ylabel("Orders")
    axis.set_title("Large book updates, and how many lined up with the game")
    fig.tight_layout()
    fig.savefig(OUTPUT / "large_order_counts.png", dpi=140)
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
    import argparse

    parser = argparse.ArgumentParser(description="NBA top-10 price paths from the class order-book files.")
    parser.add_argument(
        "--find",
        default="power",
        help="Which games to chart: power, or topN (for example top20). topN keeps the N highest volume_clob games that have order-book rows.",
    )
    parser.add_argument(
        "--plots-only",
        action="store_true",
        help="Redraw the charts from outputs/nba CSVs. Does not download hourly files.",
    )
    args = parser.parse_args()
    if args.plots_only:
        redraw()
    else:
        print(
            "Reading whole hourly Parquet files from the class CloudFront host. "
            "A full run is on the order of a few GB, and that host logs each file. "
            "Final scores are a few small requests to ESPN, not to that host.",
            flush=True,
        )
        analyze(args.find)
