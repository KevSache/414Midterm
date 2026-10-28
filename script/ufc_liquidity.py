"""What drives liquidity in UFC prediction markets.

    python script/ufc_liquidity.py              # query the class data, then the analysis
    python script/ufc_liquidity.py --from-csv   # redo the analysis from outputs/ufc/ without querying

Needs statsmodels as well as requirements.txt (pip install statsmodels). A
querying run reads the class credentials from POLYMARKET_DATABASE_URL,
POLYMARKET_CLOUDFRONT_USER, and POLYMARKET_CLOUDFRONT_PASSWORD, set in the
shell or in a gitignored .env at the repo root; --from-csv needs none.

Only two cards have UFC rows in the hourly order-book files: June 6 (Muhammad
vs. Bonfim) and June 14 (Freedom 250). The May 30 card ended before the first
file, and the files from June 20 on track a few hundred markets and no UFC ones.

Order book. Each file row is the new total size at one price level. The files
carry order placements and cancels but not trades, so a trade shows up only
when a new order crosses a resting level that was never removed; the replay
clears the crossed levels and counts them as fills. Only each market's
outcome-0 book is replayed, since outcome 1's rows mirror it.

Bout timing. ESPN gives each card segment's start, the bout order, and how long
each fight lasted. A bout's start comes from its winner market: the first
one-minute price move of at least FIGHT_MOVE, or the time the price was decided
minus the fight's length, whichever is earlier. Freedom 250 ran up to two and a
half hours behind ESPN's times.

Liquidity. Over the PRE_MINUTES before each bout's start and the fight itself,
each market's book is measured minute by minute: whether a bid and an ask are
both posted, the spread, the dollars at the best bid and ask, and how many
updates of at least MIN_NOTIONAL the book took. A minute's state is the book
just before the next such update, which is the book at the end of that minute
unless a smaller update changed it in between. Two margins:

- Extensive: which markets have a live book at all, with Wilson intervals. A
  book that never changes never appears in the files, so "not live" means no
  update of at least MIN_NOTIONAL in that window.
- Intensive: among live markets, OLS of log spread, log depth, and log updates
  on time to the bell, the fight itself, market type, card position, price
  level, recent volatility, and the card, with standard errors clustered by
  bout (minutes of one fight share shocks and books are sticky), and again on
  winner markets alone so the few live props do not carry the time and card
  effects.

Market titles are parsed into a market type, and fighters are linked to ESPN
bouts by accent-folded fuzzy matching that requires both names to match, so a
bout that changed opponents is not linked to the replacement fight.

A querying run reads 20 hourly files remotely, about 10 GB. The files are sorted
by time, so a UFC filter cannot skip any part of a file; a run took about 15
minutes at one point and would take about two hours at the 1.6 MB/s the class
CloudFront host served at another. CloudFront logs every byte under the shared
login. Everything the analysis needs is saved to outputs/ufc/, so --from-csv
works without data access.
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
import matplotlib.ticker
import numpy as np
import pandas as pd
import psycopg2
import statsmodels.formula.api as smf
from statsmodels.stats.proportion import proportion_confint

BUCKET = "https://d1yyqbluk0oz0t.cloudfront.net"
ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/mma/ufc/scoreboard?dates={day}"
OUTPUT = Path("outputs/ufc")
CARDS = ("2026-06-06", "2026-06-14")
# Hours queried before a card's first ESPN segment.
HOURS_BEFORE = 3
# Minutes queried after the last bout's scheduled start, so its result is in the files.
# A card can run late: Freedom 250's last bouts started about two hours behind ESPN's time.
MINUTES_AFTER_LAST = 180
# The files start mid-stream with no opening snapshot; these first minutes of each market only build the book.
WARMUP_MIN = 30
ROUND_MINUTES = 5
# From one bout's end to the next bout's start: decision, walkouts, introductions.
BOUT_GAP_MIN = 15
# A winner market is decided once its mid passes this on the winning side.
DECIDED_PRICE = 0.97
# A one-minute mid move this large near the bell is live fighting, not pre-fight drift.
FIGHT_MOVE = 0.04
# A price start further than this from the schedule is treated as a false signal.
START_GUARD_MIN = 150
NAME_MATCH = 0.8
# Book updates smaller than this are not saved or counted.
MIN_NOTIONAL = 5.0
# Minutes before each bout's start that the liquidity analysis covers.
PRE_MINUTES = 180
PHASES = ["3h to 2h", "2h to 1h", "60 to 30m", "30 to 10m", "last 10m", "in fight"]
PHASE_EDGES = [-180, -120, -60, -30, -10, 0]
MARKET_TYPES = ["winner", "rounds_ou", "fighter_ko", "fight_ko", "fight_sub", "distance"]
COVARIATES = (
    "C(phase, Treatment('3h to 2h')) + C(market_type, Treatment('winner')) + main_card + main_event"
    " + freedom_250 + extremity + volatility"
)
OUTCOMES = {
    "log_spread": "Spread (cents)",
    "log_depth": "Dollars at the best bid and ask",
    "log_updates": "Book updates per minute",
}

MARKETS_SQL = """
SELECT m.slug, m.event_slug, m.title, m.token_ids, m.outcomes,
       m.volume_clob::float8 AS volume_clob, e.title AS event_title
FROM markets m
LEFT JOIN events e ON e.slug = m.event_slug
WHERE m.slug LIKE 'ufc-%%'
  AND substring(m.slug from '[0-9]{4}-[0-9]{2}-[0-9]{2}') = ANY(%s)
