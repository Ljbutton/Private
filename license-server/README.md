# License server

A small Cloudflare Worker that sits between the desktop app and Whop. It
exists because checking a Whop license key needs your Whop **company API key**,
and that key must never ship inside the installer. Anyone could pull it out and
read every customer's email.

```
The Edge app ──► this Worker (holds the Whop key) ──► Whop API
```

It does three things:

| Endpoint | What for |
|---|---|
| `POST /v1/validate` | Is this key active? Registers the computer (up to `MAX_MACHINES`). |
| `GET /v1/latest` | Newest build, read from `version.json` on the GitHub release. Drives the update notice. |
| `GET /v1/download?key=…&asset=…` | Sends a paying customer straight to the installer. |
| `GET /v1/health?token=…` | What this deployment is missing. Same token as the dashboard. |

Cloudflare's free plan (100k requests/day) is far more than this needs.

## One-time setup

### 1. Whop

Already done for **The Edge** (`prod_VwekdA5QArKis`):

- Product created: $19.99/month with a 7-day trial, and a $79 season pass
  that auto-expires after six months.
- Whop's **Software** app installed and attached to the product. It issues one
  license key per purchase and lists Windows and Mac download links from the
  GitHub `latest` release.
- The fake "Save 20%" strikethrough price is turned off.

