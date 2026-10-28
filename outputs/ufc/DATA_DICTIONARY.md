# UFC output data dictionary

Written by `script/ufc_analysis.py`. Times are UTC. Prices are 0 to 1. A blank cell is a missing value.
The charted price is the first-named fighter in each winner market (`fighter`); `opponent` is the other.
The opening bell, round ends, and final bell come from ESPN's play-by-play, not the listed start.

## `summary.csv`
One row per fight whose winner market has an order book.

| Column | Meaning |
| --- | --- |
| `bout_id` | ESPN card day and bout order. Joins every other file. |
| `fighter`, `opponent` | Charted fighter and the other. |
| `slug`, `title`, `volume_clob` | Winner market slug, title, and stored lifetime matched volume. |
| `segment`, `main_event` | Card segment from ESPN, and whether it was the last bout. |
| `start`, `round1_end`, `end` | Opening bell, end of round 1 (the final bell if it ended in round 1), and final bell. |
| `won` | True when the charted fighter won (ESPN). |
| `method`, `end_round`, `ending` | ESPN result, the round it ended in, and `R1 finish`, `later finish`, or `decision`. |
| `sig_a`, `sig_b`, `winner_sig_share` | Significant strikes landed by ESPN's two fighters, and the winner's share. |
| `fight_minutes` | Bell to bell, including rests between rounds. |
| `knockdowns`, `takedowns`, `submission_attempts` | ESPN play-by-play counts. |
| `pregame_std` | Std of the one-minute mid over the 3 hours before the bell. Blank under 3 bins. |
| `in_fight_std` | Std of the one-minute mid from the opening to the final bell. Blank under 3 bins. |
| `pregame_n`, `in_fight_n` | One-minute bins with a two-sided quote before and during the fight. |
| `pre_fight_price` | Last mid before the opening bell. |
| `upset` | True when the fighter priced under 0.5 won, or the one over 0.5 lost. |
| `variance_score_pregame` | Mean variance score over the 3 hours before the bell. |
| `variance_score_round1`, `variance_score_later`, `variance_score_live` | Mean variance score in round 1, after round 1, and over the whole fight. |
| `minutes_to_decided` | Minutes after the opening bell when the mid first reached 0.9 or 0.1, searching through 5 minutes after the final bell. Blank if it never did. |
| `bid_ask_pregame`, `bid_ask_live` | Mean ask minus bid over the hour before the bell, and during the fight. |
| `large_n`, `followed_n`, `against_n`, `led_event_n` | Large posted sizes in the 3 hours before and during the fight, how many the price followed, how many it moved as far against (a baseline the NBA script does not have), and how many also led a fight event (see `large_orders.csv`). |

## `price_paths.csv`
One row per fight and minute, from 3 hours before the bell to 5 minutes after the final bell: `mid` (the
average of bid and ask, or the one side posted, as in nba_analysis.py) and `spread` (two-sided only) at the minute's end, `variance_score` (std of the mid over the trailing 5 minutes, blank
under 3 bins), `minutes_from_start`, and `phase` (pre-fight, round 1, later rounds, after).

## `event_moves.csv`
One row per knockdown, takedown, or submission attempt with quotes around it, and per in-fight window away from any
event (`type` = no event): `move` is the absolute change in the mid over the 60 seconds after it.

## `large_orders.csv`
One row per large posted size in the 3 hours before or during a fight: a row with size at least
10 times the median size on that market and side in that clock hour, with a repeat of the same size at
the same price within 10 seconds dropped. `side` is BUY (a bid for the charted fighter) or SELL.
`move_after` is the mid change over the next 5 minutes; `followed` is true when it was at least
0.03 in the order's direction, and `against` when it was at least that far the other way; `led_event` is true when the order also came during the fight, at least
15 seconds after the previous fight event, and within 120 seconds before the next one.
There is no wallet on any row.

## Saved by a querying run (`--from-csv` starts here)
`markets.csv` (linked winner markets and their `token` number), `bouts.csv` (ESPN results, strike totals, and bells),
`fight_events.csv` (every ESPN play with its wall-clock `time`, `type`, `round`, and `clock_s`), `quotes.csv.gz` (best
`bid` and `ask` of each winner market at the end of every 10-second bin, carried forward), and
`large_posts.csv` (the large posted sizes before scoring).
