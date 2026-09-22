// The Edge license server: a Cloudflare Worker between the desktop app and Whop.
//
// Why it exists: checking a Whop license key needs your Whop *company* API key.
// That key can read every customer's membership and email, so it can never ship
// inside the installer -- anyone could pull it out of the binary. The app talks
// to this Worker; only this Worker holds the key.
//
// Endpoints
//   POST /v1/support    {category, description, doing, email, details,
//                        images[]}                            -> {ok, ref}
//   POST /v1/picks      {picker, license_key, name, picks[]} -> {ok, stored}
//   DELETE /v1/picks    {picker, license_key}            -> {ok, deleted}
//   GET  /v1/dashboard?token=...                    -> the private leaderboard
//   POST /v1/validate   {license_key, machine_id}  -> {valid, status, reason, message}
//   GET  /v1/latest                                 -> {commit, built_at, notes, download_url}
//   GET  /v1/download?key=...&asset=...             -> 302 to the installer (valid keys only)
//   GET  /                                          -> "ok" (health check)
//
// Secrets / variables (Cloudflare dashboard -> Worker -> Settings -> Variables):
//   WHOP_API_KEY      secret. Whop company API key with member:basic:read,
//                     member:email:read and member:manage (the last one is needed
//                     to remember which computers a key is used on).
//   WHOP_PRODUCT_ID   optional. prod_... -- keys from any other product are refused.
//   MAX_MACHINES      optional, default 2. Computers one subscription may run on.
//   ALLOWED_STATUSES  optional, default "active,trialing,canceling,completed".
//                     `completed` is there because a one-time purchase -- the
//                     season pass -- is reported by Whop as completed once it
//                     has been paid. It means paid in full, not lapsed.
//   GITHUB_REPO       optional, e.g. Ljbutton/Private -- where releases are published.
//   GITHUB_TOKEN      secret, optional. Needed once that repository is private.
//   RELEASE_TAG       optional, default "latest".
//   DOWNLOAD_PAGE     optional. Where "Download update" sends people if the
//                     direct download is not set up (e.g. your Whop product page).
//   RESEND_API_KEY    secret. Resend API key, used to send the one email a
//                     support message becomes. Never returned in a response.
//   SUPPORT_EMAIL_TO  secret. Where support messages are emailed. A secret
//                     rather than a constant because this repository is
//                     public and an address in it is an address that gets
//                     scraped -- and
//                     because the app must not be able to reveal where reports
//                     go even to someone who unpacks the binary.
//   PICKS_DB          optional D1 binding holding shared picks. Without it
//                     the picks endpoints answer "not set up" and the app
//                     stops trying. Schema: license-server/schema.sql.
//   DASHBOARD_TOKEN   secret. The whole of the authentication on
//                     /v1/dashboard, which shows every sharer's picks. Long
//                     and random, and treated like a password -- it travels
//                     in a URL, so it ends up in browser history.
//   SUPPORT_RL        optional KV namespace binding, used to rate-limit reports
//                     by IP. See supportRoute for why KV and not something
//                     cleverer. Without it the endpoint still works and simply
//                     does not rate-limit.

const WHOP = "https://api.whop.com/api/v1";

export default {
  // Grading, on a timer rather than on a request: a leaderboard that only
  // updates when somebody looks at it is a leaderboard that makes the person
  // looking at it wait for ESPN. Two weeks each run, because a Monday night
  // game is graded after the week has rolled over.
  async scheduled(event, env, ctx) {
    if (!env.PICKS_DB) return;
    ctx.waitUntil((async () => {
      // The tables first: schema arrives through the D1 console, and a deploy
      // that needs a console visit before it works is a deploy that gets half
      // done. Everything here is idempotent.
      await ensureSchema(env);
      const now = new Date();
      const { season, week } = nflWeek(now);

      // Freeze what the crowd is saying about anything kicking off within the
      // hour, before grading, because after kickoff it is no longer a
      // prediction -- and this run is the last chance to catch it.
      try {
        // The early number first: a game picked a fortnight out gets its row
        // on the run that first sees the pick, not once its week comes round.
        const first = await captureFirst(env, season, now);
        if (first.captured) {
          console.log("picks: froze a first look", JSON.stringify(first));
        }
        const out = await captureConsensus(env, season, week, now);
        if (out.captured) console.log("picks: froze the split", JSON.stringify(out));
      } catch (err) {
        console.log("picks: capture failed", String(err && err.message || err));
      }

      for (const w of [week, week - 1]) {
        if (w < 1) continue;
        try {
          const out = await gradeWeek(env, season, w);
          console.log("picks: graded", JSON.stringify(out));
          // The crowd's own record, which is a separate thing from any
          // individual pick and does not change how one is graded.
          const crowd = await gradeCrowd(env, season, w);
          if (crowd.graded) console.log("picks: graded the crowd", JSON.stringify(crowd));
        } catch (err) {
          console.log("picks: grading failed", String(err && err.message || err));
        }
      }
    })());
  },

  async fetch(request, env) {
    const url = new URL(request.url);
    try {
      if (request.method === "OPTIONS") return cors(new Response(null, { status: 204 }));
      if (url.pathname === "/" ) return cors(new Response("ok"));
      if (url.pathname === "/v1/support" && request.method === "POST") {
        return await supportRoute(request, env);
      }
      if (url.pathname === "/v1/picks" && request.method === "POST") {
        return await picksRoute(request, env);
      }
      if (url.pathname === "/v1/picks" && request.method === "DELETE") {
        return await picksDeleteRoute(request, env);
      }
      if (url.pathname === "/v1/dashboard" && request.method === "GET") {
        return await dashboardRoute(url, env);
      }
      if (url.pathname === "/v1/validate" && request.method === "POST") {
        return json(await validate(await request.json().catch(() => ({})), env));
      }
      if (url.pathname === "/v1/latest" && request.method === "GET") {
        return json(await latest(env, url));
      }
      if (url.pathname === "/v1/download" && request.method === "GET") {
        return await download(url, env);
      }
      return json({ error: "not found" }, 404);
    } catch (err) {
      return json({ error: "server error", detail: String(err && err.message || err) }, 500);
    }
  },
};

// ------------------------------------------------------------------- support

// A support message, turned into one email.
//
// The recipient is a secret on this Worker rather than a constant in the app
// or in this file, for three reasons: this repository is public, an address in
// public gets scraped, and the app should not be able to reveal where reports
// go even to somebody who unpacks the binary. The app posts here and does not
// know the destination.
//
// No licence key is required. "My key will not activate" is the likeliest
// report this will ever receive and the people making it cannot get past the
// gate, so requiring one would exclude exactly the reports worth having. What
// stands in for it is the size cap and the rate limit below.

// The whole payload, headers and all. The words in a report are a few
// kilobytes; what sets this is the screenshots, which arrive base64-encoded
// and so a third larger than they are on disk. Three images of two megabytes
// each is the ceiling the app enforces, and this is that with room around it
// -- large enough for the biggest honest report, small enough that the
// endpoint cannot be used to push anything through.
export const SUPPORT_MAX = 8_500_000;

// What the message is about. The category picks the word in the subject line
// and what the second question was called, so a mailbox can be sorted on it.
export const SUPPORT_CATEGORIES = {
  bug: { tag: "bug", context: "What they were doing" },
  suggestion: { tag: "suggestion", context: "Where it would go" },
  general: { tag: "support", context: "More detail" },
};

// Screenshots. The app checks these too, against the magic bytes as well as
// the declared type -- this is the same ceiling again because this endpoint is
// public and the app is not the only thing that can reach it.
export const IMAGE_MAX = 3;
export const IMAGE_BYTES_MAX = 2_000_000;
const IMAGE_TYPES = {
  "image/png": ".png",
  "image/jpeg": ".jpg",
  "image/webp": ".webp",
  "image/gif": ".gif",
};

// The attachments worth sending, from whatever the caller offered. A bad one
// is dropped rather than refused: somebody has just written six paragraphs
// about a crash and losing them over a screenshot would be its own bug.
function attachments(items) {
  if (!Array.isArray(items)) return [];
  const out = [];
  for (const item of items) {
    if (out.length >= IMAGE_MAX) break;
    if (!item || typeof item !== "object") continue;
    const kind = String(item.type || "").toLowerCase();
    const content = String(item.data || "").replace(/\s+/g, "");
    if (!IMAGE_TYPES[kind] || !content) continue;
    if (!/^[A-Za-z0-9+/]+={0,2}$/.test(content)) continue;
    // Four base64 characters carry three bytes.
    if ((content.length / 4) * 3 > IMAGE_BYTES_MAX) continue;
    const name = String(item.filename || "").replace(/[^A-Za-z0-9._-]+/g, "-")
      .replace(/^[-._]+/, "").slice(0, 60);
    const stem = name.replace(/\.[^.]*$/, "") || `screenshot-${out.length + 1}`;
    out.push({ filename: `${stem}${IMAGE_TYPES[kind]}`, content });
  }
  return out;
}

// Reports per IP per hour.
export const SUPPORT_RATE = 5;

// Why KV. Durable Objects would give an exact counter and are the usual answer
// to rate limiting, but the free plan is the constraint here and KV is what it
// includes. The cost of KV being eventually consistent is that somebody on two
// networks at once might get a couple of extra reports through, which is not
// the failure mode worth engineering against -- the point is to stop a script,
// not to be exact about a human. One key per IP per hour, expiring on its own,
// so nothing accumulates and nothing has to be cleaned up. With no namespace
// bound the endpoint still works and simply does not limit.
async function overRateLimit(env, ip) {
  if (!env.SUPPORT_RL || !ip) return false;
  const bucket = `rl:${ip}:${Math.floor(Date.now() / 3_600_000)}`;
  try {
    const seen = Number(await env.SUPPORT_RL.get(bucket)) || 0;
    if (seen >= SUPPORT_RATE) return true;
    // Two hours, so a bucket written at :59 is not read back as missing.
    await env.SUPPORT_RL.put(bucket, String(seen + 1), { expirationTtl: 7200 });
  } catch (err) {
    // A rate limiter that is down must not take reporting down with it.
    console.log("support: rate limit unavailable", String(err && err.message || err));
    return false;
  }
  return false;
}

async function supportRoute(request, env) {
  // Read the body with the cap applied first, so an oversize one is refused
  // rather than parsed.
  const raw = await request.text().catch(() => "");
  if (raw.length > SUPPORT_MAX) {
    return json({ ok: false, reason: "too_large",
                  message: "That report is too big to send." }, 400);
  }
  let body = {};
  try { body = JSON.parse(raw || "{}"); } catch { body = {}; }

  const description = String(body.description || "").trim();
  if (!description) {
    return json({ ok: false, reason: "empty",
                  message: "A description is required." }, 400);
  }

  const ip = request.headers.get("cf-connecting-ip") || "";
  if (await overRateLimit(env, ip)) {
    return json({ ok: false, reason: "rate_limited",
                  message: "A few reports have come from here in the last "
                           + "hour. Try again later." }, 429);
  }

  const result = await support(body, env);
  // A send that failed is a bad gateway, not a successful request reporting
  // failure in its body: the app retries and the caller's own logs should
  // show it. Nothing about why leaves here -- see support().
  const status = result.ok ? 200 : 502;
  return json(result, status);
}