ORDER BY m.slug
"""

EVENT_TITLE = re.compile(r"^(?P<card>[^:]+): (?P<a>.+?) vs\. (?P<b>.+?) \((?P<weight>[^,]+), (?P<segment>[^)]+)\)$")
MARKET_RULES = (
    ("fighter_ko", re.compile(r"^Will .+ win by KO or TKO\?$")),
    ("fight_ko", re.compile(r"^Will the fight be won by KO or TKO\?$")),
    ("fight_sub", re.compile(r"^Will the fight be won by submission\?$")),
    ("distance", re.compile(r"^Fight to Go the Distance\?$")),
    ("rounds_ou", re.compile(r"^O/U \d+(?:\.\d+)? Rounds$")),
)


ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


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


# ---------------------------------------------------------------- markets and bouts


def load_markets(cards):
    """UFC markets on these card days, with their event title."""
    con = psycopg2.connect(require("POLYMARKET_DATABASE_URL"))
    con.set_session(readonly=True, autocommit=True)
    try:
        frame = pd.read_sql(MARKETS_SQL, con, params=(list(cards),))
    finally:
        con.close()
    frame["card_day"] = frame["slug"].str.extract(r"(\d{4}-\d{2}-\d{2})", expand=False)
    return frame


def fetch_json(url):
    request = urllib.request.Request(url, headers={"User-Agent": "stat414-ufc"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def load_espn(cards):
    """One row per ESPN bout: card segment start, order, fighters, winner, and when it ended."""
    rows = []
    for day in cards:
        data = fetch_json(ESPN_SCOREBOARD.format(day=day.replace("-", "")))
        for event in data.get("events", []):
            for order, comp in enumerate(event["competitions"]):
                fighters = comp["competitors"]
                if len(fighters) != 2:
                    continue
                status = comp["status"]
                rows.append({
                    "bout_id": f"{day}#{order:02d}",
                    "card_day": day,
                    "espn_event": event["name"],
                    "bout_order": order,
                    "segment_start": pd.Timestamp(comp["date"]),
                    "espn_a": fighters[0]["athlete"]["displayName"],
                    "espn_b": fighters[1]["athlete"]["displayName"],
                    "winner": next((f["athlete"]["displayName"] for f in fighters if f.get("winner")), None),
                    "end_round": status.get("period"),
                    "end_clock_s": status.get("clock"),
                })
    if not rows:
        raise SystemExit(f"ESPN returned no bouts for {', '.join(cards)}.")
    return schedule_bouts(pd.DataFrame(rows))


def schedule_bouts(bouts):
    """Add fight length and a scheduled start: bouts in a segment run back to back."""
    bouts = bouts.sort_values(["card_day", "bout_order"]).reset_index(drop=True)
    bouts["fight_minutes"] = (bouts["end_round"] - 1) * ROUND_MINUTES + bouts["end_clock_s"] / 60
    # One minute of rest between rounds.
    wall = (bouts["fight_minutes"] + bouts["end_round"] - 1).fillna(3 * ROUND_MINUTES + 2)
    starts = pd.Series(pd.NaT, index=bouts.index, dtype="datetime64[ns, UTC]")
    for _, segment in bouts.groupby(["card_day", "segment_start"]):
        clock = segment["segment_start"].iloc[0]
        for index in segment.index:
            starts[index] = clock
            clock = clock + pd.Timedelta(minutes=float(wall[index]) + BOUT_GAP_MIN)
    bouts["scheduled_start"] = starts
    bouts["wall_minutes"] = wall
    bouts["main_event"] = bouts["bout_order"] == bouts.groupby("card_day")["bout_order"].transform("max")
    return bouts


def fold(text):
    """Lowercase ASCII form of a name, so Cháirez and Chairez compare equal."""
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z ]+", "", text.lower()).strip()


def name_similarity(a, b):
    return SequenceMatcher(None, fold(a), fold(b)).ratio()


def market_type(row):
    if row["slug"] == row["event_slug"]:
        return "winner"
    for kind, pattern in MARKET_RULES:
        if pattern.match(row["title"]):
            return kind
    return "other"


def link_bouts(markets, bouts):
    """ESPN bout for each fight. Both fighters must match, so a changed bout stays unlinked."""
    rows = []
    for event in markets.drop_duplicates("event_slug").itertuples():
        if pd.isna(event.fighter_a):
            continue
        best, best_score = None, 0.0
        for bout in bouts[bouts["card_day"] == event.card_day].itertuples():
            score = max(
                min(name_similarity(event.fighter_a, bout.espn_a), name_similarity(event.fighter_b, bout.espn_b)),
                min(name_similarity(event.fighter_a, bout.espn_b), name_similarity(event.fighter_b, bout.espn_a)),
            )
            if score > best_score:
                best, best_score = bout.bout_id, score
        if best_score >= NAME_MATCH:
            rows.append({"event_slug": event.event_slug, "bout_id": best, "name_match": round(best_score, 3)})
    return pd.DataFrame(rows, columns=["event_slug", "bout_id", "name_match"])


def annotate_markets(markets, bouts):
    """Market type and card segment from the titles, and the linked ESPN bout."""
    markets = markets.copy()
    parsed = markets["event_title"].str.extract(EVENT_TITLE)
    markets["fighter_a"] = parsed["a"]
    markets["fighter_b"] = parsed["b"]
    markets["segment"] = parsed["segment"]
    markets["market_type"] = markets.apply(market_type, axis=1)
    markets = markets.merge(link_bouts(markets, bouts), on="event_slug", how="left")
    return markets.merge(bouts[["bout_id", "main_event"]], on="bout_id", how="left")


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


def first_tokens(markets):
    """Outcome-0 token of each market.

    Every update arrives twice, once per outcome: a bid of p on outcome 0 is
    the same order as an ask of 1 - p on outcome 1. Reading outcome 0 only
    counts each order once.
    """
    frame = pd.DataFrame({
        "asset_id": markets["token_ids"].map(lambda ids: ids[0] if len(ids) else None),
        "slug": markets["slug"],
    }).dropna()
    frame["token"] = np.arange(len(frame), dtype=np.int32)
    return frame.reset_index(drop=True)


def query_hours(bouts, hours_before):
    hours = set()
    for _, card in bouts.groupby("card_day"):
        start = (card["segment_start"].min() - pd.Timedelta(hours=hours_before)).floor("h")
        end = (card["scheduled_start"].max() + pd.Timedelta(minutes=MINUTES_AFTER_LAST)).floor("h")
        hours.update(pd.date_range(start, end, freq="h"))
    return sorted(hours)


def query_hour(duck, url, number, attempts=4):
    """One hour's UFC rows. Network errors are retried with backoff; a 403 (no such file) is not."""
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