Still to do (it's a secret, so only you should handle it):

- **Create an API key.** Dashboard → Developer → Company API keys →
  *Create API key*. Give it `member:basic:read`, `member:email:read` and
  `member:manage`. Copy it. You'll only see it once.

### 2. Cloudflare

```bash
cd license-server
npx wrangler login
npx wrangler secret put WHOP_API_KEY        # paste the API key from step 1
npx wrangler deploy
```

Wrangler prints the Worker's address, e.g.
`https://the-edge-license.<you>.workers.dev`. Open it: it should say `ok`.

That only proves the Worker is deployed. Once `DASHBOARD_TOKEN` is set,
`…/v1/health?token=YOUR_TOKEN` proves the rest — see
[When the app says it can't reach the license server](#when-the-app-says-it-cant-reach-the-license-server).

When the GitHub repo goes private, also run
`npx wrangler secret put GITHUB_TOKEN` with a fine-grained token that has
**Contents: read** on this repo, or the update notice and downloads stop.

### 3. GitHub

Repo → Settings → Secrets and variables → Actions → **Variables**:

| Variable | Value |
|---|---|
| `LICENSE_SERVER_URL` | the Worker address from step 2 |
| `WHOP_STORE_URL` | `https://whop.com/the-edge-ab78/the-edge-fd-e121` |

The next build stamps both into the app. Until they are set, builds run
unlicensed exactly as before, so nothing breaks in the meantime.

## How it behaves for a customer

- First launch: an activation screen asks for the key from their Whop purchase.
- The key is rechecked every 12 hours, and every 15 minutes instead while a
  check is coming back with no answer. If the server can't be reached, the app
  keeps working for **7 days** on the last good check, and says nothing about
  it until the silence has lasted an hour — a single dropped request is not
  worth a banner.
- Cancelled, expired or unpaid (`past_due`) subscriptions lock the app at the
  next check. `canceling` (cancelled but paid through the period) keeps working,
  and so does `completed` — which is how Whop reports a one-time purchase such
  as the season pass once it is paid, rather than one that has run out.
- One key works on up to `MAX_MACHINES` computers (default 2). To move a
  customer to a new computer, clear `edge_machines` in that membership's
  metadata on Whop. Recording the computer is bookkeeping, not the verdict: if
  Whop refuses the write, an active subscription is still let in and the seat
  goes unrecorded until a later check writes it.
- The demo season (`NFLPICKER_DEMO=1`) and source checkouts never ask for a key.

## When the app says it can't reach the license server

The app shows that whenever a check comes back with no verdict, and there are
three quite different ways to get there. Work through them in order.

**1. Is the Worker deployed, at the address the app was built with?**
Open the Worker's root in a browser — it answers `ok`. Then check the app is
pointed at that same address: it is the `LICENSE_SERVER_URL` repository
variable (GitHub → Settings → Secrets and variables → Actions → Variables),
stamped into the build. A blank or stale variable means installers are asking
an address that never answers, and only a new build fixes it.

**2. Ask the Worker what it is missing.**

```
https://the-edge-license.<you>.workers.dev/v1/health?token=YOUR_DASHBOARD_TOKEN
```

It reports whether each secret and binding is set, and the status code Whop
gives the API key right now — no secret's value, ever. `"ok": true` means the
Worker can do its job. Anything else comes with a `notes` line saying what to
run. The usual answers:

| What it says | What it means |
|---|---|
| `whop_api_key: false` | The secret was never set: `npx wrangler secret put WHOP_API_KEY` |
| `whop: { status: 401 }` or `403` | The key is wrong, revoked, or from another company. Make a new one and set it again. |
| `whop: { status: 404 }` | Healthy. 404 is the *right* answer for the made-up membership id it probes with. |

An API key also needs `member:manage`, not just the read scopes — without it
Whop refuses to record which computer a key is running on.

**3. It really is the customer's connection.** The banner says which: the
server not answering reads "Can't reach the license server", while a server
that answered but could not reach Whop says so, and quotes what Whop said.
Either way a key that checked out in the last 7 days keeps working.

## Test

```bash
node --test license-server/worker.test.mjs
```


## Support messages

Help → Contact support in the app — and the same button on the activation
screen, which is where "my key will not activate" comes from — posts to
`POST /v1/support` here, and the Worker sends it on as one email through
[Resend](https://resend.com). The message carries a category (bug report,
suggestion or general support, which picks the word in the subject line), what
the user wrote, what they were doing, up to three screenshots, and whichever of
the four optional details they left ticked: version and commit, OS, the last
four characters of their licence key, and the last hundred lines of the app's
log. The app redacts keys and anything key-shaped out of all the text before it
leaves the machine.

**Screenshots.** Shrunk in the app to a 1600px long edge and re-encoded, so a
full-screen grab arrives as a couple of hundred kilobytes rather than six
megabytes. Three per message, two megabytes each. They are checked against
their own magic bytes — in the app and again here, because this endpoint needs
no licence key and the app is not the only thing that can reach it — so what
gets attached to an email is an image and is named after the type it actually
is. Pictures are the one thing that cannot be redacted, which is why they are
only ever the ones somebody attached by hand and are shown as thumbnails before
the message goes.

Two secrets:

```
npx wrangler secret put RESEND_API_KEY
npx wrangler secret put SUPPORT_EMAIL_TO
```

`SUPPORT_EMAIL_TO` is a secret rather than a constant in the source for three
reasons: this repository is public and an address in public gets scraped, a
support address does not belong in a binary anyone can unpack, and changing
where reports go is then one setting here rather than a new build for
everybody. It is never echoed in a response — there is a test for that.

**The sender.** `The Edge Support <onboarding@resend.dev>`, which is Resend's
shared sandbox sender and needs no DNS. Its one restriction is the one that
matters here: it can only deliver to the address that owns the Resend account,
which is exactly where these are going. Sending to customers would need a
verified domain; replying to them does not, because the reporter's own address
goes in `Reply-To` and you answer from your mail client.

**Free tier.** Resend's free plan is 100 emails a day and 3,000 a month, with
a 40MB ceiling on one message, which is far more headroom than this needs. No
card required.

**Rate limiting.** Five reports per IP per hour, counted in KV:

```
npx wrangler kv namespace create SUPPORT_RL
# then add the returned id to wrangler.toml as the SUPPORT_RL binding
```

KV rather than a Durable Object because the free plan is the constraint and KV
is what it includes. KV is eventually consistent, so somebody on two networks
at once might squeeze an extra report through — that is not the failure worth
engineering against, since the point is to stop a script rather than to be
exact about a person. Each key expires on its own, so nothing accumulates and
nothing needs cleaning up. With no namespace bound the endpoint still works and
simply does not rate-limit.

With either secret unset the endpoint answers `{ok: false}` with a 502 and says
only that reporting is not set up — never which secret is missing — and the app
offers to copy the message to the clipboard so nothing the user wrote is lost.


## Shared picks

Customers who turn on **Share my picks with The Edge** send each pick to
`POST /v1/picks` here as they make it. The Worker stores it, grades it once the
game is final, and shows a leaderboard at `/v1/dashboard`.

**Pick sharing requires a live subscription.** Every `POST /v1/picks` carries
the licence key and is checked against Whop before anything is written; an
invalid or lapsed key is a 403 and stores nothing, and a batch whose `picker`
is not the id that key hashes to is refused as well, so one valid subscription
cannot write under somebody else's id or a thousand invented ones. The check is
cached for ten minutes per key, in memory, so a full slate is one Whop call
rather than one per pick, and batches are rate-limited per key in the same KV
the support endpoint uses. When Whop itself is unreachable the batch is
accepted and the skipped check logged — a customer should not lose a week of
picks to somebody else's outage. The key is never logged and never written to
D1: what is stored is the id, as before.

**What arrives.** A picker id, a display name, and one row per pick: the game,
the kind (winner, spread, total or survivor), the side, and the line, price and
book probability *as they stood when the pick was made*. Not the closing line —
somebody who took a team at -3 did not take them at -7, and grading them on a
number they never saw would make the leaderboard meaningless.

**The id is a pseudonym, not an anonymiser.** Sixteen hex characters of
`SHA-256("the-edge:picks:v1:" + licence key)`. The key is not in these requests,
so this database is not a list of anybody's subscription — but the salt is in
the app's source and you hold the keys, so you can work out which customer a
picker is whenever you want to. That is the point of the feature; it is what the
in-app notice says, and it is why the app never uses the word "anonymous".

**Two rules the routes keep.**

* Grading uses `received_at`, stamped here, never the client's `picked_at`. A
  pick that arrives after its game kicked off is marked `late` and counted in
  neither column. Without this the leaderboard ranks whoever is most willing to
  lie about when they picked.
* Deleting needs the licence key. `DELETE /v1/picks` takes `{picker,
  license_key}` and checks the key hashes to the id before removing anything —
  the id is printed on the leaderboard, so it cannot also be what authorises
  erasing somebody's record.

**Setting it up.** A D1 database and one secret:

```
npx wrangler d1 create the-edge-picks
# paste the returned id into wrangler.toml as the PICKS_DB binding, then
npx wrangler d1 execute the-edge-picks --remote --file license-server/schema.sql
npx wrangler secret put DASHBOARD_TOKEN    # long and random
```

D1 rather than the KV the rest of this Worker uses, because a leaderboard is a
`GROUP BY` and doing that over KV means reading every key on every page load. A
hundred customers picking sixteen games a week is under two thousand rows a
week, which is nowhere near the free tier.

With no `PICKS_DB` bound the endpoint answers `{ok: false, reason:
"not_configured"}` with a 200, which tells the app to drop the batch rather
than queue it for ever.

**The dashboard.** Three views behind one token, laid out like the app's own
Performance tab. The token is the whole of the authentication and it travels in
a URL, so it ends up in browser history — treat it like a password. Anything
but an exact match is a 404 rather than a 403 on every view, so the page does
not announce itself, and every response is `no-store`: this is every sharing
customer's record.

| URL | What it is |
| --- | --- |
| `/v1/dashboard?token=…` | **This week.** One row per game, by kickoff, with a countdown. |
| `…&week=6&season=2026` | The same, for a week you name. Defaults to the current NFL week. |
| `…&view=board` | **Leaderboard**, plus a league-wide by-team table. |
| `…&picker=<16 hex>` | **One picker**: record, week by week, by team. |

*This week* is for deciding before kickoff, so it is about live picks rather
than graded history. Per game: the split as a count *and* a share — the raw
count always sits beside the percentage, and two picks read "2 of 2" rather
than "100% on KC", because a percentage standing on its own is how a thin
sample starts looking like a signal; the model's own side, so agreement and
disagreement are visible at a glance; the average line the pickers got against
the last line to arrive, which is what makes being early visible; a weighted
split counting only pickers past 20 graded picks and above break-even, left
blank rather than zeroed when fewer than three of them picked that game; and an
expandable list of the individual picks, newest first, with anything that
arrived after kickoff marked **late**. A game nobody picked still appears, with
zeros. Above the games: how many pickers shared this week, how many ever have,
and how many subscriptions are live — so the thinness of the sample is always
in view. The subscription count is best effort from Whop and shows an em dash
rather than a number nobody should trust if the call fails.

A picker id that is not exactly sixteen hex characters is a 404, and so is an
unknown one: a page that renders for any sixteen characters is a page that
tells you which ids exist.

**Grading.** A cron trigger (hourly, `17 * * * *`) grades the current week and
the one before it — a Monday night game is graded after the week has rolled
over. Results come from ESPN's public scoreboard, the same source the app uses,
so the two cannot disagree about who won.

### Migrating the consensus table

**Run these two statements in the D1 console before deploying**, or as soon
after as you can. `consensus` gained a `phase` column and a two-part primary
key, and `CREATE TABLE IF NOT EXISTS` is a no-op against a table that already
exists — so `ensureSchema` **cannot** do this for you. It detects the old shape
and logs a line pointing back here on every scheduled run, but it will not
`DROP` a table by itself on a database it has never seen.

The table is empty, so nothing is lost:

```sql
DROP TABLE IF EXISTS consensus;

CREATE TABLE consensus (
    game_id      TEXT NOT NULL,
    phase        TEXT NOT NULL,        -- first|prekick
    season       INTEGER NOT NULL,
    week         INTEGER NOT NULL,
    side         TEXT,
    picks        INTEGER NOT NULL,
    total_picks  INTEGER NOT NULL,
    proven_picks INTEGER NOT NULL,
    proven_side  TEXT,
    avg_line     REAL,
    model_side   TEXT,
    captured_at  TEXT NOT NULL,
    result       TEXT,
    close_line   REAL,
    clv          REAL,
    proven_clv   REAL,
    graded_at    TEXT,
    PRIMARY KEY (game_id, phase)
);

CREATE INDEX IF NOT EXISTS idx_consensus_week ON consensus(season, week);
CREATE INDEX IF NOT EXISTS idx_consensus_phase ON consensus(phase, graded_at);
```

A **fresh** database needs none of this: `ensureSchema` creates exactly that
table on its first scheduled run, and `schema.sql` carries the same definition.

**What happens if you deploy before running it.** `ensureSchema` only runs on
the scheduled handler, so there is a window of up to an hour between a deploy
and the next hourly run. In that window, on a database still carrying the old
shape: the leaderboard's crowd rows are unavailable — the panel says so in its
header rather than erroring, and the rest of the page is unaffected — and no
split is captured, which for games kicking off inside that window is data that
cannot be recovered afterwards. Running the statements above at deploy time
closes the window entirely, which is why this section is first.

**The pre-kickoff snapshot.** The `consensus` table holds what the crowd said
about each game *before it started*, so "would following the crowd have beaten
the model" stays answerable. It cannot be answered from `picks` after the fact:
a pick can change right up to kickoff, so a split recomputed on Tuesday is not
what anybody could have acted on come Sunday.

There are two rows per game, and neither is ever overwritten.

| Phase | When | What it is for |
| --- | --- | --- |
| `first` | the first hourly run that sees *any* shared pick for the game, however many days out | the early number, which is what a closing-line figure is measured from |
| `prekick` | the run inside the hour before kickoff | the opinion the crowd went into the game with, which is what its win-loss record is graded on |

The `first` capture is deliberately not scoped to the current week: the app
lets somebody pick a game a fortnight out, and catching it only once that week
came round would make the early number a late one.

The same hourly run, before grading, writes one `prekick` row for each game
kicking off **within the next hour** and not already started — one capture per
game, as close to kickoff as an hourly cron allows. A game five hours out is left for a
later run; a game already under way is skipped, because whatever is in the
table by then includes picks made after the ball was kicked. An existing row is
never overwritten: the read skips what is frozen and the insert is
`ON CONFLICT DO NOTHING` on top, so two runs firing at once cannot rewrite
history.

Grading does two things, one per phase. It compares the `prekick` side against
the final score and stores the verdict, which gives the crowd a graded record
of its own; and it compares the `first` row's average line against the closing
line and stores the difference, which gives it a **closing-line value** — a
record says whether the crowd picks winners, CLV says whether it gets better
numbers than the close, and the second is the one that predicts the first. Both
appear on the leaderboard as **The crowd**, with **Proven pickers only** beside
it: the same games, counting only the pickers who qualify.

CLV is in points, signed so that positive means the crowd beat the close —
taking a favourite at -3.5 that closed at -7 is +3.5, and the sign is read
through whichever side the crowd was on, since lines are stored as the home
team's spread throughout. It is **blank rather than zero** when no closing line
is known: an average that quietly counts unknowns as par is an average that
flatters the crowd.

**The closing line** is the last line to arrive on a shared pick before
kickoff. There is no odds feed on this server, so that is the best it can know;
it is read at grading time rather than frozen at the `prekick` capture, because
that capture happens up to an hour out and the last hour is where a line moves.
A pick that landed after kickoff is not a close and is not used as one.

A game nobody picked has no side and is not graded as a loss: silence is not a
wrong answer. None of this changes how an individual pick is graded.

The table is in `schema.sql`, and the Worker puts it up itself on each
scheduled run for a database that does not have it (`CREATE TABLE IF NOT
EXISTS`, plus an `ALTER TABLE picks ADD COLUMN model_side` whose failure is
ignored because "duplicate column" is what success looks like the second time).
What it cannot do is reshape a table that is already there — see **Migrating
the consensus table** above.
