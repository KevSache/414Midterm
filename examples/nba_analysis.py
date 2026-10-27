"""Price history for NBA markets that already have an order book.

    python examples/nba_analysis.py
    python examples/nba_analysis.py --find top20

The default, `top10`, reads the hourly order-book files around every
completed NBA tip and keeps game winners that are high on both matched
volume (`volume_clob` at least 1,000) and posted size in those files.
Any `topN` from 1 to 40 does the same. There is no team list. A market
with a noisy book and almost no trades is left out, and so is a market
that never appears in the files.

`props5` (or `--find props --each 5`) does the same charts for player
over/under markets. It reads those same tip hours and keeps points,
assists, and rebounds lines that are high on both matched volume and
posted size. A line is charted only when its token is in the files and
its stored volume is at least 1,000. `--stats` can add
threes, steals, or blocks. Charts for that run are written to
outputs/nba/props/ so they do not replace the game-winner charts.

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
DATA_DICTIONARY.md in that folder defines every CSV column.

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
DEFAULT_TOP = 10
# Matched volume below this is quote noise in this NBA extract. The gap in
# game-winner volume_clob sits between about 900 and about 2,700.
MIN_VOLUME_CLOB = 1000
MAX_PROP_EACH = 10
DEFAULT_PROP_EACH = 4
LARGE_SIZE_MULT = 10
FOLLOW_MOVE = 0.03
PROP_STATS = ("points", "assists", "rebounds", "threes", "steals", "blocks")
STAT_SHORT = {
    "points": "pts",
    "assists": "ast",
    "rebounds": "reb",
    "threes": "3pm",
    "steals": "stl",
    "blocks": "blk",
}
BOX_KEY = {
    "points": "PTS",
    "assists": "AST",
    "rebounds": "REB",
    "threes": "3PT",
    "steals": "STL",
    "blocks": "BLK",
}
PROP_RE = re.compile(
    r"^nba-([a-z]{3})-([a-z]{3})-(\d{4}-\d{2}-\d{2})"
    r"-(points|assists|rebounds|threes|steals|blocks)-(.+)-(\d+)pt(\d+)$"
)
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

ACTIVE_PANELS = []
CHART_SUBJECT = "NBA game winners with matched volume and a posted book"
CHART_KIND = "game"


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


def search_hours(tip):
    """The tip hour, the hour before it, and the hour after it."""
    base = tip.replace(minute=0, second=0, microsecond=0)
    return [base + timedelta(hours=offset) for offset in (-1, 0, 1)]


def activity_in_hour(duck, moment, pairs):
    """Posted-size totals for these tokens in one hourly file.

    size > 0 is a posted level. A later cancel is a separate row with size 0
    and is not counted. This is not matched trade volume.
    """
    empty = pd.DataFrame(columns=["condition_id", "asset_id", "book_updates", "book_size"])
    if not pairs:
        return empty
    url = f"{BUCKET}/events-{hour_key(moment)}.parquet"
    values = ", ".join(f"('{market}', '{asset}')" for market, asset in pairs)
    sql = f"""
        WITH want(market, asset_id) AS (VALUES {values})
        SELECT w.market, w.asset_id, count(*) AS book_updates, sum(p.size) AS book_size
        FROM read_parquet('{url}') p
        JOIN want w ON p.market = w.market AND p.asset_id = w.asset_id
        WHERE p.size > 0
        GROUP BY 1, 2
    """
    try:
        rows = duck.execute(sql).fetchall()
    except Exception as error:
        print(f"  skip {hour_key(moment)}: {error.__class__.__name__}", flush=True)
        return empty
    return pd.DataFrame(rows, columns=["condition_id", "asset_id", "book_updates", "book_size"])


def discover_activity(frame):
    """Sum posted size for each token in the three hours around its listed tip.

    Each distinct hour file is read once. The returned rows are only tokens
    that appeared, so a market with no order book is absent.
    """
    empty = pd.DataFrame(columns=["condition_id", "asset_id", "book_updates", "book_size"])
    if frame is None or frame.empty:
        return empty
    buckets = {}
    for _, row in frame.iterrows():
        pair = (str(row["condition_id"]), str(row["asset_id"]))
        for moment in search_hours(row["tip"]):
            buckets.setdefault(moment, set()).add(pair)
    moments = sorted(buckets)
    print(
        f"Searching {len(moments)} hourly files for NBA tokens around each listed tip. "
        "A file is one hour for the whole venue, read once.",
        flush=True,
    )
    duck = None
    parts = []
    try:
        duck = connect_duckdb()
        for index, moment in enumerate(moments, start=1):
            if index > 1 and index % 6 == 1:
                duck.close()
                duck = connect_duckdb()
            print(f"  book hour {index}/{len(moments)} {hour_key(moment)}", flush=True)
            found = activity_in_hour(duck, moment, buckets[moment])
            if not found.empty:
                parts.append(found)
    finally:
        if duck is not None:
            try:
                duck.close()
            except Exception:
                pass
    if not parts:
        return empty
    combined = pd.concat(parts, ignore_index=True)
    combined["book_updates"] = combined["book_updates"].astype(float)
    combined["book_size"] = combined["book_size"].astype(float)
    return (
        combined.groupby(["condition_id", "asset_id"], as_index=False)[["book_updates", "book_size"]]
        .sum()
    )


def book_fields(game):
    found = {}
    if "book_size" in game and pd.notna(game["book_size"]):
        found["book_size"] = float(game["book_size"])
    if "book_updates" in game and pd.notna(game["book_updates"]):
        found["book_updates"] = int(game["book_updates"])
    if "balance" in game and pd.notna(game["balance"]):
        found["balance"] = float(game["balance"])
    return found


def parse_find(text):
    """Turn --find into a kind and a count.

    topN, props, and propsN are accepted. The default is the busiest NBA
    game winners, with no team filter. propsN sets how many markets to keep
    for each stat. A bare 'props' leaves the count unset so --each or the
    default can fill it in.
    """
    value = (text or f"top{DEFAULT_TOP}").strip().lower()
    if value == "power":
        raise SystemExit(
            "Team power rankings are no longer used. "
            "Run python examples/nba_analysis.py for the busiest NBA game winners, "
            "or --find props5 for player props."
        )
    if value == "props":
        return "props", None
    match = re.fullmatch(r"props(\d+)", value)
    if match:
        count = int(match.group(1))
        if not 1 <= count <= MAX_PROP_EACH:
            raise SystemExit(f"--find propsN must be from 1 to {MAX_PROP_EACH}")
        return "props", count
    match = re.fullmatch(r"top(\d+)", value)
    if match is None:
        raise SystemExit("--find must be 'topN' or 'propsN', for example --find top10 or --find props5")
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


def moneyline_tokens(games):
    """Away-team token for each completed game winner. That is the charted price."""
    rows = []
    for _, row in games.iterrows():
        asset_id, _first = token_for(row, row["away"])
        rows.append(
            {
                "slug": row["slug"],
                "condition_id": row["condition_id"],
                "asset_id": asset_id,
                "tip": row["tip"],
            }
        )
    return pd.DataFrame(rows)


def balance_ranks(frame):
    """Keep rows with a book and matched volume of at least MIN_VOLUME_CLOB.

    `balance` is the lower of the two percentile ranks inside that set.
    Sorting by it puts a market first only when both numbers are high.
    A huge posted size cannot rescue a volume of 129, and a huge
    volume_clob cannot rescue a book that is barely there.
    """
    if frame is None or frame.empty:
        return frame.iloc[0:0].copy() if frame is not None else frame
    ranked = frame.copy()
    ranked["volume_clob"] = pd.to_numeric(ranked["volume_clob"], errors="coerce").fillna(0.0)
    ranked["book_size"] = pd.to_numeric(ranked["book_size"], errors="coerce").fillna(0.0)
    ranked["book_updates"] = pd.to_numeric(ranked["book_updates"], errors="coerce").fillna(0.0)
    ranked = ranked[(ranked["book_updates"] > 0) & (ranked["volume_clob"] >= MIN_VOLUME_CLOB)]
    if ranked.empty:
        return ranked
    ranked["volume_pct"] = ranked["volume_clob"].rank(method="average", pct=True)
    ranked["book_pct"] = ranked["book_size"].rank(method="average", pct=True)
    ranked["balance"] = ranked[["volume_pct", "book_pct"]].min(axis=1)
    return ranked.sort_values(
        ["balance", "volume_clob", "book_size"],
        ascending=[False, False, False],
    )


def rank_game_winners(games, activity, count, side_markets):
    """Keep game winners that are high on both matched volume and posted size."""
    if games is None or games.empty or activity is None or activity.empty:
        return []
    tokens = moneyline_tokens(games)
    ranked = tokens.merge(activity, on=["condition_id", "asset_id"], how="inner")
    volumes = games[["slug", "volume_clob"]].drop_duplicates("slug")
    ranked = ranked.merge(volumes, on="slug", how="left")
    with_book = ranked[ranked["book_updates"] > 0]
    print(f"{len(with_book)} game winners appeared in the order book.", flush=True)
    ranked = balance_ranks(with_book)
    print(
        f"{len(ranked)} of them have matched volume of at least {MIN_VOLUME_CLOB:,.0f}.",
        flush=True,
    )
    if ranked.empty:
        return []
    chosen = ranked.head(count)
    by_slug = games.set_index("slug", drop=False)
    selected = []
    for _, hit in chosen.iterrows():
        row = by_slug.loc[hit["slug"]]
        if isinstance(row, pd.DataFrame):
            row = row.iloc[0]
        row = row.copy()
        row["book_size"] = float(hit["book_size"])
        row["book_updates"] = int(hit["book_updates"])
        row["balance"] = float(hit["balance"])
        asset_id, first = token_for(row, row["away"])
        name = str(list(row["outcomes"])[0])
        spreads, total = side_markets_for(side_markets, row["slug"], name)
        rank = len(selected) + 1
        selected.append(pack_game(rank, name, row["away"], "", row, asset_id, first, spreads, total))
        stored = 0 if pd.isna(row["volume_clob"]) else float(row["volume_clob"])
        print(
            f"  kept {rank}/{count} {row['slug']}  posted_size={float(hit['book_size']):,.0f}  "
            f"updates={int(hit['book_updates']):,}  volume_clob={stored:,.0f}",
            flush=True,
        )
    if len(selected) < count:
        print(
            f"Found {len(selected)} game winners with both a book and matched volume of at least {MIN_VOLUME_CLOB:,.0f}.",
            flush=True,
        )
    return selected


def select_volume(duck, games, count, side_markets, activity=None):
    """Game winners that are high on both matched volume and posted size.

    `duck` is unused. The search opens its own connection and reads each
    distinct hour once. Markets that never appear are left out, including
    ones with a large stored volume_clob. Markets under MIN_VOLUME_CLOB
    are left out even when the book is busy.
    """
    del duck
    if activity is None:
        activity = discover_activity(moneyline_tokens(games))
    print(
        f"Keeping up to {count} game winners with matched volume of at least {MIN_VOLUME_CLOB:,.0f} "
        "and a high posted size.",
        flush=True,
    )
    return rank_game_winners(games, activity, count, side_markets)


def select_games(duck, games, side_markets, find_text):
    """Choose NBA game winners for this run.

    find_text is 'topN'. Returns the selected rows and a short chart subtitle.
    """
    kind, count = parse_find(find_text)
    if kind != "volume":
        raise SystemExit("Player over/under markets use --find props, for example --find props5.")
    selected = select_volume(duck, games, count, side_markets)
    if not selected:
        raise SystemExit(
            "No game winner had both an order book and matched volume of at least "
            f"{MIN_VOLUME_CLOB:,.0f}. Nothing was charted."
        )
    return selected, f"top {len(selected)} NBA games by matched volume and posted size"


def parse_stats(text):
    """Keep --stats inside the slug families this script knows."""
    stats = [part.strip().lower() for part in str(text or "").split(",") if part.strip()]
    if not stats:
        raise SystemExit("--stats needs at least one of " + ", ".join(PROP_STATS))
    unknown = [stat for stat in stats if stat not in PROP_STATS]
    if unknown:
        raise SystemExit(f"Unknown stat {unknown[0]}. Choose from {', '.join(PROP_STATS)}.")
    chosen = []
    for stat in stats:
        if stat not in chosen:
            chosen.append(stat)
    return chosen


def resolve_each(find_count, each_flag):
    """How many markets to keep for each stat."""
    if each_flag is not None:
        count = each_flag
    elif find_count is None:
        count = DEFAULT_PROP_EACH
    else:
        count = find_count
    if not 1 <= count <= MAX_PROP_EACH:
        raise SystemExit(f"--each must be from 1 to {MAX_PROP_EACH}")
    return count


def parse_prop_slug(slug):
    match = PROP_RE.fullmatch(str(slug))
    if match is None:
        return None
    away, home, day, stat, player_slug, whole, frac = match.groups()
    return {
        "away": away,
        "home": home,
        "game_day": day,
        "stat": stat,
        "player_slug": player_slug,
        "line": float(f"{int(whole)}.{frac}"),
        "game_slug": f"nba-{away}-{home}-{day}",
    }


def player_name(title, player_slug):
    head = str(title).split(":", 1)[0].strip()
    if head:
        return head
    return str(player_slug).replace("-", " ")


def annotate_props(frame):
    if frame is None or frame.empty:
        return frame
    parsed = frame["slug"].apply(parse_prop_slug)
    keep = parsed.notna()
    frame = frame.loc[keep].copy()
    if frame.empty:
        return frame
    details = pd.DataFrame(parsed.loc[keep].tolist(), index=frame.index)
    frame = pd.concat([frame, details], axis=1)
    frame["player"] = [
        player_name(title, slug) for title, slug in zip(frame["title"], frame["player_slug"])
    ]
    return frame


def load_props(pg, stats):
    stat_re = "|".join(stats)
    frame = pd.read_sql(
        f"""
        SELECT m.slug, m.title, m.outcomes, m.token_ids, m.condition_id,
               m.end_date AS tip, m.volume_clob, m.event_slug, r.resolved_yes
        FROM markets m
        LEFT JOIN resolutions r ON r.market_slug = m.slug
        WHERE m.slug ~ '^nba-[a-z]{{3}}-[a-z]{{3}}-[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}-({stat_re})-'
          AND m.slug NOT LIKE '%%1h%%'
          AND m.end_date < now()
        """,
        pg,
    )
    frame["volume_clob"] = frame["volume_clob"].astype(float)
    return annotate_props(frame)


def yes_token(row):
    outcomes = list(row["outcomes"])
    tokens = list(row["token_ids"])
    for index, name in enumerate(outcomes):
        if str(name).strip().lower() == "yes":
            return tokens[index], index == 0
    return tokens[0], True


def other_lines(props, row, limit=MAX_SPREADS - 1):
    """Other posted numbers for the same player, stat, and game."""
    mine = props[
        (props["game_slug"] == row["game_slug"])
        & (props["stat"] == row["stat"])
        & (props["player_slug"] == row["player_slug"])
        & (props["slug"] != row["slug"])
    ].sort_values("volume_clob", ascending=False)
    records = []
    for _, alt in mine.head(limit).iterrows():
        asset_id, _first = yes_token(alt)
        volume = alt["volume_clob"]
        records.append(
            {
                "slug": alt["slug"],
                "title": alt["title"],
                "condition_id": alt["condition_id"],
                "asset_id": asset_id,
                "volume_clob": None if pd.isna(volume) else float(volume),
                "line": float(alt["line"]),
                "kind": alt["stat"],
            }
        )
    return records


def prop_short_name(player, stat, line):
    last = str(player).split()[-1]
    return f"{last} {line:g} {STAT_SHORT[stat]}"


def pack_prop(rank, row, asset_id, first, alts):
    volume = row["volume_clob"]
    game = {
        "slug": row["slug"],
        "game_slug": row["game_slug"],
        "title": row["title"],
        "away": row["away"],
        "home": row["home"],
        "tip": row["tip"],
        "condition_id": row["condition_id"],
        "outcomes": row["outcomes"],
        "volume_clob": None if pd.isna(volume) else float(volume),
        "resolved_yes": row["resolved_yes"],
        **book_fields(row),
    }
    return {
        "kind": "prop",
        "rank": rank,
        "name": prop_short_name(row["player"], row["stat"], row["line"]),
        "code": row["stat"],
        "record": "",
        "game": game,
        "asset_id": asset_id,
        "first": first,
        "spreads": alts,
        "total": None,
        "stat": row["stat"],
        "player": row["player"],
        "player_slug": row["player_slug"],
        "line": float(row["line"]),
    }


def prop_tokens(props):
    rows = []
    for _, row in props.iterrows():
        asset_id, first = yes_token(row)
        rows.append(
            {
                "slug": row["slug"],
                "condition_id": row["condition_id"],
                "asset_id": asset_id,
                "first": first,
                "tip": row["tip"],
            }
        )
    return pd.DataFrame(rows)


def rank_props(props, activity, each, stats):
    """For each stat, keep lines that are high on both matched volume and posted size.

    When one player has several numbers in the same game, the charted line is
    the one with the higher balance score. A line under MIN_VOLUME_CLOB is
    left out even if its book is busy.
    """
    if props is None or props.empty or activity is None or activity.empty:
        return []
    tokens = prop_tokens(props)
    ranked = tokens.merge(activity, on=["condition_id", "asset_id"], how="inner")
    with_book = ranked[ranked["book_updates"] > 0]
    if with_book.empty:
        return []
    details = props.drop(columns=[column for column in ("condition_id", "asset_id", "tip") if column in props.columns])
    ranked = with_book.merge(details, on="slug", how="left")
    print(f"{len(ranked)} prop lines appeared in the order book.", flush=True)
    ranked = balance_ranks(ranked)
    print(
        f"{len(ranked)} of them have matched volume of at least {MIN_VOLUME_CLOB:,.0f}.",
        flush=True,
    )
    if ranked.empty:
        return []
    ranked = ranked.drop_duplicates(["stat", "game_slug", "player_slug"], keep="first")
    selected = []
    for stat in stats:
        part = ranked[ranked["stat"] == stat]
        print(
            f"{stat}: {len(part)} players cleared both bars. Keeping {min(each, len(part))}.",
            flush=True,
        )
        kept = 0
        for _, row in part.head(each).iterrows():
            kept += 1
            rank = len(selected) + 1
            selected.append(pack_prop(rank, row, row["asset_id"], bool(row["first"]), other_lines(props, row)))
            stored = 0 if pd.isna(row["volume_clob"]) else float(row["volume_clob"])
            print(
                f"  kept {kept}/{each} {row['player']} {stat} {row['line']:g}  "
                f"posted_size={float(row['book_size']):,.0f}  volume_clob={stored:,.0f}",
                flush=True,
            )
        if kept < each:
            print(
                f"Found {kept} {stat} markets with both a book and matched volume of at least {MIN_VOLUME_CLOB:,.0f}.",
                flush=True,
            )
    return selected


def select_props(duck, props, each, stats, activity=None):
    """Keep `each` markets per stat that are high on both matched volume and posted size."""
    del duck
    if props is None or props.empty:
        return [], f"{each} each of {', '.join(stats)}"
    wanted = props[props["stat"].isin(stats)] if "stat" in props.columns else props
    if activity is None:
        activity = discover_activity(prop_tokens(wanted))
    selected = rank_props(props, activity, each, stats)
    subtitle = f"{each} each of {', '.join(stats)}"
    return selected, subtitle


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


def first_mid(frame):
    """Earliest saved mid. Used when a book has no quote before tip."""
    if frame is None or frame.empty or "mid" not in frame:
        return None
    sample = frame.dropna(subset=["mid"])
    if sample.empty:
        return None
    return float(sample.iloc[0]["mid"])


def shown_mid(frame):
    """Pregame mid when the book was quoted before tip, otherwise the first saved mid."""
    before = last_before_tip(frame)
    if before is not None:
        return before
    return first_mid(frame)


def chart_prices(summary):
    """Price to plot: the pregame mid, or the first saved mid when that is blank."""
    frame = summary.copy()
    pregame = pd.to_numeric(frame["pregame_price"], errors="coerce") if "pregame_price" in frame else pd.Series(pd.NA, index=frame.index)
    first = pd.to_numeric(frame["first_price"], errors="coerce") if "first_price" in frame else pd.Series(pd.NA, index=frame.index)
    frame["chart_price"] = pregame.where(pregame.notna(), first)
    return frame


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


def normalize_person(name):
    text = str(name).lower().replace("'", "").replace("’", "").replace(".", "")
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text


def player_lines(payload):
    """Box-score rows keyed by a normalized player name."""
    found = {}
    box = (payload or {}).get("boxscore") or {}
    for team in box.get("players") or []:
        groups = team.get("statistics") or []
        if not groups:
            continue
        group = groups[0]
        labels = group.get("labels") or []
        for athlete in group.get("athletes") or []:
            person = (athlete.get("athlete") or {}).get("displayName")
            if not person:
                continue
            found[normalize_person(person)] = dict(zip(labels, athlete.get("stats") or []))
    return found


def load_event(event_id, cache):
    if event_id in cache:
        return cache[event_id]
    empty = pd.DataFrame(columns=["wallclock", "home_score", "away_score", "text", "score_value", "scoring_play"])
    packed = {"plays": empty, "players": {}}
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
                    "text": play.get("text") or "",
                    "score_value": int(play.get("scoreValue") or 0),
                    "scoring_play": bool(play.get("scoringPlay")),
                }
            )
        frame = pd.DataFrame(rows)
        if frame.empty:
            frame = empty
        else:
            frame["wallclock"] = pd.to_datetime(frame["wallclock"], utc=True)
            frame = frame.sort_values("wallclock")
        packed = {"plays": frame, "players": player_lines(payload)}
    except Exception as error:
        print(f"  plays {event_id}: {error.__class__.__name__}", flush=True)
    cache[event_id] = packed
    return packed


def plays_frame(event_id, cache):
    return load_event(event_id, cache)["plays"]


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
    slug = game["game_slug"] if "game_slug" in game and pd.notna(game["game_slug"]) else game["slug"]
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
        packed = load_event(event["id"], play_cache)
        result = {
            "event_id": event["id"],
            "home_score": int(home.get("score") or 0),
            "away_score": int(away.get("score") or 0),
            "home_quarters": quarter_list(home),
            "away_quarters": quarter_list(away),
            "plays": packed["plays"],
            "players": packed["players"],
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
    extra = [column for column in plays.columns if column in merged.columns]
    return merged.drop(columns=extra, errors="ignore")


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
    total = frame["home_score"] + frame["away_score"]
    delta = total.diff()
    delta.iloc[0] = total.iloc[0]
    times = frame.loc[delta != 0, ["wallclock"]]
    return times if not times.empty else empty


def text_starts_with_player(text, player_slug):
    head = re.split(r"\s+makes\b", text, maxsplit=1, flags=re.I)[0]
    return normalize_person(head) == player_slug


def stat_gain(text, score_value, scoring_play, player_slug, stat):
    """Points, assists, rebounds, or the other box stat this play added for one player."""
    raw = " ".join(str(text or "").split())
    player_slug = normalize_person(player_slug)
    try:
        points = int(float(score_value or 0))
    except (TypeError, ValueError):
        points = 0
    if stat == "points":
        if not scoring_play or points <= 0:
            return 0
        return points if text_starts_with_player(raw, player_slug) else 0
    if stat == "threes":
        if not scoring_play or points != 3:
            return 0
        return 1 if text_starts_with_player(raw, player_slug) else 0
    if stat == "assists":
        match = re.search(r"\(([^)]+?)\s+assists\)", raw, flags=re.I)
        return 1 if match and normalize_person(match.group(1)) == player_slug else 0
    if stat == "steals":
        match = re.search(r"\(([^)]+?)\s+steals\)", raw, flags=re.I)
        return 1 if match and normalize_person(match.group(1)) == player_slug else 0
    if stat == "rebounds":
        if "team rebound" in raw.lower():
            return 0
        match = re.match(r"(.+?)\s+(?:offensive|defensive)\s+rebound\b", raw, flags=re.I)
        return 1 if match and normalize_person(match.group(1)) == player_slug else 0
    if stat == "blocks":
        match = re.match(r"(.+?)\s+blocks\s+", raw, flags=re.I)
        return 1 if match and normalize_person(match.group(1)) == player_slug else 0
    return 0


def stat_ticks(plays, player_slug, stat):
    """Running total each time this player's stat increases."""
    empty = pd.DataFrame(columns=["wallclock", "stat_total"])
    if plays is None or plays.empty or "text" not in plays.columns:
        return empty
    total = 0
    rows = []
    ordered = plays.sort_values("wallclock")
    for text, score_value, scoring, wall in zip(
        ordered["text"], ordered["score_value"], ordered["scoring_play"], ordered["wallclock"]
    ):
        gained = stat_gain(text, score_value, bool(scoring), player_slug, stat)
        if not gained:
            continue
        total += gained
        rows.append({"wallclock": wall, "stat_total": total})
    return pd.DataFrame(rows) if rows else empty