def query_book(duck, tokens, hours):
    """UFC rows of each hourly file, read remotely. Row order within a millisecond is kept."""
    duck.register("ufc_tokens", tokens[["asset_id", "token"]])
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
        log(f"{hour:%Y-%m-%d %H}:00 UTC ({number + 1}/{len(hours)}): {len(frame):,} UFC rows in {time.time() - started:.0f}s")
        frame["window"] = windows[number]
        frames.append(frame)
    if not frames:
        raise SystemExit("No hourly file in the query window was readable.")
    return pd.concat(frames, ignore_index=True)


def replay(book):
    """Walk each market's outcome-0 book in time order; return (actions, quotes).

    An action is one of:

    post  size added at a price.
    pull  size removed at the best bid or ask: a cancel, or what was left of
          a level that traded earlier.
    fill  a trade the files never show. A new bid at or above a resting ask
          means that ask was bought (and a new ask at or below a resting bid
          means that bid was sold), so the crossed levels are removed and
          counted as fills.

    Rows that share a market, a millisecond, a kind, and a side are one
    action, so a sweep through several levels counts once. A fall away from
    the best price is a cancel and is skipped. Each action carries the book
    just before it.

    The book restarts at every gap between queried hours, and the first
    WARMUP_MIN minutes after a restart only build the book; no quote is kept
    from them. After that, a change counts only when the level's previous size
    is known, or when a new level improves the best price, so an old resting
    order seen for the first time is not new size.
    """
    book = book.sort_values(["token", "window", "ts_ms", "hour", "row"], kind="stable")
    token = book["token"].to_numpy()
    window = book["window"].to_numpy()
    stamp = book["ts_ms"].to_numpy()
    is_buy = book["is_buy"].to_numpy()
    price = book["price"].to_numpy()
    size = book["size"].to_numpy()
    actions, quotes = [], []
    n = len(book)
    start = 0
    while start < n:
        end = start
        while end < n and token[end] == token[start] and window[end] == window[start]:
            end += 1
        bids, asks = {}, {}
        known_bids, known_asks = set(), set()
        best_bid = best_ask = np.nan
        warm_at = stamp[start] + WARMUP_MIN * 60_000
        i = start
        while i < end:
            j = i
            while j < end and stamp[j] == stamp[i]:
                j += 1
            warm = stamp[i] >= warm_at
            bid0, ask0 = best_bid, best_ask
            bid_size0 = bids.get(bid0, 0.0)
            ask_size0 = asks.get(ask0, 0.0)
            grouped = {}

            def record(kind, buy_side, shares, entry):
                total = grouped.setdefault((kind, buy_side), [0.0, 0.0])
                total[0] += shares
                total[1] += shares * entry

            for r in range(i, j):
                p, s, buy = price[r], size[r], is_buy[r]
                levels, known = (bids, known_bids) if buy else (asks, known_asks)
                was_known = p in known
                known.add(p)
                delta = s - levels.get(p, 0.0)
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
                        filled = opposite.pop(level)
                        if warm:
                            record("fill", buy, filled, level if buy else 1 - level)
                    if buy:
                        best_ask = min(asks) if asks else np.nan
                    else:
                        best_bid = max(bids) if bids else np.nan
                if not warm or delta == 0:
                    continue
                improves = (np.isnan(bid0) or p > bid0) if buy else (np.isnan(ask0) or p < ask0)
                if delta > 0 and (was_known or improves):
                    kind = "post"
                elif delta < 0 and was_known and p == (bid0 if buy else ask0):
                    kind = "pull"
                else:
                    continue
                # Dollars behind the order: p per share for a bid, 1 - p for an ask.
                record(kind, buy, abs(delta), p if buy else 1 - p)
            for (kind, _), (shares, notional) in grouped.items():
                actions.append((token[i], window[i], stamp[i], kind, shares, notional, bid0, ask0, bid_size0, ask_size0))
            if warm:
                quotes.append((token[i], window[i], stamp[i], best_bid, best_ask))
            i = j
        start = end
    actions = pd.DataFrame(actions, columns=[
        "token", "window", "ts_ms", "kind", "shares", "notional", "bid", "ask", "bid_size", "ask_size",
    ])
    quotes = pd.DataFrame(quotes, columns=["token", "window", "ts_ms", "bid", "ask"])
    return actions, quotes


def minute_mids(quotes):
    """Last two-sided mid and spread of outcome 0 in each minute, carried forward within a window."""
    two_sided = quotes.dropna(subset=["bid", "ask"])
    two_sided = two_sided[two_sided["ask"] > two_sided["bid"]]
    frame = pd.DataFrame({
        "token": two_sided["token"].to_numpy(),
        "window": two_sided["window"].to_numpy(),
        "minute": pd.to_datetime(two_sided["ts_ms"].to_numpy(), unit="ms", utc=True).floor("min"),
        "mid": ((two_sided["bid"] + two_sided["ask"]) / 2).to_numpy(),
        "spread": (two_sided["ask"] - two_sided["bid"]).to_numpy(),
    })
    grids = []
    for (token, window), group in frame.groupby(["token", "window"]):
        series = group.groupby("minute")[["mid", "spread"]].last().resample("1min").last().ffill()
        series["token"] = token
        series["window"] = window
        grids.append(series.reset_index())
    return pd.concat(grids, ignore_index=True)


