# Output data dictionary

These CSVs are a player over/under run. The charted price is Yes, which is the Over.
A blank cell means that value was missing.
Tip is the listed start in markets.end_date, not the final buzzer.

## `summary.csv`

One row per panel.

| Column | Meaning |
| --- | --- |
| `rank` | Panel number, starting at 1. The same number joins a row to the other CSVs. |
| `team` | Short panel label, such as Brunson 24.5 pts. |
| `player` | Player name from the market title. |
| `code` | Stat family: points, assists, rebounds, threes, steals, or blocks. |
| `stat` | Same stat family as code. |
| `line` | Posted over/under number. The charted price is Yes, which is the Over. |
| `record` | Blank for a prop run. |
| `game` | Market slug of the charted contract. |
| `opponent` | Matchup from the slug, written as the away team at the home team. |
| `tip_utc` | Listed tip, taken from markets.end_date. Stored in UTC. |
| `pregame_std` | Standard deviation of the 5-minute mid over the 6 hours before tip. Blank when fewer than 3 bins exist. |
| `in_game_std` | Standard deviation of the 5-minute mid over the first 2.5 hours after tip. Blank when fewer than 3 bins exist. |
| `pregame_n` | How many 5-minute bins fall in the 6 hours before tip. |
| `in_game_n` | How many 5-minute bins fall in the first 2.5 hours after tip. |
| `pregame_price` | Last mid before the listed tip. Blank when the saved book starts after tip. |
| `first_price` | Earliest mid in the saved window. Charts use this when pregame_price is blank. |
| `won` | True when Yes won. Yes is the Over. This is the Polymarket resolution when resolutions has a row. When it does not, true means the ESPN box score is above the line. Blank when neither source settles it, including an exact push. |
| `variance_score_pregame` | Mean of the five-minute variance score over the 6 hours before tip. |
| `variance_score_early` | Mean variance score from tip until 1.25 hours after tip. |
| `variance_score_late` | Mean variance score from 1.25 hours after tip until 2.5 hours after tip. |
| `variance_score_live` | Mean variance score over the first 2.5 hours after tip. |
| `minutes_to_decided` | Minutes after tip when the mid first reaches 0.90 or 0.10. Blank if that never happens in the saved window. |
| `bid_ask_pregame` | Mean of ask minus bid over the hour before tip. |
| `bid_ask_live` | Mean of ask minus bid over the first 2.5 hours after tip. |
| `final_stat` | ESPN box-score total for this stat. Threes are made three-pointers. |
| `espn_margin` | final_stat minus the posted number. Positive means the player finished over. |
| `game_bucket` | For points, close means the absolute gap is 2 or less and blowout means 6 or more. For assists, rebounds, threes, steals, and blocks, close is 1 or less and blowout is 3 or more. |
| `book_size` | Sum of posted size (size greater than 0) for the charted token in the hour before tip, the tip hour, and the hour after. Posted size is the offer traffic in the book. It is not matched trade volume. |
| `book_updates` | How many of those posted-size rows the charted token had in those three hours. |
| `balance` | Lower of this market's matched-volume percentile and its posted-size percentile among the markets that were eligible. Panels are sorted by this, highest first, so both numbers have to be high. Game winners are eligible at volume_clob of at least 1,000. Props are eligible at any matched volume above zero. |
| `volume_clob` | Stored lifetime matched CLOB volume of this contract, in dollars. It is a snapshot from the markets table. Game-winner charts leave out markets under 1,000. Prop charts leave out markets at zero. |
| `score_source` | ESPN event id, written as espn:<id>. |
| `large_n` | Posted sizes at least 10 times the median size on that token and side in that hour, after repeats within 10 seconds are collapsed. |
| `followed_n` | How many of those large sizes were followed by a mid move of at least 0.03 in the same direction over the next 30 minutes. A buy has to move the mid up. A sell has to move it down. |
| `led_score_n` | How many large sizes both followed the price and arrived at least 15 seconds after the previous event and within 2 minutes before the next one. |
| `note` | Why a row has no price path or no ESPN result. Blank when both were found. |

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
| `stat_value` | Player's running stat from the ESPN play-by-play. After tip it stays 0 until the first recorded increase. Blank before tip, and blank for the whole game when the play text never names this player. |
| `prop_line` | Posted over/under number, repeated on every bin. Drawn as the dashed line. |
| `home_score` | Home team's score at this bin. Blank before tip. |
| `away_score` | Away team's score at this bin. Blank before tip. |
| `game_points` | Home score plus away score. Drawn on price_vs_score.png when the player's running stat is missing. |
| `spread_mid` | Mid of the next most traded line for the same player, stat, and game. |
| `team` | Short panel label, such as Brunson 24.5 pts. |
| `rank` | Panel number, starting at 1. The same number joins a row to the other CSVs. |
| `label` | Title drawn on the panel. |
| `bucket` | close, competitive, or blowout. The cutoffs are the same as game_bucket. |
| `final_stat` | ESPN box-score total, repeated on every bin. Drawn as the dotted line. |

## `alternate_spreads.csv`

One row per extra line read from the same hours.

| Column | Meaning |
| --- | --- |
| `rank` | Panel number, starting at 1. The same number joins a row to the other CSVs. |
| `team` | Short panel label, such as Brunson 24.5 pts. |
| `stat` | Stat family of this line. |
| `line` | Posted over/under number. |
| `pregame_price` | Last Yes (Over) mid before tip on this line. Blank when this line has no quote before tip. |
| `shown_price` | The dot on alternate_spreads.png. Pregame mid when one exists, otherwise the first saved mid. |
| `title` | Market title from the markets table. |
| `volume_clob` | Stored lifetime matched CLOB volume of this contract, in dollars. It is a snapshot from the markets table. Game-winner charts leave out markets under 1,000. Prop charts leave out markets at zero. |