def ticks_as_plays(ticks):
    """Feed stat increases to the same 'led the next event' clock as baskets."""
    columns = ["wallclock", "home_score", "away_score"]
    if ticks is None or ticks.empty:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(
        {
            "wallclock": list(ticks["wallclock"]),
            "home_score": list(range(1, len(ticks) + 1)),
            "away_score": 0,
        }
    )


def stat_number(raw, stat):
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    text = str(raw).strip()
    if text == "" or text.lower() == "none":
        return None
    if stat == "threes":
        text = text.split("-", 1)[0]
    try:
        return int(float(text))
    except ValueError:
        return None


def final_stat(players, player_slug, stat):
    row = (players or {}).get(normalize_person(player_slug))
    if not row:
        return None
    return stat_number(row.get(BOX_KEY[stat]), stat)


def prop_final(result, player_slug, stat, ticks):
    if result is not None:
        value = final_stat(result.get("players") or {}, player_slug, stat)
        if value is not None:
            return value
    if ticks is not None and not ticks.empty:
        return int(ticks.iloc[-1]["stat_total"])
    return None


def prop_bucket(stat, edge):
    if edge is None or pd.isna(edge):
        return None
    gap = abs(float(edge))
    close, blow = (2, 6) if stat == "points" else (1, 3)
    if gap <= close:
        return "close"
    if gap >= blow:
        return "blowout"
    return "competitive"