def locate_starts(bouts, markets, mids):
    """Bout start from the winner market, falling back to the schedule.

    Two price signals, and the earlier wins: the decided time (mid past
    DECIDED_PRICE on the ESPN winner's side) minus the fight's wall-clock
    length, and the first one-minute move of at least FIGHT_MOVE from an hour
    before the scheduled start. A decision is priced only once the cards are
    read, so the first signal alone runs late; a pre-fight jump that trips the
    second only makes the start earlier. The schedule is used when neither
    exists or the price start is more than START_GUARD_MIN minutes from it,
    raised to the previous bout's start plus its length when that is later: a
    bout cannot start before the one before it ends.
    """
    bouts = bouts.copy()
    winners = markets[(markets["market_type"] == "winner") & markets["bout_id"].notna()]
    starts = {}
    for row in winners.itertuples():
        bout = bouts.loc[bouts["bout_id"] == row.bout_id].iloc[0]
        if pd.isna(bout["winner"]):
            continue
        first, second = row.outcomes
        first_won = name_similarity(first, bout["winner"]) > name_similarity(second, bout["winner"])
        path = mids[(mids["token"] == row.token) & (mids["minute"] >= bout["scheduled_start"] - pd.Timedelta(minutes=60))]
        path = path[path["spread"] <= 0.05]
        hit = path[path["mid"] >= DECIDED_PRICE] if first_won else path[path["mid"] <= 1 - DECIDED_PRICE]
        moved = path[path["mid"].diff().abs() >= FIGHT_MOVE]
        candidates = []
        if len(hit):
            candidates.append(hit["minute"].iloc[0] - pd.Timedelta(minutes=float(bout["wall_minutes"]) + 1))
        if len(moved):
            candidates.append(moved["minute"].iloc[0] - pd.Timedelta(minutes=1))
        if candidates:
            starts[row.bout_id] = min(candidates)
    bouts["price_start"] = pd.to_datetime(bouts["bout_id"].map(starts), utc=True)
    gap = (bouts["price_start"] - bouts["scheduled_start"]).abs()
    use_price = bouts["price_start"].notna() & (gap <= pd.Timedelta(minutes=START_GUARD_MIN))
    bouts["bout_start"] = bouts["price_start"].where(use_price, bouts["scheduled_start"])
    bouts["start_source"] = np.where(use_price, "price", "schedule")
    for _, card in bouts.groupby("card_day"):
        previous = None
        for index in card.sort_values("bout_order").index:
            if previous is not None and bouts.at[index, "start_source"] == "schedule":
                floor = bouts.at[previous, "bout_start"] + pd.Timedelta(minutes=float(bouts.at[previous, "wall_minutes"]))
                if floor > bouts.at[index, "bout_start"]:
                    bouts.at[index, "bout_start"] = floor
                    bouts.at[index, "start_source"] = "previous bout"
            previous = index
    return bouts


# ---------------------------------------------------------------- liquidity


def window_actions(actions, linked):
    """Actions from PRE_MINUTES before each market's bout through the end of the fight."""
    actions = actions.merge(linked[["token", "bout_start", "wall_minutes"]], on="token")
    actions["ts"] = pd.to_datetime(actions["ts_ms"], unit="ms", utc=True)
    actions["rel"] = (actions["ts"] - actions["bout_start"]).dt.total_seconds() / 60
    return actions[(actions["rel"] >= -PRE_MINUTES) & (actions["rel"] < actions["wall_minutes"])].copy()


def market_activity(linked, actions):
    """One row per linked market: whether its book was live in the window, and the dollars that moved."""
    flows = actions.pivot_table(index="token", columns="kind", values="notional", aggfunc="sum", fill_value=0)
    flows = flows.reindex(columns=["post", "pull", "fill"], fill_value=0).add_suffix("_usd")
    frame = linked[["token", "slug", "bout_id", "card_day", "market_type", "segment", "main_event", "volume_clob"]]
    frame = frame.merge(flows, left_on="token", right_index=True, how="left").fillna(
        {"post_usd": 0, "pull_usd": 0, "fill_usd": 0}
    )
    frame["updates"] = frame["token"].map(actions.groupby("token").size()).fillna(0).astype(int)
    frame["live"] = frame["updates"] > 0
    return frame


def live_share(activity, by):
    """Share of markets with a live book, with a Wilson 95% interval, by one grouping column."""
    grouped = activity.groupby(by)["live"].agg(["sum", "size"])
    low, high = proportion_confint(grouped["sum"], grouped["size"], method="wilson")
    return grouped.assign(share=grouped["sum"] / grouped["size"], ci_low=low, ci_high=high).rename(
        columns={"sum": "live", "size": "markets"}
    )


def minute_panel(actions, linked):
    """One row per live market and minute: the book at the minute's end, and that minute's updates."""
    frames = []
    for token, group in actions.groupby("token"):
        group = group.sort_values("ts")
        minutes = pd.date_range(group["ts"].min().floor("min"), group["ts"].max().floor("min"), freq="min")
        minutes = minutes[minutes + pd.Timedelta(minutes=1) <= group["ts"].max()]
        if len(minutes) == 0:
            continue
        ends = pd.DataFrame({"end": (minutes + pd.Timedelta(minutes=1)).as_unit("ns")})
        states = group[["ts", "bid", "ask", "bid_size", "ask_size"]].assign(ts=group["ts"].dt.as_unit("ns"))
        frame = pd.merge_asof(ends, states, left_on="end", right_on="ts", direction="forward")
        frame["minute"] = minutes
        counts = group.groupby(group["ts"].dt.floor("min")).size()
        frame["updates"] = counts.reindex(minutes, fill_value=0).to_numpy()
        frame["token"] = token
        frames.append(frame.drop(columns=["end", "ts"]))
    panel = pd.concat(frames, ignore_index=True).merge(
        linked[["token", "slug", "bout_id", "card_day", "market_type", "segment", "main_event", "bout_start"]],
        on="token",
    )
    panel["rel"] = (panel["minute"] - panel["bout_start"]).dt.total_seconds() / 60
    panel["phase"] = pd.cut(panel["rel"], PHASE_EDGES + [np.inf], labels=PHASES, right=False).astype(str)
    panel["two_sided"] = panel["bid"].notna() & panel["ask"].notna() & (panel["ask"] > panel["bid"])
    two = panel["two_sided"]
    panel["spread_cents"] = np.where(two, (panel["ask"] - panel["bid"]) * 100, np.nan)
    panel["mid"] = np.where(two, (panel["bid"] + panel["ask"]) / 2, np.nan)
    panel["depth_usd"] = np.where(two, panel["bid"] * panel["bid_size"] + panel["ask"] * panel["ask_size"], np.nan)
    panel = panel.sort_values(["token", "minute"]).reset_index(drop=True)
    # Std of one-minute mid changes over the previous 30 minutes, in cents.
    panel["volatility"] = panel.groupby("token")["mid"].transform(
        lambda mid: mid.diff().rolling(30, min_periods=10).std().shift(1) * 100
    )
    panel["log_spread"] = np.log(panel["spread_cents"])
    panel["log_depth"] = np.log(panel["depth_usd"].where(panel["depth_usd"] > 0))
    panel["log_updates"] = np.log1p(panel["updates"])
    # Distance of the price from a coin flip, in 10-cent steps.
    panel["extremity"] = (panel["mid"] - 0.5).abs() * 10
    panel["main_card"] = (panel["segment"] == "Main Card").astype(float)
    panel["main_event"] = panel["main_event"].astype(float)
    panel["freedom_250"] = (panel["card_day"] == "2026-06-14").astype(float)
    return panel


