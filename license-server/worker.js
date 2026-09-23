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

      // Which number is in force, every run, so `wrangler tail` answers it
      // without anyone opening the dashboard to guess. At one it is said
      // twice, and loudly: that is not a quorum, it is a single machine's
      // word, and it should never be the thing nobody noticed.
      const quorum = resultsQuorum(env);
      console.log("results: quorum in effect", JSON.stringify(
        { quorum, configured: Boolean(env.RESULTS_QUORUM) }));
      if (quorum === 1) {
        console.log("results: QUORUM IS 1 -- a reported score is taken from a "
          + "single reporter, with no second opinion. Raise RESULTS_QUORUM "
          + "once more than one person is sharing.");
      }

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

      for (const { season: s, week: w } of await weeksToGrade(env, season, week)) {
        try {
          const out = await gradeWeek(env, s, w);
          if (!out.ok) {
            // Said its piece already, with the status and the body. The week
            // keeps its ungraded picks, so the next run comes back to it.
            console.log("picks: week not graded, will retry", JSON.stringify(out));
            continue;
          }
          console.log("picks: graded", JSON.stringify(out));
          // The crowd's own record, which is a separate thing from any
          // individual pick and does not change how one is graded.
          const crowd = await gradeCrowd(env, s, w);
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
      if (url.pathname === "/v1/results" && request.method === "POST") {
        return await resultsRoute(request, env);
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
      if (url.pathname === "/v1/health" && request.method === "GET") {
        return await healthRoute(url, env);
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

// ------------------------------------------------ results, reported by the app
//
// ESPN answers the desktop app from a customer's home connection and refuses
// this Worker from a datacentre. The app has the scores already, so it sends
// them -- but a subscriber is not a source of truth, and a leaderboard graded
// on one customer's word is a leaderboard that customer can write.
//
// So a report is not a result. Reports are stored one per picker per game, and
// a score is only promoted to the results table when RESULTS_QUORUM pickers
// independently report *the same* score and kickoff. Games are objective and
// public, so honest clients agree exactly and a liar has to find two other
// live subscriptions willing to send the same wrong number for the same game.
//
// It degrades rather than fails: a game only one person watched stays
// ungraded until enough people have seen it, which is the right way round.
// And it never overwrites what the grader got from ESPN itself.
export const RESULTS_QUORUM = 3;

// ...and the number actually in force, which is a dashboard variable because
// three is right for a customer base and impossible for one.
//
// A fallback that cannot fire is not a fallback: with a single person sharing,
// three independent reports never arrive and the season stays ungraded no
// matter how well the rest of this works. Set RESULTS_QUORUM in the Cloudflare
// dashboard to lower it, and raise it again as people join.
//
// Anything that is not a positive whole number is ignored rather than
// interpreted. "0", "", "two" and "1.5" all mean the default: a typo in a
// dashboard field must not silently switch off the agreement rule, which is
// the only thing standing between the leaderboard and one client's word.
export function resultsQuorum(env = {}) {
  const raw = env && env.RESULTS_QUORUM;
  if (raw === null || raw === undefined) return RESULTS_QUORUM;
  const text = String(raw).trim();
  if (!/^\d+$/.test(text)) return RESULTS_QUORUM;
  const n = Number(text);
  return Number.isInteger(n) && n >= 1 ? n : RESULTS_QUORUM;
}

export const RESULTS_MAX = 64;                 // a week's slate, with room
export const RESULTS_RATE = 30;                // batches per key per hour

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
async function overLimit(env, picker, prefix, rate) {
  const store = env.PICKS_RL || env.SUPPORT_RL;
  if (!store) return false;
  const bucket = `${prefix}:${picker}:${Math.floor(Date.now() / 3_600_000)}`;
  try {
    const seen = Number(await store.get(bucket)) || 0;
    if (seen >= rate) return true;
    await store.put(bucket, String(seen + 1), { expirationTtl: 7200 });
  } catch (err) {
    // A rate limiter that is down must not take pick sharing down with it.
    console.log("picks: rate limit unavailable", String(err && err.message || err));
    return false;
  }
  return false;
}

// Separate buckets, because reporting scores and sharing picks run on the same
// cadence and one must not be able to starve the other.
const overPicksLimit = (env, picker) => overLimit(env, picker, "pk", PICKS_RATE);
const overResultsLimit = (env, picker) => overLimit(env, picker, "rs", RESULTS_RATE);

async function pickerFor(key) {
  const bytes = new TextEncoder().encode(`${PICK_SALT}:${key}`);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)]
    .map((b) => b.toString(16).padStart(2, "0")).join("").slice(0, PICK_ID_CHARS);
}

// One game, one id, whichever side is speaking.
//
// The app stores ESPN's event id behind an "espn-" prefix that records where
// it came from (nflpicker/sources/espn.py: `f"espn-{event['id']}"`); this
// server stores the bare id (`String(event.id)` in fetchResults). Same number,
// two spellings, and picks therefore joined to nothing at all: five picks in
// the database, sixteen games on file, no overlap, no grading, no splits.
//
// Normalising here rather than changing the app is deliberate. Neither
// spelling is more durable -- both are ESPN's event id, which survives a
// postponement (the date moves, the id does not), so on the stated tie-break
// they are equal. What is not equal is the blast radius: the app's game_id is
// the key its whole local database is built on, and changing it would rekey
// every customer's picks, predictions and odds, while any copy that did not
// update would go on missing for the rest of the season. Doing it at this
// boundary fixes every client at once, including the ones that never update,
// and it is idempotent -- a future client sending the bare id already works.
export function canonicalGameId(raw) {
  const id = String(raw === null || raw === undefined ? "" : raw).trim();
  const m = /^espn-(\d+)$/i.exec(id);
  return m ? m[1] : id;
}

function cleanPick(raw) {
  if (!raw || typeof raw !== "object") return null;
  const kind = String(raw.kind || "").toLowerCase();
  const gameId = canonicalGameId(String(raw.game_id || "").slice(0, 64));
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
    // What the app's own model said about this game, for the dashboard column
    // that puts the crowd beside the model. Normalised like `side`, because it
    // is compared against one.
    model_side: String(raw.model_side || "").toUpperCase().slice(0, 8) || null,
    picked_at: String(raw.picked_at || "").slice(0, 40) || null,
  };
}

// The gate on every write endpoint. /v1/picks shipped without one, on the
// support endpoint's reasoning -- that endpoint takes no key because "my key
// will not activate" is exactly the message that cannot produce one. A write
// is the opposite case: only a paying customer has anything worth storing, and
// an open write endpoint means anybody who finds the URL can fill the tables
// the leaderboard is built from.
//
// One function rather than one per endpoint, so a second way in cannot be a
// weaker way in.
async function licensedPicker(body, env, limiter, needsSub) {
  const picker = String(body.picker || "").toLowerCase();
  if (!/^[0-9a-f]{16}$/.test(picker)) {
    return { error: json({ ok: false, reason: "bad_picker" }, 400) };
  }

  const key = String(body.license_key || "").trim();
  if (!key) {
    return { error: json({ ok: false, reason: "unlicensed", message: needsSub }, 403) };
  }

  // The id has to be the one this key produces. Without this, one valid
  // subscription could write under any id it liked -- somebody else's, or a
  // thousand invented ones -- and the leaderboard would be whatever its
  // busiest customer decided it was. For reported scores it does more: the
  // quorum counts distinct pickers, so an id nobody can mint is the whole
  // reason counting them means anything.
  if (await pickerFor(key) !== picker) {
    return { error: json({ ok: false, reason: "picker_mismatch",
                           message: "That picker id does not belong to that key." },
                         403) };
  }

  // Before the Whop call, so a flood cannot be turned into a flood of those.
  if (await limiter(env, picker)) {
    return { error: json({ ok: false, reason: "rate_limited",
                           message: "Too many batches from this key in the last hour." },
                         429) };
  }

  const cached = cachedLicence(picker);
  if (cached) {
    if (!cached.valid) {
      return { error: json({ ok: false, reason: "unlicensed", message: needsSub }, 403) };
    }
    return { picker, degraded: false };
  }
  // No machine id: this is not an activation, and registering a computer as a
  // side effect of sharing a pick would spend one of the customer's two slots
  // without them doing anything.
  const check = await validate({ license_key: key }, env);
  if (check.valid === true) {
    rememberLicence(picker, true);
    return { picker, degraded: false };
  }
  if (check.valid === null) {
    // Whop is having a bad day. Taking the batch is the right call: the
    // alternative is losing a customer's picks over somebody else's outage,
    // and the id still had to match a well-formed key to get this far. Not
    // cached, so the next batch tries Whop again.
    console.log("picks: accepted without a licence check, Whop unreachable",
                String(check.message || check.reason || ""));
    return { picker, degraded: true };
  }
  rememberLicence(picker, false);
  return { error: json({ ok: false, reason: "unlicensed",
                         message: check.message || needsSub }, 403) };
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

  const gate = await licensedPicker(body, env, overPicksLimit,
                                    "Pick sharing needs a live subscription.");
  if (gate.error) return gate.error;
  const { picker, degraded } = gate;

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
      + " total_line, book_prob, model_side, picked_at, received_at)"
      + " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)"
      + " ON CONFLICT(picker, game_id, kind) DO UPDATE SET"
      + " side = excluded.side, line = excluded.line, price = excluded.price,"
      + " total_line = excluded.total_line, book_prob = excluded.book_prob,"
      + " model_side = excluded.model_side,"
      + " picked_at = excluded.picked_at, received_at = excluded.received_at,"
      // A changed pick is ungraded again: it is a different bet now.
      + " result = NULL, graded_at = NULL"
      // ...but only while the game has not started. Once there is a result on
      // file, the pick that was in before kickoff is the one that counts.
      + " WHERE picks.result IS NULL",
    ).bind(picker, p.game_id, p.kind, p.season, p.week, p.side, p.line, p.price,
           p.total_line, p.book_prob, p.model_side, p.picked_at, now));
  }
  try {
    await env.PICKS_DB.batch(statements);
  } catch (err) {
    console.log("picks: store failed", String(err && err.message || err));
    return json({ ok: false, reason: "store_failed" }, 502);
  }

  await warnUnmatched(env, picks);
  return json(degraded
    ? { ok: true, stored: picks.length, unchecked: true }
    : { ok: true, stored: picks.length });
}