export async function support(body, env) {
  const description = String(body.description || "").trim();
  const doing = String(body.doing || "").trim();
  const email = String(body.email || "").trim();
  const details = (body.details && typeof body.details === "object") ? body.details : {};

  // Six characters of base32-ish: enough to quote back in a reply and match
  // against an inbox, short enough to read out.
  const ref = Math.random().toString(36).slice(2, 8);

  if (!env.RESEND_API_KEY || !env.SUPPORT_EMAIL_TO) {
    // Never say which of the two is missing, and never name the recipient.
    console.log("support: not configured");
    return { ok: false, reason: "not_configured",
             message: "Reporting is not set up on the server yet." };
  }

  const hint = String(details.key_hint || "").trim();
  const version = details.version || {};
  const environment = details.environment || {};
  const kind = SUPPORT_CATEGORIES[String(body.category || "").toLowerCase()]
    || SUPPORT_CATEGORIES.bug;
  const files = attachments(body.images);
  const subject = `[The Edge ${kind.tag}] `
    + `${description.slice(0, 60).replace(/\s+/g, " ")} — ${hint || "no key"}`;

  const lines = [
    description,
    "",
    doing ? `${kind.context}:\n${doing}` : `${kind.context}: (not given)`,
    "",
    `Reference: ${ref}`,
    `Reply to: ${email || "(no address given)"}`,
    "",
    "--- included by the reporter ---",
    `Included: ${(details.included || []).join(", ") || "(nothing)"}`,
  ];
  if (version.version) {
    lines.push(`Version: ${version.version}`
      + (version.commit ? ` (${version.commit})` : "")
      + (version.built_at ? ` built ${version.built_at}` : ""));
  }
  if (environment.os) lines.push(`OS: ${environment.os}`);
  if (environment.os_detail) lines.push(`OS detail: ${environment.os_detail}`);
  if (hint) lines.push(`Licence key hint: ${hint}`);
  if (files.length) {
    lines.push(`Screenshots: ${files.map((f) => f.filename).join(", ")}`);
  }
  if (details.log) lines.push("", "--- last lines of the log ---", String(details.log));

  const message = {
    from: "The Edge Support <onboarding@resend.dev>",
    to: [env.SUPPORT_EMAIL_TO],
    subject,
    text: lines.join("\n"),
  };
  if (files.length) message.attachments = files;
  // Resend's REST field is reply_to. Only set when an address was given: an
  // empty one is rejected by the API.
  if (/^[^@\s]+@[^@\s.]+\.[^@\s]+$/.test(email)) message.reply_to = [email];

  let response;
  try {
    response = await fetch("https://api.resend.com/emails", {
      method: "POST",
      headers: {
        authorization: `Bearer ${env.RESEND_API_KEY}`,
        "content-type": "application/json",
      },
      body: JSON.stringify(message),
    });
  } catch (err) {
    // Log the reason, return none of it: an upstream error can quote back the
    // request, and the request carries the key in a header.
    console.log("support: send failed", String(err && err.message || err));
    return { ok: false, reason: "upstream",
             message: "The report could not be delivered just now." };
  }
  if (!response.ok) {
    console.log("support: resend rejected", response.status);
    return { ok: false, reason: "upstream", status: response.status,
             message: "The report could not be delivered just now." };
  }
  return { ok: true, ref };
}

// --------------------------------------------------------------------- picks
//
// Opt-in pick sharing. The app sends the picks its user made, graded here so a
// leaderboard can be built from them and the model can learn from whoever is
// consistently right.
//
// What arrives is a pseudonym, not a name: sixteen hex characters of
// SHA-256(salt + licence key). The key itself is not in these requests, so
// this database is not a list of anybody's subscription -- but the salt is in
// the app's source and the owner holds the keys, so the owner can work out
// which customer a picker is. That is the point of the feature. It is what the
// in-app notice says, and it is why "anonymous" is not the word for it.
//
// Two rules the routes below exist to keep:
//
//   * A pick received after its game kicked off is not graded. Grading uses
//     `received_at`, stamped here, never the client's own clock -- otherwise
//     the leaderboard is a ranking of whoever is willing to lie about when
//     they picked.
//   * A delete needs the licence key, not the id. The id is on the
//     leaderboard; it cannot also be the thing that authorises erasing
//     somebody's record.

// The same salt the app uses. Not a secret, and not doing a secret's job.
const PICK_SALT = "the-edge:picks:v1";
const PICK_ID_CHARS = 16;

// One request's worth. The app batches fifty; this is the ceiling it is held
// to.
export const PICKS_MAX = 200;
const PICK_KINDS = new Set(["winner", "spread", "total", "survivor"]);

// Batches per key per hour. A week of picking is a handful: a full slate goes
// in one batch of sixteen, and changing your mind all Sunday morning is a few
// dozen more. Sixty is generous enough that a real customer never sees it and
// tight enough that a leaked key cannot be used to fill the table.
export const PICKS_RATE = 60;

// How long a licence check is trusted. Without it, a customer clicking through
// a full slate makes one Whop call per pick.
export const LICENCE_TTL_MS = 10 * 60 * 1000;

// Keyed by the picker id, which is a one-way hash of the licence key: one
// entry per key, which is what this has to be, without a raw key sitting in a
// long-lived map where a heap dump or an error trace could find it. In memory
// only -- never serialised, never logged, never written to D1, and gone when
// the isolate is recycled, which is the right lifetime for a ten-minute cache.
const licenceCache = new Map();

function cachedLicence(picker) {
  const hit = licenceCache.get(picker);
  if (!hit) return null;
  if (hit.until < Date.now()) {
    licenceCache.delete(picker);
    return null;
  }
  return hit;
}

function rememberLicence(picker, valid) {
  // A ceiling, so a long-lived isolate cannot grow this without bound. Far
  // more entries than this will ever hold at once.
  if (licenceCache.size > 1000) licenceCache.clear();
  licenceCache.set(picker, { valid, until: Date.now() + LICENCE_TTL_MS });
}

// Per key per hour, in the same KV the support endpoint uses. The bucket name
// carries the picker id rather than the key, so nothing secret is in KV
// either.
async function overPicksLimit(env, picker) {
  const store = env.PICKS_RL || env.SUPPORT_RL;
  if (!store) return false;
  const bucket = `pk:${picker}:${Math.floor(Date.now() / 3_600_000)}`;
  try {
    const seen = Number(await store.get(bucket)) || 0;
    if (seen >= PICKS_RATE) return true;
    await store.put(bucket, String(seen + 1), { expirationTtl: 7200 });
  } catch (err) {
    // A rate limiter that is down must not take pick sharing down with it.
    console.log("picks: rate limit unavailable", String(err && err.message || err));
    return false;
  }
  return false;
}

async function pickerFor(key) {
  const bytes = new TextEncoder().encode(`${PICK_SALT}:${key}`);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)]
    .map((b) => b.toString(16).padStart(2, "0")).join("").slice(0, PICK_ID_CHARS);
}

function cleanPick(raw) {
  if (!raw || typeof raw !== "object") return null;
  const kind = String(raw.kind || "").toLowerCase();
  const gameId = String(raw.game_id || "").slice(0, 64);
  const side = String(raw.side || "").toUpperCase().slice(0, 8);
  if (!PICK_KINDS.has(kind) || !gameId || !side) return null;
  const num = (v) => (v === null || v === undefined || v === "" || Number.isNaN(Number(v))
    ? null : Number(v));
  return {
    game_id: gameId,
    kind,
    side,
    season: Math.trunc(num(raw.season) || 0),
    week: Math.trunc(num(raw.week) || 0),
    line: num(raw.line),
    price: raw.price === null || raw.price === undefined ? null : Math.trunc(Number(raw.price)),
    total_line: num(raw.total_line),
    book_prob: num(raw.book_prob),
    picked_at: String(raw.picked_at || "").slice(0, 40) || null,
  };
}

async function picksRoute(request, env) {
  if (!env.PICKS_DB) {
    // No database bound is not an error the app should retry for ever: say so
    // plainly and let it drop the batch.
    return json({ ok: false, reason: "not_configured",
                  message: "Pick sharing is not set up on the server." }, 200);
  }
  const raw = await request.text().catch(() => "");
  if (raw.length > 400_000) {
    return json({ ok: false, reason: "too_large" }, 400);
  }
  let body = {};
  try { body = JSON.parse(raw || "{}"); } catch { body = {}; }

  const picker = String(body.picker || "").toLowerCase();
  if (!/^[0-9a-f]{16}$/.test(picker)) {
    return json({ ok: false, reason: "bad_picker" }, 400);
  }

  // Everything below is the gate. This endpoint shipped without one, on the
  // support endpoint's reasoning -- that endpoint takes no key because "my key
  // will not activate" is exactly the message that cannot produce one. Picks
  // are the opposite case: only a paying customer has picks worth grading, and
  // an open write endpoint means anybody who finds the URL can fill the table
  // the leaderboard is built from.
  const key = String(body.license_key || "").trim();
  if (!key) {
    return json({ ok: false, reason: "unlicensed",
                  message: "Pick sharing needs a live subscription." }, 403);
  }

  // The id has to be the one this key produces. Without this, one valid
  // subscription could write under any id it liked -- somebody else's, or a
  // thousand invented ones -- and the leaderboard would be whatever its
  // busiest customer decided it was.
  if (await pickerFor(key) !== picker) {
    return json({ ok: false, reason: "picker_mismatch",
                  message: "That picker id does not belong to that key." }, 403);
  }

  // Before the Whop call, so a flood cannot be turned into a flood of those.
  if (await overPicksLimit(env, picker)) {
    return json({ ok: false, reason: "rate_limited",
                  message: "Too many batches from this key in the last hour." },
                429);
  }

  let degraded = false;
  const cached = cachedLicence(picker);
  if (cached) {
    if (!cached.valid) {
      return json({ ok: false, reason: "unlicensed",
                    message: "Pick sharing needs a live subscription." }, 403);
    }
  } else {
    // No machine id: this is not an activation, and registering a computer as
    // a side effect of sharing a pick would spend one of the customer's two
    // slots without them doing anything.
    const check = await validate({ license_key: key }, env);
    if (check.valid === true) {
      rememberLicence(picker, true);
    } else if (check.valid === null) {
      // Whop is having a bad day. Taking the batch is the right call: the
      // alternative is losing a customer's picks over somebody else's outage,
      // and the id still had to match a well-formed key to get this far. Not
      // cached, so the next batch tries Whop again.
      degraded = true;
      console.log("picks: accepted without a licence check, Whop unreachable",
                  String(check.message || check.reason || ""));
    } else {
      rememberLicence(picker, false);
      return json({ ok: false, reason: "unlicensed",
                    message: check.message
                      || "Pick sharing needs a live subscription." }, 403);
    }
  }

  const picks = (Array.isArray(body.picks) ? body.picks : [])
    .slice(0, PICKS_MAX).map(cleanPick).filter(Boolean);
  if (!picks.length) return json({ ok: false, reason: "empty" }, 400);

  const now = new Date().toISOString();
  const name = String(body.name || "").slice(0, 40) || `Picker #${picker.slice(0, 4)}`;

  const statements = [
    env.PICKS_DB.prepare(
      "INSERT INTO pickers(picker, name, first_at, last_at) VALUES(?,?,?,?) "
      + "ON CONFLICT(picker) DO UPDATE SET name = excluded.name, last_at = excluded.last_at",
    ).bind(picker, name, now, now),
  ];
  for (const p of picks) {
    statements.push(env.PICKS_DB.prepare(
      "INSERT INTO picks(picker, game_id, kind, season, week, side, line, price,"
      + " total_line, book_prob, picked_at, received_at)"
      + " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)"
      + " ON CONFLICT(picker, game_id, kind) DO UPDATE SET"
      + " side = excluded.side, line = excluded.line, price = excluded.price,"
      + " total_line = excluded.total_line, book_prob = excluded.book_prob,"
      + " picked_at = excluded.picked_at, received_at = excluded.received_at,"
      // A changed pick is ungraded again: it is a different bet now.
      + " result = NULL, graded_at = NULL"
      // ...but only while the game has not started. Once there is a result on
      // file, the pick that was in before kickoff is the one that counts.
      + " WHERE picks.result IS NULL",
    ).bind(picker, p.game_id, p.kind, p.season, p.week, p.side, p.line, p.price,
           p.total_line, p.book_prob, p.picked_at, now));
  }
  try {
    await env.PICKS_DB.batch(statements);
  } catch (err) {
    console.log("picks: store failed", String(err && err.message || err));
    return json({ ok: false, reason: "store_failed" }, 502);
  }
  return json(degraded
    ? { ok: true, stored: picks.length, unchecked: true }
    : { ok: true, stored: picks.length });
}

