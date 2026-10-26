"""Rank an extensive list of markets or events by volume and other stats.

The markets table has about 1.5 million rows and the volume columns are not
indexed, so each ranking scans the table. Expect about 10–20 seconds.

Examples:
    python examples/explore.py
    python examples/explore.py --stat volume_clob --stat updated_at --search "michigan senate" --limit 200
    python examples/explore.py --stat events --limit 50
    python examples/explore.py --stat liquidity --search "world cup"
    python examples/explore.py --stat spread --min-liquidity 5000
    python examples/explore.py --stat tight --limit 30
    python examples/explore.py --stat surprise --limit 40
    python examples/explore.py --stat volume_1wk --csv top_week.csv
    python examples/explore.py --list-stats
"""

import argparse
import csv
import sys
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import psycopg2

CONNECTION = (
    "postgresql://polymarketdatabase.merbqatlxbrcvtbnuaqe:PolymarketData2026!"
    "@aws-0-us-west-2.pooler.supabase.com:6543/postgres?sslmode=require"
)

STATS = {
    "volume_24hr": "Highest volume in the last 24 hours (volume_24hr).",
    "volume_1wk": "Highest volume in the last week (volume_1wk).",
    "volume_1mo": "Highest volume in the last month (volume_1mo).",
    "volume_clob": "Highest lifetime CLOB volume (volume_clob).",
    "liquidity": "Deepest order-book liquidity (liquidity_clob).",
    "spread": "Widest bid-ask spread, among markets with a bid and an ask.",
    "tight": "Tightest bid-ask spread among markets with at least 10,000 lifetime volume.",
    "events": "Events with the highest summed lifetime market volume.",
    "surprise": "Resolved markets whose last trade was on the losing side, highest volume first.",
    "updated_at": "Most recently updated market records (updated_at), newest first.",
}

MARKET_METRIC = {
    "volume_24hr": "m.volume_24hr",
    "volume_1wk": "m.volume_1wk",
    "volume_1mo": "m.volume_1mo",
    "volume_clob": "m.volume_clob",
    "liquidity": "m.liquidity_clob",
    "spread": "(m.best_ask - m.best_bid)",
    "tight": "(m.best_ask - m.best_bid)",
    "surprise": "m.volume_clob",
    "updated_at": "m.updated_at",
}

PACIFIC = ZoneInfo("America/Los_Angeles")


def connect():
    con = psycopg2.connect(CONNECTION)
    con.set_session(readonly=True, autocommit=True)
    return con


def like_pattern(term):
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    # A space matches any gap, so "michigan senate" also finds michigan-senate slugs.
    loose = "%".join(part for part in escaped.split() if part)
    return f"%{loose}%"


def as_float(value):
    if value is None:
        return None
    return float(value)


def fmt_number(value):
    if value is None:
        return ""
    number = float(value) if isinstance(value, Decimal) else value
    if abs(number) >= 100:
        return f"{number:,.0f}"
    return f"{number:,.3f}"


def fmt_price(value):
    if value is None:
        return ""
    return f"{float(value):.3f}"


def fmt_date(value):
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.astimezone(PACIFIC).strftime("%Y-%m-%d")
    return str(value)[:10]


def clip(text, width):
    text = "" if text is None else str(text).replace("\n", " ")
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


def print_table(headers, rows, aligns):
    widths = [len(header) for header in headers]
    rendered = []
    for row in rows:
        cells = ["" if cell is None else str(cell) for cell in row]
        rendered.append(cells)
        for index, cell in enumerate(cells):
            widths[index] = max(widths[index], len(cell))

    def pad(cell, index):
        width = widths[index]
        if aligns[index] == "r":
            return cell.rjust(width)
        return cell.ljust(width)

    print("  ".join(pad(header, index) for index, header in enumerate(headers)))
    print("  ".join("-" * width for width in widths))
    for cells in rendered:
        print("  ".join(pad(cell, index) for index, cell in enumerate(cells)))


def write_csv(path, fieldnames, records):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)
    print(f"Wrote {len(records)} rows to {path}", file=sys.stderr)