// One reported score, checked into a shape the database can hold.
//
// Everything here is a client's word, so everything is bounded. A score is a
// small non-negative integer or the report is dropped; a game that has not
// finished has no score to report; and a kickoff in the future belongs to a
// game that cannot be final, which is the one check that costs nothing and
// stops a report from voiding honest picks as "late".
export function cleanResult(raw, now = Date.now()) {
  if (!raw || typeof raw !== "object") return null;
  // Reported scores come from the app's own games table, so they arrive
  // prefixed exactly as picks do.
  const gameId = canonicalGameId(String(raw.game_id || "").slice(0, 64));
  if (!/^[A-Za-z0-9._-]+$/.test(gameId)) return null;
  const home = String(raw.home || "").toUpperCase().slice(0, 8);
  const away = String(raw.away || "").toUpperCase().slice(0, 8);
  if (!/^[A-Z]{2,8}$/.test(home) || !/^[A-Z]{2,8}$/.test(away) || home === away) {
    return null;
  }
  const score = (v) => {
    // The empty cases first, and explicitly: Number(null) and Number("") are
    // both 0, so a game with no score on it would otherwise be reported as a
    // finished nil-all draw and graded as one.
    if (v === null || v === undefined || v === "") return null;
    const n = Number(v);
    return Number.isInteger(n) && n >= 0 && n <= 200 ? n : null;
  };
  const hs = score(raw.home_score);
  const as = score(raw.away_score);
  if (hs === null || as === null) return null;          // not final, not news
  const season = Number(raw.season);
  const week = Number(raw.week);
  if (!Number.isInteger(season) || season < 2000 || season > 2100) return null;
  if (!Number.isInteger(week) || week < 1 || week > 22) return null;

  let kickoff = raw.kickoff ? String(raw.kickoff).slice(0, 40) : null;
  const at = kickoff ? Date.parse(kickoff) : NaN;
  // Unparseable or still to come: drop the kickoff rather than the report. A
  // missing kickoff only means the late-pick check is skipped for this game,
  // which is the safe way to be wrong -- grading a late pick beats voiding an
  // honest one on a timestamp a stranger chose.
  if (!Number.isFinite(at) || at > now) kickoff = null;
  return { game_id: gameId, season, week, kickoff, home, away,
           home_score: hs, away_score: as };
}

