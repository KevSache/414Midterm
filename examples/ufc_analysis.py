"""Price history for UFC fight winners that already have an order book.

    python examples/ufc_analysis.py
    python examples/ufc_analysis.py --find top5

The default, `top10`, keeps fight winners that are high on both matched
volume (volume_clob at least 100) and posted size in the hourly files.
Any `topN` from 1 to 40 does the same. A fight slug equals its event slug
and looks like `ufc-cir-ale30-2026-06-14`. The two outcomes are the two
fighters. The charted price is the first fighter.

The listed end in markets.end_date is one clock time for every bout on a
card, about 04:00 UTC on the morning after the date in the slug. It is
not the bell. ESPN does not publish a per-bout start in the scoreboard
this script reads. Hours on the charts are hours from that listed end.
The window is the eight hours before it through the two hours after,
which covers a normal card.

Final method, round, and clock come from ESPN's public UFC scoreboard.
Polymarket's resolution is the win flag when that row exists. The same
pass reads the fight-level KO/TKO, submission, decision, and
go-the-distance markets, plus the round total with the most matched volume.

Each five-minute mid is the market's win chance for the charted fighter.
A mid above 0.5 is scored as a win call and a mid below 0.5 as a loss
call. The script also groups those prices into 10-point bands and
compares the band's average price with the share of fights the fighter
actually won.

Graphs and a summary table are written to outputs/ufc/.
DATA_DICTIONARY.md in that folder defines every CSV column.

A full run downloads whole hourly files from the class CloudFront host.
That host logs each file and the bytes sent. Fights before the Parquet
window (2026-05-30) have no order book to read.
"""

import argparse
import json
import re
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import nba_analysis as nba

OUTPUT = Path("outputs/ufc")
HOURS_BEFORE = 8
HOURS_AFTER = 2
LATE_HOURS = 2
MIN_VOLUME_CLOB = 100
MAX_FIND = 40
DEFAULT_TOP = 10
ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/mma/ufc/scoreboard?dates={day}"
FIGHT_RE = re.compile(r"^ufc-.+-\d{4}-\d{2}-\d{2}$")
TITLE_RE = re.compile(
    r":\s*(?P<a>.+?)\s+vs\.?\s+(?P<b>.+?)\s+\((?P<weight>[^,]+),\s*(?P<card>[^)]+)\)\s*$"
)
TOTAL_RE = re.compile(r"totals-(\d+)pt(\d+)$")
SIDE_SUFFIXES = {
    "go-the-distance": "distance",
    "win-by-ko-tko": "ko",
    "win-by-submission": "submission",
    "win-by-decision": "decision",
}
METHOD_WORDS = (
    ("kotko", "KO/TKO"),
    ("submission", "submission"),
    ("decision", "decision"),
)
ROUND_SECONDS = 300

PANELS = []
COLUMN_MEANING = {
    ("summary.csv", "rank"): "1 is the fight whose weaker of volume and posted size is highest.",
    ("summary.csv", "fighter"): "First listed fighter. The charted price is this fighter's win price.",
    ("summary.csv", "opponent"): "The other fighter.",
    ("summary.csv", "slug"): "Fight market slug. It equals the event slug.",
    ("summary.csv", "weight"): "Weight class from the market title.",
    ("summary.csv", "card"): "Main Card or Prelims, from the market title.",
    ("summary.csv", "tip_utc"): "Listed market end, UTC. Shared by every bout on the card. Not the bell.",
    ("summary.csv", "volume_clob"): "Lifetime matched volume stored on the fight market.",
    ("summary.csv", "book_size"): "Posted size summed over the card window. Not matched trades.",
    ("summary.csv", "book_updates"): "Order-book updates with size above zero in that window.",
    ("summary.csv", "balance"): "Lower of the volume percentile and the posted-size percentile.",
    ("summary.csv", "won"): "True when the charted fighter won. Polymarket's resolution when it exists, otherwise ESPN.",
    ("summary.csv", "settle_source"): "polymarket or espn.",
    ("summary.csv", "espn_winner"): "Winner name on the ESPN scoreboard.",
    ("summary.csv", "method"): "KO/TKO, submission, or decision, from ESPN.",
    ("summary.csv", "finish_round"): "Round in which the fight ended.",
    ("summary.csv", "finish_clock"): "Elapsed time in that round, as ESPN prints it.",
    ("summary.csv", "scheduled_rounds"): "3 or 5, from the ESPN bout format.",
    ("summary.csv", "rounds_fought"): "Completed rounds plus the fraction of the finishing round. A 5:00 clock counts as a full round.",
    ("summary.csv", "went_distance"): "True when ESPN records a decision in the scheduled final round.",
    ("summary.csv", "result_agree"): "True when Polymarket and ESPN name the same winner.",
    ("summary.csv", "card_price"): "Earliest saved mid in the window. This is the price near the start of the card.",
    ("summary.csv", "late_price"): "Last mid before the listed end.",
    ("summary.csv", "early_std"): "Standard deviation of the mid from the start of the window until two hours before the listed end.",
    ("summary.csv", "late_std"): "Standard deviation of the mid in the last two hours before the listed end.",
    ("summary.csv", "large_n"): "Large orders within 10 cents of the mid.",
    ("summary.csv", "followed_n"): "Large orders whose mid moved at least 3 cents that way over the next 30 minutes.",
    ("summary.csv", "total_line"): "Round total with the most matched volume, such as 1.5.",
    ("summary.csv", "total_price"): "Earliest saved Over mid on that total.",
    ("summary.csv", "total_hit"): "True when rounds_fought is above the total.",
    ("summary.csv", "distance_price"): "Earliest saved Yes mid on go-the-distance.",
    ("summary.csv", "ko_price"): "Earliest saved Yes mid on fight won by KO or TKO.",
    ("summary.csv", "submission_price"): "Earliest saved Yes mid on fight won by submission.",
    ("summary.csv", "decision_price"): "Earliest saved Yes mid on fight won by decision.",
    ("summary.csv", "score_source"): "ESPN event id when a bout matched.",
    ("summary.csv", "note"): "Why a result or a book is missing.",
    ("price_paths.csv", "hours_from_end"): "Hours from the listed market end. Negative is earlier on the card.",
    ("price_paths.csv", "mid"): "Average of the best bid and best ask in that five-minute bin. This is the market's win chance for the charted fighter.",
    ("price_paths.csv", "win_chance"): "Same number as mid. Kept under this name in the prediction tables.",
    ("price_paths.csv", "outcome"): "1 when the charted fighter won, 0 when that fighter lost. Blank when the result is unknown.",
    ("price_paths.csv", "called_win"): "1 when the mid is above 0.5, 0 when it is below. Blank at exactly 0.5.",
    ("price_paths.csv", "call_correct"): "1 when that call matches the final result, 0 when it does not.",
    ("price_paths.csv", "brier"): "Squared gap between the win chance and the outcome. A coin flip averages 0.25.",
    ("price_paths.csv", "profit_to_close"): "Settlement minus the mid. Settlement is 1 or 0 for the charted fighter.",
    ("accuracy_by_time.csv", "minutes_from_end"): "Five-minute mark on the clock, negative before the listed market end.",
    ("accuracy_by_time.csv", "hours_from_end"): "That same mark in hours.",
    ("accuracy_by_time.csv", "fights"): "Fights with a quote at that mark. Each fight is counted once.",
    ("accuracy_by_time.csv", "mean_win_chance"): "Average mid at that mark, one number per fight.",
    ("accuracy_by_time.csv", "actual_win_rate"): "Share of those fights the charted fighter won.",
    ("accuracy_by_time.csv", "calls"): "Quotes that were not exactly 0.5.",
    ("accuracy_by_time.csv", "call_hit_rate"): "Share of those calls that matched the final result.",
    ("accuracy_by_time.csv", "brier"): "Average squared gap between the win chance and the outcome.",
    ("accuracy_by_price.csv", "band"): "Price band, such as 0.70–0.80.",
    ("accuracy_by_price.csv", "fights"): "Fights with at least one quote in the band. Each fight is counted once.",
    ("accuracy_by_price.csv", "bins"): "Five-minute rows in the band, before the one-per-fight collapse.",
    ("accuracy_by_price.csv", "mean_win_chance"): "Average mid in the band, one number per fight.",
    ("accuracy_by_price.csv", "actual_win_rate"): "Share of those fights the charted fighter won.",
    ("accuracy_by_price.csv", "brier"): "Average squared gap between the win chance and the outcome.",
    ("price_paths.csv", "spread_width"): "Best ask minus best bid. Negative when the bin's high bid is above its low ask. Those two prints need not be simultaneous.",
    ("side_markets.csv", "kind"): "distance, ko, submission, decision, or total.",
    ("side_markets.csv", "yes_won"): "True when that side market's Yes or Over won, from the ESPN result.",
    ("large_orders.csv", "size_ratio"): "Posted size divided by the median nearby size on that token and side.",
    ("large_orders.csv", "price_followed"): "True when the mid 30 minutes later moved at least 3 cents in the order's direction.",
}


