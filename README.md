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
make train                                        # full training, a few minutes
.venv/bin/python -m nflpicker.cli train --no-epa  # skip play-by-play
```

This downloads every NFL game since 1999 with its historical closing line, plus
play-by-play for every training season (~430 MB, and it fetches in well under a
minute), builds features, runs walk-forward validation and saves the models to
`data/models/`.

Play-by-play is downloaded for **every** season by default rather than the most
recent handful. Partial coverage is worse than it sounds: when most training
rows lack the EPA and quarterback columns the model learns to ignore them, and
the features look worthless when they are merely absent. Loading all of them
moved margin MAE by an order of magnitude more than loading nine seasons did.

Re-run whenever you want; the app picks up new weights on its next refresh, and
the refresher keeps the per-game detail those features need up to date.

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
| Power | Elo (MOV-adjusted, season-regressed), rolling EPA per play for offence and defence, pass/rush splits, success rate |
| Quarterback | starter's shrunk EPA per dropback, career dropbacks, starter-changed flag |
| Efficiency | opponent-adjusted offensive and defensive EPA (exponentially weighted) |
| Hidden components | special-teams EPA, turnover luck (margin minus its fumble-recovery-neutral expectation) |
| Situation | rest days, short week, off bye, travel miles, time-zone shift, divisional, week, neutral site |
| Environment | roof, surface, temperature, wind |
| Form | rolling points for/against, decayed scoring margin, Pythagorean win expectation |
| Market | consensus spread and total |

Three of these deserve a note on *why* they exist:

**Opponent adjustment.** Raw EPA rewards a team for the schedule it happened to
draw. Each game's efficiency is adjusted by the opponent's rating *as it stood
before that game* — moving the ball on a good defence counts for more.

**Turnover luck.** Fumble recoveries are close to a coin flip, so a team's
turnover margin is part skill and part luck. Recording the gap between the
actual margin and a recovery-neutral expectation lets the model treat the lucky
part as the noise it is rather than projecting it forward.

**Quarterback.** The largest week-to-week swing a power rating misses. Starter
identity comes from one source only: the schedule feed records the *starter*
while play-by-play reports whoever threw most, and those disagree on about one
game in ten — mixing them made the "starter changed" flag fire on source
disagreements instead of actual changes.

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

### What the model actually achieves — and what it does not

Measured over 5,980 walk-forward games from 2002 to 2026:

| | margin MAE | straight-up | Brier |
|---|---|---|---|
| Model (market-blind) | 10.60 | 64.2% | 0.222 |
| **Closing line** | **10.23** | — | — |

The market-blind features are worth having: they improved the standalone model
from 10.65 to roughly 10.60 MAE, lifted straight-up accuracy from 63.0% to
64.2%, and `epa_adj_diff` and `qb_value_diff` rank third and fifth by
permutation importance, behind only Elo.

**But the model does not beat the closing line, and the blend adds nothing to
it.** Fitting `(1 - w) · model + w · market` against actual results gives an
unconstrained optimum of **w = 1.002** — statistically indistinguishable from
ignoring the model entirely. The blend's MAE differs from the market's by
0.0002 points. The same holds on 2016+ alone (w = 0.998). The trailing gap of
~0.3 points is remarkably constant across every era, so it is not an artifact of
thin early data, and narrowing the training window does not close it.

Totals looked briefly more promising — the weight fits at 0.89 across all
history rather than 0.98 — but that is a dead inefficiency, not a live edge. On
2016+ the fit is 0.99, and betting the disagreement returns 52.1% against a
52.4% break-even. This is why the market weight is fitted on **recent seasons
only**: a weight fitted across twenty years bakes a 2005-era inefficiency into
today's recommendations and manufactures edges from it.

So the honest summary is that a public model built from box scores, EPA and
quarterback data lands within about a third of a point of the NFL closing line
and carries no information the line does not already have. Closing that last gap
needs inputs the market has and this does not — injury severity, personnel
grades, and the early-week openers where the line is genuinely softer. The app
reports this rather than dressing it up, which is the entire point of the fitted
weight.

### Guardrails

- **Recency-fitted market weight.** Market efficiency is not a constant, so the
  blend weight is fitted on the last eight seasons rather than all history.
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

## Cross-market: the one edge that does not need us to be right

The finding above — that the model cannot beat the closing line — is usually
read as a disappointment. It is also an asset. If the sportsbook consensus is
within a third of a point of the best estimate anyone has, then it is an
excellent *signal*, and the question becomes where else that signal is not yet
priced in.

Prediction markets like Polymarket run the same games with a different, smaller
participant base. When one sits several points away from the de-vigged
sportsbook consensus, the likelier explanation is that the thinner venue is
lagging, not that the deepest market in American sport is wrong.

So the cross-market panel inverts the usual claim:

| | Fair value | The bet | What it assumes |
|---|---|---|---|
| Best bets | our model | a sportsbook line | our model beats the market — *measured as false* |
| **Cross-market** | **the sportsbook consensus** | a prediction-market price | a thin venue lags a deep one |

The second is a far weaker assumption, which is precisely why it is the more
promising of the two. Note the direction: this is not contrarian betting against
the prediction market's conclusion, it is taking the sharp consensus price *into*
the softer venue.

Three things are enforced in code rather than left to judgement:

- **Prediction markets never enter the sportsbook consensus.** Averaging a noisy
  price into the benchmark would move it toward the very number being judged,
  and the comparison would partly be against itself. The same exclusion applies
  to best-available pricing, so a model edge is never quoted at a venue that
  never offered it.
- **Depth, not price.** Every edge is sized against the live order book. A
  twelve-point probability gap with $200 behind it is not an opportunity, and
  recommended stakes are capped by what the book can actually absorb.
- **Consensus quality.** One book is not a consensus. At least three must agree
  before their average is treated as fair value.

A gap past 20 points is rated `suspect` rather than recommended: at that size,
news the thin venue has priced and the books have not, a resolution-rule
difference, or a market heading for a void are all likelier than a gift.

**Before acting on any of this, check whether trading on the venue is available
to you where you live.** Prediction-market access for US persons has been
restricted and the regulatory picture has been changing; this project does not
attempt to track it and reads public market data only. Resolution rules also
differ from a sportsbook's — ties, postponements and voids are not handled the
same way, and that difference can be the entire "edge".

Set `PREDICTION_MARKETS_ENABLED=0` to turn the whole thing off.

### A note on using it as a model feature

The natural next thought is to feed the prediction-market price into the
market-aware model. Two reasons it is not wired that way yet: there is no
historical price series to train on, and the venue's best use is as a *target*
to bet into rather than an input. What the app does instead is snapshot every
price from now on, exactly as it does sportsbook lines — so after a season of
collection, "how far Polymarket sits from the consensus" becomes a feature with
real history behind it, and walk-forward validation can say whether it carries
information rather than anyone guessing.

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
| Prediction markets | 10 min | polled faster than the books — the lag is the whole point, and it is what closes |
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
make test     # 142 tests, fully offline
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
- **Feature wiring.** That inference actually supplies the per-game detail the
  model was trained on. Training with columns that silently arrive as NaN in
  production is invisible without a test for it.
- **Venue separation.** That a prediction market never reaches the sportsbook
  consensus or the best-available price, in either direction.

---

## Layout

```
nflpicker/
  sources/     espn, odds_api, polymarket, nflverse, news_rss, weather, demo
  ratings/     elo, efficiency, power
  ml/          features (leak-free), train (walk-forward), predict
  sim/         season monte carlo + playoff bracket
  market/      consensus, de-vigging, line movement
  picks/       edges, crossmarket, pickem, survivor
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
- **Starting quarterbacks for upcoming games** fall back to whoever started last
  week. The schedule feed only records a starter after the fact, so an announced
  midweek change is not yet picked up automatically — the news feed flags it for
  you, but the model does not consume that flag.
- Nothing here is betting advice. The app's most useful habit is telling you
  when it has no edge, and it will do that often.
