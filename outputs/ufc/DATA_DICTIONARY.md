# UFC liquidity output data dictionary

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
