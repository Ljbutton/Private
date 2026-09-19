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