async function picksDeleteRoute(request, env) {
  if (!env.PICKS_DB) return json({ ok: true, deleted: 0 });
  const body = await request.json().catch(() => ({}));
  const picker = String(body.picker || "").toLowerCase();
  const key = String(body.license_key || "").trim();
  if (!/^[0-9a-f]{16}$/.test(picker) || !key) {
    return json({ ok: false, reason: "bad_request" }, 400);
  }
  // The proof. Anybody can read an id off the leaderboard; only the person
  // holding the key can produce one that hashes to it.
  if (await pickerFor(key) !== picker) {
    return json({ ok: false, reason: "not_yours" }, 403);
  }
  try {
    const gone = await env.PICKS_DB.prepare("DELETE FROM picks WHERE picker = ?")
      .bind(picker).run();
    await env.PICKS_DB.prepare("DELETE FROM pickers WHERE picker = ?")
      .bind(picker).run();
    return json({ ok: true, deleted: (gone.meta && gone.meta.changes) || 0 });
  } catch (err) {
    console.log("picks: delete failed", String(err && err.message || err));
    return json({ ok: false, reason: "delete_failed" }, 502);
  }
}

// Which season and week a moment belongs to.
//
// Week 1 opens on the Thursday after the first Monday in September, and a week
// rolls over on the Tuesday once Monday night is done -- so the anchor is that
// Tuesday and everything after it is arithmetic. January and February are the
// tail of the previous year's season, which is the case that makes this a
// function rather than a subtraction at the call site.
export function nflWeek(now = new Date()) {
  const year = now.getUTCMonth() < 2 ? now.getUTCFullYear() - 1 : now.getUTCFullYear();
  const sept = new Date(Date.UTC(year, 8, 1));
  const firstMonday = 1 + ((8 - sept.getUTCDay()) % 7);
  const anchor = Date.UTC(year, 8, firstMonday + 1);
  const week = Math.floor((now.getTime() - anchor) / 604_800_000) + 1;
  return { season: year, week: Math.min(22, Math.max(1, week)) };
}

// ------------------------------------------------------------------ grading

// ESPN's public scoreboard, which is where the app gets its results too. No
// key, no allowance, and the same source means the two cannot disagree about
// who won.
const ESPN = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard";

export async function fetchResults(season, week, fetcher = fetch) {
  const url = `${ESPN}?dates=${season}&seasontype=2&week=${week}`;
  const res = await fetcher(url);
  if (!res.ok) throw new Error(`espn ${res.status}`);
  const body = await res.json();
  const out = [];
  for (const event of body.events || []) {
    const comp = (event.competitions || [])[0];
    if (!comp) continue;
    const done = ((comp.status || {}).type || {}).completed === true;
    const home = (comp.competitors || []).find((c) => c.homeAway === "home");
    const away = (comp.competitors || []).find((c) => c.homeAway === "away");
    if (!home || !away) continue;
    out.push({
      game_id: String(event.id),
      season, week,
      kickoff: event.date || null,
      home: (home.team || {}).abbreviation || "",
      away: (away.team || {}).abbreviation || "",
      home_score: done ? Number(home.score) : null,
      away_score: done ? Number(away.score) : null,
    });
  }
  return out;
}

// How one pick did, given the final score and the line the picker took.
//
// Straight-up is the winner. Spread applies the home line the picker saw, so
// two people who took the same team at different numbers can get different
// answers -- which is the whole reason the line travels with the pick.
// Survivor is straight-up with more riding on it. A total needs an over/under
// side; "OVER"/"UNDER" is what the app sends.
export function gradePick(pick, result) {
  const { home, away, home_score: hs, away_score: as } = result;
  if (hs === null || as === null || hs === undefined || as === undefined) return null;
  if (pick.kind === "winner" || pick.kind === "survivor") {
    if (hs === as) return "push";
    const winner = hs > as ? home : away;
    return pick.side === winner ? "win" : "loss";
  }
  if (pick.kind === "spread") {
    if (pick.line === null || pick.line === undefined) return null;
    // The stored line is the home team's number, as everywhere else in this
    // project. A home margin of exactly -line is a push.
    const margin = hs - as + (pick.side === home ? Number(pick.line) : -Number(pick.line));
    if (Math.abs(margin) < 1e-9) return "push";
    return margin > 0 ? "win" : "loss";
  }
  if (pick.kind === "total") {
    if (pick.total_line === null || pick.total_line === undefined) return null;
    const diff = (hs + as) - Number(pick.total_line);
    if (Math.abs(diff) < 1e-9) return "push";
    const over = diff > 0;
    return (pick.side === "OVER") === over ? "win" : "loss";
  }
  return null;
}

export async function gradeWeek(env, season, week, fetcher = fetch) {
  const results = await fetchResults(season, week, fetcher);
  const now = new Date().toISOString();
  const writes = [];
  const byGame = new Map();
  for (const r of results) {
    byGame.set(r.game_id, r);
    writes.push(env.PICKS_DB.prepare(
      "INSERT INTO results(game_id, season, week, kickoff, home, away, home_score,"
      + " away_score, fetched_at) VALUES(?,?,?,?,?,?,?,?,?)"
      + " ON CONFLICT(game_id) DO UPDATE SET home_score = excluded.home_score,"
      + " away_score = excluded.away_score, kickoff = excluded.kickoff,"
      + " fetched_at = excluded.fetched_at",
    ).bind(r.game_id, r.season, r.week, r.kickoff, r.home, r.away,
           r.home_score, r.away_score, now));
  }

  const pending = await env.PICKS_DB.prepare(
    "SELECT * FROM picks WHERE season = ? AND week = ? AND result IS NULL",
  ).bind(season, week).all();

  let graded = 0;
  let late = 0;
  for (const pick of pending.results || []) {
    const result = byGame.get(pick.game_id);
    if (!result || result.home_score === null) continue;
    // Received after the ball was kicked, so it is not a prediction. Marked
    // rather than deleted: a picker should be able to see that a pick was
    // thrown out, and silently dropping it looks like a bug in the app.
    if (result.kickoff && pick.received_at > result.kickoff) {
      late += 1;
      writes.push(env.PICKS_DB.prepare(
        "UPDATE picks SET result = 'late', graded_at = ? WHERE picker = ?"
        + " AND game_id = ? AND kind = ?",
      ).bind(now, pick.picker, pick.game_id, pick.kind));
      continue;
    }
    const verdict = gradePick(pick, result);
    if (!verdict) continue;
    graded += 1;
    writes.push(env.PICKS_DB.prepare(
      "UPDATE picks SET result = ?, graded_at = ? WHERE picker = ?"
      + " AND game_id = ? AND kind = ?",
    ).bind(verdict, now, pick.picker, pick.game_id, pick.kind));
  }
  if (writes.length) await env.PICKS_DB.batch(writes);
  return { season, week, games: results.length, graded, late };
}

// ------------------------------------------------- what the crowd said, frozen
//
// The question worth answering later is "would following the crowd have beaten
// the model", and the picks table cannot answer it after the fact: a pick can
// change right up to kickoff, so a split recomputed on Tuesday is not what
// anybody could have acted on come Sunday. So it is captured once, as close to
// kickoff as the hourly run allows, and never rewritten.

// Who counts as proven. Twenty graded picks is about a season and a half of
// one contest -- enough that a hot fortnight does not qualify somebody -- and
// break-even for a pool of straight-up picks is half of them. Both are here
// rather than inline so the dashboard and the capture cannot drift apart.
export const PROVEN_MIN_PICKS = 20;
export const PROVEN_MIN_RATE = 0.5;

// How close to kickoff a game has to be before its split is frozen. The run is
// hourly, so this is "the next run will not get another chance".
export const CAPTURE_WINDOW_MS = 60 * 60 * 1000;

// The Worker can put its own tables up, because schema arrives through the D1
// console and a deploy that needs a console visit first is a deploy that gets
// half done. Every statement is IF NOT EXISTS; the ALTER is not, so it is run
// on its own and its failure ignored -- "duplicate column" is what success
// looks like the second time.
export const CONSENSUS_DDL =
  "CREATE TABLE IF NOT EXISTS consensus (game_id TEXT NOT NULL, "
  + "phase TEXT NOT NULL, season INTEGER NOT NULL, week INTEGER NOT NULL, "
  + "side TEXT, picks INTEGER NOT NULL, total_picks INTEGER NOT NULL, "
  + "proven_picks INTEGER NOT NULL, proven_side TEXT, avg_line REAL, "
  + "model_side TEXT, captured_at TEXT NOT NULL, result TEXT, "
  + "close_line REAL, clv REAL, proven_clv REAL, graded_at TEXT, "
  + "PRIMARY KEY (game_id, phase))";

export async function ensureSchema(env) {
  if (!env.PICKS_DB) return false;
  try {
    await env.PICKS_DB.exec(CONSENSUS_DDL);
  } catch (err) {
    console.log("picks: could not ensure the consensus table",
                String(err && err.message || err));
    return false;
  }
  try {
    await env.PICKS_DB.exec("ALTER TABLE picks ADD COLUMN model_side TEXT");
  } catch {
    // Already there, which is the usual case.
  }

  // The one thing this cannot do for itself. CREATE TABLE IF NOT EXISTS is a
  // no-op against a table that already exists in the older single-row shape,
  // so a database carrying that shape needs the DROP in the README run by
  // hand -- and a silent no-op is exactly how somebody discovers that three
  // weeks later with a season of captures missing. Said loudly instead, every
  // run, until it is done. No automatic DROP: this code cannot know the table
  // is still empty on a database it has never seen.
  try {
    const info = await env.PICKS_DB.prepare(
      "SELECT name FROM pragma_table_info('consensus')").all();
    const columns = new Set((info.results || []).map((r) => r.name));
    if (columns.size && !columns.has("phase")) {
      console.log("picks: the consensus table is the old single-row shape and "
        + "cannot be migrated from here. Run the DROP and CREATE in "
        + "license-server/README.md, under 'Migrating the consensus table'. "
        + "Until then no split is being captured.");
      return false;
    }
  } catch (err) {
    console.log("picks: could not read the consensus shape",
                String(err && err.message || err));
  }
  return true;
}