def attach_game_clock(path, plays):
    """Home score, away score, and their sum on each in-game bin.

    A prop chart has no 'charted team', so the game total is what lines up
    with the Over price when the player's own running stat is missing.
    """
    if path is None or path.empty or plays is None or plays.empty:
        path = path.copy() if path is not None else pd.DataFrame()
        path["home_score"] = pd.NA
        path["away_score"] = pd.NA
        path["game_points"] = pd.NA
        return path
    scores = plays.sort_values("wallclock")[["wallclock", "home_score", "away_score"]]
    merged = pd.merge_asof(
        path.sort_values("bin"),
        scores,
        left_on="bin",
        right_on="wallclock",
        direction="backward",
    )
    live = merged["hours_from_tip"] >= 0
    merged.loc[~live, ["home_score", "away_score"]] = pd.NA
    merged["game_points"] = pd.NA
    both = live & merged["home_score"].notna() & merged["away_score"].notna()
    merged.loc[both, "game_points"] = (
        pd.to_numeric(merged.loc[both, "home_score"], errors="coerce")
        + pd.to_numeric(merged.loc[both, "away_score"], errors="coerce")
    )
    return merged.drop(columns=["wallclock"], errors="ignore")


def attach_stat(path, ticks, line):
    path = path.copy()
    path["stat_value"] = pd.NA
    path["prop_line"] = line
    if path.empty or ticks is None or ticks.empty:
        return path
    merged = pd.merge_asof(
        path.sort_values("bin"),
        ticks.sort_values("wallclock"),
        left_on="bin",
        right_on="wallclock",
        direction="backward",
    )
    live = merged["hours_from_tip"] >= 0
    merged.loc[live, "stat_value"] = merged.loc[live, "stat_total"]
    merged.loc[live & merged["stat_value"].isna(), "stat_value"] = 0
    return merged.drop(columns=["wallclock", "stat_total"], errors="ignore")


