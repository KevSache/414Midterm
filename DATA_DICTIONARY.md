# Data dictionary

The dataset is split across two systems.

| Store | What it holds |
| --- | --- |
| Supabase Postgres, schema `public` | Market metadata, resolutions, and wallet pseudonyms |
| CloudFront Parquet files | Hourly order-book rows |

Counts below were checked on October 25, 2026 (Pacific time). The database is still being updated, so row counts will move. There are no declared foreign keys. The relationships below are the join keys that match.

Prices are on a 0 to 1 scale: the price of a share that pays 1 if that outcome happens. Volume and liquidity figures are the CLOB notional amounts reported by Polymarket.

## How the tables connect

```text
events.slug  <----  markets.event_slug
markets.slug <----  resolutions.market_slug
markets.slug <----  market_hourly.market_slug
markets.condition_id = parquet.market
markets.token_ids    contains parquet.asset_id
```

One event has many markets. A market has at most one resolution row. `market_hourly` is built to hold one OHLC row per market per hour, and it is empty. Each Parquet row is one order-book update for one outcome token of one market.

`markets.slug`, `markets.condition_id`, `markets.end_date`, and `markets.event_slug` are indexed. The volume and liquidity columns are not, so a ranking by volume reads the whole `markets` table.

## `events`

About 321,000 rows. One Polymarket event, which is the parent of one or more markets.

Primary key: `slug`.

| Column | Type | Meaning |
| --- | --- | --- |
| `slug` | text, required | Event id. This is the value stored in `markets.event_slug`. |
| `ticker` | text | Short event id. Usually the same as `slug`. About 50 rows have no ticker. Indexed. |
| `title` | text, required | Display name, for example "World Cup Winner". |
| `description` | text | Longer event text. Often empty. |
| `start_date` | timestamptz | When the event starts. Often empty. |
| `end_date` | timestamptz | When the event is scheduled to end. |
| `active` | boolean | About 1,200 events are inactive. The rest are true. |
| `closed` | boolean | True for a few dozen events. |
| `archived` | boolean | True for a few dozen events. |
| `neg_risk` | boolean | True when the event uses Polymarket negative risk. That is a group of mutually exclusive outcomes where only one can win. About 50,000 events. |
| `created_at` | timestamptz | When Polymarket created the event. |
| `updated_at` | timestamptz | When the event record last changed. |
| `synced_at` | timestamptz, required | When this row was copied into the class database. Defaults to the copy time. |

## `markets`

About 1.48 million rows. One tradable question.

Primary key: `slug`.

Every checked row has `active = true`, `closed = false`, and `archived = false`, including markets that already have a row in `resolutions`. Use `resolutions` to tell a resolved market from an open one. Do not filter on `markets.closed`.

| Column | Type | Meaning |
| --- | --- | --- |
| `slug` | text, required | Market id. Referenced by `resolutions.market_slug` and `market_hourly.market_slug`. |
| `title` | text, required | The question, for example "Will Spain win the 2026 FIFA World Cup?". |
| `condition_id` | text | 0x-prefixed condition id. Parquet column `market` matches this. Missing on a few dozen rows. Indexed. |
| `synced_at` | bigint, required | When this row was copied, as Unix seconds. This is not a timestamptz, unlike `events.synced_at`. `to_timestamp(synced_at)` converts it. |
| `start_date` | timestamptz | When the market opened. |
| `end_date` | timestamptz | Scheduled end. Indexed. Some dates run years ahead. |
| `active` | boolean | True on every row checked. |
| `closed` | boolean | False on every row checked. |
| `archived` | boolean | False on every row checked. |
| `created_at` | timestamptz | When Polymarket created the market. |
| `updated_at` | timestamptz | When the market record last changed. |
| `resolution_source` | text | URL of the source used to settle the market, such as a league scoreboard. Empty on about 285,000 rows. |
| `neg_risk` | boolean | True for negative-risk markets, about 353,000 rows. See `events.neg_risk`. |
| `enable_order_book` | boolean | True when the central limit order book is enabled. False on only a few dozen markets. |
| `last_trade_price` | numeric | Last traded price, from 0 to 1. Populated on roughly 220,000 markets. |
| `best_bid` | numeric | Highest current bid. Empty when nobody is bidding. |
| `best_ask` | numeric | Lowest current ask. Present on every row; a market with no real offer often shows a very small ask such as 0.001. |
| `volume_clob` | numeric | Lifetime CLOB volume. Populated on roughly 240,000 markets. |
| `liquidity_clob` | numeric | Current CLOB liquidity. Populated on about 1.33 million markets. |
| `volume_24hr` | numeric | Volume over the last 24 hours. Populated on roughly 220,000 markets. |
| `volume_1wk` | numeric | Volume over the last week. Same coverage as the other volume fields. |
| `volume_1mo` | numeric | Volume over the last month. Same coverage as the other volume fields. |
| `token_ids` | jsonb | JSON array of outcome token ids, as strings. Order matches `outcomes`. Parquet column `asset_id` is one of these ids. |
| `outcomes` | jsonb | JSON array of outcome names. Common pairs are `["Yes", "No"]` and `["Over", "Under"]`. |
| `event_slug` | text | Parent event. Join to `events.slug`. Indexed. |