// Every picker's career record, as one query rather than one per picker.
// Reads one row per picker -- tens, not thousands.
export async function provenPickers(env) {
  const rows = await env.PICKS_DB.prepare(
    "SELECT picker, SUM(result = 'win') AS wins, SUM(result = 'loss') AS losses"
    + " FROM picks WHERE result IN ('win','loss') GROUP BY picker",
  ).all();
  const proven = new Set();
  const record = new Map();
  for (const r of rows.results || []) {
    const wins = Number(r.wins) || 0;
    const losses = Number(r.losses) || 0;
    const decided = wins + losses;
    record.set(r.picker, { wins, losses, decided });
    if (decided >= PROVEN_MIN_PICKS && wins / decided > PROVEN_MIN_RATE) {
      proven.add(r.picker);
    }
  }
  return { proven, record };
}

// The split on one game, from that game's picks. Straight-up sides only:
// a spread pick and a winner pick on the same team are the same opinion about
// who wins, and a total has no side to agree with.
export function splitFor(picks, proven) {
  const counts = new Map();
  const provenCounts = new Map();
  let total = 0;
  let provenTotal = 0;
  let lineSum = 0;
  let lineN = 0;
  let modelSide = null;
  let modelAt = "";
  for (const p of picks) {
    if (p.kind !== "winner" && p.kind !== "survivor") continue;
    total += 1;
    counts.set(p.side, (counts.get(p.side) || 0) + 1);
    if (proven && proven.has(p.picker)) {
      provenTotal += 1;
      provenCounts.set(p.side, (provenCounts.get(p.side) || 0) + 1);
    }
    if (p.line !== null && p.line !== undefined && p.line !== "") {
      lineSum += Number(p.line);
      lineN += 1;
    }
    // The most recently received opinion the model had on this game.
    if (p.model_side && String(p.received_at || "") >= modelAt) {
      modelSide = p.model_side;
      modelAt = String(p.received_at || "");
    }
  }
  const top = (map) => {
    let side = null;
    let best = 0;
    for (const [k, v] of map) {
      if (v > best) { best = v; side = k; }
    }
    return { side, count: best };
  };
  const crowd = top(counts);
  const provenTop = top(provenCounts);
  return {
    sides: [...counts.entries()].map(([side, count]) => ({ side, count }))
      .sort((a, b) => b.count - a.count),
    side: crowd.side,
    picks: crowd.count,
    total,
    proven_picks: provenTotal,
    proven_side: provenTop.side,
    avg_line: lineN ? lineSum / lineN : null,
    model_side: modelSide,
  };
}

// A row, from a split. The same shape whichever phase it is being written for,
// because the two phases are the same measurement taken at different moments
// and a field that existed on one and not the other would be a trap later.
function consensusInsert(env, game, phase, split, at) {
  return env.PICKS_DB.prepare(
    "INSERT INTO consensus(game_id, phase, season, week, side, picks,"
    + " total_picks, proven_picks, proven_side, avg_line, model_side,"
    + " captured_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)"
    // Belt as well as braces: the reads below already skip what is frozen,
    // and this makes a second run harmless even if two fire at once.
    + " ON CONFLICT(game_id, phase) DO NOTHING",
  ).bind(game.game_id, phase, game.season, game.week, split.side, split.picks,
         split.total, split.proven_picks, split.proven_side, split.avg_line,
         split.model_side, at);
}

// The first time the crowd had an opinion at all.
//
// Not scoped to the current week: the app lets somebody pick a game a fortnight
// out, and "the first time we saw a pick for this game" is the whole point of
// the row -- catching it only once the week comes round would make the early
// number a late one. Scoped to games that have not kicked off, which is what
// bounds it.
//
// Three queries: the picks on upcoming games, the first rows already written,
// and the career records. Reads pickers x upcoming games, one row per game
// already captured this season (~270 at most), and one per picker.
export async function captureFirst(env, season, now = new Date()) {
  const rows = await env.PICKS_DB.prepare(
    "SELECT p.game_id, p.picker, p.kind, p.side, p.line, p.model_side,"
    + " p.received_at, r.season, r.week, r.kickoff"
    + " FROM picks p JOIN results r ON r.game_id = p.game_id"
    + " WHERE r.kickoff > ?",
  ).bind(now.toISOString()).all();
  if (!(rows.results || []).length) return { captured: 0 };

  const already = await env.PICKS_DB.prepare(
    "SELECT game_id FROM consensus WHERE phase = 'first' AND season = ?",
  ).bind(season).all();
  const frozen = new Set((already.results || []).map((r) => r.game_id));

  const byGame = new Map();
  for (const p of rows.results || []) {
    if (frozen.has(p.game_id)) continue;
    if (!byGame.has(p.game_id)) byGame.set(p.game_id, []);
    byGame.get(p.game_id).push(p);
  }
  if (!byGame.size) return { captured: 0 };

  const { proven } = await provenPickers(env);
  const at = now.toISOString();
  const writes = [];
  for (const [gameId, picks] of byGame) {
    const split = splitFor(picks, proven);
    // A game whose only picks are totals has no side and nothing to measure.
    if (!split.total) continue;
    const game = { game_id: gameId, season: picks[0].season, week: picks[0].week };
    writes.push(consensusInsert(env, game, "first", split, at));
  }
  if (writes.length) await env.PICKS_DB.batch(writes);
  return { captured: writes.length };
}

// Freeze the split for every game kicking off within the hour.
//
// Three queries in total, whatever the size of the slate: the games, their
// picks, and the career records that decide who is proven. Reads one row per
// game this week (~16), one per pick this week (pickers x 16), one per picker.
export async function captureConsensus(env, season, week, now = new Date()) {
  const soon = now.getTime() + CAPTURE_WINDOW_MS;
  const games = await env.PICKS_DB.prepare(
    "SELECT game_id, kickoff FROM results WHERE season = ? AND week = ?",
  ).bind(season, week).all();

  const due = (games.results || []).filter((g) => {
    if (!g.kickoff) return false;
    const at = Date.parse(g.kickoff);
    // Not yet started, and starting within the window. A game already under
    // way is too late to freeze: whatever is in the table now includes picks
    // made after the ball was kicked.
    return Number.isFinite(at) && at > now.getTime() && at <= soon;
  });
  if (!due.length) return { captured: 0, skipped: 0 };

  const already = await env.PICKS_DB.prepare(
    "SELECT game_id FROM consensus WHERE season = ? AND week = ?"
    + " AND phase = 'prekick'",
  ).bind(season, week).all();
  const frozen = new Set((already.results || []).map((r) => r.game_id));
  const wanted = due.filter((g) => !frozen.has(g.game_id));
  if (!wanted.length) return { captured: 0, skipped: due.length };

  const picks = await env.PICKS_DB.prepare(
    "SELECT picker, game_id, kind, side, line, model_side, received_at"
    + " FROM picks WHERE season = ? AND week = ?",
  ).bind(season, week).all();
  const byGame = new Map();
  for (const p of picks.results || []) {
    if (!byGame.has(p.game_id)) byGame.set(p.game_id, []);
    byGame.get(p.game_id).push(p);
  }
  const { proven } = await provenPickers(env);

  const at = now.toISOString();
  const writes = wanted.map((g) => consensusInsert(
    env, { game_id: g.game_id, season, week }, "prekick",
    splitFor(byGame.get(g.game_id) || [], proven), at));
  await env.PICKS_DB.batch(writes);
  return { captured: writes.length, skipped: due.length - writes.length };
}

// What a number was worth against the one the game closed at.
//
// Lines are stored as the home team's spread throughout this project, so the
// sign has to be read through whichever side the pick was on. Backing the home
// side, a smaller number is better -- laying 3.5 where the close lays 7 is
// three points in hand -- so the difference is taken in that direction and
// flipped for the away side. Positive always means the number beat the close.
export function crowdClv(side, home, avgLine, closeLine) {
  if (!side || !home) return null;
  if (avgLine === null || avgLine === undefined) return null;
  if (closeLine === null || closeLine === undefined) return null;
  const diff = Number(avgLine) - Number(closeLine);
  if (!Number.isFinite(diff)) return null;
  return side === home ? diff : -diff;
}

// The crowd's side against the final score, and its early number against the
// closing one. Individual picks are graded by gradeWeek and nothing here
// touches them.
//
// Two passes over two phases, each with its own job, so neither is done twice
// and neither can be counted twice. graded_at is the marker for both: a row
// that has it is finished, whichever phase it is.
//
// Three queries: the ungraded rows, the lines on those games, and the writes.
// Reads one row per captured game this week (~32 across both phases) and one
// per shared pick this week.
export async function gradeCrowd(env, season, week) {
  const rows = await env.PICKS_DB.prepare(
    "SELECT c.game_id, c.phase, c.side, c.proven_side, c.avg_line,"
    + " r.home, r.away, r.home_score, r.away_score, r.kickoff"
    + " FROM consensus c JOIN results r ON r.game_id = c.game_id"
    + " WHERE c.season = ? AND c.week = ? AND c.graded_at IS NULL",
  ).bind(season, week).all();
  const pending = (rows.results || []).filter(
    (r) => r.home_score !== null && r.home_score !== undefined);
  if (!pending.length) return { graded: 0, valued: 0 };

  // The closing line, as far as a server with no odds feed can know one: the
  // last line to arrive on a shared pick before the ball was kicked. Read at
  // grading rather than frozen at the prekick capture, because that capture
  // happens up to an hour out and the last hour is where a line moves.
  const lines = await env.PICKS_DB.prepare(
    "SELECT p.game_id, p.line, p.received_at FROM picks p"
    + " WHERE p.season = ? AND p.week = ? AND p.line IS NOT NULL",
  ).bind(season, week).all();
  const closing = new Map();
  const closingAt = new Map();
  for (const row of pending) {
    if (!row.kickoff) continue;
    for (const p of lines.results || []) {
      if (p.game_id !== row.game_id) continue;
      const at = String(p.received_at || "");
      if (at > String(row.kickoff)) continue;          // not a closing line
      if (at >= (closingAt.get(p.game_id) || "")) {
        closingAt.set(p.game_id, at);
        closing.set(p.game_id, p.line);
      }
    }
  }

  const at = new Date().toISOString();
  const writes = [];
  let graded = 0;
  let valued = 0;
  for (const row of pending) {
    if (row.phase === "prekick") {
      // A game nobody picked has no side, so the crowd said nothing and there
      // is nothing to grade. Not a loss: silence is not a wrong answer.
      if (!row.side) continue;
      const verdict = gradePick({ kind: "winner", side: row.side }, row);
      if (!verdict) continue;
      graded += 1;
      writes.push(env.PICKS_DB.prepare(
        "UPDATE consensus SET result = ?, graded_at = ?"
        + " WHERE game_id = ? AND phase = 'prekick'",
      ).bind(verdict, at, row.game_id));
      continue;
    }
    // The first row carries the early number, so it is the one a closing-line
    // figure belongs on. Written even when the close is unknown, so the row is
    // finished and not rescanned every hour for ever -- clv stays null, which
    // is what the page shows as blank rather than as zero.
    const close = closing.has(row.game_id) ? closing.get(row.game_id) : null;
    const clv = crowdClv(row.side, row.home, row.avg_line, close);
    const provenClv = crowdClv(row.proven_side, row.home, row.avg_line, close);
    if (clv !== null) valued += 1;
    writes.push(env.PICKS_DB.prepare(
      "UPDATE consensus SET close_line = ?, clv = ?, proven_clv = ?,"
      + " graded_at = ? WHERE game_id = ? AND phase = 'first'",
    ).bind(close, clv, provenClv, at, row.game_id));
  }
  if (writes.length) await env.PICKS_DB.batch(writes);
  return { graded, valued };
}

