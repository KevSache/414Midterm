# UFC output columns

Written by `examples/ufc_analysis.py`. Prices are on a 0 to 1 scale.
The clock is hours from the listed market end, which is shared by the card and is not the bell.

## `summary.csv`

- `rank`: 1 is the fight whose weaker of volume and posted size is highest.
- `fighter`: First listed fighter. The charted price is this fighter's win price.
- `opponent`: The other fighter.
- `slug`: Fight market slug. It equals the event slug.
- `weight`: Weight class from the market title.
- `card`: Main Card or Prelims, from the market title.
- `tip_utc`: Listed market end, UTC. Shared by every bout on the card. Not the bell.
- `volume_clob`: Lifetime matched volume stored on the fight market.
- `book_size`: Posted size summed over the card window. Not matched trades.
- `book_updates`: Order-book updates with size above zero in that window.
- `balance`: Lower of the volume percentile and the posted-size percentile.
- `won`: True when the charted fighter won. Polymarket's resolution when it exists, otherwise ESPN.
- `settle_source`: polymarket or espn.
- `espn_winner`: Winner name on the ESPN scoreboard.
- `method`: KO/TKO, submission, or decision, from ESPN.
- `finish_round`: Round in which the fight ended.
- `finish_clock`: Elapsed time in that round, as ESPN prints it.
- `scheduled_rounds`: 3 or 5, from the ESPN bout format.
- `rounds_fought`: Completed rounds plus the fraction of the finishing round. A 5:00 clock counts as a full round.
- `went_distance`: True when ESPN records a decision in the scheduled final round.
- `result_agree`: True when Polymarket and ESPN name the same winner.
- `card_price`: Earliest saved mid in the window. This is the price near the start of the card.
- `late_price`: Last mid before the listed end.
- `early_std`: Standard deviation of the mid from the start of the window until two hours before the listed end.
- `late_std`: Standard deviation of the mid in the last two hours before the listed end.
- `large_n`: Large orders within 10 cents of the mid.
- `followed_n`: Large orders whose mid moved at least 3 cents that way over the next 30 minutes.
- `total_line`: Round total with the most matched volume, such as 1.5.
- `total_price`: Earliest saved Over mid on that total.
- `total_hit`: True when rounds_fought is above the total.
- `distance_price`: Earliest saved Yes mid on go-the-distance.
- `ko_price`: Earliest saved Yes mid on fight won by KO or TKO.
- `submission_price`: Earliest saved Yes mid on fight won by submission.
- `decision_price`: Earliest saved Yes mid on fight won by decision.
- `score_source`: ESPN event id when a bout matched.
- `note`: Why a result or a book is missing.

## `price_paths.csv`

- `market`: Copied from the fight row or the five-minute book.
- `asset_id`: Copied from the fight row or the five-minute book.
- `bin`: Copied from the fight row or the five-minute book.
- `best_bid`: Copied from the fight row or the five-minute book.
- `best_ask`: Copied from the fight row or the five-minute book.
- `mid`: Average of the best bid and best ask in that five-minute bin. This is the market's win chance for the charted fighter.
- `hours_from_end`: Hours from the listed market end. Negative is earlier on the card.
- `variance_score`: Copied from the fight row or the five-minute book.
- `spread_width`: Best ask minus best bid. Negative when the bin's high bid is above its low ask. Those two prints need not be simultaneous.
- `profit_to_close`: Settlement minus the mid. Settlement is 1 or 0 for the charted fighter.
- `rank`: Copied from the fight row or the five-minute book.
- `fighter`: Copied from the fight row or the five-minute book.
- `slug`: Copied from the fight row or the five-minute book.
- `label`: Copied from the fight row or the five-minute book.
- `outcome`: 1 when the charted fighter won, 0 when that fighter lost. Blank when the result is unknown.
- `win_chance`: Same number as mid. Kept under this name in the prediction tables.
- `called_win`: 1 when the mid is above 0.5, 0 when it is below. Blank at exactly 0.5.
- `call_correct`: 1 when that call matches the final result, 0 when it does not.
- `brier`: Squared gap between the win chance and the outcome. A coin flip averages 0.25.

## `side_markets.csv`

- `rank`: Copied from the fight row or the five-minute book.
- `fighter`: Copied from the fight row or the five-minute book.
- `slug`: Copied from the fight row or the five-minute book.
- `kind`: distance, ko, submission, decision, or total.
- `line`: Copied from the fight row or the five-minute book.
- `title`: Copied from the fight row or the five-minute book.
- `volume_clob`: Copied from the fight row or the five-minute book.
- `card_price`: Copied from the fight row or the five-minute book.
- `yes_won`: True when that side market's Yes or Over won, from the ESPN result.

## `large_orders.csv`

- `rank`: Copied from the fight row or the five-minute book.
- `fighter`: Copied from the fight row or the five-minute book.
- `time`: Copied from the fight row or the five-minute book.
- `hours_from_end`: Copied from the fight row or the five-minute book.
- `side`: Copied from the fight row or the five-minute book.
- `price`: Copied from the fight row or the five-minute book.
- `size`: Copied from the fight row or the five-minute book.
- `med_size`: Copied from the fight row or the five-minute book.
- `size_ratio`: Posted size divided by the median nearby size on that token and side.
- `mid_at`: Copied from the fight row or the five-minute book.
- `mid_later`: Copied from the fight row or the five-minute book.
- `price_followed`: True when the mid 30 minutes later moved at least 3 cents in the order's direction.
- `led_score`: Copied from the fight row or the five-minute book.
- `stands_out`: Copied from the fight row or the five-minute book.

## `accuracy_by_time.csv`

- `minutes_from_end`: Five-minute mark on the clock, negative before the listed market end.
- `hours_from_end`: That same mark in hours.
- `markets`: Fights with a quote at that mark. Each fight is counted once.
- `mean_win_chance`: Average mid at that mark, one number per fight.
- `actual_win_rate`: Share of those fights the charted fighter won.
- `calls`: Quotes that were not exactly 0.5.
- `call_hit_rate`: Share of those calls that matched the final result.
- `brier`: Average squared gap between the win chance and the outcome.

## `accuracy_by_price.csv`

- `band_low`: Copied from the fight row or the five-minute book.
- `band_high`: Copied from the fight row or the five-minute book.
- `band`: Price band, such as 0.70–0.80.
- `markets`: Fights with at least one quote in the band. Each fight is counted once.
- `bins`: Five-minute rows in the band, before each fight is collapsed to one row.
- `mean_win_chance`: Average mid in the band, one number per fight.
- `actual_win_rate`: Share of those fights the charted fighter won.
- `brier`: Average squared gap between the win chance and the outcome.
