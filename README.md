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

## NBA game winners

`examples/nba_analysis.py` charts completed NBA game-winner markets. It does not start from a team list. The slug equals the event slug, so the two outcomes are the two teams, and the price on each panel is the away team.

```bash
python examples/nba_analysis.py
python examples/nba_analysis.py --find top20
python examples/nba_analysis.py --find props5
python examples/nba_analysis.py --find props --each 4 --stats points,assists,rebounds
python examples/nba_analysis.py --find props5 --plots-only
python examples/nba_analysis.py --plots-only
```

The default is `--find top10`. It reads the hourly order-book files around every completed NBA tip: the hour before, the tip hour, and the hour after. That is about 143 distinct files, each read once. A market is kept only when two things are true. Its token appears in those files, and its stored matched volume (`volume_clob`) is at least 1,000. In this extract the game-winner volumes jump from about 900 to about 2,700, so 1,000 sits in that gap and drops the quote-only markets. Knicks at Spurs on June 5 is the example: about 203,000 book updates and a posted size near 10.7 billion, with matched volume of 129. That game is left out.

Among the markets that clear the bar, each one gets two percentiles: one for matched volume, one for posted size. The score is the lower of those two. Rank 1 is the market whose weaker number is still the strongest. A huge book cannot carry a tiny volume, and a huge volume cannot carry a book that barely shows up. If fewer than 10 games clear both bars, the run charts those and stops. Any `topN` from 1 to 40 works the same way. The price on each panel is the away team. `summary.csv` includes `volume_clob`, `book_size`, `book_updates`, and `balance`. This search runs before the usual chart window. The chart pass still reads six hours before tip through three hours after, for the kept games only. A market with a large `volume_clob` and no rows in the hourly files is still left out.

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
| `summary.csv` | One row per game, including the variance scores, the ESPN margin, and the game total. |
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

For each requested stat the script reads the same three hours around every completed game's tip and sums posted size on the Yes token. Yes is the Over. A line has to appear in those files, and its matched volume has to be above zero. The 1,000 floor is only for game winners. Prop volumes are smaller: threes, steals, and blocks in this extract all sit under 1,000, and most points, assists, and rebounds lines do too. The charted lines are the ones whose weaker of the two percentiles is highest, so a busy book with no trades stays out and a large `volume_clob` with no book stays out. When one player has several numbers in the same game, the line that is charted is the one with the higher balance score. A stat can still come back short when few of its lines have both a book and any matched volume. Other posted numbers for that same player are still read during the chart pass and drawn as the alternate-line chart. The run writes `outputs/nba/props/`, so it does not replace the game-winner charts. `DATA_DICTIONARY.md` in that folder defines the prop CSV columns, including `won`, `final_stat`, `espn_margin`, `book_size`, `book_updates`, and `balance`. `--find props5 --plots-only` redraws that folder and writes the dictionary from the CSVs already there. It does not repeat the hour search. The search itself is about the same length as `--find top5`, because both walk every completed tip. The file download is the slow part.

The price, variance, bid-ask gap, and large-order charts are the Over price. A panel still appears when the book starts after tip: variance uses whichever window has at least three quotes, and the calibration and final-stat charts use the first saved mid when there is no quote before tip. `price_vs_score.png` draws the Over price against the player's running stat, with a dashed line at the posted number and a dotted line at the ESPN final. When the play-by-play never names that player, the right axis is the game point total instead, and the final is still the dotted line. `espn_margin` in the summary is the final stat minus the posted number. A points prop is "close" when that gap is 2 or less and a "blowout" when it is 6 or more. Assists, rebounds, threes, steals, and blocks use 1 and 3. On the large-order chart, a dark mark means the Over price then moved that way and the order came at least 15 seconds after the player's previous stat and within two minutes before the next one. There is still no wallet on the row.

The class database does not store the point margin. `won` is the Polymarket resolution, and the score, quarter lines, and margin come from ESPN. `markets.best_bid` is the latest snapshot, not the history, so the price charts come from the Parquet files. A game winner is charted only when its token was in the order book and its matched volume is at least 1,000. A prop is charted when its token was in the order book and its matched volume is above zero.