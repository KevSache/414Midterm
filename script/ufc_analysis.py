"""Price history for UFC fight-winner markets, before and during each fight.

    python script/ufc_analysis.py              # query the class data and ESPN, then the analysis
    python script/ufc_analysis.py --from-csv   # redo the analysis from outputs/ufc/ without querying

The UFC counterpart of nba_analysis.py: how does the fight-winner price behave
before and during a fight, and does its in-fight volatility reflect how the
fight went? It compares the price variance in the PRE_HOURS before the opening
bell with the variance during the fight, writes a variance score on every
one-minute bin (the standard deviation of the mid over the trailing
VARIANCE_WINDOW minutes), times when the price is decided (first at 0.90 or
0.10), lines the price up with ESPN's timed knockdowns, takedowns, and
submission attempts, checks the pre-fight price against the result, and counts
large posted sizes that the price then followed, and those that came just
before a fight event.

Fights are minutes long, not hours, so the NBA script's 5-minute bins, 30-minute
variance window, and 30-minute follow window become 1, 5, and 5 minutes here,
and "early" and "late" are round 1 and the rounds after it. How the fight went
stands in for the final margin: whether it ended in round 1, later, or on the
cards, and the winner's share of significant strikes.

Only two cards have UFC rows in the hourly order-book files: June 6 (Muhammad
vs. Bonfim) and June 14 (Freedom 250). The May 30 card ended before the first
file, and files from June 20 on carry no UFC markets. The charted price is the
first-named fighter in each winner market, from that market's outcome-0 book;
outcome 1's rows mirror it. The files carry order placements and cancels but
not trades, so a resting level that traded away is never removed; when a new
order crosses it, the replay clears it.

ESPN's play-by-play gives each bout's opening bell, round ends, the final bell,
and every knockdown, takedown, and submission attempt with a wall-clock time.
Freedom 250 ran up to two and a half hours behind its listed times, so the
listed start is not used.

A querying run reads the class credentials from POLYMARKET_DATABASE_URL,
POLYMARKET_CLOUDFRONT_USER, and POLYMARKET_CLOUDFRONT_PASSWORD, set in the shell
or in a gitignored .env at the repo root; --from-csv needs none. It reads 20
hourly files remotely, about 10 GB, which took about 15 minutes at one point
and would take about two hours at the 1.6 MB/s the class CloudFront host served
at another. CloudFront logs every byte under the shared login.
"""

import argparse
import base64
import json
import os
import re
import time
import unicodedata
import urllib.request
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

import duckdb
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import psycopg2

BUCKET = "https://d1yyqbluk0oz0t.cloudfront.net"
SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/mma/ufc/scoreboard?dates={day}"
FIGHTCENTER = "https://site.web.api.espn.com/apis/common/v3/sports/mma/ufc/fightcenter/{event}?region=us&lang=en"
PLAYS = "https://sports.core.api.espn.com/v2/sports/mma/leagues/ufc/events/{event}/competitions/{comp}/plays?limit=300"
OUTPUT = Path("outputs/ufc")
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
CARDS = ("2026-06-06", "2026-06-14")
PRE_HOURS = 3
# The files start mid-stream with no opening snapshot; these first minutes of each market only build the book.
WARMUP_MIN = 30
GRID_SECONDS = 10
# Trailing window of the variance score, in one-minute bins.
VARIANCE_WINDOW = 5
DECIDED_HIGH, DECIDED_LOW = 0.90, 0.10
FIGHT_EVENTS = ("Knockdown", "Takedown", "Submission Attempt")
# A price move measured over this long after a fight event, against every other in-fight window.
EVENT_MOVE_SECONDS = 60
LARGE_SIZE_MULT = 10
REPEAT_SECONDS = 10
FOLLOW_MINUTES = 5
FOLLOW_MOVE = 0.03
# Prices sit on a one-cent grid, so moves of exactly FOLLOW_MOVE and mids of exactly 0.90 are common;
# comparisons allow for floating-point error so those ties count.
TOLERANCE = 1e-9
LED_AFTER_SECONDS = 15
LED_BEFORE_SECONDS = 120
NAME_MATCH = 0.8
ENDINGS = ["R1 finish", "later finish", "decision"]
ENDING_COLORS = {"R1 finish": "tab:red", "later finish": "tab:orange", "decision": "tab:blue"}
EVENT_STYLE = {"Knockdown": ("tab:red", "v"), "Takedown": ("tab:blue", "s"), "Submission Attempt": ("tab:green", "D")}

MARKETS_SQL = """
SELECT m.slug, m.title, m.token_ids, m.outcomes, m.volume_clob::float8 AS volume_clob
FROM markets m
WHERE m.slug = m.event_slug
  AND m.slug LIKE 'ufc-%%'
  AND substring(m.slug from '[0-9]{4}-[0-9]{2}-[0-9]{2}') = ANY(%s)
ORDER BY m.slug
"""


def log(message):
    print(f"[{datetime.now():%H:%M:%S}] {message}", flush=True)


def require(name):
    """Return a credential from the shell, or from the repo's .env when the shell does not set it."""
    if name not in os.environ and ENV_FILE.is_file():
        for line in ENV_FILE.read_text().splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and not key.startswith("#"):
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"{name} is not set. Export it or put it in {ENV_FILE} (gitignored).")
    return value


