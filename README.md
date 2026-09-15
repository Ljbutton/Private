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
make run       # live sources, in your browser
make desktop   # live sources, in a native window
```

`make run` serves <http://127.0.0.1:8000>. `make desktop` opens the same app in
a real application window instead — see [Running it as an app](#running-it-as-an-app).

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

**Tracks games while they are being played.** In-progress games sort to the top
with the clock, down and distance, who has the ball, and a live win probability
that updates as the scoreboard does. The live model is a time-decay
approximation: the pregame projection dominates early and fades as the game
resolves it, and uncertainty shrinks with the square root of time remaining. It
is display only — nothing there feeds a pick or a stake.

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

Three more nflverse releases feed the availability features: **weekly injury
reports** (2009+), **depth charts** (who the actual backup is) and **snap
counts** (2012+, how much of the offence or defence a missing player was
playing). Together they give 9,122 team-weeks of historical injury cost, which
is what lets availability be a *trained* feature rather than a post-hoc nudge.

### Features (79)

| Group | Features |
|---|---|
| Power | Elo (MOV-adjusted, season-regressed), rolling EPA per play for offence and defence, pass/rush splits, success rate |
| Quarterback | starter's shrunk EPA per dropback, career dropbacks, starter-changed flag |
| Availability | injury cost in points for each side and the difference, weighted by each missing player's prior snap share |
| Efficiency | opponent-adjusted offensive and defensive EPA (exponentially weighted) |
| Hidden components | special-teams EPA, turnover luck (margin minus its fumble-recovery-neutral expectation) |
| Situational | third-down conversion and allowed, red-zone touchdown rate, explosive-play rate and allowed, sack rate taken and forced, penalty yards |
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

**Situational rates, not counts.** A count of third-down conversions mostly
measures how many possessions a team got. The rate is the property of the team.
Honest note on these: an A/B test over the full walk-forward history moved
margin error by **0.002 points** — essentially nothing — while nudging
straight-up accuracy from 64.2% to 64.6% and improving Brier slightly. They are
kept because they cost nothing extra to compute, help calibration a little, and
are genuinely worth reading on the Teams tab. They are not why the model works.

**Availability.** Every player, not just the quarterback. Each name on the
weekly injury report is costed by position value scaled by that player's snap
share *before* the week in question — a starting corner missing 85% of snaps
costs more than a rotational one, and a quarterback's backup is read from the
depth chart rather than guessed from who has started before. The snap share has
to come from prior weeks specifically: an injured player has no snap row for the
week he is out, so keying on his own week silently returns nothing for exactly
the players the feature exists to price. An A/B test over 2012+ (98% coverage)
moved margin error **-0.013 points**, straight-up accuracy **+0.12%** and Brier
**-0.0012** — six times the effect of the situational rates, and the best recent
addition to the model.

**Announced starters.** The schedule feed records a starter only *after* a game
is played, so for an upcoming game the builder used to fall back to whoever
started last week.

*Measured*, on 2,011 team-games across 2021–2024 (`nflpicker starters`):

| Naming the starter correctly | All team-games | When the two rules disagree |
|---|---|---|
| Last week's starter | 88.1% | 42.1% |
| Announced starter | **88.9%** | **50.3%** |

So it helps, and less than the first draft of this paragraph claimed. The two
rules agree on 90% of team-games, where the substitution is a no-op; the 195
where they disagree are the whole feature. There it is right half the time
against last week's four-in-ten — a real gain on the cases that move a line, and
a reminder that *neither* rule is good at them. A headline rate over all games
would have diluted that to nothing and hidden both halves of it. Worse, it
was wrong twice over: the model priced the game with the injured starter's rating
*and* reported `qb_change = 0`, so nothing downstream knew the projection was
stale. The expected starter is now resolved before inference by walking the depth
chart past anyone the injury report has ruled out. Only unplayed games are
touched — overwriting a final game's starter would rewrite history with
information from the future.

Because the replacement is now a *feature*, the quarterback half of the
availability offset is suppressed for those teams. The downgrade is inside the
model; charging it again would double-count the most expensive absence in the
sport. Skill-position absences are still costed, since those have no equivalent
feature.

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

## Running it as an app

`make desktop` runs the dashboard in a native window rather than a browser tab.
It uses the platform's own engine — WebView2 on Windows, WebKit on macOS,
WebKitGTK on Linux — so nothing ships a second browser: no tab, no address bar,
no localhost URL to remember. The server still runs underneath, bound to
loopback only, which matters because the app has no authentication.

If no webview runtime is present it falls back to opening your browser rather
than failing.

### Getting the executable

**Without installing anything:** the *Build desktop app* workflow builds on real
Windows and macOS runners and attaches the result to the run. Actions tab →
*Build desktop app* → download the artifact for your platform.

- **Windows** — `NFLPicker-windows-full` unzips to one `.exe`.
- **macOS** — two layers, because a GitHub artifact is always a zip and a zip
  does not carry the executable bit: an `.app` unzipped from one would not
  launch at all. So the artifact is a zip *containing* a `.tar.gz`, and the tar
  is what preserves the mode. Unwrap both:

  ```bash
  cd ~/Downloads                          # where the browser put it
  unzip -o NFLPicker-macos-full.zip       # skip if Safari already expanded it
  tar -xzf NFLPicker-macos.tar.gz
  xattr -dr com.apple.quarantine NFLPicker.app   # it was downloaded, so Gatekeeper
  open NFLPicker.app
  ```

  Lost track of where it landed? `find ~ -maxdepth 3 -name "NFLPicker-macos*"`.

  Without the `xattr` line macOS says the app "is damaged and can't be opened".
  It is not damaged — that is what Gatekeeper says about anything unsigned that
  arrived over the network. Signing it properly needs an Apple Developer
  account; building it yourself avoids the question entirely, because a locally
  built app is never quarantined.

The runner is Apple Silicon, so the macOS build is arm64. An Intel Mac needs a
`macos-13` build instead.

The build runs the test suite first and then boots the frozen binary with
`--selftest`, because PyInstaller exiting 0 only means the bundle was written.
The usual packaging failure is a hidden import that was never collected, and
that stays invisible until someone double-clicks the icon.

**Building it yourself:**

```bash
make exe                 # full build
make exe PROFILE=lite    # smaller, no training
```

This produces one self-contained file — no Python install needed on the target
machine. Measured on Linux: **≈138 MB** for the full profile.

Two things to know:

- **PyInstaller cannot cross-compile.** Run `make exe` on the platform you want
  the build for; a Windows `.exe` has to be built on Windows.
- **The `lite` profile drops pyarrow.** It runs the dashboard, fetches odds,
  scores and news, and uses a model you trained earlier — but it cannot read
  nflverse parquet, so no training and no EPA refresh on that build. Train with
  the full install and copy `data/models/` across if you want both.

- **Windows will not trust it, and that is expected.** The build is unsigned, so
  SmartScreen shows "Windows protected your PC" on first run — *More info* →
  *Run anyway*. Defender may also quarantine it. Code signing needs a
  certificate (a few hundred dollars a year), which is not worth it for
  something only you run. UPX compression is deliberately off in the spec for
  the same reason: packed executables are a known false-positive trigger, and an
  unsigned build starts from a position of suspicion already.

Windows needs the Microsoft Edge WebView2 runtime, which ships with Windows 11
and most Windows 10 installs.

---

## Prediction markets

Polymarket and Kalshi run the same games with a different crowd than the
sportsbooks. Their prices are shown on the Picks tab beside the book consensus
and our own number, de-vigged and expressed as a home-team probability.

**It is a display, not a recommendation.** Nothing there changes a suggestion,
sizes a stake, or feeds a model. A gap is worth a second look — it may mean the
thinner venue is lagging the books, or that it has priced news the books have
not — but which of those it is depends on the game, and the app does not pretend
to know. Games are sorted by disagreement, and one is only marked as leaning
once the venues sit five percentage points from the books.

The separation is enforced rather than assumed. Prediction venues never enter
the sportsbook consensus and are never used for best-available pricing; mixing
them in would move the benchmark toward the number being judged, and would quote
a model edge at a venue that never offered it. That bug has been found twice —
once for Polymarket, once when Kalshi was added — so the venue list now lives in
one registry (`nflpicker/venues.py`) and a test fails the build if an adapter
forgets to register.

Kalshi prices are read from the bid/ask mid rather than the last trade: on a
thin market the last trade can be hours old and several points from anything
transactable.

**Check whether trading on either venue is available to you where you live**
before acting on anything shown here. Access for US persons has differed between
the two and the regulatory picture has been changing; this project reads public
market data only and does not track it. Resolution rules also differ from a
sportsbook's on ties, postponements and voids.

Set `PREDICTION_MARKETS_ENABLED=0` to turn the panel off.

---

## The Edge tab: does the line move toward us?

Everything measured so far compares our number to the **closing** line, and the
answer is that we do not beat it. But the closing line is the end of a week-long
process — lines open on Sunday night and are softest before the market has
chewed on them.

That makes a sharper question available, and the Edge tab is built around it:
**does our number predict which way the line moves?** If the model holds
information the opening market lacks, the line should drift toward us more often
than away.

It is a better test than an ATS record for two reasons. It needs no opinion
about the final score — only how the market revised. And line movement is far
less noisy than game outcomes, so it reaches significance on a fraction of the
sample. A 50% agreement rate is the baseline: the line was always going to move
one way or the other.

The tab also breaks results down by how far ahead of kickoff the view was
formed, because if an edge exists anywhere it should be largest early, before
the market has done its work.

**This cannot be backfilled.** Nobody publishes a history of intraday NFL line
movement, so it accumulates only while the app is running before kickoff — which
is the practical argument for leaving it on rather than starting it on Sunday
morning. The tab shows exactly how much it has witnessed so you can see whether
a number is a verdict or a small slice.

To make sure openers are actually captured, odds polling drops to its floor
whenever scheduled games have no line yet, overriding the budget-stretched
interval. Opening numbers exist once, and a stretched interval can miss them
entirely.

---

## Teasers: a real edge, mostly eaten by the price

`nflpicker teasers` backtests 6-point teasers through the key numbers over every
game since 1999. This is the one strategy in the project that needs nothing from
our model — only closing spreads and final scores.

The mechanism is real. NFL margins are not smooth: **3 happens in 15.0% of games
and 7 in 9.1%**, more than any other margin. Teasing a 7.5-to-8.5-point
favourite down through both, or a 1.5-to-2.5-point underdog up through both, is
a bet on that lumpiness.

Over 1,396 qualifying legs:

| Window | Win rate | 95% CI | vs 72.4% break-even |
|---|---|---|---|
| Underdogs +1.5 to +2.5 | 75.5% | 72.6–78.2% | clears it |
| Favourites −8.5 to −7.5 | 73.0% | 68.9–76.8% | inside the noise |
| Both | 74.6% | 72.3–76.9% | inside the noise |

It has *not* been arbitraged away on the field: 2014 onward is 75.6%, better
than the 73.4% before it.

**The price is what kills it.** A two-leg teaser needs each leg at 72.4% to break
even at −110, and ten cents of extra juice moves that bar by a full point:

| Price | Need | Got | ROI |
|---|---|---|---|
| −110 | 72.4% | 74.6% | **+6.4%** |
| −120 | 73.9% | 74.6% | +1.0% |
| −130 | 75.2% | 74.6% | −2.5% |
| −140 | 76.4% | 74.6% | −5.6% |

Most books now price a two-team 6-point teaser at −120 or worse, which is
precisely because this was well known. **So: worth playing only if you can find
−110, marginal at −120, and a losing bet at −130.** Shopping the teaser price
matters more than picking the legs.

Two honest caveats. The underdog half carries the result — the favourite half is
not distinguishable from break-even at all. And a blind sweep of every spread
window finds *zero* windows whose whole confidence interval clears break-even;
the Wong windows survive only because they were specified in advance by the
key-number argument rather than discovered by searching. Reassuringly, the two
best windows in that blind sweep are the two the theory names.

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
nflpicker desktop                # the same app in a native window
nflpicker refresh                # fetch everything once
nflpicker refresh --stages odds  # just one source
nflpicker picks                  # this week's recommendations, as a table
nflpicker picks --contest survivor
nflpicker teams                  # power ratings and projections
nflpicker train                  # train and evaluate
nflpicker backtest --backfill    # grade history
nflpicker teasers --sweep        # backtest key-number teasers
nflpicker sources                # what data sources exist and how often they run
nflpicker status                 # what's stored, which sources are healthy
```

