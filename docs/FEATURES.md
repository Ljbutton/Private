# The Edge — what it does

A desktop app that projects every NFL game, prices those projections against
the sportsbooks, and turns the difference into picks you can act on. It runs
on your own machine. No account, no subscription to a server, no data leaving
the computer it is installed on.

This document is the honest inventory: what is in the app, what each thing
needs, and — at the end — what it does not do. It is written to be the source
a store page, a demo script or a sales conversation is drawn from, which means
nothing in it should be a claim the app cannot survive being asked about.

---

## The short version

Eight things, in the order someone discovers them.

1. **A projection for every game**, from a model trained on every NFL game
   since 1999, shown beside the number the sportsbooks are offering.
2. **A full-season simulation** — expected wins, playoff, division and title
   odds, rerun every time the data moves.
3. **The whole market in one place** — every book the odds feed carries, the
   consensus, the best available number, and the prediction markets.
4. **Picks you can enter**: pick'em with confidence points, survivor planned
   to the end of the season, and priced betting edges.
5. **News and injuries that are triaged**, not listed — sorted by what moves
   a line, with an estimate of how much.
6. **Live games**, with win probability that updates as the scoreboard does.
7. **A scorecard on itself** — every graded pick, honestly, including the
   figures that do not flatter it.
8. **An assistant that can read all of it** and runs on your own machine.

---

## In detail

### Projections

**Every game, every week.** A gradient-boosted model over Elo ratings, EPA
efficiency, rest, travel, weather, quarterback value and game situation,
blended with the market. Each game gets a projected margin, a total, and a
win probability.

**Three numbers, not one.** The board shows what the model says on its own
("blind" — before it has seen the line), what it says after weighing the
market ("blend" — what the app actually claims), and what the sportsbook is
offering. Seeing all three is the difference between a black box and a tool:
when the blind number and the book disagree by a touchdown, that is the thing
worth looking at.

**Trained on the full history.** 6,515 games — every NFL game from 2002 on,
each with the closing line that was actually offered on it, plus play-by-play
for every one of those seasons. Validation is walk-forward: the model is only
ever tested on seasons it was not trained on.

**Retrains itself.** Once a week of results has landed, it refits — and keeps
the new model only if it is not measurably worse than the one it would
replace. A worse model is rejected rather than shipped.

### The season

**Twenty thousand simulated seasons**, rerun as the data moves. Produces
expected wins, the full win distribution, and playoff, division, bye and
championship odds for all thirty-two teams.

**Power rankings, kept week by week.** Not just where teams stand now — where
they stood going into each earlier week, saved as the season goes, so you can
watch a team's rise or collapse rather than being told about it. Any team's
whole season plots as a line.

**Against the posted win totals.** Where the odds feed carries season win
totals, the simulation is priced against them.

### The market

**Every book, not an average of them.** Spread, total and moneyline from each
sportsbook the feed carries, de-vigged, plus the consensus and the *best
available* number — which is the one you would actually bet.

**Prediction markets too.** Kalshi and Polymarket, which frequently disagree
with the sportsbooks and need no key of their own.

**Line movement, remembered.** Every refresh snapshots both the market and the
app's own number, so each game has a chart of the consensus, the individual
books and the model over time. Nobody sells you the past; the app has to be
running to record it, and it is, which is the point.

**Budget-aware.** The odds feed's free tier is 500 requests a month. The app
tracks its own usage against monthly, weekly and daily ceilings and stretches
its polling to fit, so a free key lasts the season instead of running out in
week six. The count is visible in the sidebar.

### Picks

**ESPN pick'em.** Straight picks plus the confidence-point assignment that
maximises expected score, with a **leverage mode** that deliberately gives some
of that up to differentiate from a field that picks close to the market — the
right trade only when finishing first is what pays.

**Survivor, planned to the end.** Not this week's safest team: the whole
remaining path, solved through week 17. Spending a strong team now can cost
more later than it gains today, and the app prices that — beside each
alternative it shows what taking it instead costs across the rest of the run.
Teams you have already used are one click to mark, and the plan replans
itself.

**Betting edges, priced.** Spread, total and moneyline against the best
available number, with expected value and quarter-Kelly stakes. Quarter rather
than full because full Kelly assumes your probabilities are right, and nobody's
are.

### News and injuries