def market_query(stat, status):
    metric = MARKET_METRIC[stat]
    if stat == "tight":
        order_by = f"{metric} ASC, m.volume_clob DESC NULLS LAST, m.slug"
    else:
        order_by = f"{metric} DESC NULLS LAST, m.slug"
    where = [f"{metric} IS NOT NULL"]
    join = ""
    if stat in ("spread", "tight"):
        where.append("m.best_bid IS NOT NULL")
        where.append("m.best_ask > m.best_bid")
    if stat == "tight":
        where.append("m.volume_clob >= 10000")
    if stat == "surprise":
        join = "JOIN resolutions r ON r.market_slug = m.slug"
        where.append("m.last_trade_price IS NOT NULL")
        where.append(
            """(
                (r.resolved_yes AND m.last_trade_price < 0.5)
                OR (NOT r.resolved_yes AND m.last_trade_price >= 0.5)
            )"""
        )
    elif status != "any":
        join = "LEFT JOIN resolutions r ON r.market_slug = m.slug"
        if status == "resolved":
            where.append("r.market_slug IS NOT NULL")
        else:
            where.append("r.market_slug IS NULL")
    where.append(
        "(%(search)s IS NULL OR m.title ILIKE %(search)s ESCAPE '\\' "
        "OR m.slug ILIKE %(search)s ESCAPE '\\' "
        "OR m.event_slug ILIKE %(search)s ESCAPE '\\')"
    )
    where.append("(%(min_liquidity)s IS NULL OR m.liquidity_clob >= %(min_liquidity)s)")
    if "resolutions r" not in join:
        join = "LEFT JOIN resolutions r ON r.market_slug = m.slug"
    return f"""
        SELECT
            m.slug,
            m.title,
            m.event_slug,
            m.condition_id,
            m.best_bid,
            m.best_ask,
            (m.best_ask - m.best_bid) AS spread,
            m.last_trade_price,
            m.volume_24hr,
            m.volume_1wk,
            m.volume_1mo,
            m.volume_clob,
            m.liquidity_clob,
            m.end_date,
            m.updated_at,
            r.resolved_at,
            r.resolved_yes,
            {metric} AS metric_value
        FROM markets m
        {join}
        WHERE {" AND ".join(where)}
        ORDER BY {order_by}
        LIMIT %(limit)s OFFSET %(offset)s
    """


def event_query(status):
    having = ["sum(m.volume_clob) IS NOT NULL"]
    if status == "resolved":
        having.append("count(r.market_slug) > 0")
    elif status == "unresolved":
        having.append("count(*) > count(r.market_slug)")
    return f"""
        SELECT
            e.slug,
            e.title,
            count(*) AS n_markets,
            count(r.market_slug) AS n_resolved,
            sum(m.volume_clob) AS volume_clob,
            sum(m.volume_24hr) AS volume_24hr,
            sum(m.liquidity_clob) AS liquidity_clob
        FROM markets m
        JOIN events e ON e.slug = m.event_slug
        LEFT JOIN resolutions r ON r.market_slug = m.slug
        WHERE %(search)s IS NULL
           OR e.title ILIKE %(search)s ESCAPE '\\'
           OR e.slug ILIKE %(search)s ESCAPE '\\'
        GROUP BY e.slug, e.title
        HAVING {" AND ".join(having)}
        ORDER BY sum(m.volume_clob) DESC NULLS LAST, e.slug
        LIMIT %(limit)s OFFSET %(offset)s
    """


def fetch_rows(stat, args):
    search = like_pattern(args.search) if args.search else None
    params = {
        "search": search,
        "min_liquidity": args.min_liquidity,
        "limit": args.limit,
        "offset": args.offset,
    }
    sql = event_query(args.status) if stat == "events" else market_query(stat, args.status)
    print("Scanning the markets table. This usually takes 10–20 seconds.", file=sys.stderr)
    con = connect()
    try:
        cur = con.cursor()
        cur.execute(sql, params)
        columns = [desc[0] for desc in cur.description]
        records = [dict(zip(columns, row)) for row in cur.fetchall()]
        cur.close()
    finally:
        con.close()
    return records


def show_markets(stat, records, offset):
    label = {
        "volume_24hr": "24h volume",
        "volume_1wk": "1w volume",
        "volume_1mo": "1mo volume",
        "volume_clob": "CLOB volume",
        "liquidity": "liquidity",
        "spread": "spread",
        "tight": "spread",
        "surprise": "CLOB volume",
        "updated_at": "updated",
    }[stat]
    if stat == "surprise":
        headers = ["#", label, "updated", "settled", "last", "won", "title"]
        aligns = ["r", "r", "l", "l", "r", "l", "l"]
    elif stat == "updated_at":
        headers = ["#", "updated", "settled", "CLOB volume", "bid", "ask", "title"]
        aligns = ["r", "l", "l", "r", "r", "r", "l"]
    else:
        headers = ["#", label, "updated", "settled", "bid", "ask", "title"]
        aligns = ["r", "r", "l", "l", "r", "r", "l"]
    rows = []
    for index, record in enumerate(records, start=offset + 1):
        value = record["metric_value"]
        if stat in ("spread", "tight"):
            shown = fmt_price(value)
        elif stat == "updated_at":
            shown = fmt_date(value)
        else:
            shown = fmt_number(value)
        updated = fmt_date(record["updated_at"])
        settled = fmt_date(record["resolved_at"])
        if stat == "surprise":
            rows.append(
                [
                    index,
                    shown,
                    updated,
                    settled,
                    fmt_price(record["last_trade_price"]),
                    "yes" if record["resolved_yes"] else "no",
                    clip(record["title"], 42),
                ]
            )
        elif stat == "updated_at":
            rows.append(
                [
                    index,
                    shown,
                    settled,
                    fmt_number(record["volume_clob"]),
                    fmt_price(record["best_bid"]),
                    fmt_price(record["best_ask"]),
                    clip(record["title"], 42),
                ]
            )
        else:
            rows.append(
                [
                    index,
                    shown,
                    updated,
                    settled,
                    fmt_price(record["best_bid"]),
                    fmt_price(record["best_ask"]),
                    clip(record["title"], 42),
                ]
            )
    print_table(headers, rows, aligns)
    if stat == "surprise":
        print()
        print("won is the outcome that resolved. last is the last trade price, which was on the other side of 0.50.")