def fetch_json(url):
    request = urllib.request.Request(url, headers={"User-Agent": "stat414-ufc"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def fold(text):
    """Lowercase ASCII form of a name, so Cháirez and Chairez compare equal."""
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z ]+", "", text.lower()).strip()


def name_similarity(a, b):
    return SequenceMatcher(None, fold(a), fold(b)).ratio()


# ---------------------------------------------------------------- ESPN


def first_number(text):
    match = re.match(r"\s*(\d+)", str(text or ""))
    return int(match.group(1)) if match else np.nan


def load_espn(cards):
    """Bouts (result, timing, strike totals) and timed fight events from ESPN, for each card day."""
    bouts, events = [], []
    for day in cards:
        scoreboard = fetch_json(SCOREBOARD.format(day=day.replace("-", "")))
        for event in scoreboard.get("events", []):
            center = fetch_json(FIGHTCENTER.format(event=event["id"]))
            details = {}
            for card in center.get("cards", {}).values():
                for comp in card.get("competitions", []):
                    stats = {
                        f["athlete"]["displayName"]: {s["name"]: s.get("displayValue") for s in f.get("stats", [])}
                        for f in comp["competitors"]
                    }
                    method = (comp["status"].get("result") or {}).get("displayName")
                    details[comp["id"]] = (card.get("displayName"), method, stats)
            for order, comp in enumerate(event["competitions"]):
                fighters = [f["athlete"]["displayName"] for f in comp["competitors"]]
                if len(fighters) != 2:
                    continue
                bout_id = f"{day}#{order:02d}"
                segment, method, stats = details.get(comp["id"], (None, None, {}))
                plays = fetch_json(PLAYS.format(event=event["id"], comp=comp["id"])).get("items", [])
                for play in plays:
                    events.append({
                        "bout_id": bout_id,
                        "time": pd.Timestamp(play["wallclock"]),
                        "type": play["type"]["text"],
                        "round": (play.get("period") or {}).get("number"),
                        "clock_s": (play.get("clock") or {}).get("value"),
                    })
                status = comp["status"]
                bouts.append({
                    "bout_id": bout_id,
                    "card_day": day,
                    "espn_event": event["name"],
                    "bout_order": order,
                    "segment": segment,
                    "fighter_a": fighters[0],
                    "fighter_b": fighters[1],
                    "winner": next((f["athlete"]["displayName"] for f in comp["competitors"] if f.get("winner")), None),
                    "method": method,
                    "end_round": status.get("period"),
                    "end_clock_s": float(status.get("clock") or np.nan),
                    "sig_a": first_number(stats.get(fighters[0], {}).get("sigStrikes")),
                    "sig_b": first_number(stats.get(fighters[1], {}).get("sigStrikes")),
                })
    if not bouts:
        raise SystemExit(f"ESPN returned no bouts for {', '.join(cards)}.")
    bouts, events = pd.DataFrame(bouts), pd.DataFrame(events)
    return add_timing(bouts, events), events


def add_timing(bouts, events):
    """Opening bell, end of round 1, and final bell of each bout, from the play-by-play."""
    def first(kind, rounds=None):
        rows = events[events["type"] == kind]
        if rounds is not None:
            rows = rows[rows["round"] == rounds]
        return rows.groupby("bout_id")["time"].min()

    bouts = bouts.copy()
    bouts["start"] = bouts["bout_id"].map(first("Round Start", 1))
    bouts["end"] = bouts["bout_id"].map(first("Fight Over"))
    round_one_end = bouts["bout_id"].map(first("Round End", 1))
    bouts["round1_end"] = round_one_end.where(round_one_end.notna() & (round_one_end < bouts["end"]), bouts["end"])
    bouts["main_event"] = bouts["bout_order"] == bouts.groupby("card_day")["bout_order"].transform("max")
    return bouts


# ---------------------------------------------------------------- markets


def load_markets(cards, bouts):
    """Winner market of each ESPN bout, matched by both fighters' names."""
    con = psycopg2.connect(require("POLYMARKET_DATABASE_URL"))
    con.set_session(readonly=True, autocommit=True)
    try:
        markets = pd.read_sql(MARKETS_SQL, con, params=(list(cards),))
    finally:
        con.close()
    markets["card_day"] = markets["slug"].str.extract(r"(\d{4}-\d{2}-\d{2})", expand=False)
    rows = []
    for market in markets.itertuples():
        if len(market.outcomes) != 2:
            continue
        first, second = market.outcomes
        best, best_score = None, 0.0
        for bout in bouts[bouts["card_day"] == market.card_day].itertuples():
            score = max(
                min(name_similarity(first, bout.fighter_a), name_similarity(second, bout.fighter_b)),
                min(name_similarity(first, bout.fighter_b), name_similarity(second, bout.fighter_a)),
            )
            if score > best_score:
                best, best_score = bout.bout_id, score
        # Both names must match, so a bout that changed opponents stays unlinked.
        if best_score >= NAME_MATCH:
            rows.append({"slug": market.slug, "bout_id": best, "name_match": round(best_score, 3)})
    linked = markets.merge(pd.DataFrame(rows), on="slug")
    linked["fighter"] = linked["outcomes"].str[0]
    linked["opponent"] = linked["outcomes"].str[1]
    linked["asset_id"] = linked["token_ids"].str[0]
    linked["token"] = np.arange(len(linked), dtype=np.int32)
    return linked.drop(columns=["token_ids", "outcomes"])


# ---------------------------------------------------------------- order book


def connect_duckdb():
    user = require("POLYMARKET_CLOUDFRONT_USER")
    password = require("POLYMARKET_CLOUDFRONT_PASSWORD")
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute(f"""
        CREATE SECRET cf (
            TYPE HTTP,
            EXTRA_HTTP_HEADERS MAP {{'Authorization': 'Basic {token}'}}
        );
    """)
    con.execute("SET threads=4;")
    con.execute("SET enable_external_file_cache=false;")
    return con


def query_hours(bouts):
    """Every hour from PRE_HOURS plus the warm-up before each card's first bell to its last final bell."""
    hours = set()
    for _, card in bouts.dropna(subset=["start", "end"]).groupby("card_day"):
        start = (card["start"].min() - pd.Timedelta(hours=PRE_HOURS, minutes=WARMUP_MIN)).floor("h")
        hours.update(pd.date_range(start, card["end"].max().floor("h"), freq="h"))
    return sorted(hours)


def query_hour(duck, url, number, attempts=4):
    """One hour's winner-market rows. Network errors are retried with backoff; a 403 (no such file) is not."""
    for attempt in range(1, attempts + 1):
        try:
            return duck.execute(f"""
                SELECT p.ts_ms, t.token, p.side = 'BUY' AS is_buy, p.price, p.size,
                       {number} AS hour, p.file_row_number AS row
                FROM read_parquet('{url}', file_row_number = true) AS p
                JOIN ufc_tokens AS t USING (asset_id)
            """).df()
        except duckdb.Error as exc:
            if getattr(exc, "status_code", None) == 403 or attempt == attempts:
                raise
            wait = 30 * 2 ** (attempt - 1)
            log(f"{url.rsplit('/', 1)[1]}: {exc}; retrying in {wait}s ({attempt}/{attempts - 1})")
            time.sleep(wait)


def query_book(duck, markets, hours):
    """Outcome-0 rows of every winner market, read remotely. Row order within a millisecond is kept."""
    duck.register("ufc_tokens", markets[["asset_id", "token"]])
    # A window is a run of consecutive hours; the book restarts after a gap.
    windows = np.cumsum([0] + [later - earlier > pd.Timedelta(hours=1) for earlier, later in zip(hours, hours[1:])])
    frames = []
    for number, hour in enumerate(hours):
        url = f"{BUCKET}/events-{hour:%Y-%m-%dT%H}.parquet"
        started = time.time()
        try:
            frame = query_hour(duck, url, number)
        except duckdb.HTTPException as exc:
            if getattr(exc, "status_code", None) != 403:
                raise
            log(f"{url.rsplit('/', 1)[1]} is not in the bucket; skipped")
            continue
        log(f"{hour:%Y-%m-%d %H}:00 UTC ({number + 1}/{len(hours)}): {len(frame):,} rows in {time.time() - started:.0f}s")
        frame["window"] = windows[number]
        frames.append(frame)
    if not frames:
        raise SystemExit("No hourly file in the query window was readable.")
    return pd.concat(frames, ignore_index=True).sort_values(["token", "window", "ts_ms", "hour", "row"], kind="stable")


def replay_quotes(book):
    """Best bid and ask of each market after every update, once the book has warmed up.

    A row is the new total size at one price level. A new bid at or above a
    resting ask (or a new ask at or below a resting bid) means that level
    traded without an update in the files, so it is removed. The book restarts
    at every gap between queried hours, and the first WARMUP_MIN minutes after
    a restart only build it.
    """
    token = book["token"].to_numpy()
    window = book["window"].to_numpy()
    stamp = book["ts_ms"].to_numpy()
    is_buy = book["is_buy"].to_numpy()
    price = book["price"].to_numpy()
    size = book["size"].to_numpy()
    quotes = []
    n = len(book)
    start = 0
    while start < n:
        end = start
        while end < n and token[end] == token[start] and window[end] == window[start]:
            end += 1
        bids, asks = {}, {}
        best_bid = best_ask = np.nan
        warm_at = stamp[start] + WARMUP_MIN * 60_000
        i = start
        while i < end:
            j = i
            while j < end and stamp[j] == stamp[i]:
                p, s, buy = price[j], size[j], is_buy[j]
                levels = bids if buy else asks
                if s > 0:
                    levels[p] = s
                else:
                    levels.pop(p, None)
                if buy:
                    if s > 0 and not p <= best_bid:
                        best_bid = p
                    elif s <= 0 and p == best_bid:
                        best_bid = max(bids) if bids else np.nan
                else:
                    if s > 0 and not p >= best_ask:
                        best_ask = p
                    elif s <= 0 and p == best_ask:
                        best_ask = min(asks) if asks else np.nan
                if s > 0 and (p >= best_ask if buy else p <= best_bid):
                    opposite = asks if buy else bids
                    for level in [q for q in opposite if (q <= p if buy else q >= p)]:
                        del opposite[level]
                    if buy:
                        best_ask = min(asks) if asks else np.nan
                    else:
                        best_bid = max(bids) if bids else np.nan
                j += 1
            if stamp[i] >= warm_at:
                quotes.append((token[i], window[i], stamp[i], best_bid, best_ask))
            i = j
        start = end
    return pd.DataFrame(quotes, columns=["token", "window", "ts_ms", "bid", "ask"])


def quote_grid(quotes):
    """The book's best bid and ask at the end of every GRID_SECONDS bin, carried forward within a window.

    Each bin keeps the last update's whole state, empty sides included, and a
    bin with no update repeats the bin before it.
    """
    quotes = quotes.assign(time=pd.to_datetime(quotes["ts_ms"], unit="ms", utc=True).dt.floor(f"{GRID_SECONDS}s"))
    grids = []
    for (token, window), group in quotes.groupby(["token", "window"]):
        last = group.drop_duplicates("time", keep="last").set_index("time")[["bid", "ask"]]
        bins = pd.date_range(last.index.min(), last.index.max(), freq=f"{GRID_SECONDS}s")
        grids.append(last.reindex(bins, method="ffill").rename_axis("time").assign(token=token, window=window).reset_index())
    return pd.concat(grids, ignore_index=True)


def large_posts(book):
    """Posted sizes at least LARGE_SIZE_MULT times the median on that market, side, and hour.

    The same as nba_analysis.py: a posted size is a row with size above 0,
    the median is over that market's rows on that side in that clock hour, and
    a repeat of the same size at the same price within REPEAT_SECONDS is
    dropped.
    """
    posted = book[book["size"] > 0][["token", "ts_ms", "is_buy", "price", "size"]].copy()
    posted["clock_hour"] = posted["ts_ms"] // 3_600_000
    posted["median"] = posted.groupby(["token", "is_buy", "clock_hour"])["size"].transform("median")
    large = posted[posted["size"] >= LARGE_SIZE_MULT * posted["median"]].sort_values(["token", "is_buy", "price", "size", "ts_ms"])
    gap = large.groupby(["token", "is_buy", "price", "size"])["ts_ms"].diff()
    large = large[gap.isna() | (gap > REPEAT_SECONDS * 1000)]
    large["side"] = np.where(large["is_buy"], "BUY", "SELL")
    return large[["token", "ts_ms", "side", "price", "size", "median"]].sort_values(["token", "ts_ms"]).reset_index(drop=True)


# ---------------------------------------------------------------- analysis


def in_fight_events(events, bouts):
    """Knockdowns, takedowns, and submission attempts between the opening and final bells.

    ESPN occasionally logs one after the final bell (Chandler vs. Ruffy has a
    knockdown four minutes after it); those are dropped. A play logged a few
    seconds after the bell still counts.
    """
    events = events.merge(bouts[["bout_id", "start", "end"]], on="bout_id")
    inside = (events["time"] >= events["start"]) & (events["time"] <= events["end"] + pd.Timedelta(seconds=30))
    keep = ~events["type"].isin(FIGHT_EVENTS) | inside
    return events[keep].drop(columns=["start", "end"]).reset_index(drop=True)


def fights_frame(markets, bouts, events):
    """One row per winner market with an order book: the bout, its result, and how the fight went."""
    fights = markets.merge(bouts, on=["bout_id", "card_day"])
    fights["won"] = [
        name_similarity(f, w) > name_similarity(o, w) if isinstance(w, str) else np.nan
        for f, o, w in zip(fights["fighter"], fights["opponent"], fights["winner"])
    ]
    decision = fights["method"].fillna("").str.startswith("Decision")
    fights["ending"] = np.where(decision, "decision", np.where(fights["end_round"] == 1, "R1 finish", "later finish"))
    winner_is_a = [name_similarity(w, a) > name_similarity(w, b) if isinstance(w, str) else np.nan
                   for w, a, b in zip(fights["winner"], fights["fighter_a"], fights["fighter_b"])]
    winner_sig = np.where(winner_is_a, fights["sig_a"], fights["sig_b"])
    fights["winner_sig_share"] = winner_sig / (fights["sig_a"] + fights["sig_b"])
    fights["fight_minutes"] = (fights["end"] - fights["start"]).dt.total_seconds() / 60
    counts = events[events["type"].isin(FIGHT_EVENTS)].pivot_table(index="bout_id", columns="type", values="time", aggfunc="size")
    counts = counts.reindex(columns=list(FIGHT_EVENTS), fill_value=0).fillna(0).astype(int)
    counts.columns = ["knockdowns", "takedowns", "submission_attempts"]
    return fights.merge(counts, left_on="bout_id", right_index=True, how="left").fillna(
        {"knockdowns": 0, "takedowns": 0, "submission_attempts": 0}
    )


def fight_path(grid, fight):
    """The fight's GRID_SECONDS quotes and mid from PRE_HOURS before the bell to five minutes after the final bell."""
    path = grid[(grid["token"] == fight.token)
                & (grid["time"] >= fight.start - pd.Timedelta(hours=PRE_HOURS))
                & (grid["time"] <= fight.end + pd.Timedelta(minutes=5))].copy()
    two = path["bid"].notna() & path["ask"].notna() & (path["ask"] > path["bid"])
    # As in nba_analysis.py: the average of bid and ask, or the one side that is posted.
    path["mid"] = np.where(two, (path["bid"] + path["ask"]) / 2, path["bid"].fillna(path["ask"]))
    path.loc[path["bid"].notna() & path["ask"].notna() & ~two, "mid"] = np.nan
    path["spread"] = np.where(two, path["ask"] - path["bid"], np.nan)
    path["minutes_from_start"] = (path["time"] - fight.start).dt.total_seconds() / 60
    return path


def minute_bins(path, fight):
    """Last quote in each minute, with the variance score over the trailing VARIANCE_WINDOW minutes."""
    bins = path.set_index("time")[["mid", "spread"]].resample("1min").last()
    bins["variance_score"] = bins["mid"].rolling(VARIANCE_WINDOW, min_periods=3).std()
    bins = bins.reset_index().rename(columns={"time": "minute"})
    bins["minutes_from_start"] = (bins["minute"] - fight.start).dt.total_seconds() / 60
    bins["phase"] = np.select(
        [bins["minute"] < fight.start, bins["minute"] <= fight.round1_end, bins["minute"] <= fight.end],
        ["pre-fight", "round 1", "later rounds"], "after",
    )
    bins.insert(0, "bout_id", fight.bout_id)
    return bins


def summarize(fight, path, bins):
    """One summary row: price variance before and during the fight, timing, and quote width."""
    pre = bins[bins["phase"] == "pre-fight"]["mid"].dropna()
    live = bins[bins["phase"].isin(["round 1", "later rounds"])]
    before = path[path["time"] < fight.start].dropna(subset=["mid"])
    during = path[(path["time"] >= fight.start) & (path["time"] <= fight.end)]
    # A finish is priced at the stoppage, so the search runs through the saved minutes after the final bell.
    after_bell = path[path["time"] >= fight.start]
    decided = after_bell[(after_bell["mid"] >= DECIDED_HIGH - TOLERANCE) | (after_bell["mid"] <= DECIDED_LOW + TOLERANCE)]
    last_hour = path[(path["time"] >= fight.start - pd.Timedelta(hours=1)) & (path["time"] < fight.start)]

    def score(phase):
        values = bins.loc[bins["phase"].isin(phase), "variance_score"].dropna()
        return values.mean() if len(values) else np.nan

    pre_price = before["mid"].iloc[-1] if len(before) else np.nan
    return {
        "pregame_std": pre.std() if len(pre) >= 3 else np.nan,
        "in_fight_std": live["mid"].dropna().std() if live["mid"].notna().sum() >= 3 else np.nan,
        "pregame_n": len(pre),
        "in_fight_n": int(live["mid"].notna().sum()),
        "pre_fight_price": pre_price,
        "upset": bool(pre_price < 0.5) == bool(fight.won) if pd.notna(pre_price) and pd.notna(fight.won) else np.nan,
        "variance_score_pregame": score(["pre-fight"]),
        "variance_score_round1": score(["round 1"]),
        "variance_score_later": score(["later rounds"]),
        "variance_score_live": score(["round 1", "later rounds"]),
        "minutes_to_decided": decided["minutes_from_start"].iloc[0] if len(decided) else np.nan,
        "bid_ask_pregame": last_hour["spread"].mean(),
        "bid_ask_live": during["spread"].mean(),
    }


def event_moves(grid, fights, events):
    """Absolute mid change over EVENT_MOVE_SECONDS after each fight event, and over every other in-fight window."""
    rows = []
    steps = EVENT_MOVE_SECONDS // GRID_SECONDS
    for fight in fights.itertuples():
        path = fight_path(grid, fight).set_index("time")["mid"]
        live = path[(path.index >= fight.start) & (path.index <= fight.end)]
        if live.notna().sum() < steps + 1:
            continue
        change = (live.shift(-steps) - live).abs()
        marks = events[(events["bout_id"] == fight.bout_id) & events["type"].isin(FIGHT_EVENTS)]
        near_event = pd.Series(False, index=live.index)
        for event in marks.itertuples():
            at = event.time.floor(f"{GRID_SECONDS}s")
            before = path[path.index < at].dropna()
            after = path[(path.index >= at + pd.Timedelta(seconds=EVENT_MOVE_SECONDS))].dropna()
            if len(before) and len(after):
                rows.append({"bout_id": fight.bout_id, "type": event.type, "time": event.time,
                             "move": abs(after.iloc[0] - before.iloc[-1])})
            near_event |= (live.index >= at - pd.Timedelta(seconds=EVENT_MOVE_SECONDS)) & (
                live.index <= at + pd.Timedelta(seconds=EVENT_MOVE_SECONDS))
        for when, move in change[~near_event].dropna().items():
            rows.append({"bout_id": fight.bout_id, "type": "no event", "time": when, "move": move})
    return pd.DataFrame(rows)


def score_large_orders(posts, grid, fights, events):
    """Mark large posted sizes the price followed, and those that also came just before a fight event."""
    rows = []
    for fight in fights.itertuples():
        orders = posts[(posts["token"] == fight.token)].copy()
        orders["time"] = pd.to_datetime(orders["ts_ms"], unit="ms", utc=True)
        orders = orders[(orders["time"] >= fight.start - pd.Timedelta(hours=PRE_HOURS)) & (orders["time"] <= fight.end)]
        if orders.empty:
            continue
        path = fight_path(grid, fight).set_index("time")["mid"].dropna()
        marks = events[(events["bout_id"] == fight.bout_id) & events["type"].isin(FIGHT_EVENTS)]["time"].sort_values()
        for order in orders.itertuples():
            now = path[path.index <= order.time]
            later = path[path.index <= order.time + pd.Timedelta(minutes=FOLLOW_MINUTES)]
            move = later.iloc[-1] - now.iloc[-1] if len(now) and len(later) else np.nan
            toward = move if order.side == "BUY" else -move
            followed = bool(toward >= FOLLOW_MOVE - TOLERANCE)
            # The same-size move the other way, as a baseline the NBA script does not have.
            against = bool(toward <= -FOLLOW_MOVE + TOLERANCE)
            previous = marks[marks < order.time]
            following = marks[marks >= order.time]
            led = (followed and order.time >= fight.start
                   and (previous.empty or (order.time - previous.iloc[-1]).total_seconds() >= LED_AFTER_SECONDS)
                   and not following.empty and (following.iloc[0] - order.time).total_seconds() <= LED_BEFORE_SECONDS)
            rows.append({
                "bout_id": fight.bout_id, "fighter": fight.fighter, "time": order.time, "side": order.side,
                "price": order.price, "size": order.size, "median_size": order.median,
                "in_fight": order.time >= fight.start, "move_after": move, "followed": followed, "against": against,
                "led_event": led,
            })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- charts


def save(fig, name):
    fig.tight_layout()
    fig.savefig(OUTPUT / name, dpi=150)
    plt.close(fig)


def fight_label(fight):
    return f"{fight.fighter} vs. {fight.opponent}"


def plot_price_paths(fights, paths, events):
    columns = 3
    rows = int(np.ceil(len(fights) / columns))
    fig, axes = plt.subplots(rows, columns, figsize=(15, 2.6 * rows), sharey=True)
    for ax, fight in zip(axes.flat, fights.itertuples()):
        path = paths[fight.bout_id]
        ax.plot(path["minutes_from_start"], path["mid"], lw=0.9, color="black")
        ax.axvspan(0, fight.fight_minutes, color=ENDING_COLORS[fight.ending], alpha=0.12)
        for kind, (color, marker) in EVENT_STYLE.items():
            marks = events[(events["bout_id"] == fight.bout_id) & (events["type"] == kind)]
            for when in marks["time"]:
                ax.axvline((when - fight.start).total_seconds() / 60, color=color, lw=0.8, ls=":")
        ax.set_xlim(-30, fight.fight_minutes + 3)
        ax.set_ylim(0, 1)
        result = "won" if fight.won else "lost"
        ax.set_title(f"{fight_label(fight)}\n{fight.fighter.split()[-1]} {result}, {fight.method}, R{int(fight.end_round)}", fontsize=8)
    for ax in axes.flat[len(fights):]:
        ax.axis("off")
    for ax in axes[:, 0]:
        ax.set_ylabel("Price of first-named fighter", fontsize=8)
    for ax in axes[-1, :]:
        ax.set_xlabel("Minutes from the opening bell", fontsize=8)
    handles = [plt.Line2D([], [], color=c, ls=":", label=k) for k, (c, _) in EVENT_STYLE.items()]
    handles += [plt.Rectangle((0, 0), 1, 1, color=ENDING_COLORS[e], alpha=0.3, label=f"fight ({e})") for e in ENDINGS]
    fig.legend(handles=handles, loc="upper center", ncol=6, fontsize=8, bbox_to_anchor=(0.5, 1.0))
    fig.suptitle("Winner-market price from 30 minutes before the bell to the end of the fight", y=1.02)
    save(fig, "price_paths.png")


def plot_variance(summary):
    data = summary.dropna(subset=["pregame_std"]).sort_values("in_fight_std", na_position="first")
    fig, ax = plt.subplots(figsize=(9, 0.38 * len(data) + 1.5))
    y = np.arange(len(data))
    ax.hlines(y, data["pregame_std"], data["in_fight_std"], color="lightgrey")
    ax.scatter(data["pregame_std"], y, color="tab:grey", label=f"{PRE_HOURS} hours before the bell", zorder=3)
    ax.scatter(data["in_fight_std"], y, c=[ENDING_COLORS[e] for e in data["ending"]], label="during the fight", zorder=3)
    ax.set_yticks(y, [f"{f} vs. {o}" for f, o in zip(data["fighter"], data["opponent"])], fontsize=8)
    ax.set_xscale("log")
    ax.set_xlabel("Standard deviation of the one-minute mid (log scale)")
    ax.set_title("Price variance before and during each fight")
    handles = [plt.Line2D([], [], marker="o", ls="", color="tab:grey", label=f"{PRE_HOURS} h before the bell")]
    handles += [plt.Line2D([], [], marker="o", ls="", color=ENDING_COLORS[e], label=f"in fight, {e}") for e in ENDINGS]
    ax.legend(handles=handles, fontsize=8, loc="lower right")
    save(fig, "variance.png")


def plot_variance_score(fights, bins):
    fig, ax = plt.subplots(figsize=(10, 4.5))
    for fight in fights.itertuples():
        path = bins[(bins["bout_id"] == fight.bout_id) & (bins["minutes_from_start"].between(-60, fight.fight_minutes + 1))]
        ax.plot(path["minutes_from_start"], path["variance_score"], color=ENDING_COLORS[fight.ending], alpha=0.6, lw=1)
    ax.axvline(0, color="black", lw=0.8, ls="--")
    ax.set_xlim(-60, 32)
    ax.set_yscale("symlog", linthresh=0.005)
    ax.set_ylim(0, None)
    ax.set_xlabel("Minutes from the opening bell")
    ax.set_ylabel(f"Variance score: std of the mid over the prior {VARIANCE_WINDOW} minutes")
    ax.set_title("Variance score through the last hour and the fight")
    ax.legend(handles=[plt.Line2D([], [], color=ENDING_COLORS[e], label=e) for e in ENDINGS], fontsize=8)
    save(fig, "variance_score.png")


def plot_variance_vs_outcome(summary):
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.5))
    rng = np.random.default_rng(414)
    data = summary.dropna(subset=["variance_score_live"])
    for i, ending in enumerate(ENDINGS):
        part = data[data["ending"] == ending]
        axes[0].scatter(i + rng.uniform(-0.12, 0.12, len(part)), part["variance_score_live"], color=ENDING_COLORS[ending])
        if len(part):
            axes[0].hlines(part["variance_score_live"].mean(), i - 0.25, i + 0.25, color="black")
    axes[0].set_xticks(range(len(ENDINGS)), [f"{e}\n(n={(data['ending'] == e).sum()})" for e in ENDINGS])
    axes[0].set_ylabel("Mean in-fight variance score")
    axes[0].set_title("By how the fight ended (line = mean)", fontsize=10)
    for ax, column, label in ((axes[1], "winner_sig_share", "Winner's share of significant strikes"),
                              (axes[2], "fight_minutes", "Fight length, bell to bell (minutes)")):
        ax.scatter(data[column], data["variance_score_live"], c=[ENDING_COLORS[e] for e in data["ending"]])
        ax.set_xlabel(label)
        ax.set_ylabel("Mean in-fight variance score")
    axes[1].set_title("Against how one-sided the fight was", fontsize=10)
    axes[2].set_title("Against how long it lasted", fontsize=10)
    fig.suptitle("Does in-fight price volatility reflect how the fight went?")
    save(fig, "variance_vs_outcome.png")


