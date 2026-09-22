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
- The key is rechecked every 12 hours. If the server can't be reached, the app
  keeps working for **7 days** on the last good check.
- Cancelled, expired or unpaid (`past_due`) subscriptions lock the app at the
  next check. `canceling` (cancelled but paid through the period) keeps working,
  and so does `completed` — which is how Whop reports a one-time purchase such
  as the season pass once it is paid, rather than one that has run out.
- One key works on up to `MAX_MACHINES` computers (default 2). To move a
  customer to a new computer, clear `edge_machines` in that membership's
  metadata on Whop.
- The demo season (`NFLPICKER_DEMO=1`) and source checkouts never ask for a key.

## Test

```bash
node --test license-server/worker.test.mjs
```


## Bug reports

Help → Report a bug in the app posts to `POST /v1/support` here, and the Worker
sends it on as one email through [Resend](https://resend.com). The report
carries what the user wrote, what they were doing, and whichever of the four
optional details they left ticked: version and commit, OS, the last four
characters of their licence key, and the last hundred lines of the app's log.
The app redacts keys and anything key-shaped out of all of it before it leaves
the machine.

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

**Free tier.** Resend's free plan is 100 emails a day and 3,000 a month, which
is far more headroom than a bug report flow needs. No card required.

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
offers to copy the report to the clipboard so nothing the user wrote is lost.