---

## Adding a data source

Sources are declared once, in `nflpicker/stages.py`, and both the refresh
pipeline and the background scheduler read from that list. `nflpicker sources`
prints what exists:

```
stage               enabled  every      feeds model  what it is
schedule            yes      5m         yes          Games, scores and live in-game state
odds                yes      15m        yes          Sportsbook lines across every book
prediction_markets  yes      10m        -            Polymarket and Kalshi prices, shown for comparison
weather             yes      180m       yes          Forecast at kickoff for outdoor games
news                yes      15m        yes          Headlines and injury reports
stats               yes      360m       yes          Play-by-play efficiency, team detail and depth charts
train               yes      1440m      -            Refit the model as results come in
recompute           yes      on refresh -            Ratings, projections, picks and grading
```

To add one:

1. **Write an adapter** in `nflpicker/sources/` that returns plain dicts and
   never touches the database. That is what makes it testable against a
   recorded response instead of the live network.
2. **Add `refresh_<name>(self, result)` to the pipeline** to fetch and store it.
   Catch its own failures and record them — a dead feed must not cost you odds.
3. **Register a `Stage`** with an interval and a one-line description.

That is the whole wiring. Nothing else needs editing.

**If it feeds the model, there is a fourth step, and it is the one that gets
missed:** merge the values into the game rows in `recompute`, and write a test
asserting the feature is actually populated. This project has shipped
trained-on columns that arrived as NaN in production twice — once for the
play-by-play features, once for temperature and wind — and in both cases
everything upstream looked healthy. The registry gets the data in; only that
test proves it is used.