def fit(panel, outcome, sample):
    """OLS of one log outcome on COVARIATES, with standard errors clustered by bout.

    sample "winner" refits on winner markets alone, without the market-type
    terms, so time and card effects are not carried by the few live props.
    """
    data = panel.dropna(subset=[outcome, "extremity", "volatility"])
    covariates = COVARIATES
    if sample == "winner":
        data = data[data["market_type"] == "winner"]
        covariates = COVARIATES.replace(" + C(market_type, Treatment('winner'))", "")
    model = smf.ols(f"{outcome} ~ {covariates}", data=data).fit(
        cov_type="cluster", cov_kwds={"groups": pd.factorize(data["bout_id"])[0]}
    )
    ci = model.conf_int()
    table = pd.DataFrame({
        "sample": sample,
        "outcome": outcome,
        "term": model.params.index,
        "coef": model.params.to_numpy(),
        "se": model.bse.to_numpy(),
        "p": model.pvalues.to_numpy(),
        "ci_low": ci[0].to_numpy(),
        "ci_high": ci[1].to_numpy(),
    })
    # Every outcome is a natural log, so exp(coef) - 1 is the percent change.
    for column in ("coef", "ci_low", "ci_high"):
        table[f"pct_{column}"] = 100 * np.expm1(table[column])
    table["term"] = table["term"].map(clean_term)
    return table, model, data


def clean_term(term):
    for prefix in ("C(phase, Treatment('3h to 2h'))[T.", "C(market_type, Treatment('winner'))[T."):
        if term.startswith(prefix):
            return term.removeprefix(prefix).rstrip("]")
    return term


def market_liquidity(panel, linked):
    """One row per live market: its pre-fight median spread and depth, mean updates, and lifetime volume."""
    pre = panel[panel["rel"] < 0]
    stats = pre.groupby("token").agg(
        minutes=("minute", "size"),
        spread_cents=("spread_cents", "median"),
        depth_usd=("depth_usd", "median"),
        updates=("updates", "mean"),
    )
    columns = ["token", "slug", "title", "fighter_a", "fighter_b", "market_type", "bout_id", "card_day", "main_event", "volume_clob"]
    return linked[columns].merge(stats, left_on="token", right_index=True)


def between_market_share(panel, column):
    """Share of the variance of column that lies between markets rather than within a market over time."""
    data = panel.dropna(subset=[column])
    means = data.groupby("token")[column].transform("mean")
    return float(((means - data[column].mean()) ** 2).sum() / ((data[column] - data[column].mean()) ** 2).sum())


# ---------------------------------------------------------------- charts


def save(fig, name):
    fig.tight_layout()
    fig.savefig(OUTPUT / name, dpi=150)
    plt.close(fig)


def plot_bout_starts(mids, linked, bouts):
    """Winner-market prices around the estimated start: flat before it, live swings after."""
    winners = linked[linked["market_type"] == "winner"].nlargest(6, "volume_clob")
    fig, axes = plt.subplots(2, 3, figsize=(14, 7), sharey=True)
    for ax, row in zip(axes.flat, winners.itertuples()):
        start = bouts.loc[bouts["bout_id"] == row.bout_id, "bout_start"].iloc[0]
        path = mids[mids["token"] == row.token]
        ax.plot((path["minute"] - start).dt.total_seconds() / 60, path["mid"], lw=1)
        ax.axvline(0, color="black", ls="--", lw=0.8)
        ax.set_xlim(-180, 60)
        ax.set_title(f"{row.fighter_a} vs. {row.fighter_b}", fontsize=9)
        ax.set_xlabel("Minutes from estimated start")
    for ax in axes[:, 0]:
        ax.set_ylabel("Mid price of the first-named fighter")
    fig.suptitle("Estimated bout starts: winner-market prices sit still before the line and swing after it")
    save(fig, "bout_starts.png")


def plot_live_share(by_type, by_segment):
    fig, (left, right) = plt.subplots(1, 2, figsize=(12, 4.5), gridspec_kw={"width_ratios": [3, 2]})
    for ax, table, title in ((left, by_type, "By market type"), (right, by_segment, "Props, by card position")):
        x = np.arange(len(table))
        ax.bar(x, table["share"] * 100, color="tab:blue")
        below = np.clip(table["share"] - table["ci_low"], 0, None) * 100
        above = np.clip(table["ci_high"] - table["share"], 0, None) * 100
        ax.errorbar(x, table["share"] * 100, yerr=[below, above], fmt="none", color="black", capsize=3)
        ax.set_xticks(x, table.index)
        for xi, row in zip(x, table.itertuples()):
            ax.annotate(f"{row.live}/{row.markets}", (xi, row.share * 100), textcoords="offset points",
                        xytext=(0, 4), ha="center", fontsize=8)
        ax.set_ylim(0, 110)
        ax.set_ylabel("Markets with a live book (%)")
        ax.set_title(title)
    fig.suptitle("Which markets have an order book in the 3 hours before and during the fight (Wilson 95% CI)")
    save(fig, "live_markets.png")


def plot_time_path(panel):
    winners = panel[panel["market_type"] == "winner"].copy()
    winners["bin"] = (winners["rel"] // 5) * 5
    winners = winners[winners["bin"] <= 25]
    panels = (
        ("spread_cents", "Median spread (cents)"),
        ("depth_usd", "Median dollars at the best bid and ask"),
        ("updates", "Mean book updates per minute"),
    )
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.2), sharex=True)
    for ax, (column, label) in zip(axes, panels):
        for name, part in (("main card", winners["main_card"] == 1), ("prelims", winners["main_card"] == 0)):
            series = winners[part].groupby("bin")[column]
            values = series.mean() if column == "updates" else series.median()
            ax.plot(values.index, values.to_numpy(), marker="o", ms=3, label=name)
        ax.axvspan(0, 25, color="grey", alpha=0.15, label="fight under way")
        ax.set_xlabel("Minutes from the bout's estimated start")
        ax.set_title(label, fontsize=10)
        if column == "depth_usd":
            ax.set_yscale("log")
    axes[0].legend(fontsize=8)
    fig.suptitle("Winner markets: liquidity through the last 3 hours and into the fight")
    save(fig, "time_path.png")


