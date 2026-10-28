# Output data dictionary

These CSVs are a game-winner run. The charted price is the team named on the panel.
A blank cell means that value was missing.
Tip is the listed start in markets.end_date, not the final buzzer.

## `summary.csv`

One row per panel.

| Column | Meaning |
| --- | --- |
| `rank` | Panel number, starting at 1. The same number joins a row to the other CSVs. |
| `team` | Name printed on the panel. |
| `code` | Three-letter team code from the slug. |
| `record` | Blank. Panels are NBA markets ranked from the order book, not a team ranking. |
| `game` | Market slug of the charted contract. |
| `opponent` | The other team. |
| `tip_utc` | Listed tip, taken from markets.end_date. Stored in UTC. |
| `pregame_std` | Standard deviation of the 5-minute mid over the 6 hours before tip. Blank when fewer than 3 bins exist. |
| `in_game_std` | Standard deviation of the 5-minute mid over the first 2.5 hours after tip. Blank when fewer than 3 bins exist. |
| `pregame_price` | Last mid before the listed tip. Blank when the saved book starts after tip. |
| `won` | True when the charted team won on Polymarket. resolutions.resolved_yes is true when the first outcome won, and the away team is that first outcome. Blank when the market has no resolution row. |
| `spread` | Title of the highest-volume full-game spread in this event. |
| `spread_pregame` | Last mid before tip on the charted team's side of that spread. |
| `variance_score_pregame` | Mean of the five-minute variance score over the 6 hours before tip. |
| `variance_score_early` | Mean variance score from tip until 1.25 hours after tip. |
| `variance_score_late` | Mean variance score from 1.25 hours after tip until 2.5 hours after tip. |
| `variance_score_live` | Mean variance score over the first 2.5 hours after tip. |
| `minutes_to_decided` | Minutes after tip when the mid first reaches 0.90 or 0.10. Blank if that never happens in the saved window. |
| `large_n` | Posted sizes at least 10 times the median size on that token and side in that hour, after repeats within 10 seconds are collapsed. The median and this count use only prints within 10 cents of the mid in that five-minute bin. |
| `followed_n` | How many of those large sizes were followed by a mid move of at least 0.03 in the same direction over the next 30 minutes. A buy has to move the mid up. A sell has to move it down. |
| `led_score_n` | How many large sizes both followed the price and arrived at least 15 seconds after the previous event and within 2 minutes before the next one. |
| `espn_team_score` | Charted team's final score from ESPN. |
| `espn_opponent_score` | Opponent's final score from ESPN. |
| `espn_margin` | Charted team's score minus the opponent's score. |
| `game_bucket` | close when the absolute margin is 5 or less, blowout when it is 15 or more, otherwise competitive. Blank when ESPN has no score. |
| `final_points` | Both teams' final scores added together. |
| `total_line` | Posted number on the highest-volume full-game total. |
| `total_pregame` | Last mid before tip of the Over on that total. |
| `note` | Why a row has no price path or no ESPN result. Blank when both were found. |
| `opponent_code` | Three-letter code of the other team. |
| `pregame_n` | How many 5-minute bins fall in the 6 hours before tip. |
| `in_game_n` | How many 5-minute bins fall in the first 2.5 hours after tip. |
| `first_price` | Earliest mid in the saved window. Charts use this when pregame_price is blank. |
| `spread_line` | Point number of that spread from the charted team's side. Negative means that team is laying the points. |
| `bid_ask_pregame` | Mean of ask minus bid over the hour before tip. |
| `bid_ask_live` | Mean of ask minus bid over the first 2.5 hours after tip. |
| `quarters` | Quarter scores with the charted team first, such as Q1:28-25. |
| `total_hit` | True when final_points is above total_line. |
| `book_size` | Sum of posted size (size greater than 0) for the charted token in the hour before tip, the tip hour, and the hour after. Posted size is the offer traffic in the book. It is not matched trade volume. |
| `book_updates` | How many of those posted-size rows the charted token had in those three hours. |
| `balance` | Lower of this market's matched-volume percentile and its posted-size percentile among the markets that were eligible. Panels are sorted by this, highest first, so both numbers have to be high. Game winners are eligible at volume_clob of at least 1,000. Props are eligible at any matched volume above zero. |
| `volume_clob` | Stored lifetime matched CLOB volume of this contract, in dollars. It is a snapshot from the markets table. Game-winner charts leave out markets under 1,000. Prop charts leave out markets at zero. |
| `score_source` | ESPN event id, written as espn:<id>. |

## `price_paths.csv`

One row per five-minute bin of the token drawn on the panel.

