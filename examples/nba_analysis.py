"""Price history for the top 10 NBA teams in the Week 2 power rankings.

NBA.com's Week 2 power rankings, published October 26, 2026, are the ranking.
Each team contributes one game-winner market: the market whose slug equals its
event slug, so the two outcomes are the two teams. The hourly Parquet files
supply the bid and ask path. The markets table only stores the latest quote.

The script keeps the highest-volume completed game that actually appears in
those files. A game already confirmed for another team is reused, so both
clubs in that game get a price path. From the same hourly files it also keeps
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


def quote_path(book, market, tip):
    if market is None or book.empty:
        return add_path_features(series_from(pd.DataFrame(), tip))
    rows = book[(book["market"] == market["condition_id"]) & (book["asset_id"] == market["asset_id"])]
    return add_path_features(series_from(rows, tip))


def analyze():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    pg = connect_postgres()
    games = load_games(pg)
    side_markets = load_side_markets(pg)
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
        spreads, total = side_markets_for(side_markets, row["slug"], team["name"])
        selected.append(
            {
                **team,
                "game": row,
                "asset_id": asset_id,
                "first": first,
                "spreads": spreads,
                "total": total,
            }
        )
        print(f"  using {row['slug']}", flush=True)
    duck.close()
    duck = connect_duckdb()

    needed = {}
    for item in selected:
        if item["game"] is None:
            continue
        game = item["game"]
        pairs = [(game["condition_id"], item["asset_id"])]
        for market in item["spreads"]:
            pairs.append((market["condition_id"], market["asset_id"]))
        if item["total"]:
            pairs.append((item["total"]["condition_id"], item["total"]["asset_id"]))
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
    alternates = []
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

    summary_frame = pd.DataFrame(summaries)
    alternate_frame = pd.DataFrame(alternates)
    summary_frame.to_csv(OUTPUT / "summary.csv", index=False)
    if paths:
        pd.concat(paths, ignore_index=True).to_csv(OUTPUT / "price_paths.csv", index=False)
    if not alternate_frame.empty:
        alternate_frame.to_csv(OUTPUT / "alternate_spreads.csv", index=False)
    pd.DataFrame(results).to_csv(OUTPUT / "game_results.csv", index=False)
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
    columns = [
        "rank",
        "team",
        "game",
        "variance_score_live",
        "variance_score_early",
        "variance_score_late",
        "espn_margin",
        "game_bucket",
        "won",
    ]
    print(summary_frame[columns].to_string(index=False))
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


def facet_figure(sharey=False):
    fig, axes = plt.subplots(5, 2, figsize=(11, 14), sharex=True, sharey=sharey)
    return fig, axes.ravel()


def draw_empty(axis, team):
    axis.text(0.5, 0.5, "No order-book path", ha="center", va="center", transform=axis.transAxes)
    axis.set_title(f"{team['rank']}. {team['name']} ({team['record']})", loc="left", fontsize=10)


def plot_variance_paths(paths):
    fig, axes = facet_figure(sharey=True)
    by_rank = {frame["rank"].iloc[0]: frame for frame in paths}
    for axis, team in zip(axes, TOP_TEAMS):
        frame = by_rank.get(team["rank"])
        if frame is None or frame["variance_score"].dropna().empty:
            draw_empty(axis, team)
            continue
        axis.plot(frame["hours_from_tip"], frame["variance_score"], color="#6b3fa0", linewidth=1.4)
        axis.axvline(0, color="#b0b0b0", linewidth=0.8)
        axis.set_title(f"{team['rank']}. {frame['label'].iloc[0]}", loc="left", fontsize=10)
        axis.grid(axis="y", color="#eeeeee")
    for axis in axes[8:]:
        axis.set_xlabel("Hours from listed tip")
    for axis in axes[::2]:
        axis.set_ylabel("Trailing 30-minute std")
    fig.suptitle("Variance score every five minutes", fontsize=13)
    fig.tight_layout()
    fig.savefig(OUTPUT / "variance_score.png", dpi=140)
    plt.close(fig)


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
    fig, axes = facet_figure()
    by_rank = {frame["rank"].iloc[0]: frame for frame in paths}
    for axis, team in zip(axes, TOP_TEAMS):
        frame = by_rank.get(team["rank"])
        if frame is None:
            draw_empty(axis, team)
            continue
        axis.plot(frame["hours_from_tip"], frame["mid"], color="#1f4e79", linewidth=1.4, label="Win price")
        axis.axvline(0, color="#b0b0b0", linewidth=0.8)
        axis.set_ylim(0, 1)
        scores = frame["score_margin"].dropna()
        if not scores.empty:
            twin = axis.twinx()
            twin.plot(frame["hours_from_tip"], frame["score_margin"], color="#b04a4a", linewidth=1.2, label="Score margin")
            twin.set_ylabel("Margin" if team["rank"] % 2 == 0 else "")
            limit = max(15, float(scores.abs().max()) + 2)
            twin.set_ylim(-limit, limit)
        axis.set_title(f"{team['rank']}. {frame['label'].iloc[0]}", loc="left", fontsize=10)
        axis.grid(axis="y", color="#eeeeee")
    for axis in axes[8:]:
        axis.set_xlabel("Hours from listed tip")
    for axis in axes[::2]:
        axis.set_ylabel("Win price")
    fig.suptitle("Win price and ESPN score margin", fontsize=13)
    fig.tight_layout()
    fig.savefig(OUTPUT / "price_vs_score.png", dpi=140)
    plt.close(fig)


def plot_market_paths(paths):
    fig, axes = facet_figure(sharey=True)
    by_rank = {frame["rank"].iloc[0]: frame for frame in paths}
    labeled = False
    for axis, team in zip(axes, TOP_TEAMS):
        frame = by_rank.get(team["rank"])
        if frame is None:
            draw_empty(axis, team)
            continue
        axis.plot(frame["hours_from_tip"], frame["mid"], color="#1f4e79", linewidth=1.4, label="Win")
        if "spread_mid" in frame:
            axis.plot(frame["hours_from_tip"], frame["spread_mid"], color="#d9762c", linewidth=1.2, label="Main spread")
        if "total_mid" in frame:
            axis.plot(frame["hours_from_tip"], frame["total_mid"], color="#2f7d4a", linewidth=1.2, label="Game total Over")
        axis.axvline(0, color="#b0b0b0", linewidth=0.8)
        axis.set_ylim(0, 1)
        axis.set_title(f"{team['rank']}. {frame['label'].iloc[0]}", loc="left", fontsize=10)
        axis.grid(axis="y", color="#eeeeee")
        if not labeled:
            axis.legend(fontsize=8, loc="lower left")
            labeled = True
    for axis in axes[8:]:
        axis.set_xlabel("Hours from listed tip")
    for axis in axes[::2]:
        axis.set_ylabel("Price")
    fig.suptitle("Win price, main spread, and game total", fontsize=13)
    fig.tight_layout()
    fig.savefig(OUTPUT / "market_paths.png", dpi=140)
    plt.close(fig)


def plot_bid_ask(paths):
    fig, axes = facet_figure(sharey=True)
    by_rank = {frame["rank"].iloc[0]: frame for frame in paths}
    for axis, team in zip(axes, TOP_TEAMS):
        frame = by_rank.get(team["rank"])
        if frame is None or "spread_width" not in frame or frame["spread_width"].dropna().empty:
            draw_empty(axis, team)
            continue
        axis.plot(frame["hours_from_tip"], frame["spread_width"], color="#1f4e79", linewidth=1.3)
        axis.axvline(0, color="#b0b0b0", linewidth=0.8)
        axis.set_title(f"{team['rank']}. {frame['label'].iloc[0]}", loc="left", fontsize=10)
        axis.grid(axis="y", color="#eeeeee")
    for axis in axes[8:]:
        axis.set_xlabel("Hours from listed tip")
    for axis in axes[::2]:
        axis.set_ylabel("Ask minus bid")
    fig.suptitle("Bid-ask gap every five minutes", fontsize=13)
    fig.tight_layout()
    fig.savefig(OUTPUT / "bid_ask_width.png", dpi=140)
    plt.close(fig)


def plot_alternate_spreads(frame):
    fig, axes = facet_figure()
    if frame is None or frame.empty:
        grouped = {}
    else:
        grouped = {rank: part.dropna(subset=["line", "pregame_price"]) for rank, part in frame.groupby("rank")}
    for axis, team in zip(axes, TOP_TEAMS):
        part = grouped.get(team["rank"])
        if part is None or part.empty:
            draw_empty(axis, team)
            axis.set_title(f"{team['rank']}. {team['name']}", loc="left", fontsize=10)
            continue
        part = part.sort_values("line")
        axis.plot(part["line"], part["pregame_price"], color="#1f4e79", marker="o")
        axis.set_ylim(0, 1)
        axis.axhline(0.5, color="#b0b0b0", linewidth=0.8)
        axis.set_title(f"{team['rank']}. {team['name']}", loc="left", fontsize=10)
        axis.grid(axis="y", color="#eeeeee")
    for axis in axes[8:]:
        axis.set_xlabel("Points, from that team's side")
    for axis in axes[::2]:
        axis.set_ylabel("Pregame cover price")
    fig.suptitle("Alternate spreads before tip", fontsize=13)
    fig.tight_layout()
    fig.savefig(OUTPUT / "alternate_spreads.png", dpi=140)
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
        "A full run is on the order of a few GB, and that host logs each file. "
        "Final scores are a few small requests to ESPN, not to that host.",
        flush=True,
    )
    analyze()