def plot_spread_ticks(panel):
    """Share of minutes at each spread: winner books sit at the one-tick floor before the fight."""
    data = panel.dropna(subset=["spread_cents"]).copy()
    data["group"] = np.where(data["market_type"] == "winner", "winner", "props") + np.where(
        data["rel"] < 0, ", pre-fight", ", in fight"
    )
    edges = [0, 1.0001, 2.0001, 3.0001, 5.0001, 10.0001, np.inf]
    labels = ["1 tick (<=1c)", "2c", "3c", "4-5c", "6-10c", ">10c"]
    data["band"] = pd.cut(data["spread_cents"], edges, labels=labels)
    shares = pd.crosstab(data["group"], data["band"], normalize="index").reindex(
        ["winner, pre-fight", "winner, in fight", "props, pre-fight", "props, in fight"]
    ).dropna(how="all")
    counts = data["group"].value_counts()
    fig, ax = plt.subplots(figsize=(10, 3.8))
    left = np.zeros(len(shares))
    colors = plt.cm.viridis(np.linspace(0.1, 0.95, len(labels)))
    for label, color in zip(labels, colors):
        values = shares[label].to_numpy() * 100 if label in shares else np.zeros(len(shares))
        ax.barh(range(len(shares)), values, left=left, color=color, label=label)
        left += values
    ax.set_yticks(range(len(shares)), [f"{g} (n={counts[g]:,})" for g in shares.index])
    ax.invert_yaxis()
    ax.set_xlabel("Share of market-minutes (%)")
    ax.set_title("Spread distribution: winner books are pinned at one tick until the fight starts")
    ax.legend(ncol=6, fontsize=8, loc="lower center", bbox_to_anchor=(0.5, -0.45))
    save(fig, "spread_ticks.png")


def plot_market_depth(markets):
    """Per-market pre-fight depth and spread against lifetime volume, by market type and card."""
    data = markets[markets["volume_clob"] > 0].dropna(subset=["depth_usd"])
    fig, (left, right) = plt.subplots(1, 2, figsize=(13, 5))
    colors = dict(zip(MARKET_TYPES, plt.cm.tab10.colors))
    for (kind, card), group in data.groupby(["market_type", "card_day"]):
        marker = "^" if card == "2026-06-14" else "o"
        for ax, column in ((left, "depth_usd"), (right, "spread_cents")):
            ax.scatter(group["volume_clob"], group[column], color=colors[kind], marker=marker, s=40,
                       label=f"{kind}, {'Freedom 250' if marker == '^' else 'June 6'}")
    for row in data[data["market_type"] == "winner"].nlargest(5, "volume_clob").itertuples():
        name = f"{str(row.fighter_a).split()[-1]}-{str(row.fighter_b).split()[-1]}"
        left.annotate(name, (row.volume_clob, row.depth_usd), textcoords="offset points", xytext=(4, 4), fontsize=7)
    winners = data[data["market_type"] == "winner"]
    rho_all = data["volume_clob"].corr(data["depth_usd"], method="spearman")
    rho_win = winners["volume_clob"].corr(winners["depth_usd"], method="spearman")
    for ax in (left, right):
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.set_xlabel("Lifetime matched volume (USDC, log scale)")
    left.set_ylabel("Median pre-fight dollars at the best bid and ask")
    left.set_title(f"Depth: Spearman rho {rho_all:.2f} (all {len(data)}), {rho_win:.2f} (winners {len(winners)})", fontsize=10)
    right.set_ylabel("Median pre-fight spread (cents)")
    right.set_title("Spread", fontsize=10)
    handles, names = left.get_legend_handles_labels()
    unique = dict(zip(names, handles))
    right.legend(unique.values(), unique.keys(), fontsize=7, loc="upper right")
    fig.suptitle("Each live market: deeper books go with more trading interest (association, not cause)")
    save(fig, "market_depth.png")


def plot_within_market(panel):
    """Each winner market's depth and updates relative to its own level 3 to 2 hours out."""
    winners = panel[(panel["market_type"] == "winner") & (panel["rel"] < 25)].copy()
    winners["bin"] = (winners["rel"] // 5) * 5
    fig, axes = plt.subplots(1, 2, figsize=(13, 4.5), sharex=True)
    for ax, column, label in ((axes[0], "depth_usd", "Dollars at the best bid and ask"),
                              (axes[1], "updates", "Book updates per minute")):
        binned = winners.groupby(["token", "bin"])[column].median().unstack()
        baseline = binned.loc[:, (binned.columns >= -180) & (binned.columns < -120)].median(axis=1)
        relative = binned.div(baseline.where(baseline > 0), axis=0).dropna(how="all")
        for _, row in relative.iterrows():
            ax.plot(row.index, row.to_numpy(), color="grey", alpha=0.35, lw=0.8)
        ax.plot(relative.columns, relative.median().to_numpy(), color="tab:red", lw=2.2, label="median market")
        ax.axhline(1, color="black", lw=0.8)
        ax.axvspan(0, 25, color="grey", alpha=0.15)
        ax.set_yscale("log")
        ax.set_xlabel("Minutes from the bout's estimated start")
        ax.set_ylabel("Relative to the market's own 3h-2h level")
        ax.set_title(f"{label} ({len(relative)} winner markets)", fontsize=10)
        ax.legend(fontsize=8)
    fig.suptitle("Within each fight: depth builds toward the bell and collapses when it rings")
    save(fig, "within_market.png")


def plot_spread_drivers(panel):
    data = panel.dropna(subset=["spread_cents"])
    fig, (left, right) = plt.subplots(1, 2, figsize=(12, 4.2))
    for ax, column, edges, label in (
        (left, "extremity", np.linspace(0, 5, 11), "Distance from 50 cents (10-cent steps)"),
        (right, "volatility", np.array([0, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 50]), "Volatility over the prior 30 minutes (cents)"),
    ):
        groups = data.groupby(pd.cut(data[column], edges, include_lowest=True), observed=True)["spread_cents"]
        stats = groups.agg(["median", "mean", "size"])
        x = np.arange(len(stats))
        ax.plot(x, stats["mean"], marker="o", label="mean")
        ax.plot(x, stats["median"], marker="s", label="median")
        ax.set_xticks(x, [f"{i.left:g}-{i.right:g}" for i in stats.index], rotation=45, fontsize=7)
        ax.set_xlabel(label)
        ax.set_ylabel("Spread (cents)")
        ax.legend(fontsize=8)
    left.set_title("Spread by price level")
    right.set_title("Spread by recent volatility")
    save(fig, "spread_drivers.png")


def plot_coefficients(tables):
    fig, axes = plt.subplots(1, len(tables), figsize=(16, 6), sharey=True)
    for ax, (outcome, table) in zip(axes, tables.items()):
        table = table[table["term"] != "Intercept"]
        y = np.arange(len(table))
        ax.hlines(y, np.exp(table["ci_low"]), np.exp(table["ci_high"]), color="tab:blue")
        ax.plot(np.exp(table["coef"]), y, "o", color="tab:blue")
        ax.axvline(1, color="black", lw=0.8)
        ax.set_yticks(y, table["term"])
        ax.set_xscale("log")
        ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"x{v:g}"))
        ax.set_xlabel("Multiplicative effect, bout-clustered 95% CI")
        ax.set_title(OUTCOMES[outcome])
    fig.suptitle("Drivers of liquidity in live markets (reference: winner market, prelims, 3h to 2h before the bout)")
    save(fig, "coefficients.png")