## `game_results.csv`

The summary rows for panels that had an order book. The columns match summary.csv.

| Column | Meaning |
| --- | --- |
| `rank` | Panel number, starting at 1. The same number joins a row to the other CSVs. |
| `team` | Short panel label, such as Brunson 24.5 pts. |
| `player` | Player name from the market title. |
| `code` | Stat family: points, assists, rebounds, threes, steals, or blocks. |
| `stat` | Same stat family as code. |
| `line` | Posted over/under number. The charted price is Yes, which is the Over. |
| `record` | Blank for a prop run. |
| `game` | Market slug of the charted contract. |
| `opponent` | Matchup from the slug, written as the away team at the home team. |
| `tip_utc` | Listed tip, taken from markets.end_date. Stored in UTC. |
| `pregame_std` | Standard deviation of the 5-minute mid over the 6 hours before tip. Blank when fewer than 3 bins exist. |
| `in_game_std` | Standard deviation of the 5-minute mid over the first 2.5 hours after tip. Blank when fewer than 3 bins exist. |
| `pregame_n` | How many 5-minute bins fall in the 6 hours before tip. |
| `in_game_n` | How many 5-minute bins fall in the first 2.5 hours after tip. |
| `pregame_price` | Last mid before the listed tip. Blank when the saved book starts after tip. |
| `first_price` | Earliest mid in the saved window. Charts use this when pregame_price is blank. |
| `won` | True when Yes won. Yes is the Over. This is the Polymarket resolution when resolutions has a row. When it does not, true means the ESPN box score is above the line. Blank when neither source settles it, including an exact push. |
| `variance_score_pregame` | Mean of the five-minute variance score over the 6 hours before tip. |
| `variance_score_early` | Mean variance score from tip until 1.25 hours after tip. |
| `variance_score_late` | Mean variance score from 1.25 hours after tip until 2.5 hours after tip. |
| `variance_score_live` | Mean variance score over the first 2.5 hours after tip. |
| `minutes_to_decided` | Minutes after tip when the mid first reaches 0.90 or 0.10. Blank if that never happens in the saved window. |
| `bid_ask_pregame` | Mean of ask minus bid over the hour before tip. |
| `bid_ask_live` | Mean of ask minus bid over the first 2.5 hours after tip. |
| `final_stat` | ESPN box-score total for this stat. Threes are made three-pointers. |
| `espn_margin` | final_stat minus the posted number. Positive means the player finished over. |
| `game_bucket` | For points, close means the absolute gap is 2 or less and blowout means 6 or more. For assists, rebounds, threes, steals, and blocks, close is 1 or less and blowout is 3 or more. |
| `book_size` | Sum of posted size (size greater than 0) for the charted token in the hour before tip, the tip hour, and the hour after. Posted size is the offer traffic in the book. It is not matched trade volume. |
| `book_updates` | How many of those posted-size rows the charted token had in those three hours. |
| `balance` | Lower of this market's matched-volume percentile and its posted-size percentile among the markets that were eligible. Panels are sorted by this, highest first, so both numbers have to be high. Game winners are eligible at volume_clob of at least 1,000. Props are eligible at any matched volume above zero. |
| `volume_clob` | Stored lifetime matched CLOB volume of this contract, in dollars. It is a snapshot from the markets table. Game-winner charts leave out markets under 1,000. Prop charts leave out markets at zero. |
| `score_source` | ESPN event id, written as espn:<id>. |
| `large_n` | Posted sizes at least 10 times the median size on that token and side in that hour, after repeats within 10 seconds are collapsed. |
| `followed_n` | How many of those large sizes were followed by a mid move of at least 0.03 in the same direction over the next 30 minutes. A buy has to move the mid up. A sell has to move it down. |
| `led_score_n` | How many large sizes both followed the price and arrived at least 15 seconds after the previous event and within 2 minutes before the next one. |
| `note` | Why a row has no price path or no ESPN result. Blank when both were found. |

## `large_orders.csv`

One row per unusually large posted size on the charted token. There is no wallet.

| Column | Meaning |
| --- | --- |
| `rank` | Panel number, starting at 1. The same number joins a row to the other CSVs. |
| `team` | Short panel label, such as Brunson 24.5 pts. |
| `label` | Title drawn on the panel. |
| `time` | UTC time of the book update. |
| `hours_from_tip` | Hours after the listed tip. Negative values are before tip. |
| `side` | BUY is a posted bid. SELL is a posted ask. This is a resting size, not a matched trade, and it has no wallet. |
| `price` | Price of that posted size, on the 0 to 1 contract scale. |
| `size` | Posted size. A size of 0 means the level was removed, and those rows are left out because a cancel and a fill look the same. |
| `med_size` | Median posted size on this token and side in that clock hour. |
| `size_ratio` | size divided by med_size. A row is kept when this is at least 10 and that side had at least 20 updates in the hour. |
| `mid_at` | Latest 5-minute mid at or before this update. |
| `mid_later` | Next 5-minute mid at least 30 minutes after this update. |
| `price_followed` | True when mid_later is at least 0.03 further in the order's direction than mid_at. |
| `led_score` | True when the player's previous stat increase was at least 15 seconds earlier, or there was none, and the next increase came within 2 minutes. |
| `stands_out` | True when price_followed and led_score are both true. These are the dark marks on large_orders.png. |