def plot_event_moves(moves):
    order = [*FIGHT_EVENTS, "no event"]
    rng = np.random.default_rng(414)
    stats = []
    for kind in order:
        values = moves.loc[moves["type"] == kind, "move"].dropna().to_numpy()
        if len(values) == 0:
            stats.append((kind, np.nan, np.nan, np.nan, 0))
            continue
        draws = [rng.choice(values, len(values)).mean() for _ in range(1000)]
        stats.append((kind, values.mean(), *np.quantile(draws, [0.025, 0.975]), len(values)))
    stats = pd.DataFrame(stats, columns=["type", "mean", "low", "high", "n"])
    fig, ax = plt.subplots(figsize=(8, 4.5))
    colors = [EVENT_STYLE.get(k, ("tab:grey", None))[0] for k in stats["type"]]
    ax.bar(range(len(stats)), stats["mean"] * 100, color=colors)
    ax.errorbar(range(len(stats)), stats["mean"] * 100, yerr=[(stats["mean"] - stats["low"]).clip(lower=0) * 100,
                (stats["high"] - stats["mean"]).clip(lower=0) * 100], fmt="none", color="black", capsize=3)
    ax.set_xticks(range(len(stats)), [f"{t}\n(n={n})" for t, n in zip(stats["type"], stats["n"])])
    ax.set_ylabel(f"Mean absolute mid change over {EVENT_MOVE_SECONDS} s (cents)")
    ax.set_title("How much the price moves after each kind of fight event (95% bootstrap CI)")
    save(fig, "event_moves.png")
    return stats