Example:

```json
{
  "outcomes": ["Yes", "No"],
  "token_ids": [
    "17073877653540569521404580976357616106323083368735024114890581051225291734942",
    "13338576965898253089900616421992524995358355688684341164457946630448297559067"
  ]
}
```

The two arrays line up by position. On a Yes/No market, index 0 is Yes and index 1 is No. On an Over/Under market, index 0 is Over and index 1 is Under. The second outcome usually trades near one minus the first.

## `resolutions`

About 318,000 rows. One settlement per market.

Primary key: `market_slug`.

| Column | Type | Meaning |
| --- | --- | --- |
| `market_slug` | text, required | The settled market. Join to `markets.slug`. |
| `resolved_yes` | boolean, required | True when the Yes outcome won. False when it lost. |
| `price_at_close` | numeric, required | Settlement price. Observed values are 0 or 1. |
| `price_24h_before` | numeric | Price 24 hours before resolution. The column exists and is empty on every row. |
| `resolved_at` | timestamptz | When the market resolved. |
| `uma_resolution_status` | text | UMA oracle status. Almost every row is `resolved`. A handful are `proposed`. |

A market with no row here has not been recorded as resolved. `examples/explore.py --status unresolved` uses that test.

## `market_hourly`

Zero rows. The table and its indexes exist, but there is nothing to query yet.

Primary key: (`market_slug`, `hour`).

| Column | Type | Meaning |
| --- | --- | --- |
| `market_slug` | text, required | Market id. Join to `markets.slug`. |
| `hour` | timestamptz, required | Start of the hour. |
| `open` | numeric | First price in that hour. |
| `high` | numeric | Highest price in that hour. |
| `low` | numeric | Lowest price in that hour. |
| `close` | numeric | Last price in that hour. |
| `volume` | numeric | Volume during that hour. |
| `trade_count` | integer | Number of trades during that hour. |

## `wallet_pseudonym_map`

About 1.35 million rows. Maps a real wallet address to the pseudonym used in the class dataset.

Primary key: `real_wallet`. `pseudo_wallet` is also unique.

| Column | Type | Meaning |
| --- | --- | --- |
| `real_wallet` | bytea, required | Original 20-byte address. `encode(real_wallet, 'hex')` prints it as 40 hex characters. |
| `pseudo_wallet` | bytea, required | Replacement 20-byte address. Encode it the same way. |
| `created_at` | timestamptz, required | When the mapping was written. Defaults to the copy time. |

Both columns are raw bytes, not text. A query that selects them directly prints a byte string.

```sql
SELECT encode(pseudo_wallet, 'hex') AS pseudo_wallet,
       encode(real_wallet, 'hex') AS real_wallet
FROM wallet_pseudonym_map
LIMIT 5;
```

## Parquet order-book files

CloudFront bucket `https://d1yyqbluk0oz0t.cloudfront.net`. One file per hour:

```text
events-YYYY-MM-DDTHH.parquet
```

`HH` is the hour in UTC. `events-2026-05-30T21.parquet` starts at 2026-05-30 21:00:00 UTC. The bucket does not allow listing. Request a key by name. Files were present from `events-2026-05-30T20.parquet` through at least `events-2026-10-25T23.parquet`. A busy hour can be a few hundred megabytes.

| Column | Type | Meaning |
| --- | --- | --- |
| `ts_ms` | bigint | Event time, Unix milliseconds. `to_timestamp(ts_ms / 1000.0)` converts it. |
| `asset_id` | string | Outcome token id. Matches one entry of `markets.token_ids`. |
| `market` | string | Condition id. Join to `markets.condition_id`. |
| `side` | string | `BUY` or `SELL`. |
| `price` | double | Price on the 0 to 1 scale. |
| `size` | double | Size at that price. A size of 0 removes that level. |

`examples/cloudfront_duckdb.py` reads one hour. The password has to be sent as an HTTP basic-auth header. Putting it in the URL returns 401.