def plot_churn(activity):
    totals = activity.groupby("market_type")[["post_usd", "pull_usd", "fill_usd"]].sum().reindex(MARKET_TYPES).fillna(0)
    fig, ax = plt.subplots(figsize=(9, 4.5))
    x = np.arange(len(totals))
    for offset, (column, label) in zip((-0.27, 0, 0.27), (("post_usd", "posted"), ("pull_usd", "pulled"), ("fill_usd", "filled (inferred)"))):
        ax.bar(x + offset, totals[column].where(totals[column] > 0), width=0.27, label=label)
    ax.set_yscale("log")
    ax.set_xticks(x, totals.index)
    ax.set_ylabel("USDC in the window (log scale)")
    ax.set_title("Quoting dwarfs trading: dollars posted, pulled, and filled")
    ax.legend()
    save(fig, "quote_churn.png")


# ---------------------------------------------------------------- outputs


def write_dictionary():
    (OUTPUT / "DATA_DICTIONARY.md").write_text("""# UFC liquidity output data dictionary

Written by `script/ufc_liquidity.py`. Times are UTC. Prices are 0 to 1. A blank cell is a missing value.
Books are each market's outcome-0 book: the first-named fighter, Yes, or Over. The analysis window is the
3 hours before each bout's `bout_start` and the fight itself.

## Saved by a querying run (`--from-csv` starts here)

### `markets.csv`
One row per UFC market on the queried cards: `slug`, `event_slug` (the fight), `title`, `outcomes`,
`volume_clob` (lifetime), `event_title`, `card_day`, `fighter_a`, `fighter_b`, `segment` (Main Card or
Prelims), `market_type` (parsed from the title: winner, rounds_ou, fighter_ko, fight_ko, fight_sub,
distance, other), `bout_id` (linked ESPN bout, blank when the fight changed or was cancelled),
`name_match` (fuzzy score of the weaker fighter name), `main_event`, and `token` (the market's number in the
book files below).

### `book_actions.csv.gz`
Every replayed book action of at least 5 USDC: `token`, `window` (run of consecutive queried hours),
`ts_ms`, `kind` (`post`: size added; `pull`: size removed at the best price, a cancel or the rest of a level
that traded unseen; `fill`: a trade inferred when a new order crossed a resting level), `shares`,
`notional` (dollars behind the order), and the book just before it (`bid`, `ask`, `bid_size`, `ask_size`).

### `mids.csv.gz`
One row per market, window, and minute after warm-up: the last two-sided `mid` and `spread` in that minute,
carried forward within the window. Used to find bout starts.

## Written on every run

### `bouts.csv`
One row per ESPN bout: `bout_id`, `espn_event`, `bout_order`, `segment_start` (ESPN card segment start),
`espn_a`, `espn_b`, `winner`, `end_round`, `end_clock_s`, `fight_minutes`, `wall_minutes` (fight plus rests),
`scheduled_start` (segment start plus earlier bouts and a 15-minute gap each), `main_event`, `price_start`
(the earlier of: the minute the winner market passed 0.97 on the winner's side minus `wall_minutes`, and the
first one-minute mid move of at least 0.04), `bout_start` (price_start when present and within 150 minutes of
the schedule; otherwise scheduled_start, raised to the previous bout's start plus its `wall_minutes` when
that is later), and `start_source`.

### `market_activity.csv`
One row per market linked to a bout: `market_type`, `segment`, `main_event`, `volume_clob`, `updates` (book
updates of at least 5 USDC in the window), `live` (any update), and `post_usd`, `pull_usd`, `fill_usd`.

### `live_share.csv`
Share of markets with a live book by `group` (market type, or card position among props), with a Wilson
95% interval.

### `panel.csv.gz`
One row per live market and minute: the book at the minute's end (`bid`, `ask`, `bid_size`, `ask_size` in
shares), `updates` that minute, `rel` (minutes from `bout_start`), `phase`, `two_sided`, `spread_cents`,
`mid`, `depth_usd` (bid times bid size plus ask times ask size), `volatility` (std of one-minute mid changes
over the prior 30 minutes, cents), `extremity` (distance of the mid from 0.5 in 10-cent steps), and the logs
used as outcomes.

### `market_liquidity.csv`
One row per live market: `minutes` of pre-fight book observed, median pre-fight `spread_cents` and
`depth_usd`, mean pre-fight `updates` per minute, and lifetime `volume_clob`. Behind `market_depth.png`.

### `models.csv`
OLS of `log_spread`, `log_depth`, and `log_updates` on phase, market type, `main_card`, `main_event`,
`freedom_250`, `extremity`, and `volatility`, with standard errors clustered by bout. `sample` is `all live`
(every live market) or `winner` (winner markets only, without market type). `pct_coef` and its interval are
exp(coef) - 1 in percent. References: winner market, prelims, June 6 card, 3h to 2h before.

### `summary.csv`
Per sample and outcome: rows, markets, and bouts used, R squared, and `between_market_share`, the share of
the outcome's variance that lies between markets rather than within a market over time.
""")