---

## Auto-refresh

Each source has its own interval, because they age at very different rates:

| Source | Default | Adaptive behaviour |
|---|---|---|
| Scores | 5 min | 60s while games are in progress; hourly when the next kickoff is over a day away |
| Weather | 3 h | forecasts move slowly and only matter near kickoff |
| Odds | 15 min | stretched to fit the remaining monthly API budget |
| Prediction markets | 10 min | Polymarket and Kalshi, fetched independently so one being down costs only its column |
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
make test     # 226 tests, fully offline
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
  production is invisible without a test for it — it has now happened twice, to
  the play-by-play features and to temperature and wind.
- **Venue separation.** That a prediction market never reaches the sportsbook
  consensus or the best-available price, in either direction.

---

## Your picks, and who is actually right

Click the chip in the **You** column on the board to record your own pick —
clicking again cycles away, home, none. The point is to be able to disagree: with
the model, with the book, or with both.

The **Scoreboard** then scores everyone on straight-up winners, which is the one
question all four can be asked without a spread or a price to argue about. Two
columns, and the second is the honest one:

- **All their picks** — each picker's record over the games it had a view on.
- **Same games** — only games where *every* picker had a view. Without it a
  source can look good by having an opinion about the easy games and staying
  quiet on the rest.

A source with no opinion is not scored as wrong, a tie is a push for everyone,
and an exact 50% is not a pick.

