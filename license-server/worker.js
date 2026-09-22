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
//   POST /v1/picks      {picker, name, picks[]}          -> {ok, stored}
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
      const { season, week } = nflWeek(new Date());
      for (const w of [week, week - 1]) {
        if (w < 1) continue;
        try {
          const out = await gradeWeek(env, season, w);
          console.log("picks: graded", JSON.stringify(out));
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
// to, because the endpoint takes no licence key.
export const PICKS_MAX = 200;
const PICK_KINDS = new Set(["winner", "spread", "total", "survivor"]);

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
  return json({ ok: true, stored: picks.length });
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

// ---------------------------------------------------------------- dashboard

// The leaderboard, as one query. A picker's record is their graded picks;
// pushes and late picks are in neither column, so a run of ties does not
// flatter anybody's percentage.
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

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function dashboardRoute(url, env) {
  // One secret, compared in full. This page is every customer's picks.
  const token = url.searchParams.get("token") || "";
  if (!env.DASHBOARD_TOKEN || token !== env.DASHBOARD_TOKEN) {
    return new Response("Not found", { status: 404 });
  }
  if (!env.PICKS_DB) return new Response("No picks database bound.", { status: 503 });
  const season = Number(url.searchParams.get("season"))
    || new Date().getUTCFullYear();
  const board = await leaderboard(env, season);

  const rows = board.map((r, i) => `<tr>
    <td class="n">${i + 1}</td>
    <td>${escapeHtml(r.name)}<span class="id">${escapeHtml(r.picker)}</span></td>
    <td class="n">${r.wins || 0}-${r.losses || 0}${r.pushes ? `-${r.pushes}` : ""}</td>
    <td class="n">${r.rate === null ? "—" : `${(r.rate * 100).toFixed(1)}%`}</td>
    <td class="n dim">${r.pending || 0}</td>
    <td class="n dim">${r.late || 0}</td>
    <td class="dim">${escapeHtml(String(r.last_at || "").slice(0, 10))}</td>
  </tr>`).join("");

  const page = `<!doctype html><html lang="en"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Pickers · ${season}</title><style>
:root { color-scheme: dark; --bg:#12161a; --panel:#181d23; --line:#2a3138;
        --text:#e6edf3; --dim:#8b949e; --good:#2ee6a0; }
body { margin:0; padding:24px 16px; background:var(--bg); color:var(--text);
       font:14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
.wrap { max-width:860px; margin:0 auto; }
h1 { font-size:20px; margin:0 0 4px; }
p.sub { color:var(--dim); margin:0 0 20px; font-size:13px; }
table { width:100%; border-collapse:collapse; background:var(--panel);
        border:1px solid var(--line); border-radius:10px; overflow:hidden; }
th, td { padding:9px 12px; text-align:left; border-bottom:1px solid var(--line); }
th { font-size:11px; text-transform:uppercase; letter-spacing:.06em;
     color:var(--dim); font-weight:650; }
tr:last-child td { border-bottom:0; }
td.n, th.n { text-align:right; font-variant-numeric:tabular-nums; }
tbody tr:nth-child(-n+3) td:nth-child(4) { color:var(--good); font-weight:700; }
.id { display:block; font-size:10px; color:var(--dim); font-family:ui-monospace,monospace; }
.dim { color:var(--dim); }
.empty { padding:28px 12px; text-align:center; color:var(--dim); }
</style></head><body><div class="wrap">
<h1>Pickers · ${season}</h1>
<p class="sub">Everyone sharing picks, by straight record. Pushes and picks
that arrived after kickoff are in neither column.</p>
${board.length ? `<table><thead><tr><th class="n">#</th><th>Picker</th>
<th class="n">Record</th><th class="n">Rate</th><th class="n">Open</th>
<th class="n">Late</th><th>Last seen</th></tr></thead><tbody>${rows}</tbody></table>`
    : '<div class="empty">No shared picks yet this season.</div>'}
</div></body></html>`;
  return new Response(page, {
    status: 200,
    headers: { "Content-Type": "text/html; charset=utf-8",
               // Never cached anywhere: this is customer data behind a token
               // that lives in a URL.
               "Cache-Control": "no-store, private" },
  });
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