// Scores the app already fetched, from a connection ESPN will answer.
//
// The report is recorded against the picker who sent it and goes no further on
// its own. Promotion to the results table happens below, and only on agreement.
async function resultsRoute(request, env) {
  if (!env.PICKS_DB) {
    return json({ ok: false, reason: "not_configured",
                  message: "Result reporting is not set up on the server." }, 200);
  }
  const raw = await request.text().catch(() => "");
  if (raw.length > 200_000) return json({ ok: false, reason: "too_large" }, 400);
  let body = {};
  try { body = JSON.parse(raw || "{}"); } catch { body = {}; }

  const gate = await licensedPicker(body, env, overResultsLimit,
                                    "Reporting results needs a live subscription.");
  if (gate.error) return gate.error;
  const { picker, degraded } = gate;

  const now = Date.now();
  const reports = (Array.isArray(body.results) ? body.results : [])
    .slice(0, RESULTS_MAX).map((r) => cleanResult(r, now)).filter(Boolean);
  if (!reports.length) return json({ ok: false, reason: "empty" }, 400);

  const stamp = new Date(now).toISOString();
  const writes = reports.map((r) => env.PICKS_DB.prepare(
    "INSERT INTO result_reports(game_id, picker, season, week, kickoff, home,"
    + " away, home_score, away_score, reported_at) VALUES(?,?,?,?,?,?,?,?,?,?)"
    // A picker who reports the same game twice replaces their own row. One
    // row per picker per game is what makes counting them a count of people.
    + " ON CONFLICT(game_id, picker) DO UPDATE SET"
    + " home_score = excluded.home_score, away_score = excluded.away_score,"
    + " kickoff = excluded.kickoff, reported_at = excluded.reported_at",
  ).bind(r.game_id, picker, r.season, r.week, r.kickoff, r.home, r.away,
         r.home_score, r.away_score, stamp));
  try {
    await env.PICKS_DB.batch(writes);
  } catch (err) {
    console.log("results: store failed", String(err && err.message || err));
    return json({ ok: false, reason: "store_failed" }, 502);
  }

  const agreed = await promoteResults(env, reports.map((r) => r.game_id), stamp);
  return json(degraded
    ? { ok: true, reported: reports.length, agreed, unchecked: true }
    : { ok: true, reported: reports.length, agreed });
}