## Alerts

In-app only, deliberately — nothing is pushed anywhere. The app already sits
open; what it lacked was a way to say "something changed" without re-reading
sixteen rows.

It raises a starter being ruled out, a line crossing 3 or 7, a steam move, and a
disagreement of two points or more against the **opening** line. Two rules keep
it from becoming noise: every alert is about a *change* rather than a state, and
each distinct event fires once — recompute runs every few minutes, so anything
keyed on current state would re-raise the same alert until kickoff.

## The opener

The model does not beat the closing line, and the app says so. The opener is a
different question: it is the same market before it has been corrected, and it is
the only place a disagreement is worth a second look.

So the opener is **not** a training target and not what the app recommends on.
It is reported separately — `opener_edge` on each game, and an alert past two
points — which keeps that distinction visible instead of quietly blending two
different claims into one number.

---

## Turning on real data

Demo mode is off unless you ask for it, so a normal launch is already live. Most
of what the app reads needs no account at all:

| Source | Needs a key? | What it gives you |
|---|---|---|
| ESPN | no | schedule, scores, live in-game state, one consensus line |
| nflverse | no | play-by-play, EPA, injuries, depth charts, snap counts |
| News RSS | no | headlines and injury reports |
| Open-Meteo | no | kickoff weather |
| Polymarket, Kalshi | no | prediction-market prices, shown for comparison |
| **The Odds API** | **yes** | **every sportsbook separately** |

Without an Odds API key the app still works and says so — it falls back to
ESPN's single consensus line. What you lose is the spread *between* books, which
is what best-available pricing and closing-line value are computed from. The
free tier is 500 requests a month; at the default 15-minute cadence that is
comfortably inside it.