// ---------------------------------------------------------------- dashboard
//
// Three views behind one token: what the crowd is saying about this week's
// games, the leaderboard, and one picker's record. Laid out like the app's own
// Performance tab -- same palette, same panel-with-a-hint headings, same
// tabular figures -- because it is the same product seen from the other side.
//
// Rules that hold in all three. The token is checked first and a miss is a 404
// rather than a 403, so the page does not announce itself. Every value that
// came out of the database goes through escapeHtml on the way into the HTML.
// Every query is parameterised and there is one query per table, never one per
// row; each says roughly how many rows it reads. No licence key is read, held
// or printed anywhere in here -- the tables do not have one to print.

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

const pct = (v, digits = 1) => (v === null || v === undefined
  ? "—" : `${(v * 100).toFixed(digits)}%`);

const signed = (v) => (v === null || v === undefined || v === ""
  ? "—" : `${Number(v) > 0 ? "+" : ""}${Number(v).toFixed(1)}`);

// "4h ago", "just now". The page is read on a Sunday morning to decide
// something, so how old a pick is matters more than when it was made.
function ago(iso, now = Date.now()) {
  const at = Date.parse(iso || "");
  if (!Number.isFinite(at)) return "";
  const secs = Math.max(0, Math.round((now - at) / 1000));
  if (secs < 90) return "just now";
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 48) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

const DASH_CSS = `
:root { color-scheme: dark; --bg:#12161a; --panel:#181d23; --sunken:#141920;
        --line:#2a3138; --text:#e6edf3; --dim:#8b949e; --good:#2ee6a0;
        --bad:#ff6b6b; --warn:#fbbf24; --accent:#2ee6a0; }
* { box-sizing: border-box; }
body { margin:0; padding:22px 16px 48px; background:var(--bg); color:var(--text);
       font:14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
.wrap { max-width:1100px; margin:0 auto; }
h1 { font-size:19px; margin:0; letter-spacing:-.01em; }
h2 { font-size:14px; margin:0; letter-spacing:-.01em; }
a { color:inherit; }
.top { display:flex; align-items:baseline; gap:14px; flex-wrap:wrap;
       margin:0 0 4px; }
.nav { display:flex; gap:6px; margin:14px 0 18px; flex-wrap:wrap; }
.nav a { padding:5px 11px; border-radius:999px; border:1px solid var(--line);
         text-decoration:none; font-size:12px; color:var(--dim); }
.nav a.on { color:var(--text); border-color:var(--accent);
            background:color-mix(in srgb, var(--accent) 12%, transparent); }
.panel { background:var(--panel); border:1px solid var(--line);
         border-radius:10px; margin:0 0 14px; overflow:hidden; }
.panel > header { display:flex; align-items:baseline; gap:10px; flex-wrap:wrap;
  padding:11px 14px; border-bottom:1px solid var(--line); }
.panel > header h2 { color:var(--accent); }
.hint { font-size:11.5px; color:var(--dim); }
.hint.push { margin-left:auto; }
.body { padding:12px 14px; }
table { width:100%; border-collapse:collapse; }
th, td { padding:8px 14px; text-align:left; border-bottom:1px solid var(--line);
         font-size:13px; }
th { font-size:10.5px; text-transform:uppercase; letter-spacing:.07em;
     color:var(--dim); font-weight:650; white-space:nowrap; }
tbody tr:last-child td { border-bottom:0; }
td.n, th.n { text-align:right; font-variant-numeric:tabular-nums; }
.dim { color:var(--dim); }
.mono { font-family:ui-monospace, SFMono-Regular, Menlo, monospace;
        font-size:10.5px; color:var(--dim); }
/* Under the name rather than trailing off it: an id and a name on one line
   read as one string, and the id is the half nobody is looking for. */
.mono.under { display:block; margin-top:1px; }
.empty { padding:26px 14px; text-align:center; color:var(--dim); }
.tiles { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
         gap:10px; padding:12px 14px; }
.tile { background:var(--sunken); border:1px solid var(--line);
        border-radius:8px; padding:9px 11px; }
.tile .label { font-size:10.5px; text-transform:uppercase; letter-spacing:.06em;
               color:var(--dim); }
.tile .value { font-size:20px; font-weight:700; font-variant-numeric:tabular-nums;
               margin-top:2px; }
.tile .value .sub { font-size:12px; font-weight:400; color:var(--dim); }
.win { color:var(--good); } .loss { color:var(--bad); } .late { color:var(--warn); }
/* The split bar. Width is the share; the count sits beside it in words,
   because a bar on two picks is a picture of nothing. */
.split { display:flex; align-items:center; gap:8px; }
.bar { flex:1 1 90px; height:7px; border-radius:4px; background:var(--sunken);
       border:1px solid var(--line); overflow:hidden; min-width:60px; }
.bar i { display:block; height:100%; background:var(--accent); }
.count { font-variant-numeric:tabular-nums; white-space:nowrap; font-size:12px; }
.side { font-weight:650; }
.game { border-bottom:1px solid var(--line); }
.game:last-child { border-bottom:0; }
.grow { display:grid; grid-template-columns:minmax(150px,1.1fr) 1.4fr repeat(3,minmax(90px,.8fr));
        gap:10px; align-items:center; padding:10px 14px; }
.match { font-weight:650; }
.kick { font-size:11px; color:var(--dim); }
.col-label { font-size:10.5px; text-transform:uppercase; letter-spacing:.07em;
             color:var(--dim); }
details.picks { border-top:1px dashed var(--line); background:var(--sunken); }
details.picks > summary { cursor:pointer; padding:7px 14px; font-size:11.5px;
                          color:var(--dim); list-style:none; }
details.picks > summary::-webkit-details-marker { display:none; }
details.picks > summary::before { content:"▸ "; }
details.picks[open] > summary::before { content:"▾ "; }
details.picks table { background:transparent; }
.agree { font-size:11px; }
.spark { display:inline-flex; align-items:flex-end; gap:2px; height:16px; }
.spark i { width:5px; background:var(--accent); border-radius:1px; min-height:1px; }
.sortable th { cursor:pointer; user-select:none; }
.sortable th:hover { color:var(--text); }
@media (max-width:760px) {
  .grow { grid-template-columns:1fr 1fr; }
}
`;

function shell(title, view, query, body) {
  const link = (name, label, extra = "") =>
    `<a href="?${query}${extra}" class="${view === name ? "on" : ""}">${label}</a>`;
  return `<!doctype html><html lang="en"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>${escapeHtml(title)}</title><style>${DASH_CSS}</style></head><body>
<div class="wrap">
<div class="top"><h1>${escapeHtml(title)}</h1></div>
<nav class="nav">${link("week", "This week")}${link("board", "Leaderboard", "&view=board")}</nav>
${body}
</div></body></html>`;
}

function page(html) {
  return new Response(html, {
    status: 200,
    headers: { "Content-Type": "text/html; charset=utf-8",
               // Never cached anywhere: this is customer data behind a token
               // that lives in a URL.
               "Cache-Control": "no-store, private" },
  });
}

const notFound = () => new Response("Not found", { status: 404 });

// How many subscriptions are live, so the thinness of the sample is always in
// view beside it. Best effort: Whop's list shape is not something this Worker
// can insist on, so anything unexpected shows as an em dash rather than as a
// number nobody should trust. Cached, because it is per page load.
let subsCache = { at: 0, count: null };
async function activeSubscriptions(env) {
  if (!env.WHOP_API_KEY) return null;
  if (Date.now() - subsCache.at < 10 * 60 * 1000) return subsCache.count;
  let count = null;
  try {
    const res = await whop(env, "/memberships?valid=true&per=1");
    if (res.ok) {
      const body = await res.json();
      const total = body && body.pagination
        && (body.pagination.total_count ?? body.pagination.total);
      if (Number.isFinite(Number(total))) count = Number(total);
      else if (Array.isArray(body && body.data)) count = body.data.length;
    }
  } catch {
    count = null;
  }
  subsCache = { at: Date.now(), count };
  return count;
}

// ------------------------------------------------------------- view: week