// Reports that enough people agree on, written where grading will find them.
//
// The GROUP BY is the whole mechanism: rows only count together when the
// score *and* the kickoff match exactly, so two people saying 27-20 and two
// saying 28-20 is four reports and no quorum. Honest clients read the same
// public scoreboard and agree to the character.
export async function promoteResults(env, gameIds, stamp, quorum = null) {
  // The argument is for tests; production reads the deployment's setting.
  const need = quorum === null || quorum === undefined ? resultsQuorum(env) : quorum;
  const ids = [...new Set(gameIds)].filter(Boolean);
  if (!ids.length) return 0;
  const holes = ids.map(() => "?").join(",");
  let rows;
  try {
    rows = await env.PICKS_DB.prepare(
      "SELECT game_id, season, week, kickoff, home, away, home_score, away_score,"
      + " COUNT(*) AS reporters FROM result_reports"
      + ` WHERE game_id IN (${holes})`                            // eslint-disable-line
      + " GROUP BY game_id, kickoff, home, away, home_score, away_score"
      + " HAVING reporters >= ?",
    ).bind(...ids, need).all();
  } catch (err) {
    console.log("results: could not count reports", String(err && err.message || err));
    return 0;
  }
  const agreed = rows.results || [];
  if (!agreed.length) return 0;

  await env.PICKS_DB.batch(agreed.map((r) => env.PICKS_DB.prepare(
    "INSERT INTO results(game_id, season, week, kickoff, home, away, home_score,"
    + " away_score, fetched_at, source) VALUES(?,?,?,?,?,?,?,?,?,'crowd')"
    // Only ever fills a gap. A score the grader got from ESPN itself is the
    // better fact and is never replaced by a vote, however large the vote.
    + " ON CONFLICT(game_id) DO UPDATE SET home_score = excluded.home_score,"
    + " away_score = excluded.away_score, kickoff = excluded.kickoff,"
    + " fetched_at = excluded.fetched_at, source = 'crowd'"
    + " WHERE results.home_score IS NULL",
  ).bind(r.game_id, r.season, r.week, r.kickoff, r.home, r.away,
         r.home_score, r.away_score, stamp)));
  console.log("results: agreed", JSON.stringify(
    { games: agreed.length, quorum: need }));
  return agreed.length;
}