def read_saved():
    """What a querying run saved: enough to redo everything after the replay."""
    def read(name, dates=()):
        path = OUTPUT / name
        if not path.exists():
            raise SystemExit(f"{path} is missing. Run once without --from-csv.")
        frame = pd.read_csv(path)
        for column in dates:
            frame[column] = pd.to_datetime(frame[column], utc=True)
        return frame

    markets = read("markets.csv")
    markets["outcomes"] = markets["outcomes"].map(json.loads)
    markets["token"] = markets["token"].astype("Int64")
    bouts = read("bouts.csv", ["segment_start", "scheduled_start"])
    bouts = bouts.drop(columns=["price_start", "bout_start", "start_source"], errors="ignore")
    return markets, bouts, read("book_actions.csv.gz"), read("mids.csv.gz", ["minute"])


def main():
    parser = argparse.ArgumentParser(description="What drives liquidity in UFC prediction markets.")
    parser.add_argument("--from-csv", action="store_true", help="Redo the analysis from outputs/ufc/ without querying.")
    parser.add_argument("--cards", nargs="+", default=list(CARDS), help="Card days (YYYY-MM-DD) to query.")
    parser.add_argument("--hours-before", type=int, default=HOURS_BEFORE, help="Hours read before a card's first segment.")
    args = parser.parse_args()
    OUTPUT.mkdir(parents=True, exist_ok=True)

    if not args.from_csv:
        bouts = load_espn(args.cards)
        markets = annotate_markets(load_markets(args.cards), bouts)
        log(f"{len(markets)} markets, {markets['bout_id'].notna().sum()} linked to {bouts['bout_id'].nunique()} ESPN bouts")
        tokens = first_tokens(markets)
        markets = markets.merge(tokens[["slug", "token"]], on="slug", how="left")
        hours = query_hours(bouts, args.hours_before)
        log(f"Querying {len(hours)} hourly files remotely")
        book = query_book(connect_duckdb(), tokens, hours)
        log(f"Replaying {len(book):,} book rows")
        actions, quotes = replay(book)
        del book
        actions = actions[actions["notional"] >= MIN_NOTIONAL].reset_index(drop=True)
        mids = minute_mids(quotes)
        log(f"{len(actions):,} book actions of at least {MIN_NOTIONAL:g} USDC; {len(mids):,} market-minutes of quotes")
        markets.drop(columns=["token_ids"]).assign(outcomes=markets["outcomes"].map(json.dumps)).to_csv(OUTPUT / "markets.csv", index=False)
        bouts.to_csv(OUTPUT / "bouts.csv", index=False)
        actions.to_csv(OUTPUT / "book_actions.csv.gz", index=False, float_format="%.6g")
        mids.to_csv(OUTPUT / "mids.csv.gz", index=False, float_format="%.6g")

    # Both modes continue from the saved files, so --from-csv reproduces a querying run exactly.
    markets, bouts, actions, mids = read_saved()
    bouts = locate_starts(bouts, markets, mids)
    bouts.to_csv(OUTPUT / "bouts.csv", index=False)
    log(f"Bout starts: {bouts['start_source'].value_counts().to_dict()}")

    linked = markets[markets["bout_id"].notna()].merge(bouts[["bout_id", "bout_start", "wall_minutes"]], on="bout_id")
    window = window_actions(actions, linked)
    activity = market_activity(linked, window)
    by_type = live_share(activity, "market_type").reindex(MARKET_TYPES)
    props = activity[activity["market_type"] != "winner"]
    position = np.where(props["main_event"].astype(bool), "main event", props["segment"].str.lower())
    by_segment = live_share(props.assign(position=position), "position")
    live = pd.concat([by_type.assign(dimension="market type"), by_segment.assign(dimension="prop card position")])
    live.rename_axis("group").reset_index().to_csv(OUTPUT / "live_share.csv", index=False)
    activity.to_csv(OUTPUT / "market_activity.csv", index=False)

    panel = minute_panel(window, linked)
    tables, fitted, summary = {}, [], []
    for sample in ("all live", "winner"):
        for outcome in OUTCOMES:
            table, model, data = fit(panel, outcome, sample)
            fitted.append(table)
            if sample == "all live":
                tables[outcome] = table
            summary.append({
                "sample": sample, "outcome": outcome, "rows": int(model.nobs), "markets": data["token"].nunique(),
                "bouts": data["bout_id"].nunique(), "r_squared": round(model.rsquared, 3),
                "between_market_share": round(between_market_share(data, outcome), 3),
            })
    models = pd.concat(fitted, ignore_index=True)
    models.to_csv(OUTPUT / "models.csv", index=False, float_format="%.6g")
    summary = pd.DataFrame(summary)
    summary.to_csv(OUTPUT / "summary.csv", index=False)
    panel.drop(columns=["bout_start"]).to_csv(OUTPUT / "panel.csv.gz", index=False, float_format="%.6g")
    per_market = market_liquidity(panel, linked)
    per_market.to_csv(OUTPUT / "market_liquidity.csv", index=False, float_format="%.6g")

    plot_bout_starts(mids, linked, bouts)
    plot_live_share(by_type, by_segment)
    plot_spread_ticks(panel)
    plot_market_depth(per_market)
    plot_within_market(panel)
    plot_time_path(panel)
    plot_spread_drivers(panel)
    plot_coefficients(tables)
    plot_churn(activity)
    write_dictionary()

    print(live.to_string())
    print(summary.to_string(index=False))
    shown = models[models["term"] != "Intercept"][["sample", "outcome", "term", "pct_coef", "pct_ci_low", "pct_ci_high", "p"]]
    print(shown.round(3).to_string(index=False))
    log(f"Wrote charts and tables to {OUTPUT}/")


if __name__ == "__main__":
    main()