async function weekView(env, season, week) {
  // Four queries, whatever the size of the slate.
  //   games    one row per game this week            (~16)
  //   picks    one row per shared pick this week     (pickers x 16)
  //   career   one row per picker who has ever won   (tens)
  //   known    one row                               (1)
  const games = await env.PICKS_DB.prepare(
    "SELECT game_id, home, away, kickoff, home_score, away_score"
    + " FROM results WHERE season = ? AND week = ? ORDER BY kickoff",
  ).bind(season, week).all();

  const picks = await env.PICKS_DB.prepare(
    "SELECT p.game_id, p.picker, p.kind, p.side, p.line, p.price, p.model_side,"
    + " p.received_at, p.result,"
    + " COALESCE(k.name, 'Picker #' || substr(p.picker, 1, 4)) AS name"
    + " FROM picks p LEFT JOIN pickers k ON k.picker = p.picker"
    + " WHERE p.season = ? AND p.week = ? ORDER BY p.received_at DESC",
  ).bind(season, week).all();

  const { proven } = await provenPickers(env);
  const known = await env.PICKS_DB.prepare(
    "SELECT COUNT(*) AS n FROM pickers").all();

  const byGame = new Map();
  const sharers = new Set();
  for (const p of picks.results || []) {
    if (!byGame.has(p.game_id)) byGame.set(p.game_id, []);
    byGame.get(p.game_id).push(p);
    sharers.add(p.picker);
  }
  const subs = await activeSubscriptions(env);
  const knownCount = ((known.results || [])[0] || {}).n || 0;

  const rows = (games.results || []).map((g) => {
    const mine = byGame.get(g.game_id) || [];
    const split = splitFor(mine, proven);
    const kicked = g.kickoff ? Date.parse(g.kickoff) : NaN;

    // Never aggregated away. Two picks reads "2 picks", and the share is only
    // spelled out beside the count it came from -- a percentage standing on
    // its own is the whole way a thin sample starts looking like a signal.
    const sideRows = split.sides.length
      ? split.sides.map((s) => `<div class="split">
          <span class="side">${escapeHtml(s.side)}</span>
          <span class="bar"><i style="width:${
            split.total ? Math.round((s.count / split.total) * 100) : 0}%"></i></span>
          <span class="count">${s.count} of ${split.total}
            <span class="dim">· ${pct(s.count / split.total, 0)}</span></span>
        </div>`).join("")
      : '<div class="dim">no picks</div>';

    // Blank, not zero and not a percentage: with fewer than three proven
    // pickers on a game there is no weighted opinion to report, and a "0%"
    // would read as one.
    const provenCell = split.proven_picks >= 3 && split.proven_side
      ? `<div class="split"><span class="side">${escapeHtml(split.proven_side)}</span>
         <span class="count">${
           mine.filter((p) => proven.has(p.picker)
             && p.side === split.proven_side
             && (p.kind === "winner" || p.kind === "survivor")).length
         } of ${split.proven_picks}</span></div>`
      : `<div class="dim">—</div>`;

    const agree = split.model_side && split.side
      ? (split.model_side === split.side
        ? '<span class="agree win">agrees</span>'
        : '<span class="agree loss">disagrees</span>')
      : "";

    // The line the pickers got against the last one anybody saw. The server
    // has no odds feed of its own, so "last seen" is the most recent line to
    // arrive on a shared pick -- which is what makes being early visible.
    const lines = mine.filter((p) => p.line !== null && p.line !== undefined);
    const latest = lines.length ? lines[0].line : null;

    const list = mine.length ? `<details class="picks"><summary>${
      mine.length} pick${mine.length === 1 ? "" : "s"}, newest first</summary>
      <table><thead><tr><th>Picker</th><th>Side</th><th class="n">Line</th>
        <th class="n">Price</th><th>When</th></tr></thead><tbody>${
        mine.map((p) => {
          // Marked, never quietly counted: a pick that arrived after kickoff
          // is not a prediction, and on this page it sits beside ones that are.
          const late = Number.isFinite(kicked)
            && Date.parse(p.received_at || "") > kicked;
          return `<tr><td>${escapeHtml(p.name)}</td>
            <td class="side">${escapeHtml(p.side)}${
              late ? ' <span class="late">late</span>' : ""}</td>
            <td class="n">${signed(p.line)}</td>
            <td class="n dim">${p.price === null || p.price === undefined
              ? "—" : escapeHtml(String(p.price))}</td>
            <td class="dim">${escapeHtml(ago(p.received_at))}</td></tr>`;
        }).join("")}</tbody></table></details>` : "";

    return `<div class="game">
      <div class="grow">
        <div>
          <div class="match">${escapeHtml(g.away)} at ${escapeHtml(g.home)}</div>
          <div class="kick" data-kick="${escapeHtml(g.kickoff || "")}">${
            escapeHtml(String(g.kickoff || "").replace("T", " ").slice(0, 16))}</div>
        </div>
        <div><div class="col-label">Split</div>${sideRows}</div>
        <div><div class="col-label">Proven pickers (n=${split.proven_picks})</div>${provenCell}</div>
        <div><div class="col-label">Model</div>
          <div>${split.model_side
            ? `<span class="side">${escapeHtml(split.model_side)}</span> ${agree}`
            : '<span class="dim">—</span>'}</div></div>
        <div><div class="col-label">Line avg / last</div>
          <div class="count">${signed(split.avg_line)}
            <span class="dim">/ ${signed(latest)}</span></div></div>
      </div>
      ${list}
    </div>`;
  }).join("");

  const body = `<div class="panel">
    <header><h2>Week ${week} · ${season}</h2>
      <span class="hint">who the crowd is on, before kickoff · a split is only
        ever shown beside the count it came from</span></header>
    <div class="tiles">
      <div class="tile"><div class="label">Shared this week</div>
        <div class="value">${sharers.size}<span class="sub"> picker${
          sharers.size === 1 ? "" : "s"}</span></div></div>
      <div class="tile"><div class="label">Have ever shared</div>
        <div class="value">${knownCount}</div></div>
      <div class="tile"><div class="label">Active subscriptions</div>
        <div class="value">${subs === null
          ? '<span class="sub">—</span>' : subs}</div></div>
      <div class="tile"><div class="label">Picks this week</div>
        <div class="value">${(picks.results || []).length}</div></div>
    </div>
    ${rows || '<div class="empty">No games on file for this week yet. The '
      + 'grader fills these in from the scoreboard on its hourly run.</div>'}
  </div>
  <script>
  // The countdown, and nothing else. Rewritten from the timestamp on each row
  // rather than from the server's clock, so a page left open stays honest.
  (function () {
    function tick() {
      var now = Date.now();
      document.querySelectorAll("[data-kick]").forEach(function (el) {
        var at = Date.parse(el.getAttribute("data-kick"));
        if (!at) return;
        var left = at - now;
        if (left <= 0) { el.textContent = "under way or done"; return; }
        var m = Math.floor(left / 60000), h = Math.floor(m / 60), d = Math.floor(h / 24);
        el.textContent = d > 0 ? "in " + d + "d " + (h % 24) + "h"
          : h > 0 ? "in " + h + "h " + (m % 60) + "m" : "in " + m + "m";
      });
    }
    tick(); setInterval(tick, 30000);
  })();
  </script>`;
  return body;
}

// -------------------------------------------------------- view: leaderboard

// One row per picker, as before. Pushes and late picks are in neither column,
// so a run of ties does not flatter anybody's percentage.
export async function leaderboard(env, season) {
  const rows = await env.PICKS_DB.prepare(
    "SELECT p.picker, COALESCE(k.name, 'Picker #' || substr(p.picker, 1, 4)) AS name,"
    + " SUM(p.result = 'win') AS wins, SUM(p.result = 'loss') AS losses,"
    + " SUM(p.result = 'push') AS pushes, SUM(p.result IS NULL) AS pending,"
    + " SUM(p.result = 'late') AS late, COUNT(*) AS total,"
    + " MAX(p.received_at) AS last_at"
    + " FROM picks p LEFT JOIN pickers k ON k.picker = p.picker"
    + " WHERE p.season = ? GROUP BY p.picker",
  ).bind(season).all();
  return (rows.results || []).map((r) => {
    const decided = (r.wins || 0) + (r.losses || 0);
    return { ...r, decided, rate: decided ? (r.wins || 0) / decided : null };
  }).sort((a, b) => (b.rate ?? -1) - (a.rate ?? -1) || b.decided - a.decided);
}

// The crowd's own record, and the model's, from the frozen splits.
//
// Both are read from consensus rather than recomputed from picks, which is the
// whole point of freezing it: a split recomputed today is not what anybody
// could have acted on before kickoff. The crowd's verdict is the stored one;
// the proven-only and model records are derived here, because they are the
// same rows measured a different way rather than a second thing to keep
// up to date. Reads one row per captured game this season (~270 at most).
function crowdRecords(rows) {
  const blank = () => ({ wins: 0, losses: 0, pushes: 0 });
  const crowd = blank();
  const provenOnly = blank();
  const model = blank();
  const byTeam = new Map();
  const team = (name) => {
    if (!byTeam.has(name)) {
      byTeam.set(name, { team: name, model_wins: 0, model_losses: 0 });
    }
    return byTeam.get(name);
  };

  // Two phases, two different measurements, never mixed. The record comes off
  // the prekick rows -- what the crowd went into the game with -- and the
  // closing-line figures off the first rows, which hold the early number. A
  // query that forgot to separate them would count every game twice.
  const clv = { sum: 0, n: 0 };
  const provenClv = { sum: 0, n: 0 };

  for (const r of rows) {
    if (r.phase === "first") {
      // Blank, not zero, when no closing line is known: an average that quietly
      // counts unknowns as par is an average that flatters the crowd.
      if (r.clv !== null && r.clv !== undefined) {
        clv.sum += Number(r.clv);
        clv.n += 1;
      }
      if (r.proven_clv !== null && r.proven_clv !== undefined) {
        provenClv.sum += Number(r.proven_clv);
        provenClv.n += 1;
      }
      continue;
    }
    if (r.home_score === null || r.home_score === undefined) continue;
    const tie = r.home_score === r.away_score;
    const winner = tie ? null : (r.home_score > r.away_score ? r.home : r.away);
    const tally = (bucket, side) => {
      if (!side) return;
      if (tie) bucket.pushes += 1;
      else if (side === winner) bucket.wins += 1;
      else bucket.losses += 1;
    };
    // The stored verdict for the crowd, so the page shows what grading wrote.
    if (r.result === "win") crowd.wins += 1;
    else if (r.result === "loss") crowd.losses += 1;
    else if (r.result === "push") crowd.pushes += 1;
    tally(provenOnly, r.proven_side);
    tally(model, r.model_side);
    if (r.model_side && !tie) {
      const t = team(r.model_side);
      if (r.model_side === winner) t.model_wins += 1;
      else t.model_losses += 1;
    }
  }
  crowd.clv = clv.n ? clv.sum / clv.n : null;
  crowd.clv_n = clv.n;
  provenOnly.clv = provenClv.n ? provenClv.sum / provenClv.n : null;
  provenOnly.clv_n = provenClv.n;
  return { crowd, provenOnly, model, byTeam };
}

const recordCell = (r) => {
  const decided = r.wins + r.losses;
  return `<td class="n">${r.wins}-${r.losses}${r.pushes ? `-${r.pushes}` : ""}</td>
    <td class="n">${decided ? pct(r.wins / decided) : "—"}</td>`;
};