def as_utc(value):
    tip = pd.Timestamp(value)
    if tip.tzinfo is None:
        tip = tip.tz_localize("UTC")
    else:
        tip = tip.tz_convert("UTC")
    return tip.to_pydatetime()


def as_list(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return []
    if isinstance(value, str):
        parsed = json.loads(value)
        return list(parsed)
    return list(value)


def norm_name(name):
    text = unicodedata.normalize("NFKD", str(name or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("'", "").replace("\u2019", "").replace("\u2018", "")
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    return text


def card_hours(tip):
    """Hour starts from eight hours before the listed end through two hours after."""
    moment = pd.Timestamp(as_utc(tip))
    start = (moment - pd.Timedelta(hours=HOURS_BEFORE)).replace(minute=0, second=0, microsecond=0)
    stop = moment + pd.Timedelta(hours=HOURS_AFTER)
    cursor = start.to_pydatetime()
    end = stop.to_pydatetime()
    hours = []
    while cursor <= end:
        hours.append(cursor)
        cursor += timedelta(hours=1)
    return hours


def slug_day(slug):
    year, month, day = slug.split("-")[-3:]
    return datetime(int(year), int(month), int(day))


def title_parts(title, outcomes):
    names = [str(name) for name in outcomes[:2]]
    fighter = names[0] if names else ""
    opponent = names[1] if len(names) > 1 else ""
    weight = ""
    card = ""
    match = TITLE_RE.search(str(title or ""))
    if match:
        fighter = match.group("a").strip()
        opponent = match.group("b").strip()
        weight = match.group("weight").strip()
        card = match.group("card").strip()
    return fighter, opponent, weight, card


def suffix_kind(slug):
    for suffix, kind in SIDE_SUFFIXES.items():
        if slug.endswith("-" + suffix):
            return kind, None
    match = TOTAL_RE.search(slug)
    if match:
        return "total", float(f"{int(match.group(1))}.{match.group(2)}")
    return None, None


def load_fights(pg):
    frame = pd.read_sql(
        """
        SELECT m.slug, m.title, m.outcomes, m.token_ids, m.condition_id,
               m.end_date AS tip, m.volume_clob, r.resolved_yes
        FROM markets m
        LEFT JOIN resolutions r ON r.market_slug = m.slug
        WHERE m.slug = m.event_slug
          AND m.slug LIKE 'ufc-%'
          AND m.slug ~ '^ufc-.+-[0-9]{4}-[0-9]{2}-[0-9]{2}$'
          AND m.end_date >= '2026-05-30'
          AND m.end_date < now()
        """,
        pg,
    )
    frame = frame[frame["slug"].map(lambda slug: FIGHT_RE.match(str(slug)) is not None)].copy()
    frame["volume_clob"] = pd.to_numeric(frame["volume_clob"], errors="coerce")
    return frame.reset_index(drop=True)


def load_sides(pg, slugs):
    if not slugs:
        return pd.DataFrame()
    cursor = pg.cursor()
    cursor.execute(
        """
        SELECT event_slug, slug, title, outcomes, token_ids, condition_id, volume_clob
        FROM markets
        WHERE event_slug = ANY(%s)
        """,
        (list(slugs),),
    )
    columns = [desc[0] for desc in cursor.description]
    return pd.DataFrame(cursor.fetchall(), columns=columns)


def token_row(row):
    tokens = as_list(row["token_ids"])
    if not tokens or pd.isna(row["condition_id"]):
        return None
    return {
        "slug": row["slug"],
        "condition_id": str(row["condition_id"]),
        "asset_id": str(tokens[0]),
        "tip": as_utc(row["tip"]),
    }


def discover_activity(frame):
    """Posted size for each fighter token across the card window. Each hour is read once."""
    empty = pd.DataFrame(columns=["condition_id", "asset_id", "book_updates", "book_size"])
    buckets = {}
    for _, row in frame.iterrows():
        picked = token_row(row)
        if picked is None:
            continue
        pair = (picked["condition_id"], picked["asset_id"])
        for moment in card_hours(picked["tip"]):
            buckets.setdefault(moment, set()).add(pair)
    moments = sorted(buckets)
    print(
        f"Searching {len(moments)} hourly files for UFC fight tokens. "
        "A file is one hour for the whole venue, read once.",
        flush=True,
    )
    duck = None
    parts = []
    try:
        duck = nba.connect_duckdb()
        for index, moment in enumerate(moments, start=1):
            if index > 1 and index % 6 == 1:
                duck.close()
                duck = nba.connect_duckdb()
            print(f"  book hour {index}/{len(moments)} {nba.hour_key(moment)}", flush=True)
            found = nba.activity_in_hour(duck, moment, buckets[moment])
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
    return combined.groupby(["condition_id", "asset_id"], as_index=False)[["book_updates", "book_size"]].sum()


def rank_fights(fights, activity, count):
    tokens = []
    for _, row in fights.iterrows():
        picked = token_row(row)
        if picked is not None:
            tokens.append(picked)
    if not tokens or activity is None or activity.empty:
        return []
    ranked = pd.DataFrame(tokens).merge(activity, on=["condition_id", "asset_id"], how="inner")
    volumes = fights[["slug", "volume_clob"]].drop_duplicates("slug")
    ranked = ranked.merge(volumes, on="slug", how="left")
    ranked = nba.balance_ranks(ranked, minimum=MIN_VOLUME_CLOB)
    print(
        f"{len(ranked)} fights have a posted book and matched volume of at least {MIN_VOLUME_CLOB:,.0f}.",
        flush=True,
    )
    chosen = ranked.head(count)
    by_slug = fights.set_index("slug", drop=False)
    selected = []
    for _, hit in chosen.iterrows():
        row = by_slug.loc[hit["slug"]]
        if isinstance(row, pd.DataFrame):
            row = row.iloc[0]
        row = row.copy()
        outcomes = as_list(row["outcomes"])
        fighter, opponent, weight, card = title_parts(row["title"], outcomes)
        selected.append(
            {
                "rank": len(selected) + 1,
                "fighter": fighter,
                "opponent": opponent,
                "weight": weight,
                "card": card,
                "slug": row["slug"],
                "condition_id": str(row["condition_id"]),
                "asset_id": str(hit["asset_id"]),
                "tip": as_utc(row["tip"]),
                "volume_clob": None if pd.isna(row["volume_clob"]) else float(row["volume_clob"]),
                "book_size": float(hit["book_size"]),
                "book_updates": int(hit["book_updates"]),
                "balance": float(hit["balance"]),
                "resolved_yes": row["resolved_yes"],
                "sides": [],
            }
        )
        print(
            f"  kept {len(selected)}/{count} {row['slug']}  {fighter} vs {opponent}",
            flush=True,
        )
    return selected


def attach_sides(selected, sides):
    if sides is None or sides.empty:
        return selected
    for item in selected:
        mine = sides[sides["event_slug"] == item["slug"]]
        totals = []
        for _, row in mine.iterrows():
            kind, line = suffix_kind(str(row["slug"]))
            if kind is None or str(row["slug"]) == item["slug"]:
                continue
            # Fighter-specific props end in the same words. Keep only the fight-level suffix.
            tail = str(row["slug"])[len(item["slug"]) + 1 :]
            if kind == "total":
                if not TOTAL_RE.fullmatch(tail):
                    continue
            elif tail not in SIDE_SUFFIXES:
                continue
            tokens = as_list(row["token_ids"])
            if not tokens or pd.isna(row["condition_id"]):
                continue
            record = {
                "kind": kind,
                "line": line,
                "slug": row["slug"],
                "title": row["title"],
                "condition_id": str(row["condition_id"]),
                "asset_id": str(tokens[0]),
                "volume_clob": None if pd.isna(row["volume_clob"]) else float(row["volume_clob"]),
            }
            if kind == "total":
                totals.append(record)
            else:
                item["sides"].append(record)
        if totals:
            totals.sort(key=lambda record: record["volume_clob"] or 0.0, reverse=True)
            item["sides"].append(totals[0])
    return selected


def download_book(selected):
    """Five-minute quotes for the charted fighter and the fight-level side markets."""
    duck = nba.connect_duckdb()
    needed = {}
    try:
        for item in selected:
            pairs = [(item["condition_id"], item["asset_id"], True)]
            for side in item["sides"]:
                pairs.append((side["condition_id"], side["asset_id"], False))
            for moment in card_hours(item["tip"]):
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
                duck = nba.connect_duckdb()
            print(f"hour {index}/{len(moments)} {nba.hour_key(moment)}", flush=True)
            pairs = [(market, asset, focus) for (market, asset), focus in needed[moment].items()]
            frame, larges = nba.read_hour_scan(duck, moment, pairs)
            if not frame.empty:
                quotes.append(frame)
            if not larges.empty:
                large_hits.append(larges)
    finally:
        try:
            duck.close()
        except Exception:
            pass
    book = pd.concat(quotes, ignore_index=True) if quotes else pd.DataFrame()
    large_book = pd.concat(large_hits, ignore_index=True) if large_hits else pd.DataFrame()
    if not book.empty:
        book = book.drop_duplicates(["market", "asset_id", "bin"])
    return book, large_book


def method_name(details):
    for item in details or []:
        text = str((item.get("type") or {}).get("text") or "")
        if "winner" not in text.lower():
            continue
        lower = text.lower()
        for word, label in METHOD_WORDS:
            if word in lower:
                return label
    return None


def elapsed_seconds(clock):
    text = str(clock or "")
    if ":" not in text:
        return None
    minutes, seconds = text.split(":", 1)
    try:
        return int(minutes) * 60 + int(seconds)
    except ValueError:
        return None


def rounds_fought(period, clock):
    """Completed rounds plus the fraction of the finishing round."""
    if period is None or pd.isna(period):
        return None
    elapsed = elapsed_seconds(clock)
    if elapsed is None:
        return None
    fraction = min(max(elapsed, 0), ROUND_SECONDS) / ROUND_SECONDS
    return (int(period) - 1) + fraction


def went_the_distance(method, period, scheduled):
    if method is None or period is None or scheduled is None:
        return None
    if method != "decision":
        return False
    return int(period) == int(scheduled)


def load_board(day, boards):
    if day not in boards:
        try:
            boards[day] = nba.fetch_json(ESPN_SCOREBOARD.format(day=day)).get("events", [])
        except Exception as error:
            print(f"  scoreboard {day}: {error.__class__.__name__}", flush=True)
            boards[day] = []
    return boards[day]


def find_bout(fighter, opponent, day, boards):
    wanted = {norm_name(fighter), norm_name(opponent)}
    if len(wanted) < 2:
        return None, None
    for shift in (0, 1, -1):
        stamp = (day + timedelta(days=shift)).strftime("%Y%m%d")
        for event in load_board(stamp, boards):
            for comp in event.get("competitions") or []:
                names = {
                    norm_name((row.get("athlete") or {}).get("displayName"))
                    for row in comp.get("competitors") or []
                }
                if wanted <= names:
                    return event, comp
    return None, None


def bout_result(event, comp, fighter, opponent):
    if comp is None:
        return None
    status = comp.get("status") or {}
    competitors = comp.get("competitors") or []
    winner = None
    for row in competitors:
        if row.get("winner"):
            winner = (row.get("athlete") or {}).get("displayName")
    winner_key = norm_name(winner)
    if winner_key and winner_key == norm_name(fighter):
        charted_won = True
    elif winner_key and winner_key == norm_name(opponent):
        charted_won = False
    else:
        charted_won = None
    period = status.get("period")
    clock = status.get("displayClock")
    scheduled = ((comp.get("format") or {}).get("regulation") or {}).get("periods")
    method = method_name(comp.get("details"))
    fought = rounds_fought(period, clock)
    return {
        "event_id": event.get("id") if event else None,
        "winner": winner,
        "charted_won": charted_won,
        "method": method,
        "finish_round": None if period is None else int(period),
        "finish_clock": clock,
        "scheduled_rounds": None if scheduled is None else int(scheduled),
        "rounds_fought": fought,
        "went_distance": went_the_distance(method, period, scheduled),
    }


def chart_won(item, result):
    resolved = item.get("resolved_yes")
    if pd.notna(resolved):
        return bool(resolved), "polymarket"
    if result is not None and result["charted_won"] is not None:
        return bool(result["charted_won"]), "espn"
    return None, ""


def results_agree(item, result):
    resolved = item.get("resolved_yes")
    if result is None or result["charted_won"] is None or pd.isna(resolved):
        return None
    return bool(resolved) == bool(result["charted_won"])


def side_hit(side, result):
    if result is None:
        return None
    kind = side["kind"]
    if kind == "distance":
        return result["went_distance"]
    if kind == "ko":
        return None if result["method"] is None else result["method"] == "KO/TKO"
    if kind == "submission":
        return None if result["method"] is None else result["method"] == "submission"
    if kind == "decision":
        return None if result["method"] is None else result["method"] == "decision"
    if kind == "total":
        fought = result["rounds_fought"]
        if fought is None or side["line"] is None:
            return None
        return fought > side["line"]
    return None


def quote_for(book, market, tip):
    if market is None or book is None or book.empty:
        return nba.add_path_features(nba.series_from(pd.DataFrame(), tip))
    rows = book[(book["market"] == market["condition_id"]) & (book["asset_id"] == market["asset_id"])]
    path = nba.add_path_features(nba.series_from(rows, tip))
    if "hours_from_tip" in path.columns:
        path = path.rename(columns={"hours_from_tip": "hours_from_end"})
    return path


def window_std(frame, start, end):
    if frame.empty or "mid" not in frame:
        return None
    sample = frame[(frame["hours_from_end"] >= start) & (frame["hours_from_end"] < end)]["mid"].dropna()
    if len(sample) < 3:
        return None
    return float(sample.std())


def first_mid(frame):
    if frame is None or frame.empty or "mid" not in frame:
        return None
    sample = frame.dropna(subset=["mid"])
    if sample.empty:
        return None
    return float(sample.iloc[0]["mid"])


def last_before_end(frame):
    if frame is None or frame.empty:
        return None
    sample = frame[frame["hours_from_end"] < 0].dropna(subset=["mid"])
    if sample.empty:
        return None
    return float(sample.iloc[-1]["mid"])


def empty_summary(item, note):
    return {
        "rank": item["rank"],
        "fighter": item["fighter"],
        "opponent": item["opponent"],
        "slug": item["slug"],
        "weight": item["weight"],
        "card": item["card"],
        "tip_utc": item["tip"],
        "volume_clob": item["volume_clob"],
        "book_size": item["book_size"],
        "book_updates": item["book_updates"],
        "balance": item["balance"],
        "won": None,
        "settle_source": "",
        "espn_winner": None,
        "method": None,
        "finish_round": None,
        "finish_clock": None,
        "scheduled_rounds": None,
        "rounds_fought": None,
        "went_distance": None,
        "result_agree": None,
        "card_price": None,
        "late_price": None,
        "early_std": None,
        "late_std": None,
        "large_n": 0,
        "followed_n": 0,
        "total_line": None,
        "total_price": None,
        "total_hit": None,
        "distance_price": None,
        "ko_price": None,
        "submission_price": None,
        "decision_price": None,
        "score_source": None,
        "note": note,
    }


def analyze(count):
    global PANELS
    OUTPUT.mkdir(parents=True, exist_ok=True)
    pg = nba.connect_postgres()
    try:
        fights = load_fights(pg)
        print(f"{len(fights)} completed UFC fights in the order-book window.", flush=True)
        activity = discover_activity(fights)
        selected = rank_fights(fights, activity, count)
        if not selected:
            raise SystemExit(
                "No UFC fight had both an order book and matched volume of at least "
                f"{MIN_VOLUME_CLOB:,.0f}. Nothing was charted."
            )
        sides = load_sides(pg, [item["slug"] for item in selected])
        attach_sides(selected, sides)
    finally:
        pg.close()
    book, large_book = download_book(selected)
    PANELS = [{"rank": item["rank"], "name": item["fighter"], "record": item["opponent"]} for item in selected]

    boards = {}
    summaries = []
    paths = []
    side_rows = []
    order_frames = []
    for item in selected:
        summary = empty_summary(item, "")
        event, comp = find_bout(item["fighter"], item["opponent"], slug_day(item["slug"]), boards)
        result = bout_result(event, comp, item["fighter"], item["opponent"])
        won, source = chart_won(item, result)
        market = {"condition_id": item["condition_id"], "asset_id": item["asset_id"]}
        path = quote_for(book, market, item["tip"])
        path = nba.attach_close_profit(path, won)
        if "hours_from_tip" in path.columns and "hours_from_end" not in path.columns:
            path = path.rename(columns={"hours_from_tip": "hours_from_end"})
        summary["won"] = won
        summary["settle_source"] = source
        summary["card_price"] = first_mid(path)
        summary["late_price"] = last_before_end(path)
        summary["early_std"] = window_std(path, -HOURS_BEFORE, -LATE_HOURS)
        summary["late_std"] = window_std(path, -LATE_HOURS, 0)
        summary["result_agree"] = results_agree(item, result)
        if result is None:
            summary["note"] = "Order book read. ESPN had no matching bout."
            print(f"  no ESPN bout for {item['slug']}", flush=True)
        else:
            summary.update(
                {
                    "espn_winner": result["winner"],
                    "method": result["method"],
                    "finish_round": result["finish_round"],
                    "finish_clock": result["finish_clock"],
                    "scheduled_rounds": result["scheduled_rounds"],
                    "rounds_fought": result["rounds_fought"],
                    "went_distance": result["went_distance"],
                    "score_source": f"espn:{result['event_id']}",
                }
            )
            print(
                f"  ESPN {result['winner']} by {result['method']} R{result['finish_round']} {result['finish_clock']}",
                flush=True,
            )
        for side in item["sides"]:
            series = quote_for(book, side, item["tip"])
            price = first_mid(series)
            hit = side_hit(side, result)
            column = {
                "distance": "distance_price",
                "ko": "ko_price",
                "submission": "submission_price",
                "decision": "decision_price",
                "total": "total_price",
            }[side["kind"]]
            summary[column] = price
            if side["kind"] == "total":
                summary["total_line"] = side["line"]
                summary["total_hit"] = hit
            side_rows.append(
                {
                    "rank": item["rank"],
                    "fighter": item["fighter"],
                    "slug": side["slug"],
                    "kind": side["kind"],
                    "line": side["line"],
                    "title": side["title"],
                    "volume_clob": side["volume_clob"],
                    "card_price": price,
                    "yes_won": hit,
                }
            )
        label = f"{item['fighter']} vs {item['opponent']}"
        if summary["method"]:
            label += f" ({summary['method']} R{summary['finish_round']})"
        if not path.empty:
            plot_path = path.copy()
            plot_path["rank"] = item["rank"]
            plot_path["fighter"] = item["fighter"]
            plot_path["slug"] = item["slug"]
            plot_path["label"] = label
            paths.append(plot_path)
        else:
            summary["note"] = summary["note"] or "No quotes in the card window."
        if not large_book.empty:
            mine = large_book[
                (large_book["market"] == item["condition_id"]) & (large_book["asset_id"] == item["asset_id"])
            ]
            tagged = nba.classify_orders(mine, path.rename(columns={"hours_from_end": "hours_from_tip"}), None, item["tip"])
            if not tagged.empty:
                tagged = tagged.rename(columns={"hours_from_tip": "hours_from_end"})
                tagged.insert(0, "rank", item["rank"])
                tagged.insert(1, "fighter", item["fighter"])
                order_frames.append(tagged)
                summary["large_n"] = int(len(tagged))
                summary["followed_n"] = int(tagged["price_followed"].sum())
        summaries.append(summary)

    summary_frame = pd.DataFrame(summaries)
    path_frame = pd.concat(paths, ignore_index=True) if paths else pd.DataFrame()
    side_frame = pd.DataFrame(side_rows)
    order_frame = pd.concat(order_frames, ignore_index=True) if order_frames else pd.DataFrame()
    summary_frame.to_csv(OUTPUT / "summary.csv", index=False)
    path_frame = write_prediction(path_frame, summary_frame)
    path_frame.to_csv(OUTPUT / "price_paths.csv", index=False)
    side_frame.to_csv(OUTPUT / "side_markets.csv", index=False)
    order_path = OUTPUT / "large_orders.csv"
    if order_frame.empty:
        if order_path.exists():
            order_path.unlink()
    else:
        order_frame.to_csv(order_path, index=False)
    draw_all(summary_frame, path_frame, order_frame)
    write_dictionary()
    print(f"Wrote {OUTPUT}")


def outcome_flag(value):
    """1 when the charted fighter won, 0 when that fighter lost, otherwise unknown."""
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
    """Attach the win chance, the 0.5 call, and the squared error on every bin.

    The mid is the market's probability that the charted fighter wins.
    Above 0.5 that is a win call. Below 0.5 it is a loss call. Exactly
    0.5 is left unscored as a call, and it still has a Brier score.
    """
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


def fight_key(frame):
    if "slug" in frame.columns and frame["slug"].notna().any():
        return frame["slug"].astype(str)
    return frame["rank"].astype(str)


def accuracy_tables(frame):
    """One row per five-minute mark, and one row per 10-point price band.

    A fight that sits in one band for an hour is one fight, not twelve
    bins. The bin count is still saved so the overweighting is visible.
    """
    time_columns = [
        "minutes_from_end", "hours_from_end", "fights", "mean_win_chance",
        "actual_win_rate", "calls", "call_hit_rate", "brier",
    ]
    price_columns = [
        "band_low", "band_high", "band", "fights", "bins",
        "mean_win_chance", "actual_win_rate", "brier",
    ]
    empty_time = pd.DataFrame(columns=time_columns)
    empty_price = pd.DataFrame(columns=price_columns)
    if frame is None or frame.empty or "win_chance" not in frame.columns:
        return empty_time, empty_price
    usable = frame.dropna(subset=["win_chance", "outcome"]).copy()
    if usable.empty or "hours_from_end" not in usable.columns:
        return empty_time, empty_price
    usable["fight"] = fight_key(usable)
    minutes = (pd.to_numeric(usable["hours_from_end"], errors="coerce") * 60).round()
    usable["minutes_from_end"] = (minutes / 5).round() * 5

    per_slot = usable.groupby(["minutes_from_end", "fight"], as_index=False).agg(
        win_chance=("win_chance", "mean"),
        outcome=("outcome", "first"),
        call_correct=("call_correct", "mean"),
        brier=("brier", "mean"),
    )
    time_rows = []
    for minute, part in per_slot.groupby("minutes_from_end"):
        calls = part["call_correct"].dropna()
        time_rows.append(
            {
                "minutes_from_end": int(minute),
                "hours_from_end": float(minute) / 60,
                "fights": int(part["fight"].nunique()),
                "mean_win_chance": float(part["win_chance"].mean()),
                "actual_win_rate": float(part["outcome"].mean()),
                "calls": int(len(calls)),
                "call_hit_rate": float(calls.mean()) if len(calls) else np.nan,
                "brier": float(part["brier"].mean()),
            }
        )
    by_time = pd.DataFrame(time_rows, columns=time_columns)
    if not by_time.empty:
        by_time = by_time.sort_values("minutes_from_end").reset_index(drop=True)

    clipped = usable["win_chance"].clip(lower=0, upper=0.999999)
    usable["band_low"] = np.floor(clipped * 10) / 10
    per_band = usable.groupby(["band_low", "fight"], as_index=False).agg(
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
                "fights": int(part["fight"].nunique()),
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


def prediction_report(scored, by_time, by_price):
    lines = ["How often the price called the winner", ""]
    lines.append(
        "The win chance on a five-minute row is that row's mid. A mid of 0.70 is a 70% chance "
        "the charted fighter wins. A call predicts a win above 0.5 and a loss below 0.5. "
        "A mid of exactly 0.5 is not a call."
    )
    lines.append(
        "One fight has one result, so an hour spent at the same price is twelve rows and still one fight. "
        "The tables count each fight once inside a five-minute mark or a price band."
    )
    lines.append("")
    if scored is None or scored.empty or "call_correct" not in scored.columns:
        lines.append("No settled fight had a five-minute mid.")
        return "\n".join(lines) + "\n"
    calls = scored["call_correct"].dropna()
    brier = scored["brier"].dropna()
    fights = fight_key(scored.dropna(subset=["outcome"])).nunique() if "outcome" in scored else 0
    if calls.empty:
        lines.append("No settled quote was away from 0.5, so there is no call to score.")
    else:
        lines.append(
            f"Bin-level calls: {calls.mean():.1%} of {len(calls)} quotes matched the final result "
            f"({fights} fights). This counts a long quote path more than once."
        )
    if not brier.empty:
        lines.append(
            f"Bin-level Brier score: {brier.mean():.3f}. "
            "This is the average of (win chance − outcome) squared. A coin flip scores 0.250."
        )
    if by_time is not None and not by_time.empty:
        rated = by_time.dropna(subset=["call_hit_rate"])
        if not rated.empty:
            lines.append(
                f"Across five-minute marks, the average call hit rate is {rated['call_hit_rate'].mean():.1%}. "
                "Each mark counts once, so a fight quoted all night does not dominate this average."
            )
    lines.append("")
    lines.append("Price bands. A calibrated market wins about as often as its price.")
    if by_price is None or by_price.empty:
        lines.append("No band had a settled quote.")
    else:
        for _, row in by_price.iterrows():
            fights = int(row["fights"])
            bins = int(row["bins"])
            fight_word = "fight" if fights == 1 else "fights"
            bin_word = "bin" if bins == 1 else "bins"
            lines.append(
                f"  {row['band']}: price {row['mean_win_chance']:.2f}, "
                f"won {row['actual_win_rate']:.0%} of {fights} {fight_word} "
                f"({bins} {bin_word})."
            )
    return "\n".join(lines) + "\n"


def write_prediction(paths, summary):
    """Score every bin and write the by-time and by-price tables."""
    scored = score_predictions(paths, summary)
    by_time, by_price = accuracy_tables(scored)
    by_time.to_csv(OUTPUT / "accuracy_by_time.csv", index=False)
    by_price.to_csv(OUTPUT / "accuracy_by_price.csv", index=False)
    text = prediction_report(scored, by_time, by_price)
    (OUTPUT / "prediction_report.txt").write_text(text, encoding="utf-8")
    print(text, flush=True)
    return scored


def panels_from_summary(summary):
    panels = []
    for _, row in summary.sort_values("rank").iterrows():
        panels.append(
            {
                "rank": int(row["rank"]),
                "name": row["fighter"],
                "record": row.get("opponent") or "",
            }
        )
    return panels


def grid_for(count, sharey=False):
    count = max(count, 1)
    cols = 2 if count <= 10 else 4
    rows = (count + cols - 1) // cols
    fig, axes = plt.subplots(
        rows,
        cols,
        figsize=(5.6 * cols, 2.6 * rows),
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
        if index // cols == (count - 1) // cols:
            axes[index].set_xlabel(xlabel)
        if index % cols == 0:
            axes[index].set_ylabel(ylabel)
    fig.suptitle(title, fontsize=13)
    fig.tight_layout()
    fig.savefig(OUTPUT / filename, dpi=140)
    plt.close(fig)


def heading(panel):
    opponent = panel.get("record") or ""
    if opponent:
        return f"{panel['rank']}. {panel['name']} vs {opponent}"
    return f"{panel['rank']}. {panel['name']}"


def draw_empty(axis, panel):
    axis.text(0.5, 0.5, "No quotes in the card window", ha="center", va="center", transform=axis.transAxes)
    axis.set_title(heading(panel), loc="left", fontsize=8)


def paths_by_rank(paths):
    if paths is None or paths.empty:
        return {}
    return {int(rank): part for rank, part in paths.groupby("rank")}


def plot_prices(paths):
    by_rank = paths_by_rank(paths)
    fig, axes, count, cols = grid_for(len(PANELS), sharey=True)
    for axis, panel in zip(axes, PANELS):
        frame = by_rank.get(panel["rank"])
        if frame is None or frame.empty:
            draw_empty(axis, panel)
            axis.set_xlim(-HOURS_BEFORE, HOURS_AFTER)
            axis.set_ylim(0, 1)
        else:
            axis.plot(frame["hours_from_end"], frame["mid"], color="#1f4e79", linewidth=1.4)
            axis.axvline(0, color="#b0b0b0", linewidth=0.8)
            axis.set_ylim(0, 1)
            title = frame["label"].iloc[0] if "label" in frame else heading(panel)
            axis.set_title(f"{panel['rank']}. {title}", loc="left", fontsize=8)
        axis.grid(axis="y", color="#eeeeee")
    finish_grid(
        fig,
        axes,
        count,
        cols,
        "Hours from listed market end",
        "Win price",
        "Charted fighter's win price across the card",
        "price_paths.png",
    )


def plot_profit(paths):
    by_rank = paths_by_rank(paths)
    fig, axes, count, cols = grid_for(len(PANELS), sharey=True)
    for axis, panel in zip(axes, PANELS):
        frame = by_rank.get(panel["rank"])
        if frame is None or frame.empty or "profit_to_close" not in frame:
            draw_empty(axis, panel)
        else:
            axis.plot(frame["hours_from_end"], frame["profit_to_close"], color="#1f4e79", linewidth=1.4)
            axis.axvline(0, color="#b0b0b0", linewidth=0.8)
            axis.axhline(0, color="#b0b0b0", linewidth=0.8)
            axis.set_ylim(-1, 1)
            axis.set_title(heading(panel), loc="left", fontsize=8)
        axis.grid(axis="y", color="#eeeeee")
    finish_grid(
        fig,
        axes,
        count,
        cols,
        "Hours from listed market end",
        "Profit per share",
        "Profit of buying the mid and holding to settlement",
        "profit_to_close.png",
    )


def plot_widths(paths):
    by_rank = paths_by_rank(paths)
    fig, axes, count, cols = grid_for(len(PANELS), sharey=False)
    for axis, panel in zip(axes, PANELS):
        frame = by_rank.get(panel["rank"])
        if frame is None or frame.empty or "spread_width" not in frame:
            draw_empty(axis, panel)
        else:
            axis.plot(frame["hours_from_end"], frame["spread_width"], color="#1f4e79", linewidth=1.2)
            axis.axvline(0, color="#b0b0b0", linewidth=0.8)
            axis.axhline(0, color="#b0b0b0", linewidth=0.8)
            axis.set_title(heading(panel), loc="left", fontsize=8)
        axis.grid(axis="y", color="#eeeeee")
    finish_grid(
        fig,
        axes,
        count,
        cols,
        "Hours from listed market end",
        "Ask minus bid",
        "Bid-ask gap. A flat line below zero for a long stretch can be a stale book",
        "bid_ask_width.png",
    )


def plot_orders(paths, orders):
    by_rank = paths_by_rank(paths)
    grouped = {}
    if orders is not None and not orders.empty:
        orders = nba.drop_far_orders(orders)
        if "hours_from_end" in orders:
            orders["hours_from_end"] = pd.to_numeric(orders["hours_from_end"], errors="coerce")
        if "price_followed" in orders:
            orders["price_followed"] = nba.flag_true(orders["price_followed"])
        grouped = {int(rank): part for rank, part in orders.groupby("rank")}
    fig, axes, count, cols = grid_for(len(PANELS))
    for axis, panel in zip(axes, PANELS):
        frame = by_rank.get(panel["rank"])
        if frame is None or frame.empty:
            draw_empty(axis, panel)
            continue
        axis.plot(frame["hours_from_end"], frame["mid"], color="#d0d0d0", linewidth=1.2)
        axis.axvline(0, color="#b0b0b0", linewidth=0.8)
        axis.set_ylim(0, 1)
        part = grouped.get(panel["rank"])
        if part is not None and not part.empty:
            if len(part) > 60 and "size_ratio" in part:
                part = part.nlargest(60, "size_ratio")
            followed = part[part["price_followed"] == True] if "price_followed" in part else part.iloc[0:0]  # noqa: E712
            quiet = part.drop(index=followed.index) if not followed.empty else part
            axis.scatter(quiet["hours_from_end"], quiet["price"], s=18, color="#1f4e79", zorder=3)
            if not followed.empty:
                axis.scatter(followed["hours_from_end"], followed["price"], s=28, color="#d9762c", zorder=4)
        axis.set_title(heading(panel), loc="left", fontsize=8)
        axis.grid(axis="y", color="#eeeeee")
    finish_grid(
        fig,
        axes,
        count,
        cols,
        "Hours from listed market end",
        "Price",
        "Large orders within 10 cents of the mid. Orange then moved that way",
        "large_orders.png",
    )


def plot_variance(summary):
    frame = summary.copy()
    for column in ("early_std", "late_std"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce") if column in frame else pd.NA
    fig, axis = plt.subplots(figsize=(max(10, 0.55 * max(len(frame), 1)), 5))
    usable = frame.dropna(subset=["early_std", "late_std"], how="all")
    if usable.empty:
        axis.text(0.5, 0.5, "Not enough quotes to measure variance", ha="center")
    else:
        positions = range(len(usable))
        width = 0.38
        axis.bar(
            [p - width / 2 for p in positions],
            usable["early_std"],
            width=width,
            label="Earlier on the card",
            color="#1f4e79",
        )
        axis.bar(
            [p + width / 2 for p in positions],
            usable["late_std"],
            width=width,
            label="Last 2 hours before the listed end",
            color="#d9762c",
        )
        axis.set_xticks(list(positions))
        axis.set_xticklabels(usable["fighter"], rotation=30, ha="right")
        axis.set_ylabel("Standard deviation of the 5-minute mid")
        axis.legend()
    axis.set_title("Win-price variance across the card window")
    fig.tight_layout()
    fig.savefig(OUTPUT / "variance.png", dpi=140)
    plt.close(fig)


def plot_calibration(summary):
    frame = summary.copy()
    frame["card_price"] = pd.to_numeric(frame["card_price"], errors="coerce")
    frame = frame.dropna(subset=["card_price", "won"])
    fig, axis = plt.subplots(figsize=(6.5, 6))
    axis.plot([0, 1], [0, 1], color="#b0b0b0", linewidth=1)
    if frame.empty:
        axis.text(0.5, 0.5, "No settled fight with a saved price", ha="center")
    else:
        wins = frame[frame["won"] == True]  # noqa: E712
        losses = frame[frame["won"] == False]  # noqa: E712
        axis.scatter(wins["card_price"], [1] * len(wins), color="#1f4e79", label="Fighter won", zorder=3)
        axis.scatter(losses["card_price"], [0] * len(losses), color="#d9762c", label="Fighter lost", zorder=3)
        for _, row in frame.iterrows():
            axis.annotate(
                row["fighter"],
                (row["card_price"], 1 if row["won"] else 0),
                textcoords="offset points",
                xytext=(4, 4),
                fontsize=8,
            )
        axis.legend()
    axis.set_xlim(0, 1)
    axis.set_ylim(-0.1, 1.1)
    axis.set_xlabel("Earliest mid in the card window")
    axis.set_ylabel("Charted fighter won")
    axis.set_title("Price near the start of the card against the result")
    fig.tight_layout()
    fig.savefig(OUTPUT / "calibration.png", dpi=140)
    plt.close(fig)


def plot_prediction(paths):
    """Call hit rate through the card, and win rate against the price band."""
    by_time, by_price = accuracy_tables(paths)
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.4))
    if by_time.empty:
        axes[0].text(0.5, 0.5, "No settled quotes", ha="center", va="center")
    else:
        axes[0].plot(by_time["hours_from_end"], by_time["mean_win_chance"], color="#1f4e79", marker="o", linewidth=1.3, label="Average win chance")
        axes[0].plot(by_time["hours_from_end"], by_time["actual_win_rate"], color="#d9762c", marker="o", linewidth=1.3, label="Fights actually won")
        axes[0].plot(by_time["hours_from_end"], by_time["call_hit_rate"], color="#2f6f4e", marker="o", linewidth=1.2, label="Favorite call was right")
        axes[0].legend(fontsize=8)
    axes[0].axvline(0, color="#b0b0b0", linewidth=0.8)
    axes[0].set_ylim(-0.05, 1.05)
    axes[0].set_xlabel("Hours from listed market end")
    axes[0].set_ylabel("Share")
    axes[0].set_title("At each five-minute mark")
    axes[0].grid(axis="y", color="#eeeeee")

    axes[1].plot([0, 1], [0, 1], color="#b0b0b0", linewidth=1)
    if by_price.empty:
        axes[1].text(0.5, 0.5, "No settled quotes", ha="center", va="center")
    else:
        axes[1].scatter(by_price["mean_win_chance"], by_price["actual_win_rate"], s=40 + by_price["fights"] * 6, color="#1f4e79", zorder=3)
        for _, row in by_price.iterrows():
            above = row["actual_win_rate"] > 0.85
            axes[1].annotate(
                f"{row['band']}  n={int(row['fights'])}",
                (row["mean_win_chance"], row["actual_win_rate"]),
                textcoords="offset points",
                xytext=(6, -12 if above else 4),
                fontsize=7,
            )
    axes[1].set_xlim(0, 1)
    axes[1].set_ylim(0, 1)
    axes[1].set_xlabel("Average win chance in the band")
    axes[1].set_ylabel("Share of fights won")
    axes[1].set_title("Does a 70% price win 70% of the time?")
    fig.tight_layout()
    fig.savefig(OUTPUT / "prediction_accuracy.png", dpi=140)
    plt.close(fig)


def draw_all(summary, paths, orders):
    plot_prices(paths)
    plot_profit(paths)
    plot_widths(paths)
    plot_orders(paths, orders)
    plot_variance(summary)
    plot_calibration(summary)
    plot_prediction(paths)


def write_dictionary():
    lines = [
        "# UFC output columns",
        "",
        "Written by `examples/ufc_analysis.py`. Prices are on a 0 to 1 scale.",
        "The clock is hours from the listed market end, which is shared by the card and is not the bell.",
        "",
    ]
    for name in (
        "summary.csv",
        "price_paths.csv",
        "side_markets.csv",
        "large_orders.csv",
        "accuracy_by_time.csv",
        "accuracy_by_price.csv",
    ):
        path = OUTPUT / name
        if not path.exists():
            continue
        try:
            header = list(pd.read_csv(path, nrows=0).columns)
        except pd.errors.EmptyDataError:
            header = []
        lines.append(f"## `{name}`")
        lines.append("")
        for column in header:
            meaning = COLUMN_MEANING.get((name, column), "Copied from the fight row or the five-minute book.")
            lines.append(f"- `{column}`: {meaning}")
        lines.append("")
    (OUTPUT / "DATA_DICTIONARY.md").write_text("\n".join(lines), encoding="utf-8")


def redraw():
    global PANELS
    summary_path = OUTPUT / "summary.csv"
    path_path = OUTPUT / "price_paths.csv"
    if not summary_path.exists() or not path_path.exists():
        raise SystemExit(f"Nothing to redraw. Run python examples/ufc_analysis.py first. Expected files in {OUTPUT}.")
    summary = pd.read_csv(summary_path)
    paths = pd.read_csv(path_path)
    if "hours_from_tip" in paths.columns and "hours_from_end" not in paths.columns:
        paths = paths.rename(columns={"hours_from_tip": "hours_from_end"})
    if not paths.empty and "rank" in paths.columns:
        rebuilt = []
        for rank, part in paths.groupby("rank"):
            won = None
            match = summary[summary["rank"] == rank]
            if not match.empty:
                won = match.iloc[0]["won"]
            rebuilt.append(nba.attach_close_profit(part, won))
        paths = pd.concat(rebuilt, ignore_index=True)
        if "hours_from_tip" in paths.columns and "hours_from_end" not in paths.columns:
            paths = paths.rename(columns={"hours_from_tip": "hours_from_end"})
    paths = write_prediction(paths, summary)
    paths.to_csv(path_path, index=False)
    orders = pd.read_csv(OUTPUT / "large_orders.csv") if (OUTPUT / "large_orders.csv").exists() else pd.DataFrame()
    PANELS = panels_from_summary(summary)
    draw_all(summary, paths, orders)
    write_dictionary()
    print(f"Rewrote charts in {OUTPUT}")


def parse_find(text):
    value = (text or f"top{DEFAULT_TOP}").strip().lower()
    match = re.fullmatch(r"top(\d+)", value)
    if match is None:
        raise SystemExit("--find must be topN, for example --find top10")
    count = int(match.group(1))
    if not 1 <= count <= MAX_FIND:
        raise SystemExit(f"--find topN must be from 1 to {MAX_FIND}")
    return count


def main():
    parser = argparse.ArgumentParser(description="UFC fight prices from the class order-book files.")
    parser.add_argument(
        "--find",
        default=f"top{DEFAULT_TOP}",
        help="topN. The default, top10, keeps fights with matched volume of at least 100 and a busy order book.",
    )
    parser.add_argument(
        "--plots-only",
        action="store_true",
        help="Redraw the charts from the CSVs in outputs/ufc. Does not download hourly files.",
    )
    args = parser.parse_args()
    if args.plots_only:
        redraw()
        return
    print(
        "Reading whole hourly Parquet files from the class CloudFront host. "
        "A full run reads the hours around each UFC card, and that host logs each file. "
        "Fight results are a few small requests to ESPN, not to that host.",
        flush=True,
    )
    analyze(parse_find(args.find))


if __name__ == "__main__":
    main()