| Column | Meaning |
| --- | --- |
| `market` | Condition id of the charted contract. This joins to markets.condition_id and to the Parquet market column. |
| `asset_id` | Token id of the charted outcome. |
| `bin` | Start of the 5-minute window, in UTC. |
| `best_bid` | Highest buy price with size still posted in that 5-minute window. |
| `best_ask` | Lowest sell price with size still posted in that 5-minute window. |
| `mid` | Average of best bid and best ask. When only one side is present, that side's price. |
| `hours_from_tip` | Hours after the listed tip. Negative values are before tip. |
| `variance_score` | Standard deviation of the mid over the trailing 30 minutes, which is 6 bins. Blank until 3 bins exist. |
| `spread_width` | Best ask minus best bid in that bin. |
| `score_margin` | Charted team's score minus the opponent's score, using the latest ESPN play at or before this bin. Blank before tip. |
| `team_score` | Charted team's score at this bin. |
| `opponent_score` | Opponent's score at this bin. |
| `spread_mid` | Mid of the highest-volume spread in this bin, from the charted team's side. |
| `total_mid` | Mid of the Over on the highest-volume game total in this bin. |
| `team` | Name printed on the panel. |
| `rank` | Panel number, starting at 1. The same number joins a row to the other CSVs. |
| `label` | Title drawn on the panel. |
| `bucket` | close, competitive, or blowout. The cutoffs are the same as game_bucket. |
| `profit_to_close` | Profit per share of buying at this bin's mid and holding to settlement. The contract pays 1 when the charted outcome won and 0 when it lost, so a buy at 0.40 that wins is 0.60. Positive means that buyer made money. Blank when the market has no settlement. The order book is not a trade tape, so this is one share at the mid, not a matched fill. |
| `outcome` | 1 when the charted outcome happened, 0 when it did not. Blank when the market has no settlement. |
| `win_chance` | The five-minute mid. This is the market's probability that the charted outcome happens. |
| `called_win` | 1 when the mid is above 0.5, 0 when it is below. Blank at exactly 0.5. |
| `call_correct` | 1 when that call matches the final result, 0 when it does not. |
| `brier` | Squared gap between the win chance and the outcome. A coin flip averages 0.25. |

## `alternate_spreads.csv`

One row per extra line read from the same hours.

| Column | Meaning |
| --- | --- |
| `rank` | Panel number, starting at 1. The same number joins a row to the other CSVs. |
| `team` | Name printed on the panel. |
| `line` | Spread number from the charted team's side. Negative means that team is laying the points. |
| `pregame_price` | Last mid before tip on that team's side of this spread. |
| `title` | Market title from the markets table. |
| `volume_clob` | Stored lifetime matched CLOB volume of this contract, in dollars. It is a snapshot from the markets table. Game-winner charts leave out markets under 1,000. Prop charts leave out markets at zero. |

## `game_results.csv`

The summary rows for panels that had an order book. The columns match summary.csv.

| Column | Meaning |
| --- | --- |
| `rank` | Panel number, starting at 1. The same number joins a row to the other CSVs. |
| `team` | Name printed on the panel. |
| `code` | Three-letter team code from the slug. |
| `record` | Blank. Panels are NBA markets ranked from the order book, not a team ranking. |
| `game` | Market slug of the charted contract. |
| `opponent` | The other team. |
| `tip_utc` | Listed tip, taken from markets.end_date. Stored in UTC. |
| `pregame_std` | Standard deviation of the 5-minute mid over the 6 hours before tip. Blank when fewer than 3 bins exist. |
| `in_game_std` | Standard deviation of the 5-minute mid over the first 2.5 hours after tip. Blank when fewer than 3 bins exist. |
| `pregame_price` | Last mid before the listed tip. Blank when the saved book starts after tip. |
| `won` | True when the charted team won on Polymarket. resolutions.resolved_yes is true when the first outcome won, and the away team is that first outcome. Blank when the market has no resolution row. |
| `spread` | Title of the highest-volume full-game spread in this event. |
| `spread_pregame` | Last mid before tip on the charted team's side of that spread. |
| `variance_score_pregame` | Mean of the five-minute variance score over the 6 hours before tip. |
| `variance_score_early` | Mean variance score from tip until 1.25 hours after tip. |
| `variance_score_late` | Mean variance score from 1.25 hours after tip until 2.5 hours after tip. |
| `variance_score_live` | Mean variance score over the first 2.5 hours after tip. |
| `minutes_to_decided` | Minutes after tip when the mid first reaches 0.90 or 0.10. Blank if that never happens in the saved window. |
| `large_n` | Posted sizes at least 10 times the median size on that token and side in that hour, after repeats within 10 seconds are collapsed. The median and this count use only prints within 10 cents of the mid in that five-minute bin. |
| `followed_n` | How many of those large sizes were followed by a mid move of at least 0.03 in the same direction over the next 30 minutes. A buy has to move the mid up. A sell has to move it down. |
| `led_score_n` | How many large sizes both followed the price and arrived at least 15 seconds after the previous event and within 2 minutes before the next one. |
| `espn_team_score` | Charted team's final score from ESPN. |
| `espn_opponent_score` | Opponent's final score from ESPN. |
| `espn_margin` | Charted team's score minus the opponent's score. |
| `game_bucket` | close when the absolute margin is 5 or less, blowout when it is 15 or more, otherwise competitive. Blank when ESPN has no score. |
| `final_points` | Both teams' final scores added together. |
| `total_line` | Posted number on the highest-volume full-game total. |
| `total_pregame` | Last mid before tip of the Over on that total. |
| `note` | Why a row has no price path or no ESPN result. Blank when both were found. |
| `opponent_code` | Three-letter code of the other team. |
| `pregame_n` | How many 5-minute bins fall in the 6 hours before tip. |
| `in_game_n` | How many 5-minute bins fall in the first 2.5 hours after tip. |
| `first_price` | Earliest mid in the saved window. Charts use this when pregame_price is blank. |
| `spread_line` | Point number of that spread from the charted team's side. Negative means that team is laying the points. |
| `bid_ask_pregame` | Mean of ask minus bid over the hour before tip. |
| `bid_ask_live` | Mean of ask minus bid over the first 2.5 hours after tip. |
| `quarters` | Quarter scores with the charted team first, such as Q1:28-25. |
| `total_hit` | True when final_points is above total_line. |
| `book_size` | Sum of posted size (size greater than 0) for the charted token in the hour before tip, the tip hour, and the hour after. Posted size is the offer traffic in the book. It is not matched trade volume. |
| `book_updates` | How many of those posted-size rows the charted token had in those three hours. |
| `balance` | Lower of this market's matched-volume percentile and its posted-size percentile among the markets that were eligible. Panels are sorted by this, highest first, so both numbers have to be high. Game winners are eligible at volume_clob of at least 1,000. Props are eligible at any matched volume above zero. |
| `volume_clob` | Stored lifetime matched CLOB volume of this contract, in dollars. It is a snapshot from the markets table. Game-winner charts leave out markets under 1,000. Prop charts leave out markets at zero. |
| `score_source` | ESPN event id, written as espn:<id>. |