async function boardView(env, season, query) {
  // Three queries.
  //   board     one row per picker this season          (tens)
  //   frozen    one row per captured game this season   (~270 at most)
  //   byTeam    one row per team the crowd has picked   (<= 32)
  const board = await leaderboard(env, season);
  // Said out loud rather than swallowed: until the consensus table has been
  // migrated by hand (see the README) this query names columns it does not
  // have, and a dashboard that 500s on its own leaderboard is a worse answer
  // than one that says which statement has not been run yet.
  let frozen = { results: [] };
  let migrationNeeded = false;
  try {
    frozen = await env.PICKS_DB.prepare(
      "SELECT c.game_id, c.phase, c.side, c.proven_side, c.model_side,"
      + " c.result, c.clv, c.proven_clv,"
      + " r.home, r.away, r.home_score, r.away_score"
      + " FROM consensus c LEFT JOIN results r ON r.game_id = c.game_id"
      + " WHERE c.season = ?",
    ).bind(season).all();
  } catch (err) {
    migrationNeeded = true;
    console.log("picks: the leaderboard could not read consensus",
                String(err && err.message || err));
  }
  const crowdTeams = await env.PICKS_DB.prepare(
    "SELECT side AS team, COUNT(*) AS picked, SUM(result = 'win') AS wins,"
    + " SUM(result = 'loss') AS losses"
    + " FROM picks WHERE season = ? AND kind IN ('winner','survivor')"
    + " GROUP BY side",
  ).bind(season).all();

  const { crowd, provenOnly, byTeam } = crowdRecords(frozen.results || []);

  const rows = board.map((r, i) => `<tr>
    <td class="n dim">${i + 1}</td>
    <td><a href="?${query}&picker=${encodeURIComponent(r.picker)}">${
      escapeHtml(r.name)}</a><span class="mono under">${escapeHtml(r.picker)}</span></td>
    ${recordCell({ wins: r.wins || 0, losses: r.losses || 0, pushes: r.pushes || 0 })}
    <td class="n dim">—</td>
    <td class="n dim">${r.pending || 0}</td>
    <td class="n dim">${r.late || 0}</td>
    <td class="dim">${escapeHtml(String(r.last_at || "").slice(0, 10))}</td>
  </tr>`).join("");

  // The crowd sits in the same table as the people in it, because the only
  // interesting thing about its record is what it beat.
  // What the early number was worth against the close, averaged. Blank rather
  // than zero when nothing has a closing line to measure against.
  const clvCell = (r) => (r.clv === null
    ? '<td class="n dim">—</td>'
    : `<td class="n ${r.clv > 0 ? "win" : r.clv < 0 ? "loss" : ""}">${
      r.clv > 0 ? "+" : ""}${r.clv.toFixed(2)}<span class="mono"> of ${
      r.clv_n}</span></td>`);

  const crowdRows = `<tr class="dim">
      <td class="n">—</td><td><b>The crowd</b><span class="mono under">the frozen
        pre-kickoff side on every captured game, and the first number it had
        against the close</span></td>
      ${recordCell(crowd)}${clvCell(crowd)}<td class="n">—</td><td class="n">—</td><td>—</td></tr>
    <tr class="dim">
      <td class="n">—</td><td><b>Proven pickers only</b><span class="mono under">the
        same games, counting only pickers past ${PROVEN_MIN_PICKS} graded
        picks and above break-even</span></td>
      ${recordCell(provenOnly)}${clvCell(provenOnly)}<td class="n">—</td><td class="n">—</td><td>—</td></tr>`;

  const teams = (crowdTeams.results || []).map((t) => {
    const model = byTeam.get(t.team) || { model_wins: 0, model_losses: 0 };
    const decided = (t.wins || 0) + (t.losses || 0);
    const modelDecided = model.model_wins + model.model_losses;
    return { team: t.team, picked: t.picked || 0, wins: t.wins || 0,
             losses: t.losses || 0, decided,
             rate: decided ? (t.wins || 0) / decided : null,
             model_wins: model.model_wins, model_losses: model.model_losses,
             model_rate: modelDecided ? model.model_wins / modelDecided : null };
  }).sort((a, b) => b.picked - a.picked);

  const teamRows = teams.map((t) => `<tr>
    <td>${escapeHtml(t.team)}</td>
    <td class="n">${t.picked}</td>
    <td class="n">${t.wins}-${t.losses}</td>
    <td class="n">${t.rate === null ? "—" : pct(t.rate)}</td>
    <td class="n dim">${t.model_wins}-${t.model_losses}</td>
    <td class="n dim">${t.model_rate === null ? "—" : pct(t.model_rate)}</td>
  </tr>`).join("");

  return `<div class="panel">
    <header><h2>Pickers · ${season}</h2>
      <span class="hint">straight-up winners · pushes and picks that arrived
        after kickoff are in neither column</span>
      ${migrationNeeded ? '<span class="hint push late">the consensus table '
        + 'still needs its migration — see the README; no crowd rows until '
        + 'then</span>' : ""}</header>
    ${// The crowd's record comes from the frozen splits, not from this board,
      // so it shows even in a week when nobody's own picks have been graded --
      // which is exactly the week it is most interesting.
      board.length || crowd.wins + crowd.losses + crowd.pushes
      ? `<table><thead><tr><th class="n">#</th><th>Picker</th>
      <th class="n">Record</th><th class="n">Rate</th>
      <th class="n" title="Average closing-line value: the first number the crowd had on a game against the last line seen before kickoff, in points, positive when it beat the close. Only the crowd rows carry one — an individual picker's number is on their own page.">CLV</th>
      <th class="n">Open</th><th class="n">Late</th><th>Last seen</th>
      </tr></thead>
      <tbody>${rows}${crowdRows}</tbody></table>`
    : '<div class="empty">No shared picks yet this season.</div>'}
  </div>

  <div class="panel">
    <header><h2>By team</h2>
      <span class="hint">how often the crowd took each team and how often that
        was right, against the model on the same games · click a column to sort
        by it</span></header>
    ${teams.length ? `<table class="sortable"><thead><tr>
      <th data-sort="0">Team</th><th class="n" data-sort="1">Picked</th>
      <th class="n" data-sort="2">Crowd</th><th class="n" data-sort="3">Rate</th>
      <th class="n" data-sort="4">Model</th><th class="n" data-sort="5">Rate</th>
      </tr></thead><tbody>${teamRows}</tbody></table>`
    : '<div class="empty">Nothing picked yet this season.</div>'}
  </div>
  ${SORT_SCRIPT}`;
}

// Column sorting, for the two tables that have enough rows to want it.
const SORT_SCRIPT = `<script>
document.querySelectorAll("table.sortable").forEach(function (table) {
  var dir = {};
  table.querySelectorAll("th[data-sort]").forEach(function (th, i) {
    th.addEventListener("click", function () {
      var rows = Array.prototype.slice.call(table.tBodies[0].rows);
      dir[i] = !dir[i];
      rows.sort(function (a, b) {
        var x = a.cells[i].textContent.trim(), y = b.cells[i].textContent.trim();
        var nx = parseFloat(x), ny = parseFloat(y);
        var same = !isNaN(nx) && !isNaN(ny) ? nx - ny : x.localeCompare(y);
        return dir[i] ? same : -same;
      });
      rows.forEach(function (r) { table.tBodies[0].appendChild(r); });
    });
  });
});
</script>`;

// ------------------------------------------------------------ view: picker

async function pickerView(env, picker, query) {
  // Three queries.
  //   who     one row                                       (1)
  //   picks   one row per pick this picker has ever shared  (hundreds)
  //   lines   one row per game anybody has picked, per season, for the last
  //           line seen                                     (~270 per season)
  const who = await env.PICKS_DB.prepare(
    "SELECT picker, name, first_at, last_at FROM pickers WHERE picker = ?",
  ).bind(picker).all();
  const profile = (who.results || [])[0];

  const rows = await env.PICKS_DB.prepare(
    "SELECT p.season, p.week, p.game_id, p.kind, p.side, p.line, p.price,"
    + " p.received_at, p.result, r.home, r.away, r.home_score, r.away_score,"
    + " r.kickoff FROM picks p LEFT JOIN results r ON r.game_id = p.game_id"
    + " WHERE p.picker = ? ORDER BY p.season DESC, p.week DESC, r.kickoff",
  ).bind(picker).all();
  const picks = rows.results || [];

  // An unknown id is a 404, not an empty page: a page that renders for any
  // sixteen characters is a page that tells you which ids exist.
  if (!profile && !picks.length) return null;

  const lines = await env.PICKS_DB.prepare(
    "SELECT p.game_id, p.line FROM picks p JOIN (SELECT game_id,"
    + " MAX(received_at) AS m FROM picks GROUP BY game_id) x"
    + " ON x.game_id = p.game_id AND x.m = p.received_at",
  ).all();
  const lastLine = new Map(
    (lines.results || []).map((r) => [r.game_id, r.line]));

  const name = (profile && profile.name)
    || `Picker #${picker.slice(0, 4)}`;

  const blank = () => ({ wins: 0, losses: 0, pushes: 0, pending: 0, late: 0 });
  const overall = blank();
  const weeks = new Map();
  const teams = new Map();
  let noResult = 0;

  for (const p of picks) {
    const key = `${p.season}-${p.week}`;
    if (!weeks.has(key)) {
      weeks.set(key, { season: p.season, week: p.week, ...blank(), games: [] });
    }
    const w = weeks.get(key);
    // Late is counted on its own and stays outside the percentage: it is not a
    // wrong prediction, it is not a prediction.
    const bucket = p.result === "win" ? "wins" : p.result === "loss" ? "losses"
      : p.result === "push" ? "pushes" : p.result === "late" ? "late" : "pending";
    overall[bucket] += 1;
    w[bucket] += 1;
    w.games.push(p);

    if (p.home_score === null || p.home_score === undefined) {
      noResult += 1;
    } else if (p.result === "win" || p.result === "loss" || p.result === "push") {
      // Their side, and the side they were against. The opponent comes off the
      // result row rather than the pick, which only ever names one team.
      const against = p.side === p.home ? p.away : p.home;
      for (const [team, column] of [[p.side, "for"], [against, "against"]]) {
        if (!team) continue;
        if (!teams.has(team)) {
          teams.set(team, { team, picked: 0, for: blank(), against: blank() });
        }
        const t = teams.get(team);
        if (column === "for") t.picked += 1;
        t[column][p.result === "win" ? "wins"
          : p.result === "loss" ? "losses" : "pushes"] += 1;
      }
    }
  }

  const decided = overall.wins + overall.losses;
  // Sorted here rather than leaned on from the query's ORDER BY. The grouping
  // above preserves whatever order the rows arrived in, and "newest first" is
  // a promise the page makes -- it should not depend on the planner keeping
  // one it never made.
  const weekList = [...weeks.values()]
    .sort((a, b) => b.season - a.season || b.week - a.week);
  const best = Math.max(1, ...weekList.map((w) => {
    const d = w.wins + w.losses;
    return d ? w.wins / d : 0;
  }));

  const weekRows = weekList.map((w) => {
    const d = w.wins + w.losses;
    const rate = d ? w.wins / d : null;
    const games = w.games.map((g) => {
      const verdict = g.result === "win" ? '<span class="win">win</span>'
        : g.result === "loss" ? '<span class="loss">loss</span>'
          : g.result === "push" ? "push"
            : g.result === "late" ? '<span class="late">late</span>'
              // No result row yet is pending, said out loud rather than left
              // blank -- a blank cell reads as a bug.
              : '<span class="dim">pending</span>';
      const score = (g.home_score === null || g.home_score === undefined)
        ? '<span class="dim">pending</span>'
        : `${escapeHtml(g.away)} ${g.away_score} – ${escapeHtml(g.home)} ${g.home_score}`;
      return `<tr>
        <td class="side">${escapeHtml(g.side)}</td>
        <td class="dim">${escapeHtml(g.kind)}</td>
        <td class="n">${signed(g.line)}</td>
        <td class="n dim">${g.price === null || g.price === undefined
          ? "—" : escapeHtml(String(g.price))}</td>
        <td class="n dim">${signed(lastLine.get(g.game_id))}</td>
        <td>${score}</td>
        <td>${verdict}</td></tr>`;
    }).join("");

    return `<tr><td>Week ${w.week}</td>
      <td class="n">${w.wins}-${w.losses}${w.pushes ? `-${w.pushes}` : ""}</td>
      <td class="n">${rate === null ? "—" : pct(rate)}</td>
      <td class="n dim">${w.pending}</td>
      <td class="n dim">${w.late}</td>
      <td><span class="spark"><i style="height:${
        rate === null ? 1 : Math.round((rate / best) * 16)}px"></i></span></td>
    </tr>
    <tr><td colspan="6" style="padding:0">
      <details class="picks"><summary>${w.games.length} game${
        w.games.length === 1 ? "" : "s"}</summary>
      <table><thead><tr><th>Side</th><th>Kind</th><th class="n">Line</th>
        <th class="n">Price</th><th class="n">Last seen</th><th>Final</th>
        <th>Result</th></tr></thead><tbody>${games}</tbody></table>
      </details></td></tr>`;
  }).join("");

  const teamRows = [...teams.values()]
    .sort((a, b) => b.picked - a.picked)
    .map((t) => {
      const f = t.for;
      const a = t.against;
      const fd = f.wins + f.losses;
      const ad = a.wins + a.losses;
      return `<tr>
        <td>${escapeHtml(t.team)}</td>
        <td class="n">${t.picked}</td>
        <td class="n">${f.wins}-${f.losses}${f.pushes ? `-${f.pushes}` : ""}</td>
        <td class="n">${fd ? pct(f.wins / fd) : "—"}</td>
        <td class="n dim">${a.wins}-${a.losses}${a.pushes ? `-${a.pushes}` : ""}</td>
        <td class="n dim">${ad ? pct(a.wins / ad) : "—"}</td>
      </tr>`;
    }).join("");

  return `<div class="panel">
    <header><h2>${escapeHtml(name)}</h2>
      <span class="hint mono">${escapeHtml(picker)}</span>
      <span class="hint push"><a href="?${query}&view=board">back to the
        leaderboard</a></span></header>
    <div class="tiles">
      <div class="tile"><div class="label">Record</div>
        <div class="value">${overall.wins}-${overall.losses}${
          overall.pushes ? `-${overall.pushes}` : ""}</div></div>
      <div class="tile"><div class="label">Win rate</div>
        <div class="value">${decided ? pct(overall.wins / decided) : "—"}
          <span class="sub">of ${decided}</span></div></div>
      <div class="tile"><div class="label">Pending</div>
        <div class="value">${overall.pending}</div></div>
      <div class="tile"><div class="label">Late</div>
        <div class="value">${overall.late}</div></div>
      <div class="tile"><div class="label">First seen</div>
        <div class="value"><span class="sub">${escapeHtml(
          String((profile && profile.first_at) || "").slice(0, 10) || "—")}</span></div></div>
      <div class="tile"><div class="label">Last seen</div>
        <div class="value"><span class="sub">${escapeHtml(
          String((profile && profile.last_at) || "").slice(0, 10) || "—")}</span></div></div>
    </div>
  </div>

  <div class="panel">
    <header><h2>Week by week</h2>
      <span class="hint">newest first · correct out of decided, with late picks
        counted apart and outside the percentage</span></header>
    ${weekList.length ? `<table><thead><tr><th>Week</th><th class="n">Record</th>
      <th class="n">Rate</th><th class="n">Open</th><th class="n">Late</th>
      <th>Form</th></tr></thead><tbody>${weekRows}</tbody></table>`
    : '<div class="empty">Nothing shared yet.</div>'}
  </div>

  <div class="panel">
    <header><h2>By team</h2>
      <span class="hint">every team they have picked, and how they did when
        they picked against it · click a column to sort by it</span>
      ${noResult ? `<span class="hint push">${noResult} pick${
        noResult === 1 ? "" : "s"} left out — no result yet</span>` : ""}
    </header>
    ${teamRows ? `<table class="sortable"><thead><tr>
      <th data-sort="0">Team</th><th class="n" data-sort="1">Picked</th>
      <th class="n" data-sort="2">With</th><th class="n" data-sort="3">Rate</th>
      <th class="n" data-sort="4">Against</th><th class="n" data-sort="5">Rate</th>
      </tr></thead><tbody>${teamRows}</tbody></table>`
    : '<div class="empty">No graded picks yet.</div>'}
  </div>
  ${SORT_SCRIPT}`;
}