def classify_orders(orders, path, plays, tip):
    """Mark large sizes whose price then moved, and which of those led the next event.

    There is no wallet on these rows. A size that is removed cannot be told from a
    cancel, so only posted sizes are kept. 'Led a score' means the previous event
    was at least 15 seconds earlier and the next event came within two minutes.
    For a game winner the event is any scoring play. For a player prop it is the
    next time that player's stat went up.
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


def download_book(selected):
    """Read each needed hour once. The focus flag marks the charted token."""
    duck = connect_duckdb()
    needed = {}
    for item in selected:
        if item.get("game") is None:
            continue
        game = item["game"]
        pairs = [(game["condition_id"], item["asset_id"], True)]
        for market in item.get("spreads") or []:
            pairs.append((market["condition_id"], market["asset_id"], False))
        if item.get("total"):
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
    return book, large_book


def set_panels(selected):
    return [
        {"rank": item["rank"], "name": item["name"], "code": item["code"], "record": item.get("record") or ""}
        for item in selected
    ]


def analyze(find_text="top10"):
    global ACTIVE_PANELS, CHART_SUBJECT, CHART_KIND
    CHART_KIND = "game"
    OUTPUT.mkdir(parents=True, exist_ok=True)
    pg = connect_postgres()
    games = load_games(pg)
    side_markets = load_side_markets(pg)
    pg.close()
    duck = connect_duckdb()
    try:
        selected, CHART_SUBJECT = select_games(duck, games, side_markets, find_text)
    finally:
        duck.close()
    ACTIVE_PANELS = set_panels(selected)
    book, large_book = download_book(selected)

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
            "note": "This market had no order-book rows in the hours around its listed tip.",
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
                "first_price": first_mid(path),
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
                **book_fields(game),
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

    finish_run(summaries, paths, alternates, results, order_frames, [
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
        "book_size",
        "book_updates",
        "volume_clob",
        "balance",
        "won",
    ])


def analyze_props(each, stats):
    """Same charts for player over/under markets, `each` per stat."""
    global ACTIVE_PANELS, CHART_SUBJECT, CHART_KIND
    CHART_KIND = "prop"
    OUTPUT.mkdir(parents=True, exist_ok=True)
    pg = connect_postgres()
    props = load_props(pg, stats)
    pg.close()
    duck = connect_duckdb()
    selected, CHART_SUBJECT = select_props(duck, props, each, stats)
    duck.close()
    if not selected:
        raise SystemExit(
            "No prop line had both an order book and matched volume of at least "
            f"{MIN_VOLUME_CLOB:,.0f}. Nothing was charted."
        )
    ACTIVE_PANELS = set_panels(selected)
    book, large_book = download_book(selected)

    summaries = []
    paths = []
    alternates = []
    order_frames = []
    results = []
    score_cache = {}
    boards = {}
    play_cache = {}
    for item in selected:
        game = item["game"]
        tip = game["tip"]
        path = quote_path(
            book,
            {"condition_id": game["condition_id"], "asset_id": item["asset_id"]},
            tip,
        )
        alt = item["spreads"][0] if item["spreads"] else None
        alt_path = quote_path(book, alt, tip)
        result = fetch_result(game, score_cache, boards, play_cache)
        plays = None if result is None else result["plays"]
        ticks = stat_ticks(plays, item["player_slug"], item["stat"])
        path = attach_stat(path, ticks, item["line"])
        path = attach_game_clock(path, plays)
        if not path.empty and not alt_path.empty:
            path = path.merge(
                alt_path[["bin", "mid"]].rename(columns={"mid": "spread_mid"}),
                on="bin",
                how="left",
            )
        elif not path.empty:
            path["spread_mid"] = pd.NA
        final = prop_final(result, item["player_slug"], item["stat"], ticks)
        edge = None if final is None else final - item["line"]
        if result is None:
            print(f"  no ESPN score for {game['game_slug']}", flush=True)
        else:
            shown = "n/a" if final is None else f"{final:g}"
            print(
                f"  ESPN {game['away']} at {game['home']} {item['player']} {item['stat']} {shown} "
                f"(line {item['line']:g})",
                flush=True,
            )
        resolved = game["resolved_yes"]
        if pd.notna(resolved):
            won = bool(resolved) if item["first"] else (not bool(resolved))
        elif final is None or final == item["line"]:
            won = None
        else:
            won = final > item["line"]
        pre_std, pre_n = window_std(path, -HOURS_BEFORE, 0)
        live_std, live_n = window_std(path, 0, IN_GAME_HOURS)
        label = prop_short_name(item["player"], item["stat"], item["line"])
        if final is not None:
            label = f"{label}  final {final:g}"
        summary = {
            "rank": item["rank"],
            "team": item["name"],
            "player": item["player"],
            "code": item["code"],
            "stat": item["stat"],
            "line": item["line"],
            "record": "",
            "game": game["slug"],
            "opponent": f"{game['away']} at {game['home']}",
            "tip_utc": tip,
            "pregame_std": pre_std,
            "in_game_std": live_std,
            "pregame_n": pre_n,
            "in_game_n": live_n,
            "pregame_price": last_before_tip(path),
            "first_price": first_mid(path),
            "won": won,
            "variance_score_pregame": window_mean(path, "variance_score", -HOURS_BEFORE, 0),
            "variance_score_early": window_mean(path, "variance_score", 0, IN_GAME_HOURS / 2),
            "variance_score_late": window_mean(path, "variance_score", IN_GAME_HOURS / 2, IN_GAME_HOURS),
            "variance_score_live": window_mean(path, "variance_score", 0, IN_GAME_HOURS),
            "minutes_to_decided": minutes_until_decided(path),
            "bid_ask_pregame": window_mean(path, "spread_width", -1, 0),
            "bid_ask_live": window_mean(path, "spread_width", 0, IN_GAME_HOURS),
            "final_stat": final,
            "espn_margin": edge,
            "game_bucket": prop_bucket(item["stat"], edge),
            **book_fields(game),
            "volume_clob": game["volume_clob"],
            "score_source": None if result is None else f"espn:{result['event_id']}",
            "large_n": 0,
            "followed_n": 0,
            "led_score_n": 0,
            "note": "" if result is not None else "Order book found. ESPN had no matching game.",
        }
        summaries.append(summary)
        results.append(summary)
        chosen = {
            "slug": game["slug"],
            "title": game["title"],
            "condition_id": game["condition_id"],
            "asset_id": item["asset_id"],
            "volume_clob": game["volume_clob"],
            "line": item["line"],
        }
        for market in [chosen, *item["spreads"]]:
            same = market["condition_id"] == game["condition_id"]
            series = path if same else quote_path(book, market, tip)
            alternates.append(
                {
                    "rank": item["rank"],
                    "team": item["name"],
                    "stat": item["stat"],
                    "line": market["line"],
                    "pregame_price": last_before_tip(series),
                    "shown_price": shown_mid(series),
                    "title": market["title"],
                    "volume_clob": market["volume_clob"],
                }
            )
        if not path.empty:
            plot_path = path.copy()
            plot_path["team"] = item["name"]
            plot_path["rank"] = item["rank"]
            plot_path["label"] = label
            plot_path["bucket"] = summary["game_bucket"]
            plot_path["final_stat"] = final
            paths.append(plot_path)
        if not large_book.empty:
            mine_orders = large_book[
                (large_book["market"] == game["condition_id"]) & (large_book["asset_id"] == item["asset_id"])
            ]
            tagged = classify_orders(mine_orders, path, ticks_as_plays(ticks), tip)
            if not tagged.empty:
                tagged.insert(0, "rank", item["rank"])
                tagged.insert(1, "team", item["name"])
                tagged.insert(2, "label", label)
                order_frames.append(tagged)
                summary["large_n"] = int(len(tagged))
                summary["followed_n"] = int(tagged["price_followed"].sum())
                summary["led_score_n"] = int(tagged["stands_out"].sum())

    finish_run(summaries, paths, alternates, results, order_frames, [
        "rank",
        "team",
        "stat",
        "line",
        "final_stat",
        "espn_margin",
        "game_bucket",
        "variance_score_live",
        "large_n",
        "followed_n",
        "led_score_n",
        "book_size",
        "book_updates",
        "volume_clob",
        "balance",
        "won",
    ])


def column_meaning(filename, column, kind):
    """Plain-language meaning of one output column. Game and prop runs differ."""
    specific = PROP_COLUMN_DOCS if kind == "prop" else GAME_COLUMN_DOCS
    for key in ((filename, column), ("summary.csv", column) if filename == "game_results.csv" else None, ("*", column)):
        if key is None:
            continue
        if key in specific:
            return specific[key]
        if key in SHARED_COLUMN_DOCS:
            return SHARED_COLUMN_DOCS[key]
    return "Saved with this run."


FILE_INTRO = {
    "summary.csv": "One row per panel.",
    "price_paths.csv": "One row per five-minute bin of the token drawn on the panel.",
    "alternate_spreads.csv": "One row per extra line read from the same hours.",
    "game_results.csv": "The summary rows for panels that had an order book. The columns match summary.csv.",
    "large_orders.csv": "One row per unusually large posted size on the charted token. There is no wallet.",
}


SHARED_COLUMN_DOCS = {
    ("*", "rank"): "Panel number, starting at 1. The same number joins a row to the other CSVs.",
    ("*", "team"): "Name printed on the panel.",
    ("*", "label"): "Title drawn on the panel.",
    ("*", "code"): "Three-letter team code from the slug.",
    ("*", "record"): "Blank. Panels are NBA markets ranked from the order book, not a team ranking.",
    ("*", "game"): "Market slug of the charted contract.",
    ("*", "opponent"): "The other team.",
    ("*", "opponent_code"): "Three-letter code of the other team.",
    ("*", "tip_utc"): "Listed tip, taken from markets.end_date. Stored in UTC.",
    ("*", "pregame_std"): "Standard deviation of the 5-minute mid over the 6 hours before tip. Blank when fewer than 3 bins exist.",
    ("*", "in_game_std"): "Standard deviation of the 5-minute mid over the first 2.5 hours after tip. Blank when fewer than 3 bins exist.",
    ("*", "pregame_n"): "How many 5-minute bins fall in the 6 hours before tip.",
    ("*", "in_game_n"): "How many 5-minute bins fall in the first 2.5 hours after tip.",
    ("*", "pregame_price"): "Last mid before the listed tip. Blank when the saved book starts after tip.",
    ("*", "first_price"): "Earliest mid in the saved window. Charts use this when pregame_price is blank.",
    ("*", "variance_score_pregame"): "Mean of the five-minute variance score over the 6 hours before tip.",
    ("*", "variance_score_early"): "Mean variance score from tip until 1.25 hours after tip.",
    ("*", "variance_score_late"): "Mean variance score from 1.25 hours after tip until 2.5 hours after tip.",
    ("*", "variance_score_live"): "Mean variance score over the first 2.5 hours after tip.",
    ("*", "minutes_to_decided"): "Minutes after tip when the mid first reaches 0.90 or 0.10. Blank if that never happens in the saved window.",
    ("*", "bid_ask_pregame"): "Mean of ask minus bid over the hour before tip.",
    ("*", "bid_ask_live"): "Mean of ask minus bid over the first 2.5 hours after tip.",
    ("*", "large_n"): "Posted sizes at least 10 times the median size on that token and side in that hour, after repeats within 10 seconds are collapsed.",
    ("*", "followed_n"): "How many of those large sizes were followed by a mid move of at least 0.03 in the same direction over the next 30 minutes. A buy has to move the mid up. A sell has to move it down.",
    ("*", "led_score_n"): "How many large sizes both followed the price and arrived at least 15 seconds after the previous event and within 2 minutes before the next one.",
    ("*", "book_size"): "Sum of posted size (size greater than 0) for the charted token in the hour before tip, the tip hour, and the hour after. Posted size is the offer traffic in the book. It is not matched trade volume.",
    ("*", "book_updates"): "How many of those posted-size rows the charted token had in those three hours.",
    ("*", "balance"): "Lower of this market's matched-volume percentile and its posted-size percentile, among NBA markets that have an order book and volume_clob of at least 1,000. Panels are sorted by this, highest first, so both numbers have to be high.",
    ("*", "volume_clob"): "Stored lifetime matched CLOB volume of this contract, in dollars. It is a snapshot from the markets table. A market under 1,000 is left out of the charts.",
    ("*", "score_source"): "ESPN event id, written as espn:<id>.",
    ("*", "note"): "Why a row has no price path or no ESPN result. Blank when both were found.",
    ("*", "bin"): "Start of the 5-minute window, in UTC.",
    ("*", "market"): "Condition id of the charted contract. This joins to markets.condition_id and to the Parquet market column.",
    ("*", "asset_id"): "Token id of the charted outcome.",
    ("*", "best_bid"): "Highest buy price with size still posted in that 5-minute window.",
    ("*", "best_ask"): "Lowest sell price with size still posted in that 5-minute window.",
    ("*", "mid"): "Average of best bid and best ask. When only one side is present, that side's price.",
    ("*", "hours_from_tip"): "Hours after the listed tip. Negative values are before tip.",
    ("*", "variance_score"): "Standard deviation of the mid over the trailing 30 minutes, which is 6 bins. Blank until 3 bins exist.",
    ("*", "spread_width"): "Best ask minus best bid in that bin.",
    ("*", "bucket"): "close, competitive, or blowout. The cutoffs are the same as game_bucket.",
    ("*", "title"): "Market title from the markets table.",
    ("*", "time"): "UTC time of the book update.",
    ("*", "side"): "BUY is a posted bid. SELL is a posted ask. This is a resting size, not a matched trade, and it has no wallet.",
    ("*", "price"): "Price of that posted size, on the 0 to 1 contract scale.",
    ("*", "size"): "Posted size. A size of 0 means the level was removed, and those rows are left out because a cancel and a fill look the same.",
    ("*", "med_size"): "Median posted size on this token and side in that clock hour.",
    ("*", "size_ratio"): "size divided by med_size. A row is kept when this is at least 10 and that side had at least 20 updates in the hour.",
    ("*", "mid_at"): "Latest 5-minute mid at or before this update.",
    ("*", "mid_later"): "Next 5-minute mid at least 30 minutes after this update.",
    ("*", "price_followed"): "True when mid_later is at least 0.03 further in the order's direction than mid_at.",
    ("*", "stands_out"): "True when price_followed and led_score are both true. These are the dark marks on large_orders.png.",
}


GAME_COLUMN_DOCS = {
    ("summary.csv", "won"): "True when the charted team won on Polymarket. resolutions.resolved_yes is true when the first outcome won, and the away team is that first outcome. Blank when the market has no resolution row.",
    ("summary.csv", "spread"): "Title of the highest-volume full-game spread in this event.",
    ("summary.csv", "spread_line"): "Point number of that spread from the charted team's side. Negative means that team is laying the points.",
    ("summary.csv", "spread_pregame"): "Last mid before tip on the charted team's side of that spread.",
    ("summary.csv", "espn_team_score"): "Charted team's final score from ESPN.",
    ("summary.csv", "espn_opponent_score"): "Opponent's final score from ESPN.",
    ("summary.csv", "espn_margin"): "Charted team's score minus the opponent's score.",
    ("summary.csv", "game_bucket"): "close when the absolute margin is 5 or less, blowout when it is 15 or more, otherwise competitive. Blank when ESPN has no score.",
    ("summary.csv", "quarters"): "Quarter scores with the charted team first, such as Q1:28-25.",
    ("summary.csv", "final_points"): "Both teams' final scores added together.",
    ("summary.csv", "total_line"): "Posted number on the highest-volume full-game total.",
    ("summary.csv", "total_pregame"): "Last mid before tip of the Over on that total.",
    ("summary.csv", "total_hit"): "True when final_points is above total_line.",
    ("price_paths.csv", "score_margin"): "Charted team's score minus the opponent's score, using the latest ESPN play at or before this bin. Blank before tip.",
    ("price_paths.csv", "team_score"): "Charted team's score at this bin.",
    ("price_paths.csv", "opponent_score"): "Opponent's score at this bin.",
    ("price_paths.csv", "spread_mid"): "Mid of the highest-volume spread in this bin, from the charted team's side.",
    ("price_paths.csv", "total_mid"): "Mid of the Over on the highest-volume game total in this bin.",
    ("alternate_spreads.csv", "line"): "Spread number from the charted team's side. Negative means that team is laying the points.",
    ("alternate_spreads.csv", "pregame_price"): "Last mid before tip on that team's side of this spread.",
    ("large_orders.csv", "led_score"): "True when the previous scoring play was at least 15 seconds earlier, or there was no earlier scoring play, and the next scoring play came within 2 minutes.",
}


PROP_COLUMN_DOCS = {
    ("*", "team"): "Short panel label, such as Brunson 24.5 pts.",
    ("*", "code"): "Stat family: points, assists, rebounds, threes, steals, or blocks.",
    ("*", "record"): "Blank for a prop run.",
    ("*", "opponent"): "Matchup from the slug, written as the away team at the home team.",
    ("summary.csv", "player"): "Player name from the market title.",
    ("summary.csv", "stat"): "Same stat family as code.",
    ("summary.csv", "line"): "Posted over/under number. The charted price is Yes, which is the Over.",
    ("summary.csv", "won"): "True when Yes won. Yes is the Over. This is the Polymarket resolution when resolutions has a row. When it does not, true means the ESPN box score is above the line. Blank when neither source settles it, including an exact push.",
    ("summary.csv", "final_stat"): "ESPN box-score total for this stat. Threes are made three-pointers.",
    ("summary.csv", "espn_margin"): "final_stat minus the posted number. Positive means the player finished over.",
    ("summary.csv", "game_bucket"): "For points, close means the absolute gap is 2 or less and blowout means 6 or more. For assists, rebounds, threes, steals, and blocks, close is 1 or less and blowout is 3 or more.",
    ("price_paths.csv", "stat_value"): "Player's running stat from the ESPN play-by-play. After tip it stays 0 until the first recorded increase. Blank before tip, and blank for the whole game when the play text never names this player.",
    ("price_paths.csv", "prop_line"): "Posted over/under number, repeated on every bin. Drawn as the dashed line.",
    ("price_paths.csv", "final_stat"): "ESPN box-score total, repeated on every bin. Drawn as the dotted line.",
    ("price_paths.csv", "home_score"): "Home team's score at this bin. Blank before tip.",
    ("price_paths.csv", "away_score"): "Away team's score at this bin. Blank before tip.",
    ("price_paths.csv", "game_points"): "Home score plus away score. Drawn on price_vs_score.png when the player's running stat is missing.",
    ("price_paths.csv", "spread_mid"): "Mid of the next most traded line for the same player, stat, and game.",
    ("alternate_spreads.csv", "stat"): "Stat family of this line.",
    ("alternate_spreads.csv", "line"): "Posted over/under number.",
    ("alternate_spreads.csv", "pregame_price"): "Last Yes (Over) mid before tip on this line. Blank when this line has no quote before tip.",
    ("alternate_spreads.csv", "shown_price"): "The dot on alternate_spreads.png. Pregame mid when one exists, otherwise the first saved mid.",
    ("large_orders.csv", "led_score"): "True when the player's previous stat increase was at least 15 seconds earlier, or there was none, and the next increase came within 2 minutes.",
}


def write_data_dictionary(files):
    """Write DATA_DICTIONARY.md beside the CSVs. files is (name, columns) in write order."""
    kind_line = (
        "These CSVs are a player over/under run. The charted price is Yes, which is the Over."
        if CHART_KIND == "prop"
        else "These CSVs are a game-winner run. The charted price is the team named on the panel."
    )
    lines = [
        "# Output data dictionary",
        "",
        kind_line,
        "A blank cell means that value was missing.",
        "Tip is the listed start in markets.end_date, not the final buzzer.",
        "",
    ]
    present = {name for name, _columns in files}
    for name, columns in files:
        lines.append(f"## `{name}`")
        lines.append("")
        lines.append(FILE_INTRO.get(name, "Rows written by this run."))
        lines.append("")
        if not columns:
            lines.append("This file was written with no columns because this run had no rows.")
            lines.append("")
            continue
        lines.append("| Column | Meaning |")
        lines.append("| --- | --- |")
        for column in columns:
            meaning = column_meaning(name, column, CHART_KIND).replace("|", "/")
            lines.append(f"| `{column}` | {meaning} |")
        lines.append("")
    missing = []
    if "price_paths.csv" not in present:
        missing.append("`price_paths.csv` is absent because no panel had a price path.")
    if "alternate_spreads.csv" not in present:
        missing.append("`alternate_spreads.csv` is absent because no extra line was found.")
    if "large_orders.csv" not in present:
        missing.append("`large_orders.csv` is absent because no posted size was at least 10 times the median.")
    if missing:
        lines.append("## Files not written")
        lines.append("")
        lines.extend(missing)
        lines.append("")
    path = OUTPUT / "DATA_DICTIONARY.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def finish_run(summaries, paths, alternates, results, order_frames, columns):
    summary_frame = pd.DataFrame(summaries)
    alternate_frame = pd.DataFrame(alternates)
    result_frame = pd.DataFrame(results)
    orders = pd.concat(order_frames, ignore_index=True) if order_frames else pd.DataFrame()
    path_frame = pd.concat(paths, ignore_index=True) if paths else pd.DataFrame()
    summary_frame.to_csv(OUTPUT / "summary.csv", index=False)
    if not path_frame.empty:
        path_frame.to_csv(OUTPUT / "price_paths.csv", index=False)
    if not alternate_frame.empty:
        alternate_frame.to_csv(OUTPUT / "alternate_spreads.csv", index=False)
    result_frame.to_csv(OUTPUT / "game_results.csv", index=False)
    if not orders.empty:
        orders.to_csv(OUTPUT / "large_orders.csv", index=False)
    documented = [("summary.csv", list(summary_frame.columns))]
    if not path_frame.empty:
        documented.append(("price_paths.csv", list(path_frame.columns)))
    if not alternate_frame.empty:
        documented.append(("alternate_spreads.csv", list(alternate_frame.columns)))
    documented.append(("game_results.csv", list(result_frame.columns)))
    if not orders.empty:
        documented.append(("large_orders.csv", list(orders.columns)))
    write_data_dictionary(documented)
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
    show = [column for column in columns if column in summary_frame.columns]
    print(summary_frame[show].to_string(index=False))
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
    global ACTIVE_PANELS, CHART_SUBJECT, CHART_KIND
    summary_path = OUTPUT / "summary.csv"
    if not summary_path.exists():
        raise SystemExit(f"No saved table at {summary_path}. Run the script once without --plots-only.")
    summary = pd.read_csv(summary_path)
    ACTIVE_PANELS = panels_from_summary(summary)
    CHART_KIND = "prop" if "stat" in summary.columns and summary["stat"].notna().any() else "game"
    noun = "saved props" if CHART_KIND == "prop" else "saved games"
    CHART_SUBJECT = f"{len(ACTIVE_PANELS)} {noun}"
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
    documented = []
    for name in ("summary.csv", "price_paths.csv", "alternate_spreads.csv", "game_results.csv", "large_orders.csv"):
        path = OUTPUT / name
        if not path.exists():
            continue
        try:
            header = list(pd.read_csv(path, nrows=0).columns)
        except pd.errors.EmptyDataError:
            header = []
        documented.append((name, header))
    write_data_dictionary(documented)
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
    noun = "Over price" if CHART_KIND == "prop" else "Win price"
    finish_grid(fig, axes, count, cols, "Hours from listed tip", noun, f"{noun} around tip, {CHART_SUBJECT}", "price_paths.png")


def plot_variance(summary):
    frame = summary.copy()
    for column in ("pregame_std", "in_game_std"):
        if column not in frame:
            frame[column] = pd.NA
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["pregame_std", "in_game_std"], how="all")
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
    frame = chart_prices(summary).dropna(subset=["chart_price", "won"]).copy()
    fig, axis = plt.subplots(figsize=(6.5, 6))
    axis.plot([0, 1], [0, 1], color="#b0b0b0", linewidth=1)
    if frame.empty:
        axis.text(0.5, 0.5, "No resolved market with a saved price", ha="center")
    else:
        wins = frame[frame["won"] == True]  # noqa: E712
        losses = frame[frame["won"] == False]  # noqa: E712
        yes_label = "Over hit" if CHART_KIND == "prop" else "Team won"
        no_label = "Over missed" if CHART_KIND == "prop" else "Team lost"
        axis.scatter(wins["chart_price"], [1] * len(wins), color="#1f4e79", label=yes_label, zorder=3)
        axis.scatter(losses["chart_price"], [0] * len(losses), color="#d9762c", label=no_label, zorder=3)
        for _, row in frame.iterrows():
            axis.annotate(row["team"], (row["chart_price"], 1 if row["won"] else 0), textcoords="offset points", xytext=(4, 4), fontsize=8)
        axis.legend()
    axis.set_xlim(0, 1)
    axis.set_ylim(-0.1, 1.1)
    axis.set_xlabel("Last mid before tip, or the first saved mid")
    axis.set_ylabel("Over hit" if CHART_KIND == "prop" else "Game result")
    axis.set_title("Pregame price against the result")
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


def draw_empty(axis, panel, message="No quotes in the chart window"):
    axis.text(0.5, 0.5, message, ha="center", va="center", transform=axis.transAxes)
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
            "stat_value",
            "prop_line",
            "final_stat",
            "game_points",
            "home_score",
            "away_score",
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
        if frame is None:
            draw_empty(axis, panel)
            continue
        if "variance_score" not in frame or frame["variance_score"].dropna().empty:
            draw_empty(axis, panel, "Fewer than three five-minute quotes")
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
        empty = "No prop has both a variance score and a final stat" if CHART_KIND == "prop" else "No game has both a variance score and an ESPN score"
        axis.text(0.5, 0.5, empty, ha="center")
    else:
        frame["abs_margin"] = frame["espn_margin"].abs()
        for bucket, color in colors.items():
            part = frame[frame["game_bucket"] == bucket]
            axis.scatter(part["abs_margin"], part["variance_score_live"], color=color, label=bucket, zorder=3)
        for _, row in frame.iterrows():
            axis.annotate(row["team"], (abs(row["espn_margin"]), row["variance_score_live"]), textcoords="offset points", xytext=(4, 4), fontsize=8)
        axis.legend()
    if CHART_KIND == "prop":
        axis.set_xlabel("How far the final stat finished from the number")
        axis.set_title("Variance score against how close the stat finished")
    else:
        axis.set_xlabel("Absolute final margin")
        axis.set_title("Variance score against the final margin")
    axis.set_ylabel("Mean in-game variance score")
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
        price_label = "Over price" if CHART_KIND == "prop" else "Win price"
        axis.plot(frame["hours_from_tip"], frame["mid"], color="#1f4e79", linewidth=1.4, label=price_label)
        axis.axvline(0, color="#b0b0b0", linewidth=0.8)
        axis.set_ylim(0, 1)
        if CHART_KIND == "prop":
            twin = axis.twinx()
            stat = pd.to_numeric(frame["stat_value"], errors="coerce") if "stat_value" in frame else pd.Series(dtype=float)
            points = pd.to_numeric(frame["game_points"], errors="coerce") if "game_points" in frame else pd.Series(dtype=float)
            finals = pd.to_numeric(frame["final_stat"], errors="coerce") if "final_stat" in frame else pd.Series(dtype=float)
            posted = pd.to_numeric(frame["prop_line"], errors="coerce") if "prop_line" in frame else pd.Series(dtype=float)
            line = float(posted.dropna().iloc[0]) if posted.notna().any() else 0.0
            final = float(finals.dropna().iloc[0]) if finals.notna().any() else None
            if stat.notna().any():
                twin.plot(frame["hours_from_tip"], stat, color="#b04a4a", linewidth=1.2)
                series_top = float(stat.max())
                right_label = "Player stat"
            elif points.notna().any():
                twin.plot(frame["hours_from_tip"], points, color="#b04a4a", linewidth=1.2)
                series_top = float(points.max())
                right_label = "Game points"
            else:
                series_top = 0.0
                right_label = "Player stat"
            if line:
                twin.axhline(line, color="#b04a4a", linewidth=0.8, linestyle="--")
            if final is not None:
                twin.axhline(final, color="#2f7d4a", linewidth=0.8, linestyle=":")
            top = max(line, series_top, 0 if final is None else final) + 2
            twin.set_ylim(0, max(top, 2))
            if (index + 1) % cols == 0:
                twin.set_ylabel(right_label)
        else:
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
    if CHART_KIND == "prop":
        figure_title, ylabel = "Over price, the player stat, and the posted number", "Over price"
    else:
        figure_title, ylabel = "Win price and ESPN score margin", "Win price"
    finish_grid(fig, axes, count, cols, "Hours from listed tip", ylabel, figure_title, "price_vs_score.png")


def plot_market_paths(paths):
    fig, axes, count, cols = grid_for(sharey=True)
    by_rank = paths_by_rank(paths)
    labeled = False
    for axis, panel in zip(axes, ACTIVE_PANELS):
        frame = by_rank.get(panel["rank"])
        if frame is None:
            draw_empty(axis, panel)
            continue
        main_label = "Chosen line" if CHART_KIND == "prop" else "Win"
        next_label = "Next line" if CHART_KIND == "prop" else "Main spread"
        axis.plot(frame["hours_from_tip"], frame["mid"], color="#1f4e79", linewidth=1.4, label=main_label)
        if "spread_mid" in frame and frame["spread_mid"].notna().any():
            axis.plot(frame["hours_from_tip"], frame["spread_mid"], color="#d9762c", linewidth=1.2, label=next_label)
        if CHART_KIND != "prop" and "total_mid" in frame and frame["total_mid"].notna().any():
            axis.plot(frame["hours_from_tip"], frame["total_mid"], color="#2f7d4a", linewidth=1.2, label="Game total Over")
        axis.axvline(0, color="#b0b0b0", linewidth=0.8)
        axis.set_ylim(0, 1)
        axis.set_title(f"{panel['rank']}. {frame['label'].iloc[0]}", loc="left", fontsize=9)
        axis.grid(axis="y", color="#eeeeee")
        if not labeled:
            axis.legend(fontsize=8, loc="lower left")
            labeled = True
    if CHART_KIND == "prop":
        market_title = "Chosen over/under and the next most traded line"
    else:
        market_title = "Win price, main spread, and game total"
    finish_grid(fig, axes, count, cols, "Hours from listed tip", "Price", market_title, "market_paths.png")


def plot_bid_ask(paths):
    fig, axes, count, cols = grid_for(sharey=True)
    by_rank = paths_by_rank(paths)
    for axis, panel in zip(axes, ACTIVE_PANELS):
        frame = by_rank.get(panel["rank"])
        if frame is None:
            draw_empty(axis, panel)
            continue
        if "spread_width" not in frame or frame["spread_width"].dropna().empty:
            draw_empty(axis, panel, "No two-sided quote")
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
        price_column = "shown_price" if "shown_price" in frame.columns else "pregame_price"
        frame[price_column] = pd.to_numeric(frame[price_column], errors="coerce")
        if price_column == "shown_price" and "pregame_price" in frame:
            fallback = pd.to_numeric(frame["pregame_price"], errors="coerce")
            frame[price_column] = frame[price_column].where(frame[price_column].notna(), fallback)
        grouped = {int(rank): part.dropna(subset=["line", price_column]) for rank, part in frame.groupby("rank")}
    for axis, panel in zip(axes, ACTIVE_PANELS):
        part = grouped.get(panel["rank"])
        if part is None or part.empty:
            empty = "No priced line for this player" if CHART_KIND == "prop" else "No priced spread"
            draw_empty(axis, panel, empty)
            continue
        part = part.sort_values("line")
        axis.plot(part["line"], part[price_column], color="#1f4e79", marker="o")
        axis.set_ylim(0, 1)
        axis.axhline(0.5, color="#b0b0b0", linewidth=0.8)
        axis.set_title(panel_heading(panel), loc="left", fontsize=9)
        axis.grid(axis="y", color="#eeeeee")
    if CHART_KIND == "prop":
        x_label, y_label, alt_title = "Posted number", "Over price", "Other lines for the same player"
    else:
        x_label, y_label, alt_title = "Points, from that team's side", "Pregame cover price", "Alternate spreads before tip"
    finish_grid(fig, axes, count, cols, x_label, y_label, alt_title, "alternate_spreads.png")


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
                led_buy = "Led the next stat" if CHART_KIND == "prop" else "Led a score"
                led_sell = "Led the next stat, sell" if CHART_KIND == "prop" else "Led a score, sell"
                axis.scatter(loud.loc[buys, "hours_from_tip"], loud.loc[buys, "price"], marker="^", s=36, color="#1f4e79", label=led_buy if not labeled else None, zorder=3)
                axis.scatter(loud.loc[~buys, "hours_from_tip"], loud.loc[~buys, "price"], marker="v", s=36, color="#d9762c", label=led_sell if not labeled else None, zorder=3)
            labeled = True
        title = frame["label"].iloc[0] if "label" in frame else panel["name"]
        axis.set_title(f"{panel['rank']}. {title}", loc="left", fontsize=9)
        axis.grid(axis="y", color="#eeeeee")
    if legend_axis is not None:
        legend_axis.legend(fontsize=7, loc="lower left")
    finish_grid(
        fig, axes, count, cols,
        "Hours from listed tip", "Price",
        "Large orders. Dark marks led the next stat and the price followed."
        if CHART_KIND == "prop"
        else "Large orders. Dark marks led the next score and the price followed.",
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
        led_label = "Also led the next stat" if CHART_KIND == "prop" else "Also led the next score"
        axis.bar([p + width for p in positions], frame["led_score_n"], width=width, color="#1f4e79", label=led_label)
        axis.set_xticks(positions)
        axis.set_xticklabels(frame["team"], rotation=30, ha="right")
        axis.legend()
    axis.set_ylabel("Orders")
    lined = "the player's next stat" if CHART_KIND == "prop" else "the game"
    axis.set_title(f"Large book updates, and how many lined up with {lined}")
    fig.tight_layout()
    fig.savefig(OUTPUT / "large_order_counts.png", dpi=140)
    plt.close(fig)


def plot_spread(summary):
    fig, axis = plt.subplots(figsize=(6.5, 6))
    if CHART_KIND == "prop":
        frame = chart_prices(summary).dropna(subset=["chart_price", "espn_margin"]).copy()
        axis.axhline(0, color="#b0b0b0", linewidth=1)
        if frame.empty:
            axis.text(0.5, 0.5, "No final stat in the same window", ha="center")
        else:
            axis.scatter(frame["chart_price"], frame["espn_margin"], color="#1f4e79")
            for _, row in frame.iterrows():
                axis.annotate(row["team"], (row["chart_price"], row["espn_margin"]), textcoords="offset points", xytext=(4, 4), fontsize=8)
        axis.set_xlim(0, 1)
        axis.set_xlabel("Over mid before tip, or the first saved mid")
        axis.set_ylabel("Final stat minus the posted number")
        axis.set_title("Pregame Over price against the final stat")
    else:
        frame = summary.dropna(subset=["pregame_price", "spread_pregame"]).copy()
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


def use_output(find_text):
    global OUTPUT
    kind, _count = parse_find(find_text)
    OUTPUT = Path("outputs/nba/props") if kind == "props" else Path("outputs/nba")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="NBA price paths from the class order-book files.")
    parser.add_argument(
        "--find",
        default="top10",
        help="topN or propsN. The default, top10, keeps NBA game winners that have matched volume of at least 1,000 and a busy order book, ranked so both are high. props5 does the same for points, assists, and rebounds. No team filter.",
    )
    parser.add_argument(
        "--each",
        type=int,
        default=None,
        help="With --find props, how many markets to keep for each stat (1 to 10). --find props5 is the same as --each 5.",
    )
    parser.add_argument(
        "--stats",
        default="points,assists,rebounds",
        help="With --find props, which over/under stats. Any of points, assists, rebounds, threes, steals, blocks.",
    )
    parser.add_argument(
        "--plots-only",
        action="store_true",
        help="Redraw the charts from the CSVs. Uses outputs/nba/props when --find is props. Does not download hourly files.",
    )
    args = parser.parse_args()
    use_output(args.find)
    if args.plots_only:
        redraw()
    else:
        print(
            "Reading whole hourly Parquet files from the class CloudFront host. "
            "A full run is on the order of a few GB, and that host logs each file. "
            "Final scores and player stats are a few small requests to ESPN, not to that host.",
            flush=True,
        )
        kind, count = parse_find(args.find)
        if kind == "props":
            analyze_props(resolve_each(count, args.each), parse_stats(args.stats))
        else:
            analyze(args.find)