def wilson(wins, n, z=1.96):
    """Wilson 95% interval for a proportion."""
    rate = wins / n
    centre = (rate + z * z / (2 * n)) / (1 + z * z / n)
    half = z * np.sqrt(rate * (1 - rate) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return centre - half, centre + half


def plot_calibration(summary):
    """Pre-fight price against the result, from the favorite's side, with win rates by price band."""
    data = summary.dropna(subset=["pre_fight_price", "won"]).copy()
    first_favored = data["pre_fight_price"] >= 0.5
    data["favorite_price"] = np.where(first_favored, data["pre_fight_price"], 1 - data["pre_fight_price"])
    data["favorite_won"] = np.where(first_favored, data["won"].astype(bool), ~data["won"].astype(bool)).astype(float)
    data["favorite"] = np.where(first_favored, data["fighter"], data["opponent"])
    brier = ((data["pre_fight_price"] - data["won"].astype(float)) ** 2).mean()
    fig, ax = plt.subplots(figsize=(7, 5))
    ax.plot([0.5, 1], [0.5, 1], color="grey", ls="--", label="perfect calibration")
    jitter = np.random.default_rng(414).uniform(-0.025, 0.025, len(data))
    ax.scatter(data["favorite_price"], data["favorite_won"] + jitter, color="tab:grey", alpha=0.6, s=25, label="one fight")
    for row in data[data["favorite_won"] == 0].itertuples():
        ax.annotate(f"{row.favorite.split()[-1]} lost", (row.favorite_price, 0), textcoords="offset points",
                    xytext=(0, 8), fontsize=7, ha="center", rotation=45)
    edges = [0.5, 0.6, 0.7, 0.8, 1.0]
    for low, high in zip(edges, edges[1:]):
        band = data[(data["favorite_price"] >= low) & (data["favorite_price"] < high)]
        if band.empty:
            continue
        wins, n = band["favorite_won"].sum(), len(band)
        lower, upper = wilson(wins, n)
        x = band["favorite_price"].mean()
        ax.errorbar(x, wins / n, yerr=[[wins / n - lower], [upper - wins / n]], fmt="o", color="tab:blue", capsize=3, ms=7)
        ax.annotate(f"{int(wins)}/{n}", (x, wins / n), textcoords="offset points", xytext=(8, -3), fontsize=8, color="tab:blue")
    ax.plot([], [], "o", color="tab:blue", label="win rate in price band (Wilson 95% CI)")
    ax.set_xlim(0.48, 1.0)
    ax.set_ylim(-0.1, 1.1)
    ax.set_xlabel("Favorite's last price before the opening bell")
    ax.set_ylabel("Favorite won")
    ax.set_title(f"Favorites won {int(data['favorite_won'].sum())} of {len(data)} at a mean price of "
                 f"{data['favorite_price'].mean():.2f}; Brier {brier:.3f} (0.25 for a coin flip)", fontsize=9)
    ax.legend(fontsize=8, loc="center right")
    save(fig, "calibration.png")
    return brier


def plot_bid_ask(summary):
    data = summary.dropna(subset=["bid_ask_pregame"]).sort_values("bid_ask_pregame")
    fig, ax = plt.subplots(figsize=(9, 0.38 * len(data) + 1.5))
    y = np.arange(len(data))
    ax.hlines(y, data["bid_ask_pregame"] * 100, data["bid_ask_live"] * 100, color="lightgrey")
    ax.scatter(data["bid_ask_pregame"] * 100, y, color="tab:grey", label="hour before the bell", zorder=3)
    ax.scatter(data["bid_ask_live"] * 100, y, color="tab:red", label="during the fight", zorder=3)
    ax.set_yticks(y, [f"{f} vs. {o}" for f, o in zip(data["fighter"], data["opponent"])], fontsize=8)
    ax.set_xlim(0, None)
    ax.set_xlabel("Mean ask minus bid (cents)")
    ax.set_title("Bid-ask width before and during each fight")
    ax.legend(fontsize=8)
    save(fig, "bid_ask_width.png")


def plot_large_orders(summary):
    data = summary.sort_values("large_n", ascending=False)
    fig, ax = plt.subplots(figsize=(11, 4.5))
    x = np.arange(len(data))
    bars = (("large_n", "large posted sizes", "tab:grey"),
            ("followed_n", f"price moved {FOLLOW_MOVE:g} toward it within {FOLLOW_MINUTES} min", "tab:blue"),
            ("against_n", "moved as far against it (baseline)", "tab:orange"),
            ("led_event_n", "followed and led a fight event", "tab:red"))
    for offset, (column, label, color) in zip((-0.3, -0.1, 0.1, 0.3), bars):
        ax.bar(x + offset, data[column], width=0.2, label=label, color=color)
    ax.set_xticks(x, [f.split()[-1] + "-" + o.split()[-1] for f, o in zip(data["fighter"], data["opponent"])], rotation=60, fontsize=7)
    ax.set_yscale("symlog", linthresh=10)
    ax.set_ylabel("Count (symlog scale)")
    totals = data[["large_n", "followed_n", "against_n"]].sum()
    ax.set_title(f"Large posted sizes (at least {LARGE_SIZE_MULT}x the hour's median): price followed "
                 f"{totals['followed_n'] / totals['large_n']:.0%}, moved as far against {totals['against_n'] / totals['large_n']:.0%}")
    ax.legend(fontsize=8)
    save(fig, "large_orders.png")


# ---------------------------------------------------------------- outputs


def write_dictionary():
    (OUTPUT / "DATA_DICTIONARY.md").write_text(f"""# UFC output data dictionary

Written by `script/ufc_analysis.py`. Times are UTC. Prices are 0 to 1. A blank cell is a missing value.
The charted price is the first-named fighter in each winner market (`fighter`); `opponent` is the other.
The opening bell, round ends, and final bell come from ESPN's play-by-play, not the listed start.

## `summary.csv`
One row per fight whose winner market has an order book.

| Column | Meaning |
| --- | --- |
| `bout_id` | ESPN card day and bout order. Joins every other file. |
| `fighter`, `opponent` | Charted fighter and the other. |
| `slug`, `title`, `volume_clob` | Winner market slug, title, and stored lifetime matched volume. |
| `segment`, `main_event` | Card segment from ESPN, and whether it was the last bout. |
| `start`, `round1_end`, `end` | Opening bell, end of round 1 (the final bell if it ended in round 1), and final bell. |
| `won` | True when the charted fighter won (ESPN). |
| `method`, `end_round`, `ending` | ESPN result, the round it ended in, and `R1 finish`, `later finish`, or `decision`. |
| `sig_a`, `sig_b`, `winner_sig_share` | Significant strikes landed by ESPN's two fighters, and the winner's share. |
| `fight_minutes` | Bell to bell, including rests between rounds. |
| `knockdowns`, `takedowns`, `submission_attempts` | ESPN play-by-play counts. |
| `pregame_std` | Std of the one-minute mid over the {PRE_HOURS} hours before the bell. Blank under 3 bins. |
| `in_fight_std` | Std of the one-minute mid from the opening to the final bell. Blank under 3 bins. |
| `pregame_n`, `in_fight_n` | One-minute bins with a two-sided quote before and during the fight. |
| `pre_fight_price` | Last mid before the opening bell. |
| `upset` | True when the fighter priced under 0.5 won, or the one over 0.5 lost. |
| `variance_score_pregame` | Mean variance score over the {PRE_HOURS} hours before the bell. |
| `variance_score_round1`, `variance_score_later`, `variance_score_live` | Mean variance score in round 1, after round 1, and over the whole fight. |
| `minutes_to_decided` | Minutes after the opening bell when the mid first reached {DECIDED_HIGH} or {DECIDED_LOW}, searching through 5 minutes after the final bell. Blank if it never did. |
| `bid_ask_pregame`, `bid_ask_live` | Mean ask minus bid over the hour before the bell, and during the fight. |
| `large_n`, `followed_n`, `against_n`, `led_event_n` | Large posted sizes in the {PRE_HOURS} hours before and during the fight, how many the price followed, how many it moved as far against (a baseline the NBA script does not have), and how many also led a fight event (see `large_orders.csv`). |

## `price_paths.csv`
One row per fight and minute, from {PRE_HOURS} hours before the bell to 5 minutes after the final bell: `mid` (the
average of bid and ask, or the one side posted, as in nba_analysis.py) and `spread` (two-sided only) at the minute's end, `variance_score` (std of the mid over the trailing {VARIANCE_WINDOW} minutes, blank
under 3 bins), `minutes_from_start`, and `phase` (pre-fight, round 1, later rounds, after).

## `event_moves.csv`
One row per knockdown, takedown, or submission attempt with quotes around it, and per in-fight window away from any
event (`type` = no event): `move` is the absolute change in the mid over the {EVENT_MOVE_SECONDS} seconds after it.

## `large_orders.csv`
One row per large posted size in the {PRE_HOURS} hours before or during a fight: a row with size at least
{LARGE_SIZE_MULT} times the median size on that market and side in that clock hour, with a repeat of the same size at
the same price within {REPEAT_SECONDS} seconds dropped. `side` is BUY (a bid for the charted fighter) or SELL.
`move_after` is the mid change over the next {FOLLOW_MINUTES} minutes; `followed` is true when it was at least
{FOLLOW_MOVE} in the order's direction, and `against` when it was at least that far the other way; `led_event` is true when the order also came during the fight, at least
{LED_AFTER_SECONDS} seconds after the previous fight event, and within {LED_BEFORE_SECONDS} seconds before the next one.
There is no wallet on any row.

## Saved by a querying run (`--from-csv` starts here)
`markets.csv` (linked winner markets and their `token` number), `bouts.csv` (ESPN results, strike totals, and bells),
`fight_events.csv` (every ESPN play with its wall-clock `time`, `type`, `round`, and `clock_s`), `quotes.csv.gz` (best
`bid` and `ask` of each winner market at the end of every {GRID_SECONDS}-second bin, carried forward), and
`large_posts.csv` (the large posted sizes before scoring).
""")


def read_saved():
    def read(name, dates=()):
        path = OUTPUT / name
        if not path.exists():
            raise SystemExit(f"{path} is missing. Run once without --from-csv.")
        frame = pd.read_csv(path)
        for column in dates:
            frame[column] = pd.to_datetime(frame[column], utc=True)
        return frame

    return (
        read("markets.csv"),
        read("bouts.csv", ["start", "round1_end", "end"]),
        read("fight_events.csv", ["time"]),
        read("quotes.csv.gz", ["time"]),
        read("large_posts.csv"),
    )


def main():
    parser = argparse.ArgumentParser(description="Price history for UFC fight-winner markets.")
    parser.add_argument("--from-csv", action="store_true", help="Redo the analysis from outputs/ufc/ without querying.")
    parser.add_argument("--cards", nargs="+", default=list(CARDS), help="Card days (YYYY-MM-DD) to query.")
    args = parser.parse_args()
    OUTPUT.mkdir(parents=True, exist_ok=True)

    if not args.from_csv:
        bouts, events = load_espn(args.cards)
        markets = load_markets(args.cards, bouts)
        log(f"{len(bouts)} ESPN bouts, {len(markets)} winner markets linked")
        hours = query_hours(bouts)
        log(f"Querying {len(hours)} hourly files remotely")
        book = query_book(connect_duckdb(), markets, hours)
        log(f"Replaying {len(book):,} book rows")
        grid = quote_grid(replay_quotes(book))
        posts = large_posts(book)
        del book
        markets.drop(columns=["asset_id"]).to_csv(OUTPUT / "markets.csv", index=False)
        bouts.to_csv(OUTPUT / "bouts.csv", index=False)
        events.to_csv(OUTPUT / "fight_events.csv", index=False)
        grid.to_csv(OUTPUT / "quotes.csv.gz", index=False, float_format="%.6g")
        posts.to_csv(OUTPUT / "large_posts.csv", index=False, float_format="%.6g")

    # Both modes continue from the saved files, so --from-csv reproduces a querying run exactly.
    markets, bouts, events, grid, posts = read_saved()
    events = in_fight_events(events, bouts)
    fights = fights_frame(markets, bouts, events)
    fights = fights[fights["token"].isin(grid["token"].unique())].sort_values(["card_day", "bout_order"]).reset_index(drop=True)
    paths, all_bins, rows = {}, [], []
    for fight in fights.itertuples():
        path = fight_path(grid, fight)
        bins = minute_bins(path, fight)
        paths[fight.bout_id] = path
        all_bins.append(bins)
        rows.append({"bout_id": fight.bout_id, **summarize(fight, path, bins)})
    bins = pd.concat(all_bins, ignore_index=True)
    orders = score_large_orders(posts, grid, fights, events)
    counts = orders.groupby("bout_id").agg(large_n=("followed", "size"), followed_n=("followed", "sum"),
                                           against_n=("against", "sum"), led_event_n=("led_event", "sum"))
    summary = fights.merge(pd.DataFrame(rows), on="bout_id").merge(counts, left_on="bout_id", right_index=True, how="left")
    count_columns = ["large_n", "followed_n", "against_n", "led_event_n"]
    summary[count_columns] = summary[count_columns].fillna(0).astype(int)
    moves = event_moves(grid, fights, events)

    columns = [
        "bout_id", "fighter", "opponent", "slug", "title", "volume_clob", "segment", "main_event", "start", "round1_end",
        "end", "won", "method", "end_round", "ending", "sig_a", "sig_b", "winner_sig_share", "fight_minutes", "knockdowns",
        "takedowns", "submission_attempts", "pregame_std", "in_fight_std", "pregame_n", "in_fight_n", "pre_fight_price",
        "upset", "variance_score_pregame", "variance_score_round1", "variance_score_later", "variance_score_live",
        "minutes_to_decided", "bid_ask_pregame", "bid_ask_live", "large_n", "followed_n", "against_n", "led_event_n",
    ]
    summary[columns].to_csv(OUTPUT / "summary.csv", index=False, float_format="%.6g")
    bins.to_csv(OUTPUT / "price_paths.csv", index=False, float_format="%.6g")
    moves.to_csv(OUTPUT / "event_moves.csv", index=False, float_format="%.6g")
    orders.to_csv(OUTPUT / "large_orders.csv", index=False, float_format="%.6g")

    plot_price_paths(fights, paths, events)
    plot_variance(summary)
    plot_variance_score(fights, bins)
    plot_variance_vs_outcome(summary)
    move_stats = plot_event_moves(moves)
    brier = plot_calibration(summary)
    plot_bid_ask(summary)
    plot_large_orders(summary)
    write_dictionary()

    shown = ["fighter", "opponent", "ending", "pregame_std", "in_fight_std", "variance_score_live", "minutes_to_decided",
             "pre_fight_price", "won", "large_n", "followed_n", "led_event_n"]
    print(summary[shown].round(3).to_string(index=False))
    print(move_stats.round(4).to_string(index=False))
    log(f"{len(summary)} fights; Brier {brier:.3f}; wrote charts and tables to {OUTPUT}/")


if __name__ == "__main__":
    main()