// ------------------------------------------------------------- the route

async function dashboardRoute(url, env) {
  // One secret, compared in full, before anything else happens. A miss is a
  // 404 rather than a 403 so the page does not announce that it is there.
  const token = url.searchParams.get("token") || "";
  if (!env.DASHBOARD_TOKEN || token !== env.DASHBOARD_TOKEN) return notFound();
  if (!env.PICKS_DB) return new Response("No picks database bound.", { status: 503 });

  const query = `token=${encodeURIComponent(token)}`;
  const now = new Date();
  const current = nflWeek(now);
  const season = Number(url.searchParams.get("season")) || current.season;

  const picker = url.searchParams.get("picker");
  if (picker !== null) {
    // Sixteen hex characters or nothing. Anything else is a 404 rather than a
    // query, so a malformed id cannot become a database round trip.
    if (!/^[0-9a-f]{16}$/.test(picker)) return notFound();
    const body = await pickerView(env, picker, query);
    if (!body) return notFound();
    return page(shell("Picker", "picker", query, body));
  }

  if (url.searchParams.get("view") === "board") {
    return page(shell(`Pickers · ${season}`, "board", query,
                      await boardView(env, season, query)));
  }

  const week = Number(url.searchParams.get("week")) || current.week;
  return page(shell(`This week · ${season}`, "week", query,
                    await weekView(env, season, week)));
}

// ------------------------------------------------------------------ validate

export async function validate(body, env) {
  const key = String(body.license_key || "").trim();
  const machine = String(body.machine_id || "").trim().slice(0, 64);
  if (!key) return deny("missing_key", "Enter the license key from your Whop purchase.");
  if (!/^[A-Za-z0-9_-]{4,100}$/.test(key)) {
    return deny("not_found", "That doesn't look like a license key. Copy it from your Whop purchase.");
  }

  const res = await whop(env, `/memberships/${encodeURIComponent(key)}`);
  if (res.status === 404) {
    return deny("not_found", "That license key wasn't found. Check it matches the one on Whop.");
  }
  if (!res.ok) {
    // Our problem, not the customer's: the app treats this like being offline
    // and keeps working on its last good check.
    return { valid: null, reason: "upstream", message: `Whop answered ${res.status}.` };
  }
  const m = await res.json();

  if (env.WHOP_PRODUCT_ID && m.product && m.product.id && m.product.id !== env.WHOP_PRODUCT_ID) {
    return deny("wrong_product", "That key is for a different product.");
  }

  // `completed` is an entitlement, not an ending: Whop reports a one-time
  // purchase that way once it is paid, which is what the season pass is. A
  // subscription that genuinely lapsed comes back as canceled or expired.
  const allowed = String(env.ALLOWED_STATUSES || "active,trialing,canceling,completed")
    .split(",").map((s) => s.trim()).filter(Boolean);
  if (!allowed.includes(m.status)) {
    return deny("inactive", statusMessage(m.status), m.status);
  }

  // Remember which computers this key runs on. The first MAX_MACHINES are
  // accepted; after that the customer is told to free one up.
  if (machine) {
    const max = Math.max(1, Number(env.MAX_MACHINES || 2));
    const meta = m.metadata && typeof m.metadata === "object" ? { ...m.metadata } : {};
    const machines = String(meta.edge_machines || "").split(",").filter(Boolean);
    if (!machines.includes(machine)) {
      if (machines.length >= max) {
        return deny("too_many_machines",
          `This key is already in use on ${max} computer${max > 1 ? "s" : ""}. ` +
          "Contact support to move it to this one.", m.status);
      }
      machines.push(machine);
      meta.edge_machines = machines.join(",");
      const upd = await whop(env, `/memberships/${encodeURIComponent(m.id)}`, {
        method: "PATCH", body: JSON.stringify({ metadata: meta }),
      });
      if (!upd.ok) {
        return { valid: null, reason: "upstream", message: `Could not register this computer (${upd.status}).` };
      }
    }
  }

  return {
    valid: true,
    status: m.status,
    reason: "ok",
    message: m.status === "canceling"
      ? "Your subscription is set to end at the close of this billing period."
      : "",
    // Never the email or anything else personal: the app only needs a yes.
    renews_at: m.renewal_period_end || null,
  };
}

function deny(reason, message, status = null) {
  return { valid: false, reason, message, status };
}

function statusMessage(status) {
  switch (status) {
    case "past_due": return "Your last payment didn't go through. Update your card on Whop to keep using The Edge.";
    case "canceled":
    case "expired": return "This subscription has ended. Resubscribe on Whop to keep using The Edge.";
    default: return `This subscription isn't active (${status}).`;
  }
}

function whop(env, path, init = {}) {
  return fetch(WHOP + path, {
    ...init,
    headers: {
      Authorization: `Bearer ${env.WHOP_API_KEY}`,
      "Content-Type": "application/json",
      Accept: "application/json",
    },
  });
}

// ------------------------------------------------------------ latest version

async function releaseInfo(env) {
  if (!env.GITHUB_REPO) return null;
  const tag = env.RELEASE_TAG || "latest";
  const res = await fetch(
    `https://api.github.com/repos/${env.GITHUB_REPO}/releases/tags/${tag}`,
    { headers: gh(env), cf: { cacheTtl: 300 } });
  if (!res.ok) return null;
  return res.json();
}

export async function latest(env, url) {
  const release = await releaseInfo(env);
  if (!release) return { commit: null, built_at: null, notes: "", download_url: env.DOWNLOAD_PAGE || null };
  // The build workflow uploads version.json beside the installers; it names
  // the exact commit and build time the installers were made from.
  let version = {};
  const asset = (release.assets || []).find((a) => a.name === "version.json");
  if (asset) {
    const res = await fetch(asset.url, { headers: { ...gh(env), Accept: "application/octet-stream" } });
    if (res.ok) version = await res.json().catch(() => ({}));
  }
  return {
    commit: version.commit || null,
    built_at: version.built_at || null,
    notes: version.notes || "",
    // The app appends ?key=... and the file name for its platform.
    download_url: `${url.origin}/v1/download`,
    download_page: env.DOWNLOAD_PAGE || null,
  };
}

async function download(url, env) {
  const key = url.searchParams.get("key") || "";
  const check = await validate({ license_key: key }, env);
  if (check.valid !== true) {
    return new Response(check.message || "License not valid.", { status: 403 });
  }
  const want = url.searchParams.get("asset") || "TheEdge-windows-setup.exe";
  const release = await releaseInfo(env);
  const asset = release && (release.assets || []).find((a) => a.name === want);
  if (!asset) {
    if (env.DOWNLOAD_PAGE) return Response.redirect(env.DOWNLOAD_PAGE, 302);
    return new Response("No download available.", { status: 404 });
  }
  // GitHub answers an asset request with a short-lived signed link; hand that
  // to the customer so the file never passes through the Worker.
  const res = await fetch(asset.url, {
    headers: { ...gh(env), Accept: "application/octet-stream" }, redirect: "manual",
  });
  const loc = res.headers.get("Location");
  if (loc) return Response.redirect(loc, 302);
  return new Response(res.body, {
    status: res.status,
    headers: { "Content-Disposition": `attachment; filename="${want}"` },
  });
}

function gh(env) {
  const h = { "User-Agent": "the-edge-license-server", Accept: "application/vnd.github+json" };
  if (env.GITHUB_TOKEN) h.Authorization = `Bearer ${env.GITHUB_TOKEN}`;
  return h;
}

// ------------------------------------------------------------------- helpers

function json(data, status = 200) {
  return cors(new Response(JSON.stringify(data), {
    status, headers: { "Content-Type": "application/json" },
  }));
}

function cors(res) {
  res.headers.set("Access-Control-Allow-Origin", "*");
  res.headers.set("Access-Control-Allow-Headers", "Content-Type");
  return res;
}