**Aggregated and classified.** ESPN, ProFootballTalk, CBS, Yahoo and NFL.com,
each item typed (quarterback, injury, suspension, transaction, coaching,
weather), attributed to the teams it touches, and attached to the games it
affects.

**Sorted by relevance, not recency.** A backup guard's hamstring does not lead
because it happened most recently.

**With a points estimate.** Each item carries a coarse estimate of its effect
on the spread — a starting quarterback is worth two to three points, a backup
almost nothing. It is a triage signal for what to look at, and the app says so
rather than dressing it up as a forecast.

**An injury report that forgets.** Questionable, doubtful, out, IR and PUP —
who, what, how long, and status. Players who have returned are cleared rather
than accumulating forever, which is the failure mode of every injury page that
has ever been scraped.

### Live games

In-progress games sort to the top with the clock, down and distance, who has
the ball, and a win probability that updates as the scoreboard does. The
pregame projection dominates early and fades as the game resolves it.

It is display only. Nothing on the live board feeds a pick or a stake, and the
app does not pretend otherwise.

### Grading itself

Every finished game is scored: against the spread, return on risk, straight-up
accuracy, Brier score, a calibration curve, and closing-line value — whether
the line moved toward the app's number after it was taken, which is the
sharpest test available of whether a projection knew something.

**In-sample figures are labelled as in-sample.** Before the app has watched a
season's games live, its record is a replay through a model that was trained on
those same games, which flatters every number. The page says so in a yellow box
and shows the walk-forward figure underneath. That box goes away on its own
once there are genuine out-of-sample results.

### The assistant

A chat panel that can read everything above — this week's slate, a team's
season, the model's record — and answers from it. Multiple conversations, kept.

**It sets itself up.** One button downloads a model server and a model, with a
progress bar and byte counts, and starts them. Nothing to install by hand, no
terminal, no address to copy. Everything lands in the app's own folder on its
own port, so a machine that already runs Ollama keeps its models, its settings
and its port — and deleting The Edge takes the whole thing with it.

**It runs on your machine, and that is enforced rather than promised.** The app
refuses any endpoint that is not loopback, so a conversation cannot leave the
computer even if someone edits the setting.

### The app itself

- **A real desktop application.** A window, an icon in the taskbar, a Start
  menu entry. Not a browser tab, not a localhost link.
- **Windows installer** and a **macOS app** for Apple silicon.
- **Its own database**, kept in your user profile, backed up before every
  schema change and on demand from Settings.
- **Settings in the app** — every key, budget and endpoint, each with a note
  saying what breaks without it. No config files to edit, no terminal.
- **Light and dark**, full screen, and a mark in the corner that keeps the
  time.
- **Demo mode**: a complete synthetic season that runs with no keys and no
  network, which is what makes it demonstrable to someone who has not bought
  it yet.

---

## What it needs

| | |
|---|---|
| **To run at all** | Nothing. Schedules, scores, news and the app's own projections work out of the box. |
| **For book-by-book lines** | A free key from the-odds-api.com — 500 requests a month, which the app budgets itself to fit. Without it you get one consensus line from ESPN. |
| **For the assistant** | One button, and 1–3 GB of disk for the model it downloads. Optional; the rest of the app is unaffected, and nothing is downloaded unless you ask. |
| **For the trained model** | Ships trained. Retraining locally downloads about 430 MB of history and takes a few minutes. |

---

## What it does not do

Worth knowing before someone else finds out.

- **It does not beat the closing line.** Over 5,981 validation games the
  model's margin error is 10.54 points against the market's 10.23 — the market
  is closer, and on totals too. Against the spread it picks 50.7%, and a
  bettor needs 52.4% to break even at standard juice. It is right on the
  straight-up winner 64% of the time, which is a genuinely useful number for a
  pick'em pool and a useless one for a bet. What the app is good at is being
  organised, fast, honest and in one place: it is a research desk, not a money
  printer, and the Performance page tells anyone who asks exactly that.
- **It does not place bets.** There is no sportsbook integration and no
  account linking anywhere in it.
- **It does not sync.** One machine, one database. No cloud, no multi-device,
  no shared pool view.
- **It is not signed.** Windows SmartScreen and macOS Gatekeeper both warn on
  first launch. Fixable with a code-signing certificate; not fixed today.
- **NFL only.** No college, no other sports.