// A pick that landed on no game, said out loud when it happens.
//
// The id mismatch hid for a day because a join that matches nothing looks
// exactly like a quiet week. It cannot hide again: an id that matches no known
// game gets a log line here and a count on the dashboard.
//
// Only for weeks whose games are on file. A pick can be made a fortnight out,
// before the grader has ever fetched that week, and an unmatched count that
// fires on every early pick is a warning nobody reads.
//
// One query. Seasons and weeks are taken as two lists rather than as pairs,
// which over-fetches slightly when a batch spans both -- a season-week is
// about sixteen rows, so the cross product is still tiny.
async function warnUnmatched(env, picks) {
  const seasons = [...new Set(picks.map((p) => p.season))];
  const weeks = [...new Set(picks.map((p) => p.week))];
  if (!seasons.length || !weeks.length) return 0;
  let rows;
  try {
    rows = await env.PICKS_DB.prepare(
      "SELECT game_id, season, week FROM results"
      + ` WHERE season IN (${seasons.map(() => "?").join(",")})`   // eslint-disable-line
      + ` AND week IN (${weeks.map(() => "?").join(",")})`,        // eslint-disable-line
    ).bind(...seasons, ...weeks).all();
  } catch (err) {
    console.log("picks: could not check ids against the schedule",
                String(err && err.message || err));
    return 0;
  }
  const known = new Set();
  const weeksOnFile = new Set();
  for (const r of rows.results || []) {
    known.add(r.game_id);
    weeksOnFile.add(`${r.season}:${r.week}`);
  }
  const orphans = [...new Set(picks
    .filter((p) => weeksOnFile.has(`${p.season}:${p.week}`) && !known.has(p.game_id))
    .map((p) => p.game_id))];
  if (orphans.length) {
    console.log("picks: game ids match no game on file", JSON.stringify(
      { count: orphans.length, ids: orphans.slice(0, 5) }));
  }
  return orphans.length;
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

// Workers `fetch` sends no User-Agent at all unless one is set, and an
// unidentified request is the kind ESPN turns away -- which is what the
// production log's `espn 403` was.
//
// This string is the desktop app's, verbatim: `user_agent` in
// nflpicker/config.py. The app has been asking this same endpoint for months
// without being refused, so it is the one identity known to work here, and
// one convention beats two -- if ESPN ever decides what it will answer, it
// should decide it once for both. Change either and change the other.
//
// SCOREBOARD_USER_AGENT in wrangler.toml overrides it, so a different string
// can be tried against a live 403 without a code change.
const USER_AGENT = "nflpicker/0.1 (+https://github.com/Ljbutton/ESPNpicem)";

// No Referer or Origin. They would claim this request came from a page on
// espn.com, which is not true of a cron running in a datacentre, and a header
// that lies is a bad thing to have to reason about later.
export function scoreboardHeaders(env = {}) {
  return {
    "User-Agent": (env && env.SCOREBOARD_USER_AGENT) || USER_AGENT,
    Accept: "application/json",
    "Accept-Language": "en-US,en;q=0.9",
  };
}

// How much of a refusal's body to keep. Enough to carry ESPN's own wording or
// the first line of a block page; short enough that a log line stays a line.
const BODY_PEEK = 300;

async function peek(res) {
  try {
    return String(await res.text()).replace(/\s+/g, " ").trim().slice(0, BODY_PEEK);
  } catch {
    return "";
  }
}

export async function fetchResults(season, week, fetcher = fetch, env = {}) {
  const url = `${ESPN}?dates=${season}&seasontype=2&week=${week}`;
  const res = await fetcher(url, { headers: scoreboardHeaders(env) });
  if (!res.ok) {
    // The status alone says a request was refused and nothing about why. Two
    // identical `espn 403` lines is all the last outage left behind; ESPN says
    // more than that in the body, so keep the front of it.
    const body = await peek(res);
    console.log("picks: scoreboard refused",
                JSON.stringify({ status: res.status, season, week, body }));
    const err = new Error(`espn ${res.status}`);
    err.status = res.status;
    err.body = body;
    throw err;
  }
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

// At most this many weeks in one run. A backlog that grows without a ceiling
// is a cron that eventually runs longer than the hour between runs.
export const WEEKS_MAX = 6;

// Which weeks this run should grade.
//
// The current week and the one before it always, because a Monday night game
// is graded after the week has rolled over. Then any earlier week that still
// holds an ungraded pick -- which is how a week whose run failed comes back
// instead of falling out of that two-week window and staying ungraded for the
// rest of the season. No table of failures to keep in step with reality: the
// ungraded picks *are* the backlog, so anything missed for any reason is
// picked up, and a week leaves the list by being graded.
//
// Weeks ahead of the current one are left out. A pick can be made a fortnight
// early and is ungraded for good reason; fetching its week now would spend a
// request on a game nobody has played.
export async function weeksToGrade(env, season, week, limit = WEEKS_MAX) {
  const weeks = [];
  const add = (s, w) => {
    if (!(w >= 1) || weeks.some((x) => x.season === s && x.week === w)) return;
    weeks.push({ season: s, week: w });
  };
  add(season, week);
  add(season, week - 1);
  try {
    const rows = await env.PICKS_DB.prepare(
      "SELECT DISTINCT season, week FROM picks WHERE result IS NULL"
      + " AND (season < ? OR (season = ? AND week <= ?))"
      + " ORDER BY season DESC, week DESC LIMIT ?",
    ).bind(season, season, week, limit).all();
    for (const r of rows.results || []) add(Number(r.season), Number(r.week));
  } catch (err) {
    // The two weeks above still get graded; only the catching-up is lost.
    console.log("picks: could not read the backlog",
                String((err && err.message) || err));
  }
  return weeks.slice(0, limit);
}

export async function gradeWeek(env, season, week, fetcher = fetch) {
  let results = [];
  let refused = null;
  try {
    results = await fetchResults(season, week, fetcher, env);
  } catch (err) {
    // Not a reason to stop. Grading carries on against whatever scores are
    // already on file -- which, when ESPN is refusing this Worker outright, is
    // what customers reported and agreed on. Anything still ungraded keeps the
    // week in the backlog, so the next run comes back to it either way.
    refused = { status: (err && err.status) || null,
                error: String((err && err.message) || err) };
  }
  const now = new Date().toISOString();
  const writes = [];
  const byGame = new Map();

  // What is already on file, first. Mostly this is what previous runs wrote,
  // and it changes nothing -- but when ESPN has refused and `results` is empty,
  // these are the scores customers reported and agreed on, and they are the
  // only reason the week can be graded at all.
  try {
    const stored = await env.PICKS_DB.prepare(
      "SELECT game_id, season, week, kickoff, home, away, home_score, away_score"
      + " FROM results WHERE season = ? AND week = ?",
    ).bind(season, week).all();
    for (const r of stored.results || []) byGame.set(r.game_id, r);
  } catch (err) {
    console.log("picks: could not read stored results",
                String(err && err.message || err));
  }

  // Then the fetch, which wins where it has an answer: ESPN is the better
  // fact, and a score it just gave us replaces one a vote put there.
  for (const r of results) {
    byGame.set(r.game_id, r);
    writes.push(env.PICKS_DB.prepare(
      "INSERT INTO results(game_id, season, week, kickoff, home, away, home_score,"
      + " away_score, fetched_at, source) VALUES(?,?,?,?,?,?,?,?,?,'espn')"
      + " ON CONFLICT(game_id) DO UPDATE SET home_score = excluded.home_score,"
      + " away_score = excluded.away_score, kickoff = excluded.kickoff,"
      + " fetched_at = excluded.fetched_at, source = 'espn'",
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
  // `ok` is about the fetch, not about the grading: a refused week that graded
  // from reported scores still says so, because the refusal is the thing worth
  // knowing in the log.
  return { ok: !refused, season, week, games: results.length,
           known: byGame.size, graded, late, ...(refused || {}) };
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

// One row per picker per game: a score somebody says they saw. Kept after
// promotion, because the count is the evidence -- and because a second report
// arriving later is how a game that missed quorum reaches it.
export const REPORTS_DDL =
  "CREATE TABLE IF NOT EXISTS result_reports (game_id TEXT NOT NULL, "
  + "picker TEXT NOT NULL, season INTEGER NOT NULL, week INTEGER NOT NULL, "
  + "kickoff TEXT, home TEXT NOT NULL, away TEXT NOT NULL, "
  + "home_score INTEGER NOT NULL, away_score INTEGER NOT NULL, "
  + "reported_at TEXT NOT NULL, PRIMARY KEY (game_id, picker))";

// Rows stored before the ids were reconciled, brought to the same spelling.
//
// This is a rename, not a guess: both sides read the same ESPN scoreboard and
// take the same `event.id`, so "espn-401671789" and "401671789" are the same
// game by construction. Only a prefix followed by digits and nothing else is
// touched; anything odder is left alone to be counted as unmatched, where it
// can be seen.
//
// OR IGNORE rather than OR REPLACE: if a picker somehow holds both spellings
// of one game, the canonical row wins and the odd one stays put and visible.
// Losing a row quietly is the failure this whole change exists to stop.
//
// Runs every scheduled run and matches nothing once it is done, because the
// boundary now normalises and no prefixed row can arrive again.
export async function dropIdPrefix(env) {
  const DIGITS = "game_id LIKE 'espn-%' AND length(game_id) > 5"
    + " AND substr(game_id, 6) NOT GLOB '*[^0-9]*'";
  const moved = {};
  for (const table of ["picks", "consensus", "result_reports", "results"]) {
    try {
      const out = await env.PICKS_DB.prepare(
        `UPDATE OR IGNORE ${table} SET game_id = substr(game_id, 6) WHERE ${DIGITS}`,
      ).run();                                                  // eslint-disable-line
      const n = (out && out.meta && out.meta.changes) || 0;
      if (n) moved[table] = n;
    } catch (err) {
      // A table that is not there yet on a fresh database, most likely.
      console.log(`picks: could not normalise ids in ${table}`,
                  String(err && err.message || err));
    }
  }
  if (Object.keys(moved).length) {
    console.log("picks: normalised game ids", JSON.stringify(moved));
  }
  return moved;
}

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
    await env.PICKS_DB.exec(REPORTS_DDL);
  } catch (err) {
    console.log("results: could not ensure the reports table",
                String(err && err.message || err));
    return false;
  }
  // Both are ALTERs a fresh database gets from schema.sql and an existing one
  // needs adding. They throw when the column is already there, which is the
  // usual case and not worth saying anything about.
  try {
    await env.PICKS_DB.exec("ALTER TABLE picks ADD COLUMN model_side TEXT");
  } catch {
    // Already there, which is the usual case.
  }
  try {
    await env.PICKS_DB.exec("ALTER TABLE results ADD COLUMN source TEXT");
  } catch {
    // Likewise. Where it says 'crowd' the score came from agreeing customers
    // rather than from ESPN, which is worth being able to tell apart later.
  }

  await dropIdPrefix(env);

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
/* Where a score came from. Only ever drawn on a score that came from agreeing
   customers rather than from ESPN: the vote is the thing worth marking, and
   marking both would make the page noisier without making it clearer. */
.src { font-size:10px; letter-spacing:.04em; text-transform:uppercase;
       color:var(--warn); border:1px solid var(--warn); border-radius:3px;
       padding:0 4px; margin-left:6px; white-space:nowrap; vertical-align:1px; }
/* The standing note when the agreement rule is switched off. Not a tooltip:
   at a quorum of one this is a caveat on every number below it. */
.alone { display:block; margin-top:3px; font-size:11px; color:var(--warn); }
/* A pick that joined to nothing. Loud on purpose: the whole reason this
   exists is that the silent version of it hid for a day. */
.warn-row { border-color:var(--warn); color:var(--warn); text-align:left; }
.warn-row code { font-family:ui-monospace, SFMono-Regular, Menlo, monospace;
                 font-size:11px; }
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
  const quorum = resultsQuorum(env);
  // Four queries, whatever the size of the slate.
  //   games    one row per game this week            (~16)
  //   picks    one row per shared pick this week     (pickers x 16)
  //   career   one row per picker who has ever won   (tens)
  //   known    one row                               (1)
  const games = await env.PICKS_DB.prepare(
    "SELECT game_id, home, away, kickoff, home_score, away_score, source"
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

  // Picks that landed on no game on this week's card. Counted only once the
  // week's games are on file, because before that every pick is trivially
  // unmatched and the number would mean nothing.
  //
  // This is the number that would have shown the id mismatch on day one:
  // "5 picks this week" beside "no picks" on all sixteen games is only a
  // contradiction if something says so.
  const onFile = new Set((games.results || []).map((g) => g.game_id));
  const orphans = onFile.size
    ? (picks.results || []).filter((p) => !onFile.has(p.game_id))
    : [];
  const orphanIds = [...new Set(orphans.map((p) => p.game_id))];
  const unmatchedNote = orphans.length
    ? `<div class="empty warn-row"><b>${orphans.length} pick${
      orphans.length === 1 ? "" : "s"} matched no game on this week's card.</b>
      Their game ids are not ids this server knows: ${
        orphanIds.slice(0, 3).map((id) => `<code>${escapeHtml(id)}</code>`).join(", ")
      }${orphanIds.length > 3 ? ` and ${orphanIds.length - 3} more` : ""}.
      Those picks cannot be graded or counted in a split until the ids agree.</div>`
    : "";
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
          <div class="match">${escapeHtml(g.away)} at ${escapeHtml(g.home)}${
            sourceNote(g.source, quorum)}</div>
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
    ${unmatchedNote}
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

// A score that a vote put there, marked as one. ESPN's own scores and rows
// written before this column existed carry nothing: neither is a vote, and a
// badge on every row is a badge nobody reads.
const sourceNote = (source, quorum) => (source === "crowd"
  ? `<span class="src" title="Reported by ${quorum} agreeing subscriber${
    quorum === 1 ? "" : "s"} rather than fetched from ESPN.">reported${
    quorum === 1 ? " · 1" : ""}</span>`
  : "");

const recordCell = (r) => {
  const decided = r.wins + r.losses;
  return `<td class="n">${r.wins}-${r.losses}${r.pushes ? `-${r.pushes}` : ""}</td>
    <td class="n">${decided ? pct(r.wins / decided) : "—"}</td>`;
};

async function boardView(env, season, query) {
  const quorum = resultsQuorum(env);
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

  // At a quorum of one there is no agreement rule left: whatever the single
  // reporting client says becomes the score every row below is graded on. That
  // belongs beside the record it qualifies, on every visit, rather than in a
  // setting somebody remembers three weeks later.
  const aloneNote = quorum === 1
    ? '<span class="alone">Results are being taken from a single reporter '
      + '(RESULTS_QUORUM = 1) — no second opinion, so these records are only '
      + 'as honest as that one client.</span>'
    : "";

  const crowdRows = `<tr class="dim">
      <td class="n">—</td><td><b>The crowd</b><span class="mono under">the frozen
        pre-kickoff side on every captured game, and the first number it had
        against the close</span>${aloneNote}</td>
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
  const quorum = resultsQuorum(env);
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
    + " r.kickoff, r.source"
    + " FROM picks p LEFT JOIN results r ON r.game_id = p.game_id"
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
        : `${escapeHtml(g.away)} ${g.away_score} – ${escapeHtml(g.home)} ${
          g.home_score}${sourceNote(g.source, quorum)}`;
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

// -------------------------------------------------------------------- health

// What this deployment is actually missing, in one page.
//
// "Can't reach the license server" is what the app shows for every check that
// comes back without a verdict, and from the outside those look alike: a
// Worker that was never deployed, an API key Whop rejects, a binding that was
// never made. Opening the root only rules out the first. This rules out the
// rest, and it reports presence and status codes -- never a secret's value,
// and never a license key.
//
// Behind DASHBOARD_TOKEN because what it lists is a map of the deployment.
export async function health(env) {
  const out = {
    ok: true,
    whop_api_key: Boolean(env.WHOP_API_KEY),
    whop_product_id: Boolean(env.WHOP_PRODUCT_ID),
    picks_db: Boolean(env.PICKS_DB),
    support_rl: Boolean(env.SUPPORT_RL),
    picks_rl: Boolean(env.PICKS_RL),
    resend_api_key: Boolean(env.RESEND_API_KEY),
    support_email_to: Boolean(env.SUPPORT_EMAIL_TO),
    github_repo: env.GITHUB_REPO || null,
    whop: null,
    notes: [],
  };

  if (!env.WHOP_API_KEY) {
    out.ok = false;
    out.notes.push("WHOP_API_KEY is not set, so every validate answers "
      + "\"upstream\" and the app reports the license server as unreachable. "
      + "Set it with: npx wrangler secret put WHOP_API_KEY");
  } else {
    // A membership id that cannot exist. An authorised key gets 404 back; an
    // unauthorised one gets 401 or 403 without the id mattering at all.
    let status = null;
    try {
      const res = await whop(env, "/memberships/mem_healthcheck_does_not_exist");
      status = res.status;
    } catch (err) {
      out.whop = { reachable: false, error: String(err && err.message || err) };
      out.ok = false;
      out.notes.push("Could not reach api.whop.com at all.");
    }
    if (status !== null) {
      out.whop = { reachable: true, status };
      if (status === 401 || status === 403) {
        out.ok = false;
        out.notes.push(`Whop rejected the API key (${status}). It is wrong, `
          + "revoked, or from a different company. Replace it with: "
          + "npx wrangler secret put WHOP_API_KEY");
      } else if (status !== 404 && status >= 400) {
        out.ok = false;
        out.notes.push(`Whop answered ${status} to a plain read.`);
      }
    }
  }

  if (!env.PICKS_DB) out.notes.push("No PICKS_DB binding: pick sharing and the dashboard are off.");
  if (!env.RESEND_API_KEY || !env.SUPPORT_EMAIL_TO) out.notes.push("Support email is not configured.");
  return out;
}

async function healthRoute(url, env) {
  const token = url.searchParams.get("token") || "";
  if (!env.DASHBOARD_TOKEN || token !== env.DASHBOARD_TOKEN) return notFound();
  return json(await health(env));
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
        // Whop has already told us this subscription is active -- that is the
        // question the customer asked. Recording which computer they are on is
        // our bookkeeping, and failing bookkeeping must not lock out somebody
        // who paid: an API key without write scope would otherwise make every
        // new install look like a dead license server. The seat simply goes
        // unrecorded and is counted on a later check that does succeed.
        console.log("validate: could not record the machine",
                    JSON.stringify({ status: upd.status }));
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
