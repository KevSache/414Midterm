"""Read a few rows from one hourly order-book Parquet file.

Usage:
    python examples/cloudfront_duckdb.py
    python examples/cloudfront_duckdb.py 2026-10-25T18

Putting the password in the URL returns 401 from this CloudFront
distribution. The Authorization header below is the call that works.
DuckDB fetches the Parquet footer with range requests, so LIMIT does
not download the whole hour.
"""

import base64
import sys

import duckdb

HOUR = sys.argv[1] if len(sys.argv) > 1 else "2026-05-30T21"
USER = "polymarketdatabase"
PASSWORD = "PolymarketData2026!"
URL = f"https://d1yyqbluk0oz0t.cloudfront.net/events-{HOUR}.parquet"

token = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()

con = duckdb.connect()
con.execute("INSTALL httpfs; LOAD httpfs;")
con.execute(f"""
    CREATE SECRET cf (
        TYPE HTTP,
        EXTRA_HTTP_HEADERS MAP {{'Authorization': 'Basic {token}'}}
    );
""")

print(URL)
schema = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{URL}')").fetchall()
for name, col_type, *_rest in schema:
    print(f"{name:12} {col_type}")

result = con.execute(f"""
    SELECT
        CAST(to_timestamp(ts_ms / 1000.0) AS VARCHAR) AS ts,
        side,
        price,
        size,
        market,
        asset_id
    FROM read_parquet('{URL}')
    LIMIT 10
""")
columns = [col[0] for col in result.description]
print("\t".join(columns))
for row in result.fetchall():
    print("\t".join("" if value is None else str(value) for value in row))