Get a key at [the-odds-api.com](https://the-odds-api.com/), then:

```bash
# from source
echo "ODDS_API_KEY=your-key-here" >> .env

# packaged app, macOS
echo "ODDS_API_KEY=your-key-here" >> ~/Library/Application\ Support/NFLPicker/.env

# packaged app, Windows
echo ODDS_API_KEY=your-key-here >> %LOCALAPPDATA%\NFLPicker\.env
```

Restart the app, then hit refresh. The Connections list in the sidebar will show
`Odds API` with your month's usage instead of `no key`.

**The model.** A trained bundle ships in `data/models/`, so the sidebar should
read `1.0@<date>` rather than `power-only` on a fresh clone. It is a pickle of
fitted estimators and therefore only loads back under the scikit-learn version
that wrote it, which is recorded in `training_report.json` beside it. If it will
not load, the app says so in the log and falls back to power ratings — retrain
and it is fixed:

```bash
make refresh    # pull real data first
make train      # walk-forward fit, ~25 seasons, a few minutes
```

**It retrains itself as the season goes on.** The scheduler checks daily and
refits once a week of results has landed, so the cadence follows the season
rather than the clock. `make train` is still there for an immediate rebuild, and
it prints a validation report worth reading.

Three guards, because an unattended fit is the one scheduled job that can make
the app *worse*:

- **Nothing live.** Training holds the refresh lock for minutes; doing that
  during a game would stall the score poll exactly when it matters, so a live
  slate defers to the next check.
- **Enough new evidence.** A week of results moves the weights; two or three
  games spend minutes of CPU to move them by nothing.
- **No regression.** The fit happens in a scratch directory and is promoted only
  if its walk-forward error is not materially worse than the model already in
  place. An upstream schema change or a feature that quietly went empty would
  otherwise replace a good model with a broken one overnight, with the app
  reporting nothing but a new timestamp. A rejected fit says so in the log and
  changes nothing.

The comparison has a caveat worth knowing: walk-forward MAE is computed over all
history each run, and one week moves the sample by about a third of a percent, so
runs are comparable in practice but not identical. The tolerance
(`NFLPICKER_TRAIN_MAX_REGRESSION`, 0.15 points) is sized to catch a genuine
break, not to arbitrate noise.

Turn it off with `NFLPICKER_TRAIN_AUTO=0`.

---

## Updating, and where your data lives

Everything the app has learned is one SQLite file. Nothing is held in memory
between runs, so switching the machine off loses nothing — on the next start the
scheduler runs every due job immediately and carries on from the last snapshot.

| | Path |
|---|---|
| From source | `data/nflpicker.db` |
| Packaged, macOS | `~/Library/Application Support/NFLPicker/` |
| Packaged, Windows | `%LOCALAPPDATA%\NFLPicker\` |

The database is deliberately **outside** the application. Updating means
replacing code, never touching that file:

```bash
make update      # from source: pull, reinstall, migrate, done
```

For a packaged build, replace the executable. The data directory is untouched
because it was never inside it.

**Schema changes migrate forward.** `CREATE TABLE IF NOT EXISTS` handles a new
table, but does nothing to a table that already exists — so a column added later
would never appear in your database, and the new code would query a column your
file has never had. Columns are therefore also declared in `COLUMN_ADDITIONS`
and applied on open, and a copy of the database is written beside it
(`nflpicker.db.v4.backup`) before anything changes.

That caution is specifically about the data that cannot be rebuilt. Scores,
schedules and play-by-play can all be re-fetched. **Line movement and closing-line
value cannot** — they exist only because the app was running and recorded them at
the time. That is also why the sooner it runs continuously, the sooner the Edge
tab has anything to say.

---

## Layout

```
nflpicker/
  sources/     espn, odds_api, polymarket, kalshi, nflverse, news_rss, weather, demo
  ratings/     elo, efficiency, power
  ml/          features (leak-free), train (walk-forward), predict
  sim/         season monte carlo + playoff bracket
  market/      consensus, de-vigging, line movement, openers, prediction markets
  picks/       edges, pickem, survivor
  news/        impact classification
  live.py      in-game win probability
  availability.py  injury-adjusted projections
  backtest/    grading, CLV, calibration, teasers
  web/         dashboard (vanilla JS, no build step)
  stages.py    the source registry both of the below read from
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
- **Announced starters** are resolved from the injury report and the depth
  chart, not from headline text. The news classifier reports a team and a
  position but never a player name, so it cannot say *which* quarterback a
  headline means — and a wrong identity here does not degrade gracefully, it
  prices the wrong player. A starter who is only *questionable* is still treated
  as starting; that is closer to a coin flip than to a change.
- Nothing here is betting advice. The app's most useful habit is telling you
  when it has no edge, and it will do that often.
