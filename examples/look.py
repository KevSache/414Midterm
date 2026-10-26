"""Print a short tour of Supabase metadata and one Parquet hour.

    python examples/look.py
    python examples/look.py 2026-10-25T18
"""

import base64
import sys

import duckdb
import psycopg2

HOUR = sys.argv[1] if len(sys.argv) > 1 else "2026-05-30T21"
CF_USER = "polymarketdatabase"
CF_PASSWORD = "PolymarketData2026!"
PARQUET_URL = f"https://d1yyqbluk0oz0t.cloudfront.net/events-{HOUR}.parquet"


def section(title):
    print(f"\n== {title} ==")


def show_postgres():
    con = psycopg2.connect(
        "postgresql://polymarketdatabase.merbqatlxbrcvtbnuaqe:PolymarketData2026!"
        "@aws-0-us-west-2.pooler.supabase.com:6543/postgres?sslmode=require"
    )
    con.set_session(readonly=True, autocommit=True)
    cur = con.cursor()

    section("public tables (planner row estimates)")
    cur.execute("""
        SELECT c.relname, c.reltuples::bigint AS estimate
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind = 'r'
        ORDER BY c.relname;
    """)
    for name, estimate in cur.fetchall():
        shown = "empty or not analyzed" if estimate < 0 else str(estimate)
        print(f"{name:24} {shown}")

    section("markets with the highest 24h volume")
    cur.execute("""
        SELECT slug, left(title, 72), best_bid, best_ask, volume_24hr
        FROM markets
        ORDER BY volume_24hr DESC NULLS LAST
        LIMIT 5;
    """)
    for row in cur.fetchall():
        print(row)

    section("recent events")
    cur.execute("""
        SELECT slug, left(title, 72), active, closed
        FROM events
        ORDER BY updated_at DESC NULLS LAST
        LIMIT 5;
    """)
    for row in cur.fetchall():
        print(row)

    section("latest resolutions")
    cur.execute("""
        SELECT market_slug, resolved_yes, price_at_close, resolved_at
        FROM resolutions
        ORDER BY resolved_at DESC NULLS LAST
        LIMIT 5;
    """)
    for row in cur.fetchall():
        print(row)

    section("wallet pseudonyms (hex)")
    cur.execute("""
        SELECT encode(pseudo_wallet, 'hex'), encode(real_wallet, 'hex')
        FROM wallet_pseudonym_map
        LIMIT 3;
    """)
    for pseudo, real in cur.fetchall():
        print(f"pseudo {pseudo}")
        print(f"real   {real}")

    cur.close()
    con.close()


def show_parquet():
    token = base64.b64encode(f"{CF_USER}:{CF_PASSWORD}".encode()).decode()
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute(f"""
        CREATE SECRET cf (
            TYPE HTTP,
            EXTRA_HTTP_HEADERS MAP {{'Authorization': 'Basic {token}'}}
        );
    """)

    section(f"parquet {PARQUET_URL}")
    result = con.execute(f"""
        SELECT
            CAST(to_timestamp(ts_ms / 1000.0) AS VARCHAR) AS ts,
            side,
            price,
            size,
            left(market, 18) AS market,
            left(asset_id, 18) AS asset_id
        FROM read_parquet('{PARQUET_URL}')
        LIMIT 5
    """)
    columns = [col[0] for col in result.description]
    print("\t".join(columns))
    for row in result.fetchall():
        print("\t".join("" if value is None else str(value) for value in row))


if __name__ == "__main__":
    show_postgres()
    show_parquet()
