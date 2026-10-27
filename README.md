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
python examples/explore.py --stat volume_24hr --updated-within 24h --limit 200 --csv largest_last_24h.csv
python examples/explore.py --stat volume_clob --since 2026-10-01 --until 2026-10-08
python examples/explore.py --stat volume_clob --stat updated_at --search "michigan senate" --limit 200
python examples/explore.py --stat events --search "world cup"
python examples/explore.py --list-stats
```

Each ranking scans about 1.5 million markets and usually takes 10–20 seconds. Repeat `--stat` to print more than one ranking. `--limit` can go up to 2000, and `--offset` pages through the list. `--csv results.csv` writes the full rows. A repeated `--stat` writes one CSV per ranking. Dates in the table are Pacific time. A filled `settled` column means the market has a resolution.

`--updated-within 24h` keeps markets whose record changed in that window, then ranks them. `--since` and `--until` do the same for a clock range, interpreted in Pacific time. `volume_24hr` is the stored 24-hour figure from that update. Old World Cup rows stay in an unfiltered `volume_24hr` ranking because their saved totals were never cleared.

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
| `examples/nba_analysis.py` | Price path, variance, resolution, and large-order charts. `--find power` uses the top 10 power rankings. `--find top20` keeps the 20 game winners with the most posted size in the order book. `--find props5` does the same charts for player points, assists, and rebounds |

## NBA top 10

`examples/nba_analysis.py` charts one completed game-winner market per panel. The slug equals the event slug, so the two outcomes are the two teams. `--find` chooses which games:

```bash
python examples/nba_analysis.py --find power
python examples/nba_analysis.py --find top20
python examples/nba_analysis.py --find props5
python examples/nba_analysis.py --find props --each 4 --stats points,assists,rebounds
python examples/nba_analysis.py --find props5 --plots-only
python examples/nba_analysis.py --plots-only
```

`--find power` is the default. It uses the NBA.com Week 2 power rankings published October 26, 2026, one game per team. A game already used for the opponent is reused. It still probes each team's candidates until one hourly file contains that token.

`--find top20` does not sort by `volume_clob`. It reads the hourly order-book files around every completed NBA tip: the hour before, the tip hour, and the hour after. That is about 143 distinct files, each read once, including games whose stored volume is zero. It then keeps the 20 away-team tokens with the most posted size in those hours. Posted size is the sum of book rows with size greater than 0. It is not matched dollar volume, and it is not `volume_clob`. `volume_clob` is still written on the summary row so the two numbers can be compared. A market that never appears in those files is left out, including one with a huge stored volume and no price history. Any `topN` from 1 to 40 works the same way. The price on each panel is the away team, the first outcome. This search runs before the usual chart window, so `top5` takes longer than the old pass that only checked the highest `volume_clob` slugs. The chart pass still reads six hours before tip through three hours after, for the kept games only.

`--plots-only` redraws the charts from the CSVs already in `outputs/nba/`. It does not download the hourly files. Use it when a run finished the download and then failed while drawing. It also writes `DATA_DICTIONARY.md` from the CSV headers already in that folder.

The run reads six hours before the listed tip through three hours after it. A busy hour is tens or hundreds of megabytes, and one full pass is on the order of a few gigabytes. CloudFront logs each file and the bytes sent under the shared class login. The spread, alternate spreads, and game total are filtered out of those same hours, so they do not add another download. Final scores are a few small requests to ESPN's public scoreboard. The charts are written on your machine only; they are not stored in this repo. Output files:

| File | What it shows |
| --- | --- |
| `price_paths.png` | Five-minute mid price of that team's win token. The vertical line is the listed tip. |
| `variance.png` | One number for the six hours before tip, and one for the first 2.5 hours after tip. |
| `variance_score.png` | A variance score on every five-minute bin: the standard deviation of the mid over the trailing 30 minutes. |
| `variance_vs_margin.png` | That in-game score against the absolute final margin from ESPN. Close games are margins of 5 or fewer. Blowouts are 15 or more. |
| `price_vs_score.png` | The win price with the ESPN score margin on the same clock. Positive margin means the charted team is ahead. |
| `market_paths.png` | Win price, the highest-volume spread, and the Over on the highest-volume game total. |
| `alternate_spreads.png` | Pregame price of the four most traded spreads, using the point line from that team's side. |
| `bid_ask_width.png` | Ask minus bid, every five minutes. |
| `calibration.png` | Last mid before tip against the Polymarket resolution (won or lost). |
| `moneyline_vs_spread.png` | That same pregame win price against the highest-volume spread. |
| `summary.csv` | One row per team, including the variance scores, the ESPN margin, and the game total. |
| `price_paths.csv` | The five-minute rows behind the charts, including the variance score and the score margin. |
| `game_results.csv` | ESPN score, quarter lines, and margin for each team that had an order book. |
| `alternate_spreads.csv` | Pregame price of each alternate spread. |
| `large_orders.png` | Unusually large posted sizes on the win price. A dark mark means the price then moved that way and the order came before the next scoring play, not just after the previous one. |
| `large_order_counts.png` | How many of those sizes each game had, how many the price followed, and how many also led a score. |
| `large_orders.csv` | The rows behind those two charts. There is no wallet on them. |
| `DATA_DICTIONARY.md` | Column-by-column definitions of the CSVs in this folder. |

The variance score is high while the win price is moving around, and it falls once the price sits still near 0 or 1. A blowout that runs the price from a coin flip to 0.95 in one smooth stretch still produces a high score during that run. A close game that keeps crossing back and forth stays high for longer. `variance_score_early` and `variance_score_late` in `summary.csv` split the first 2.5 hours after tip into two halves, which is the check for whether the score rises as the game goes on. `minutes_to_decided` is how many minutes after tip the win price first reaches 0.90 or 0.10.

`large_orders.png` is built from the same hourly files, in the same pass. A kept row has a posted size at least 10 times the median size on that token and side. Repeated prints of the same resting size are collapsed. The price "followed" when the mid 30 minutes later is at least 0.03 further in that direction. "Led a score" means the previous scoring play was at least 15 seconds earlier and the next one came within two minutes. A dark mark is a row that did both. These rows are still anonymous order-book updates. A removed size is not included, because a cancel and a fill look the same. `--plots-only` draws this chart only when `large_orders.csv` is already in `outputs/nba/`. A run from before this change does not have that file, so produce it with a new full run.

## Player points, assists, and rebounds

The same charts can be drawn for player over/under markets. A slug looks like `nba-sas-okc-2026-05-30-rebounds-chet-holmgren-8pt5`, and the title is `Chet Holmgren: Rebounds O/U 8.5`. The two outcomes are Yes and No. The charted price is Yes, which is the Over.

```bash
python examples/nba_analysis.py --find props5
python examples/nba_analysis.py --find props --each 3 --stats points,assists,rebounds,threes
```

`--find props5` keeps five points markets, five assists markets, and five rebounds markets. `--each` is the same knob, and `--find props` with no number uses 4. The maximum is 10 per stat. `--stats` accepts `points`, `assists`, `rebounds`, `threes`, `steals`, and `blocks`.

For each requested stat the script reads the same three hours around every completed game's tip and sums posted size on the Yes token. Yes is the Over. It keeps the lines with the most posted size. When one player has several numbers in the same game, the charted line is the one with the most posted size, not the highest `volume_clob`. A line with stored volume 0 is kept when its token is in the files. A line with a large `volume_clob` and no rows is left out. A stat can come back with fewer panels when few tokens appear. Other posted numbers for that same player are still read during the chart pass and drawn as the alternate-line chart. The run writes `outputs/nba/props/`, so it does not replace the game-winner charts. `DATA_DICTIONARY.md` in that folder defines the prop CSV columns, including `won`, `final_stat`, `espn_margin`, `book_size`, and `book_updates`. `--find props5 --plots-only` redraws that folder and writes the dictionary from the CSVs already there. It does not repeat the hour search. The search itself is about the same length as `--find top5`, because both walk every completed tip. `top5` only totals the game-winner token. `props5` totals the Yes token of each requested line. The file download is the slow part.

The price, variance, bid-ask gap, and large-order charts are the Over price. `price_vs_score.png` draws that price against the player's running stat, with a dashed line at the posted number. The final stat is the ESPN box score. `espn_margin` in the summary is the final stat minus the posted number. A points prop is "close" when that gap is 2 or less and a "blowout" when it is 6 or more. Assists, rebounds, threes, steals, and blocks use 1 and 3. On the large-order chart, a dark mark means the Over price then moved that way and the order came at least 15 seconds after the player's previous stat and within two minutes before the next one. There is still no wallet on the row.

The class database does not store the point margin. `won` is the Polymarket resolution, and the score, quarter lines, and margin come from ESPN. `markets.best_bid` is the latest snapshot, not the history, so the price charts come from the Parquet files. `--find power` can still leave a team blank when none of that team's completed games appear in the probed hours. `--find topN` and `--find props` only keep markets whose token was in the order book.

## Update your local copy

After this repository changes, download those commits from the same folder in Ubuntu:

```bash
cd /mnt/c/Users/kevin/repositories/Polymarket_414
git pull
```

That brings over new files such as `DATA_DICTIONARY.md`. Reinstall when `requirements.txt` changed (the NBA charts need `matplotlib`):

```bash
source .venv/bin/activate
pip install -r requirements.txt
```
