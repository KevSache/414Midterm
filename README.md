# Polymarket data access

Sample code for the class Polymarket dataset. Two systems:

- **CloudFront** serves hourly order-book Parquet files.
- **Supabase Postgres** serves market metadata (events, markets, resolutions, wallet pseudonyms).

Column names, types, and join keys are in [DATA_DICTIONARY.md](DATA_DICTIONARY.md).

Access window: **October 14, 2026 6:00pm PDT** through **October 29, 2026 11:59pm PDT**.

## Credentials

| System | Username | Password |
| --- | --- | --- |
| CloudFront (HTTP basic auth) | `polymarketdatabase` | `PolymarketData2026!` |
| Supabase | `polymarketdatabase.merbqatlxbrcvtbnuaqe` | `PolymarketData2026!` |

The `.merbqatlxbrcvtbnuaqe` suffix is part of the Supabase username, not a separator.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Rank markets by 24-hour volume (100 rows). Other rankings are listed with `--list-stats`.

```bash
python examples/explore.py
python examples/explore.py --stat volume_clob --stat updated_at --search "michigan senate" --limit 200
python examples/explore.py --stat events --search "world cup"
python examples/explore.py --list-stats
```

Each ranking scans about 1.5 million markets and usually takes 10–20 seconds. Repeat `--stat` to print more than one ranking. `--limit` can go up to 2000, and `--offset` pages through the list. `--csv results.csv` writes the full rows. A repeated `--stat` writes one CSV per ranking. Dates in the table are Pacific time. A filled `settled` column means the market has a resolution.

Look at a small slice of both sources:

```bash
python examples/look.py
```

Read one hourly Parquet file (default `events-2026-05-30T21.parquet`):

```bash
python examples/cloudfront_duckdb.py
python examples/cloudfront_duckdb.py 2026-10-25T18
```

## CloudFront Parquet files

Base URL: `https://d1yyqbluk0oz0t.cloudfront.net`

Object keys:

```text
events-YYYY-MM-DDTHH.parquet
```

`HH` is the hour in UTC. `events-2026-05-30T21.parquet` starts at `2026-05-30 21:00:00 UTC`. The bucket does not allow listing, so you request a key by name.

Checked range: hourly files exist from `events-2026-05-30T20.parquet` through at least `events-2026-10-25T23.parquet`. `events-2026-05-30T19.parquet` is only 704 bytes. A busy hour can be a few hundred megabytes (`events-2026-06-10T12.parquet` is about 598 MB). Later hours are often 10–70 MB.

Columns:

| Column | Type | Meaning |
| --- | --- | --- |
| `ts_ms` | bigint | Event time, Unix milliseconds |
| `asset_id` | string | Outcome token id |
| `market` | string | Condition id (`0x...`). Joins to `markets.condition_id` |
| `side` | string | `BUY` or `SELL` |
| `price` | double | Price |
| `size` | double | Size |

HTTP basic auth works with `requests`. The handout DuckDB URL puts the password in the URL (`https://polymarketdatabase:PolymarketData2026!@...`). That request comes back **401** from this distribution. Send an `Authorization: Basic ...` header instead. `examples/cloudfront_duckdb.py` does that.

The objects are Parquet, not JSON. `response.json()` fails. Use DuckDB (it reads the Parquet footer with range requests, so a `LIMIT` does not download the whole hour).

```python
import base64
import duckdb

user = "polymarketdatabase"
password = "PolymarketData2026!"
token = base64.b64encode(f"{user}:{password}".encode()).decode()

con = duckdb.connect()
con.execute("INSTALL httpfs; LOAD httpfs;")
con.execute(f"""
    CREATE SECRET cf (
        TYPE HTTP,
        EXTRA_HTTP_HEADERS MAP {{'Authorization': 'Basic {token}'}}
    );
""")

hour = "2026-05-30T21"
url = f"https://d1yyqbluk0oz0t.cloudfront.net/events-{hour}.parquet"
print(con.execute(f"SELECT * FROM read_parquet('{url}') LIMIT 10").fetchdf())
```

## Supabase Postgres

Connection string:

```text
postgresql://polymarketdatabase.merbqatlxbrcvtbnuaqe:PolymarketData2026!@aws-0-us-west-2.pooler.supabase.com:6543/postgres
```

Host `aws-0-us-west-2.pooler.supabase.com`, port `6543`, database `postgres`, `sslmode=require`. This is the transaction pooler. Use it read-only.

### Tables in `public`

| Table | Approx. rows | What it holds |
| --- | --- | --- |
| `markets` | 1.5 million | One row per market: slug, title, bids, volume, token ids, parent event |
| `events` | 320 thousand | Event slug, title, dates, active/closed flags |
| `resolutions` | 320 thousand | Resolved outcome and close price per market |
| `wallet_pseudonym_map` | 1.3 million | `real_wallet` and `pseudo_wallet`, both `bytea` |
| `market_hourly` | 0 | OHLC schema exists (`market_slug`, `hour`, `open`, `high`, `low`, `close`, `volume`, `trade_count`). The table is empty. |

Useful `markets` columns: `slug`, `title`, `condition_id`, `event_slug`, `best_bid`, `best_ask`, `last_trade_price`, `volume_clob`, `volume_24hr`, `token_ids`, `outcomes`, `active`, `closed`, `end_date`.

`markets.slug` is the primary key. `resolutions.market_slug` and `market_hourly.market_slug` point at it. `markets.event_slug` points at `events.slug`. Parquet `market` matches `markets.condition_id`.

Wallet ids are raw bytes. Display them as hex:

```sql
SELECT encode(pseudo_wallet, 'hex') AS pseudo_wallet,
       encode(real_wallet, 'hex') AS real_wallet
FROM wallet_pseudonym_map
LIMIT 5;
```

### psycopg2

```python
import psycopg2

con = psycopg2.connect(
    "postgresql://polymarketdatabase.merbqatlxbrcvtbnuaqe:PolymarketData2026!"
    "@aws-0-us-west-2.pooler.supabase.com:6543/postgres?sslmode=require"
)
cur = con.cursor()
cur.execute("SELECT slug, title, best_bid, best_ask FROM markets LIMIT 10;")
print(cur.fetchall())
```

### pandas

```python
import pandas as pd
from sqlalchemy import create_engine

engine = create_engine(
    "postgresql+psycopg2://polymarketdatabase.merbqatlxbrcvtbnuaqe:PolymarketData2026!"
    "@aws-0-us-west-2.pooler.supabase.com:6543/postgres",
    connect_args={"sslmode": "require"},
)
print(pd.read_sql("SELECT slug, title, best_bid, best_ask FROM markets LIMIT 10", engine))
```

### DuckDB

```python
import duckdb

con = duckdb.connect()
con.execute("INSTALL postgres; LOAD postgres;")
con.execute("""
    ATTACH 'host=aws-0-us-west-2.pooler.supabase.com port=6543 dbname=postgres
             user=polymarketdatabase.merbqatlxbrcvtbnuaqe password=PolymarketData2026! sslmode=require'
    AS pm (TYPE postgres, READ_ONLY);
""")
print(con.execute("SELECT slug, title, best_bid, best_ask FROM pm.markets LIMIT 10").fetchdf())
```

## Examples

| Script | What it does |
| --- | --- |
| `examples/explore.py` | Ranks up to 2,000 markets or events by volume, liquidity, spread, or resolutions that went against the last trade |
| `examples/look.py` | Prints a few markets, events, resolutions, wallet pseudonyms, and Parquet rows |
| `examples/cloudfront_requests.py` | Checks a Parquet object with `requests` and prints status, size, and the `PAR1` magic bytes |
| `examples/cloudfront_duckdb.py` | Reads schema and 10 rows from one hour |
| `examples/supabase_psycopg2.py` | `markets` sample via psycopg2 |
| `examples/supabase_pandas.py` | Same sample via pandas |
| `examples/supabase_duckdb.py` | Same sample via DuckDB `ATTACH` |

## Update your local copy

After this repository changes, download those commits from the same folder in Ubuntu:

```bash
cd /mnt/c/Users/kevin/repositories/Polymarket_414
git pull
```

That brings over new files such as `DATA_DICTIONARY.md`. You do not need to reinstall the Python packages unless `requirements.txt` changed.