## `large_orders.csv`

One row per unusually large posted size on the charted token. There is no wallet.

| Column | Meaning |
| --- | --- |
| `rank` | Panel number, starting at 1. The same number joins a row to the other CSVs. |
| `team` | Name printed on the panel. |
| `label` | Title drawn on the panel. |
| `time` | UTC time of the book update. |
| `hours_from_tip` | Hours after the listed tip. Negative values are before tip. |
| `side` | BUY is a posted bid. SELL is a posted ask. This is a resting size, not a matched trade, and it has no wallet. |
| `price` | Price of that posted size, on the 0 to 1 contract scale. A row more than 10 cents from the mid at that time is left out of large_orders.csv. |
| `size` | Posted size. A size of 0 means the level was removed, and those rows are left out because a cancel and a fill look the same. |
| `med_size` | Median posted size on this token and side in that clock hour, using only prints within 10 cents of the mid. |
| `size_ratio` | size divided by med_size. A row is kept when this is at least 10, the price is within 10 cents of the mid, and that side had at least 20 such updates in the hour. |
| `mid_at` | Latest 5-minute mid at or before this update. |
| `mid_later` | Next 5-minute mid at least 30 minutes after this update. |
| `price_followed` | True when mid_later is at least 0.03 further in the order's direction than mid_at. |
| `led_score` | True when the previous scoring play was at least 15 seconds earlier, or there was no earlier scoring play, and the next scoring play came within 2 minutes. |
| `stands_out` | True when price_followed and led_score are both true. These are the dark marks on large_orders.png. |

## `accuracy_by_time.csv`

One row per five-minute mark. Each market is counted once at that mark.

| Column | Meaning |
| --- | --- |
| `minutes_from_tip` | Five-minute mark relative to the listed tip. Negative is before tip. |
| `hours_from_tip` | That same mark in hours. |
| `markets` | Markets with a quote at that mark. Each market is counted once. |
| `mean_win_chance` | Average mid at that mark, one number per market. |
| `actual_win_rate` | Share of those markets in which the charted outcome happened. |
| `calls` | Quotes that were not exactly 0.5. |
| `call_hit_rate` | Share of those calls that matched the final result. |
| `brier` | Average squared gap between the win chance and the outcome. |

## `accuracy_by_price.csv`

One row per 10-point price band. A market quoted in the band is counted once, and the win rate is how often the charted outcome happened.

| Column | Meaning |
| --- | --- |
| `band_low` | Low end of the price band. 0.50 means prices from 0.50 up to 0.60. |
| `band_high` | High end of the price band. |
| `band` | Price band label, such as 0.50–0.60. |
| `markets` | Markets with at least one quote in the band. Each market is counted once. |
| `bins` | Five-minute rows in the band before that one-per-market collapse. |
| `mean_win_chance` | Average mid in the band, one number per market. The win rate on the next column should sit near this price. |
| `actual_win_rate` | Share of those markets in which the charted outcome happened. |
| `brier` | Average squared gap between the win chance and the outcome. |
