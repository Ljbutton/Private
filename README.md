# NFL Picker

A self-hosted NFL projection and pick engine. It pulls schedules, scores, odds
from multiple sportsbooks, play-by-play efficiency data and news; projects every
game and the full season; compares its own numbers against the sportsbook
consensus; keeps the history of both so you can see how each moved; and turns
all of that into recommended picks for ESPN pick'em, survivor pools and betting
markets.

Runs locally. One command, one page, no cloud services.

```bash
make demo      # runs immediately on a synthetic season — no keys, no network
make run       # live sources
```

Then open <http://127.0.0.1:8000>.

---

## What it does

**Projects every game.** A gradient-boosted model over Elo, EPA efficiency,
rest, travel, weather and situation, blended with the market (see
[How the model works](#how-the-model-works)).

**Projects the season.** Twenty thousand Monte Carlo seasons produce expected
wins, the win distribution, playoff, division, bye and championship odds, and
an over/under read against posted season win totals.

**Compares to the sportsbook consensus.** Spread, total and moneyline from every
book the odds feed carries, de-vigged and averaged. It also tracks the *best
available* number at any single book, which is the one you would actually bet.

**Remembers.** Every refresh snapshots both the market and our own prediction,
so each game has a movement chart showing the consensus, the individual books,
and our number over time — plus a full prediction history.

**Flags news that matters.** Aggregates ESPN, ProFootballTalk, CBS, Yahoo and
NFL.com, classifies each item (QB / injury / suspension / transaction /
coaching / weather), attributes it to teams, estimates its effect on the spread
in points, and attaches it to the games it touches. Sorted by relevance, not
recency.

**Recommends picks.**
- *Pick'em* — straight picks plus optimal confidence-point assignment, with a
  leverage mode for large pools.
- *Survivor* — a planned path over the remaining weeks, not just this week's
  safest team, so you don't burn a team you'll need in November.
- *Betting* — spread, total and moneyline edges priced against the best
  available number, with expected value and quarter-Kelly stakes.

**Grades itself.** Every finished game is scored: ATS record, ROI, straight-up
accuracy, Brier score, calibration curve, and closing-line value.

---

## Setup

Requires Python 3.10+.

```bash
git clone https://github.com/Ljbutton/ESPNpicem.git
cd ESPNpicem
make install
cp .env.example .env      # optional, see below
make run
```

### Sportsbook odds (optional but recommended)

Without a key the app falls back to ESPN's single consensus line — usable, but
you lose book-by-book comparison and best-number pricing.

With a free key from [the-odds-api.com](https://the-odds-api.com) (500 requests
a month) you get real lines from DraftKings, FanDuel, BetMGM, Caesars and
others. Put it in `.env`:

```
ODDS_API_KEY=your_key_here
```

The refresher is **budget-aware**: it tracks its own monthly usage and stretches
the polling interval so a free tier lasts the whole month instead of running out
in week 6. The current count is shown in the status bar.

### Training the model

The app works out of the box on power ratings alone. To train the full model:

```bash
make train                       # downloads nflverse history, ~1 minute
.venv/bin/python -m nflpicker.cli train --epa-seasons 6   # adds EPA features
```

This downloads every NFL game since 1999 with its historical closing line,
builds features, runs walk-forward validation and saves the models to
`data/models/`. Re-run whenever you want; the app picks up new weights on its
next refresh.

---

## How the model works

### Training data

nflverse's game file — one row per game back to 1999, ~6,800 games. It carries
final scores *and* historical closing spread and total, rest days, roof, surface
and starting quarterbacks, so the market history needed for supervision comes
with it. Play-by-play parquet (optional) adds EPA.

### Features (~35)

| Group | Features |
|---|---|
| Power | Elo (MOV-adjusted, season-regressed), rolling opponent-adjusted EPA per play for each side's offence and defence, pass/rush splits, success rate |
| Situation | rest days, short week, off bye, travel miles, time-zone shift, divisional, week, neutral site |
| Environment | roof, surface, temperature, wind |
| Form | rolling points for/against and margin over the last 8 games |
| Market | consensus spread and total |

### Two model variants, on purpose

A **market-blind** model never sees the betting line. A **market-aware** one
does. Train only the market-aware model and it learns to reproduce the closing
line almost exactly — its "edge" against that line is then circular and
converges to zero. So the number shown as *our model* comes from the blind
variant, and the aware variant is used for calibration and the blended
projection.

### Three numbers per game

- `model_margin` — market-blind. What our model thinks, uncontaminated.
- `fair_margin` — model and market combined. Our actual best estimate.
- `spread_edge` — `fair_margin` against the line.

**The edge is deliberately not the raw model-versus-market gap**, and this is
the single most important design decision in the project. Both the model and
the market are noisy estimates of the same unknown, and the market's is usually
the better one. If the model says +7 into a line of 0, the honest conclusion is
"the truth is probably around +2", not "there are seven points of value here".
Quoting the raw gap inflates every expected value on the board and produces the
most confident recommendations on exactly the games where the model is most
wrong. The raw gap is still shown, as a diagnostic, next to the shrunk one.

### The blend weight is fitted, not guessed

How much the market is trusted is not a hard-coded constant. It is fitted on
walk-forward predictions by minimising the error of
`(1 - w) · model + w · market` against actual results — a one-dimensional least
squares with a closed form. **If the model adds nothing over the closing line,
the fit drives `w` toward 1 on its own and the reported edges collapse toward
zero.** That is the correct behaviour, and the training output says so:

```
Fitted market weight is 0.98: on this data the closing line carries almost
all the information and the model adds little. Edges will be small, which
is the honest result.
```

### Validation

Walk-forward by season — train on everything before season *S*, test on *S*,
roll forward. Never random k-fold, which leaks future information backward
through the rolling features and produces flattering, meaningless scores.
Reported metrics: margin MAE against the market's own MAE (the bar to beat),
ATS rate against the closing number, Brier score and log loss, and CLV.

### Guardrails

- **Cold-start suppression.** Before each team has played ~5 games the ratings
  sit near their priors, so any disagreement with the market is ignorance rather
  than edge. Below that threshold no bets are recommended at all.
- **Implausible-edge flagging.** A disagreement past 10 points with a liquid
  market is far more likely our blind spot than the market's mistake. It is
  shown, rated `suspect`, and never presented as a strong play.
- **In-sample honesty.** Backfilled grades (replaying the model over games
  already played) are labelled, and the walk-forward figure is displayed
  alongside every affected number.

---

## Survivor: why it plans a path

The mistake that ends most survivor entries is taking the safest available team
this week and discovering in week 12 that everything left is a coin flip.

Choosing one team per week, each usable once, to maximise the chance of
surviving every week is exactly a rectangular assignment problem — maximise
`Σ log P(win)` over weeks × teams — solved optimally by the Hungarian algorithm
in milliseconds. So the recommendation is **not always this week's safest team**,
and the interface shows what deviating costs *over the whole path* rather than
just this week's win probability.

The horizon is capped at six weeks by default: projections five weeks out are
much weaker than this week's, and planning twenty weeks ahead optimises against
noise.

---

## Command line

```bash
nflpicker serve                  # dashboard + background refresher
nflpicker refresh                # fetch everything once
nflpicker refresh --stages odds  # just one source
nflpicker picks                  # this week's recommendations, as a table
nflpicker picks --contest survivor
nflpicker teams                  # power ratings and projections
nflpicker train                  # train and evaluate
nflpicker backtest --backfill    # grade history
nflpicker status                 # what's stored, which sources are healthy
```

---

## Auto-refresh

Each source has its own interval, because they age at very different rates:

| Source | Default | Adaptive behaviour |
|---|---|---|
| Scores | 5 min | 60s while games are in progress; hourly when the next kickoff is over a day away |
| Odds | 15 min | stretched to fit the remaining monthly API budget |
| News | 15 min | — |
| EPA / stats | 6 h | — |

Stages are failure-isolated: a dead RSS feed cannot stop odds from updating, and
an exhausted odds quota cannot stop scores from coming in. Whatever fails is
recorded and surfaced as a source-health chip in the status bar rather than
silently swallowed. The open page polls and re-renders on its own.

---

## Demo mode

`make demo` runs the entire app against a deterministic synthetic league: a
structurally valid 18-week schedule (272 games, 17 per team, one bye each),
six seasons of history with team strength that persists year to year, and
six sportsbooks quoting lines with their own biases and a movement history.

It exists so the app is fully explorable with no key and no network, and so the
test suite is deterministic and offline.

**Note on the demo's numbers:** the synthetic market is deliberately sharp, and
an Elo-plus-EPA model cannot beat it. The training output will honestly report
`worse than the closing line` and the fitted market weight will sit near its cap,
so few or no bets are recommended. That is the machinery working correctly, not
a failure — and it is the behaviour you want when it is your money.

---

## Testing

```bash
make test     # 108 tests, fully offline
make lint
```

The suite deliberately concentrates on the properties that are both critical and
easy to get silently wrong:

- **Leakage.** A game's features must never reflect its own result. Tested by
  mutating a score and asserting that game's feature row is byte-identical while
  later games for the same team do change.
- **Sign conventions.** Spread direction, cover rules and CLV direction are each
  pinned, because every one of them is silent when reversed.
- **Simulation identities.** Playoff probabilities must sum to exactly 14,
  division winners to 8, byes to 2, championships to 1.
- **Survivor optimality.** That it really does save a team it needs later.
- **Live parsers.** ESPN, Odds API and RSS shapes, including the fields those
  feeds routinely omit.

---

## Layout

```
nflpicker/
  sources/     espn, odds_api, nflverse, news_rss, weather, demo
  ratings/     elo, efficiency, power
  ml/          features (leak-free), train (walk-forward), predict
  sim/         season monte carlo + playoff bracket
  market/      consensus, de-vigging, line movement
  picks/       edges, pickem, survivor
  news/        impact classification
  backtest/    grading, CLV, calibration
  web/         dashboard (vanilla JS, no build step)
  pipeline.py  refresh orchestration
  scheduler.py background jobs
  api.py       FastAPI
```

Data lives in `data/nflpicker.db` (SQLite). Everything is append-only snapshots
keyed by capture time, which is what makes the history views answerable.

---

## Limits worth knowing

- Playoff **tiebreakers are simplified** — ties in simulated records break
  randomly rather than running the NFL's full twelve-step procedure. Across
  20,000 simulations this is unbiased and moves playoff odds by well under a
  point.
- **Season win-total lines** only appear when the odds feed publishes futures;
  the simulated distribution is always shown regardless.
- The **news impact estimate** is a coarse prior from position and availability.
  It is a triage signal for what to look at, never a substitute for watching how
  the market actually reacts.
- Nothing here is betting advice. The app's most useful habit is telling you
  when it has no edge, and it will do that often.