def show_events(records, offset):
    headers = ["#", "CLOB volume", "24h volume", "markets", "resolved", "title", "slug"]
    aligns = ["r", "r", "r", "r", "r", "l", "l"]
    rows = []
    for index, record in enumerate(records, start=offset + 1):
        rows.append(
            [
                index,
                fmt_number(record["volume_clob"]),
                fmt_number(record["volume_24hr"]),
                record["n_markets"],
                record["n_resolved"],
                clip(record["title"], 42),
                clip(record["slug"], 36),
            ]
        )
    print_table(headers, rows, aligns)


def csv_records(stat, records, offset):
    exported = []
    for index, record in enumerate(records, start=offset + 1):
        row = {"rank": index, "stat": stat}
        for key, value in record.items():
            if isinstance(value, Decimal):
                row[key] = float(value)
            else:
                row[key] = value
        exported.append(row)
    return exported


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="List the highest-volume Polymarket markets and events, or rank them by another stat.",
    )
    parser.add_argument(
        "--stat",
        action="append",
        choices=sorted(STATS),
        help="Ranking to print. Repeat the flag to print more than one. Default: volume_24hr.",
    )
    parser.add_argument("--limit", type=int, default=100, help="Rows to print. Default 100. Maximum 2000.")
    parser.add_argument("--offset", type=int, default=0, help="Skip this many ranked rows. Use it to page.")
    parser.add_argument("--search", help="Case-insensitive match on title or slug.")
    parser.add_argument(
        "--status",
        choices=("any", "resolved", "unresolved"),
        default="any",
        help="Keep all markets, only resolved ones, or only markets with no resolution row.",
    )
    parser.add_argument(
        "--min-liquidity",
        type=float,
        help="Drop markets whose liquidity_clob is below this value.",
    )
    parser.add_argument("--csv", help="Also write the full, untruncated rows to this CSV path.")
    parser.add_argument("--list-stats", action="store_true", help="Print the ranking choices and exit.")
    args = parser.parse_args(argv)
    if args.limit < 1 or args.limit > 2000:
        parser.error("--limit must be from 1 to 2000")
    if args.offset < 0:
        parser.error("--offset must be 0 or greater")
    if not args.stat:
        args.stat = ["volume_24hr"]
    return args


def main(argv):
    args = parse_args(argv)
    if args.list_stats:
        for name, description in STATS.items():
            print(f"{name:14} {description}")
        return 0
    if "surprise" in args.stat and args.status == "unresolved":
        print("Surprise rankings only include resolved markets.", file=sys.stderr)
        return 1

    for index, stat in enumerate(args.stat):
        if index:
            print()
        print(STATS[stat], flush=True)
        records = fetch_rows(stat, args)
        if not records:
            print("No rows matched.")
            continue
        if stat == "events":
            show_events(records, args.offset)
        else:
            show_markets(stat, records, args.offset)
        print()
        print(f"{len(records)} rows. Offset {args.offset}.")
        if len(records) == args.limit:
            print(f"Next page: --offset {args.offset + args.limit}")
        if args.csv and len(args.stat) == 1:
            exported = csv_records(stat, records, args.offset)
            fieldnames = ["rank", "stat", *records[0].keys()]
            write_csv(args.csv, fieldnames, exported)
        elif args.csv:
            stem = args.csv.rsplit(".", 1)
            path = f"{stem[0]}_{stat}.{stem[1]}" if len(stem) == 2 else f"{args.csv}_{stat}"
            exported = csv_records(stat, records, args.offset)
            fieldnames = ["rank", "stat", *records[0].keys()]
            write_csv(path, fieldnames, exported)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
