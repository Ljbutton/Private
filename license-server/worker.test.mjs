// node --test license-server/worker.test.mjs
import assert from "node:assert/strict";
import { test } from "node:test";

import { IMAGE_BYTES_MAX, IMAGE_MAX, PICKS_RATE, SUPPORT_MAX, SUPPORT_RATE,
  gradePick, gradeWeek, leaderboard, nflWeek, support, validate }
  from "./worker.js";
import worker from "./worker.js";

function fakeWhop(membership, { status = 200, patchStatus = 200 } = {}) {
  const calls = [];
  globalThis.fetch = async (url, init = {}) => {
    calls.push({ url: String(url), method: init.method || "GET", body: init.body });
    if ((init.method || "GET") === "PATCH") {
      return new Response("{}", { status: patchStatus });
    }
    if (status !== 200) return new Response("{}", { status });
    return new Response(JSON.stringify(membership), { status: 200 });
  };
  return calls;
}

const env = { WHOP_API_KEY: "k", WHOP_PRODUCT_ID: "prod_1", MAX_MACHINES: "2" };
const base = { id: "mem_1", status: "active", product: { id: "prod_1" }, metadata: {} };

test("active key on a new machine is accepted and the machine recorded", async () => {
  const calls = fakeWhop({ ...base });
  const out = await validate({ license_key: "ABC-123", machine_id: "m1" }, env);
  assert.equal(out.valid, true);
  const patch = calls.find((c) => c.method === "PATCH");
  assert.ok(patch, "machine should be saved to metadata");
  assert.deepEqual(JSON.parse(patch.body), { metadata: { edge_machines: "m1" } });
});

test("known machine does not write again", async () => {
  const calls = fakeWhop({ ...base, metadata: { edge_machines: "m1", other: "x" } });
  const out = await validate({ license_key: "ABC-123", machine_id: "m1" }, env);
  assert.equal(out.valid, true);
  assert.equal(calls.filter((c) => c.method === "PATCH").length, 0);
});

test("third machine is refused", async () => {
  fakeWhop({ ...base, metadata: { edge_machines: "m1,m2" } });
  const out = await validate({ license_key: "ABC-123", machine_id: "m3" }, env);
  assert.equal(out.valid, false);
  assert.equal(out.reason, "too_many_machines");
});

test("existing metadata is kept when a machine is added", async () => {
  const calls = fakeWhop({ ...base, metadata: { note: "vip" } });
  await validate({ license_key: "ABC-123", machine_id: "m1" }, env);
  const patch = calls.find((c) => c.method === "PATCH");
  assert.deepEqual(JSON.parse(patch.body).metadata, { note: "vip", edge_machines: "m1" });
});

test("canceled subscription is refused with a readable message", async () => {
  fakeWhop({ ...base, status: "canceled" });
  const out = await validate({ license_key: "ABC-123", machine_id: "m1" }, env);
  assert.equal(out.valid, false);
  assert.equal(out.reason, "inactive");
  assert.match(out.message, /ended/);
});

// `env` above deliberately does not set ALLOWED_STATUSES, so these exercise
// the default the Worker ships with rather than a value the test supplies.

test("a completed one-time purchase is accepted", async () => {
  // The season pass. Whop reports a one-time purchase as `completed` once it
  // has been paid, which reads like an ending and is the opposite: it is the
  // entitlement. Refusing it locked out everyone who bought the pass rather
  // than a subscription.
  const calls = fakeWhop({ ...base, status: "completed" });
  const out = await validate({ license_key: "ABC-123", machine_id: "m1" }, env);
  assert.equal(out.valid, true);
  assert.equal(out.status, "completed");
  assert.equal(out.reason, "ok");
  const patch = calls.find((c) => c.method === "PATCH");
  assert.ok(patch, "a season pass registers its machine like any other key");
  assert.deepEqual(JSON.parse(patch.body), { metadata: { edge_machines: "m1" } });
});

test("a completed purchase is not told to resubscribe", async () => {
  // It carries no ending message, because nothing has ended.
  fakeWhop({ ...base, status: "completed" });
  const out = await validate({ license_key: "ABC-123", machine_id: "m1" }, env);
  assert.equal(out.message, "");
});

test("completed is still refusable when the list is narrowed", async () => {
  // Someone selling subscriptions only can take it back out, and the refusal
  // must not then claim a one-time purchase has expired -- it never had a
  // term to expire.
  fakeWhop({ ...base, status: "completed" });
  const out = await validate({ license_key: "ABC-123", machine_id: "m1" },
                             { ...env, ALLOWED_STATUSES: "active,trialing" });
  assert.equal(out.valid, false);
  assert.equal(out.reason, "inactive");
  assert.doesNotMatch(out.message, /ended|Resubscribe/);
});

test("the season pass keeps working with no renewal date", async () => {
  // A one-time purchase has nothing to renew, so the field is absent upstream.
  fakeWhop({ ...base, status: "completed", renewal_period_end: undefined });
  const out = await validate({ license_key: "ABC-123", machine_id: "m1" }, env);
  assert.equal(out.valid, true);
  assert.equal(out.renews_at, null);
});

test("key for another product is refused", async () => {
  fakeWhop({ ...base, product: { id: "prod_other" } });
  const out = await validate({ license_key: "ABC-123", machine_id: "m1" }, env);
  assert.equal(out.reason, "wrong_product");
});

test("unknown key", async () => {
  fakeWhop(null, { status: 404 });
  const out = await validate({ license_key: "NOPE-1", machine_id: "m1" }, env);
  assert.equal(out.valid, false);
  assert.equal(out.reason, "not_found");
});

test("Whop outage is not a refusal", async () => {
  fakeWhop(null, { status: 503 });
  const out = await validate({ license_key: "ABC-123", machine_id: "m1" }, env);
  assert.equal(out.valid, null);
  assert.equal(out.reason, "upstream");
});

test("junk input never reaches Whop", async () => {
  const calls = fakeWhop({ ...base });
  const out = await validate({ license_key: "../../etc", machine_id: "m1" }, env);
  assert.equal(out.valid, false);
  assert.equal(calls.length, 0);
});

test("the membership lookup uses the key and the bearer token", async () => {
  const calls = fakeWhop({ ...base, metadata: { edge_machines: "m1" } });
  await validate({ license_key: "ABC-123", machine_id: "m1" }, env);
  assert.equal(calls[0].url, "https://api.whop.com/api/v1/memberships/ABC-123");
});

// ------------------------------------------------------------------- report

// ------------------------------------------------------------------ support
//
// The one thing these have to prove beyond "it sends an email" is that nothing
// about the destination or the API key can be read out of a response. The
// address is a secret precisely because this repository is public.

const TO = "owner@example.invalid";
const supportEnv = { RESEND_API_KEY: "re_test_key", SUPPORT_EMAIL_TO: TO };

function fakeResend({ status = 200 } = {}) {
  const calls = [];
  globalThis.fetch = async (url, init = {}) => {
    calls.push({
      url: String(url),
      headers: init.headers || {},
      body: JSON.parse(init.body || "{}"),
    });
    return new Response(JSON.stringify({ id: "email_1" }), { status });
  };
  return calls;
}

function post(body, { ip = "1.2.3.4" } = {}) {
  return new Request("https://edge.example/v1/support", {
    method: "POST",
    headers: { "content-type": "application/json", "cf-connecting-ip": ip },
    body: typeof body === "string" ? body : JSON.stringify(body),
  });
}

// A KV namespace, in the twelve lines of it these tests need.
function fakeKv() {
  const store = new Map();
  return {
    async get(key) { return store.has(key) ? store.get(key) : null; },
    async put(key, value) { store.set(key, value); },
  };
}

test("a report sends exactly one email, to the configured address", async () => {
  const calls = fakeResend();
  const out = await support({
    description: "The bracket shows the wrong seed",
    doing: "opening the Playoffs window",
    email: "reporter@example.com",
    details: { included: ["version", "os"], key_hint: "…4F2A",
               version: { version: "1.0.0", commit: "abc1234" },
               environment: { os: "Windows 11" } },
  }, supportEnv);

  assert.equal(out.ok, true);
  assert.match(out.ref, /^[a-z0-9]{4,8}$/);
  assert.equal(calls.length, 1, "one email, not one per detail");
  assert.equal(calls[0].url, "https://api.resend.com/emails");
  assert.equal(calls[0].headers.authorization, "Bearer re_test_key");
  assert.deepEqual(calls[0].body.to, [TO]);
  assert.deepEqual(calls[0].body.reply_to, ["reporter@example.com"]);
  assert.match(calls[0].body.subject, /^\[The Edge bug\] The bracket shows the wrong seed — …4F2A$/);
  assert.match(calls[0].body.text, /The bracket shows the wrong seed/);
  assert.match(calls[0].body.text, /opening the Playoffs window/);
  assert.match(calls[0].body.text, new RegExp(out.ref));
});

test("no address given means no reply-to header", async () => {
  // Resend rejects an empty reply_to, so it has to be absent rather than "".
  const calls = fakeResend();
  const out = await support({ description: "it crashed" }, supportEnv);
  assert.equal(out.ok, true);
  assert.equal("reply_to" in calls[0].body, false);
  assert.match(calls[0].body.subject, /— no key$/);
});

test("a malformed address is not used as a reply-to", async () => {
  const calls = fakeResend();
  await support({ description: "x", email: "not-an-address" }, supportEnv);
  assert.equal("reply_to" in calls[0].body, false);
});

test("the category picks the subject line and what the second answer was", async () => {
  // A mailbox is sorted on the subject, so a suggestion must not arrive
  // looking like a crash report.
  const calls = fakeResend();
  await support({ category: "suggestion", description: "a dark bracket",
                  doing: "the Playoffs window" }, supportEnv);
  assert.match(calls[0].body.subject, /^\[The Edge suggestion\] a dark bracket/);
  assert.match(calls[0].body.text, /Where it would go:\nthe Playoffs window/);

  const general = fakeResend();
  await support({ category: "general", description: "how do I install it" },
                supportEnv);
  assert.match(general[0].body.subject, /^\[The Edge support\]/);
});

test("an unknown category is filed as a bug rather than refused", async () => {
  const calls = fakeResend();
  const out = await support({ category: "nonsense", description: "x" }, supportEnv);
  assert.equal(out.ok, true);
  assert.match(calls[0].body.subject, /^\[The Edge bug\]/);
});

test("screenshots arrive as attachments, named and capped", async () => {
  const calls = fakeResend();
  const shot = "aGVsbG8="; // any valid base64; the app checks the pixels
  const out = await support({
    description: "look at this",
    images: [
      { filename: "one.png", type: "image/png", data: shot },
      { filename: "two.jpg", type: "image/jpeg", data: shot },
      { filename: "three.gif", type: "image/gif", data: shot },
      { filename: "four.png", type: "image/png", data: shot },
    ],
  }, supportEnv);
  assert.equal(out.ok, true);
  assert.equal(calls[0].body.attachments.length, IMAGE_MAX, "no more than three");
  assert.deepEqual(calls[0].body.attachments.map((a) => a.filename),
                   ["one.png", "two.jpg", "three.gif"]);
  assert.equal(calls[0].body.attachments[0].content, shot);
  assert.match(calls[0].body.text, /Screenshots: one.png, two.jpg, three.gif/);
});

test("an attachment that is not an image this endpoint takes is dropped", async () => {
  // This endpoint is public and needs no key: without these checks it is a
  // way to mail an arbitrary file to an address nobody here can see.
  const calls = fakeResend();
  const out = await support({
    description: "still worth reading",
    images: [
      { filename: "payload.exe", type: "application/x-msdownload", data: "aGk=" },
      { filename: "escape.png", type: "image/png", data: "not base64!!" },
      { filename: "huge.png", type: "image/png",
        data: "A".repeat(Math.ceil((IMAGE_BYTES_MAX / 3) * 4) + 8) },
    ],
  }, supportEnv);
  assert.equal(out.ok, true, "the words still go, with or without the pictures");
  assert.equal("attachments" in calls[0].body, false);
});

test("an attachment filename cannot carry a path or a second extension", async () => {
  const calls = fakeResend();
  await support({ description: "x", images: [
    { filename: "../../run.exe", type: "image/png", data: "aGk=" },
  ] }, supportEnv);
  assert.deepEqual(calls[0].body.attachments.map((a) => a.filename), ["run.png"]);
});

test("an oversize body is refused before it is parsed", async () => {
  fakeResend();
  const response = await worker.fetch(post("x".repeat(SUPPORT_MAX + 1)), supportEnv);
  assert.equal(response.status, 400);
  assert.equal((await response.json()).reason, "too_large");
});

test("an empty description is refused", async () => {
  fakeResend();
  const response = await worker.fetch(post({ description: "   " }), supportEnv);
  assert.equal(response.status, 400);
  assert.equal((await response.json()).reason, "empty");
});

test("the sixth report in an hour from one address is refused", async () => {
  fakeResend();
  const env2 = { ...supportEnv, SUPPORT_RL: fakeKv() };
  for (let i = 0; i < SUPPORT_RATE; i += 1) {
    const ok = await worker.fetch(post({ description: `report ${i}` }), env2);
    assert.equal(ok.status, 200, `report ${i} should go through`);
  }
  const blocked = await worker.fetch(post({ description: "one too many" }), env2);
  assert.equal(blocked.status, 429);
  assert.equal((await blocked.json()).reason, "rate_limited");

  // And the limit is per address, not global.
  const other = await worker.fetch(
    post({ description: "from somewhere else" }, { ip: "9.9.9.9" }), env2);
  assert.equal(other.status, 200);
});

test("Resend failing is a 502 and not a throw", async () => {
  fakeResend({ status: 500 });
  const response = await worker.fetch(post({ description: "it broke" }), supportEnv);
  assert.equal(response.status, 502);
  const body = await response.json();
  assert.equal(body.ok, false);
  assert.equal(body.reason, "upstream");
});

test("missing secrets is a 502 that does not say which", async () => {
  fakeResend();
  const response = await worker.fetch(post({ description: "hello" }), {});
  assert.equal(response.status, 502);
  const text = JSON.stringify(await response.json());
  assert.equal(text.includes("RESEND"), false);
  assert.equal(text.includes("SUPPORT_EMAIL_TO"), false);
});

test("no response ever carries the API key or the recipient", async () => {
  for (const [body, env2] of [
    [{ description: "fine" }, supportEnv],
    [{ description: "" }, supportEnv],
    [{ description: "hello" }, {}],
  ]) {
    fakeResend();
    const response = await worker.fetch(post(body), env2);
    const text = await response.text();
    assert.equal(text.includes(TO), false, "the recipient must never come back");
    assert.equal(text.includes("re_test_key"), false, "nor the API key");
  }
});

test("a rate limiter that is down does not take reporting down with it", async () => {
  fakeResend();
  const broken = {
    ...supportEnv,
    SUPPORT_RL: { async get() { throw new Error("kv down"); }, async put() {} },
  };
  const response = await worker.fetch(post({ description: "still works" }), broken);
  assert.equal(response.status, 200);
});

// -------------------------------------------------------------------- picks
//
// The two things worth pinning: a pick is graded against the line the picker
// took rather than the one the game closed at, and an id read off the
// leaderboard is not enough to delete somebody's record.

// D1, in the dozen lines of it these tests need. Enough to prove the routes
// bind what they say they bind; the SQL itself is D1's problem.
function fakeD1() {
  const calls = [];
  const rows = { picks: [], results: [] };
  const db = {
    calls,
    rows,
    prepare(sql) {
      const stmt = {
        sql,
        args: [],
        bind(...args) { stmt.args = args; calls.push({ sql, args }); return stmt; },
        async run() { return { meta: { changes: 7 } }; },
        async all() {
          if (/FROM picks/.test(sql)) return { results: rows.picks };
          return { results: [] };
        },
      };
      return stmt;
    },
    async batch(statements) { return statements.map(() => ({ success: true })); },
  };
  return db;
}

function picksPost(body) {
  return new Request("https://edge.example/v1/picks", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

// The id a key produces, worked out the same way the app and the Worker do.
// Tests use a different key each time: the licence check is cached per key for
// ten minutes in a module-level map, so sharing one key between tests would
// let the first decide the rest.
async function pickerIdFor(key) {
  const bytes = new TextEncoder().encode(`the-edge:picks:v1:${key}`);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)]
    .map((b) => b.toString(16).padStart(2, "0")).join("").slice(0, 16);
}

// Whop, the picks database, and nothing else.
const picksEnv = (PICKS_DB) => ({
  PICKS_DB, WHOP_API_KEY: "k", WHOP_PRODUCT_ID: "prod_1",
});
const live = { id: "mem_1", status: "active", product: { id: "prod_1" }, metadata: {} };

const A_PICKER = "0123456789abcdef";

test("a batch of picks is stored under the picker's id", async () => {
  const PICKS_DB = fakeD1();
  const key = "KEY-STORE-0001";
  fakeWhop({ ...live });
  const response = await worker.fetch(picksPost({
    picker: await pickerIdFor(key),
    license_key: key,
    name: "The Commissioner",
    picks: [
      { game_id: "g1", kind: "winner", side: "KC", season: 2026, week: 5,
        line: -3.5, book_prob: 0.64, picked_at: "2026-10-06T12:00:00Z" },
      { game_id: "g1", kind: "survivor", side: "KC", season: 2026, week: 5 },
    ],
  }), picksEnv(PICKS_DB));
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { ok: true, stored: 2 });
  // The name goes on the picker, the picks on the picks.
  assert.match(PICKS_DB.calls[0].sql, /INSERT INTO pickers/);
  assert.equal(PICKS_DB.calls[0].args[1], "The Commissioner");
  assert.equal(PICKS_DB.calls[1].args[0], await pickerIdFor(key));
  assert.equal(PICKS_DB.calls[1].args[6], -3.5, "the line rides with the pick");
  // Only the id is stored. The key authenticated the batch and goes no further.
  const everything = JSON.stringify(PICKS_DB.calls);
  assert.equal(everything.includes(key), false, "no licence key reaches D1");
});

test("the server stamps its own received_at and ignores the client's clock", async () => {
  // Grading believes this one. A leaderboard graded on a timestamp the client
  // chose is a ranking of whoever is willing to lie about when they picked.
  const PICKS_DB = fakeD1();
  const key = "KEY-CLOCK-0002";
  fakeWhop({ ...live });
  await worker.fetch(picksPost({
    picker: await pickerIdFor(key),
    license_key: key,
    picks: [{ game_id: "g1", kind: "winner", side: "KC", season: 2026, week: 5,
              picked_at: "1999-01-01T00:00:00Z" }],
  }), picksEnv(PICKS_DB));
  const args = PICKS_DB.calls[1].args;
  assert.equal(args[10], "1999-01-01T00:00:00Z", "kept, for the record");
  assert.match(args[11], /^20\d\d-/, "but received_at is ours");
  assert.notEqual(args[11], args[10]);
});

test("a picker id that is not sixteen hex characters is refused", async () => {
  for (const picker of ["", "nope", "0123456789ABCDEF!", "0123456789abcde"]) {
    const response = await worker.fetch(
      picksPost({ picker, picks: [{ game_id: "g", kind: "winner", side: "KC" }] }),
      { PICKS_DB: fakeD1() });
    assert.equal(response.status, 400, picker);
  }
});

test("a pick of a kind this does not grade is dropped, not stored", async () => {
  const PICKS_DB = fakeD1();
  const key = "KEY-PARLAY-0003";
  fakeWhop({ ...live });
  const response = await worker.fetch(picksPost({
    picker: await pickerIdFor(key),
    license_key: key,
    picks: [{ game_id: "g1", kind: "parlay", side: "KC", season: 2026, week: 5 }],
  }), picksEnv(PICKS_DB));
  assert.equal(response.status, 400);
  assert.equal((await response.json()).reason, "empty");
});

test("with no database bound the app is told to stop rather than retry", async () => {
  const response = await worker.fetch(picksPost({ picker: A_PICKER, picks: [] }), {});
  assert.equal(response.status, 200, "not an error the app should queue against");
  assert.equal((await response.json()).reason, "not_configured");
});

test("deleting needs the licence key, not just the id", async () => {
  // The id is printed on the leaderboard. If it authorised a delete, the
  // leaderboard would be a list of records anybody could erase.
  const del = (body) => new Request("https://edge.example/v1/picks", {
    method: "DELETE",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  const PICKS_DB = fakeD1();

  const wrong = await worker.fetch(
    del({ picker: A_PICKER, license_key: "SOMEONE-ELSES-KEY" }), { PICKS_DB });
  assert.equal(wrong.status, 403);
  assert.equal((await wrong.json()).reason, "not_yours");

  // The real one: the id this key actually hashes to.
  const key = "EDGE-TEST-KEY-0001";
  const bytes = new TextEncoder().encode(`the-edge:picks:v1:${key}`);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  const mine = [...new Uint8Array(digest)]
    .map((b) => b.toString(16).padStart(2, "0")).join("").slice(0, 16);

  const right = await worker.fetch(del({ picker: mine, license_key: key }), { PICKS_DB });
  assert.equal(right.status, 200);
  assert.equal((await right.json()).deleted, 7);
});

// ------------------------------------------------------------------ grading

test("a straight-up pick is graded on who won", () => {
  const game = { home: "KC", away: "DEN", home_score: 27, away_score: 20 };
  assert.equal(gradePick({ kind: "winner", side: "KC" }, game), "win");
  assert.equal(gradePick({ kind: "winner", side: "DEN" }, game), "loss");
  assert.equal(gradePick({ kind: "survivor", side: "KC" }, game), "win");
  assert.equal(
    gradePick({ kind: "winner", side: "KC" },
              { ...game, home_score: 20 }), "push", "a tie is neither");
});

test("a spread pick is graded against the line that picker took", () => {
  // The whole reason the line travels with the pick: KC won by 7, so -3.5 is a
  // win and -10.5 is a loss, and both people picked the same team.
  const game = { home: "KC", away: "DEN", home_score: 27, away_score: 20 };
  assert.equal(gradePick({ kind: "spread", side: "KC", line: -3.5 }, game), "win");
  assert.equal(gradePick({ kind: "spread", side: "KC", line: -10.5 }, game), "loss");
  // The away side of the same number, and the exact-number push.
  assert.equal(gradePick({ kind: "spread", side: "DEN", line: -10.5 }, game), "win");
  assert.equal(gradePick({ kind: "spread", side: "KC", line: -7 }, game), "push");
});

test("a total is graded over or under the number on the ticket", () => {
  const game = { home: "KC", away: "DEN", home_score: 27, away_score: 20 };
  assert.equal(gradePick({ kind: "total", side: "OVER", total_line: 44.5 }, game), "win");
  assert.equal(gradePick({ kind: "total", side: "UNDER", total_line: 44.5 }, game), "loss");
  assert.equal(gradePick({ kind: "total", side: "OVER", total_line: 47 }, game), "push");
});

test("a game with no final score is not graded at all", () => {
  const open = { home: "KC", away: "DEN", home_score: null, away_score: null };
  assert.equal(gradePick({ kind: "winner", side: "KC" }, open), null);
  // Nor is a spread pick that arrived without a line to grade against.
  assert.equal(gradePick({ kind: "spread", side: "KC", line: null },
                         { home: "KC", away: "DEN", home_score: 27, away_score: 20 }),
               null);
});

test("a pick that arrived after kickoff is marked late, not counted", async () => {
  const PICKS_DB = fakeD1();
  PICKS_DB.rows.picks = [
    { picker: A_PICKER, game_id: "401", kind: "winner", side: "KC",
      received_at: "2026-10-11T19:30:00Z" },                 // after kickoff
    { picker: "aaaaaaaaaaaaaaaa", game_id: "401", kind: "winner", side: "KC",
      received_at: "2026-10-11T12:00:00Z" },                 // before it
  ];
  const espn = async () => new Response(JSON.stringify({
    events: [{
      id: "401", date: "2026-10-11T17:00:00Z",
      competitions: [{
        status: { type: { completed: true } },
        competitors: [
          { homeAway: "home", score: "27", team: { abbreviation: "KC" } },
          { homeAway: "away", score: "20", team: { abbreviation: "DEN" } },
        ],
      }],
    }],
  }), { status: 200 });

  const out = await gradeWeek({ PICKS_DB }, 2026, 5, espn);
  assert.equal(out.games, 1);
  assert.equal(out.graded, 1, "only the one that beat the kickoff");
  assert.equal(out.late, 1);
  const updates = PICKS_DB.calls.filter((c) => /UPDATE picks SET result/.test(c.sql));
  assert.equal(updates.find((c) => c.args.includes(A_PICKER)).sql.includes("'late'"), true);
});

test("the leaderboard ranks by rate and leaves pushes out of it", async () => {
  const PICKS_DB = fakeD1();
  PICKS_DB.prepare = (sql) => ({
    bind: () => ({
      async all() {
        return { results: [
          { picker: "b".repeat(16), name: "Steady", wins: 6, losses: 4,
            pushes: 2, pending: 1, late: 0, total: 13, last_at: "2026-10-11" },
          { picker: "a".repeat(16), name: "Sharp", wins: 9, losses: 1,
            pushes: 0, pending: 0, late: 0, total: 10, last_at: "2026-10-11" },
        ] };
      },
    }),
    sql,
  });
  const board = await leaderboard({ PICKS_DB }, 2026);
  assert.deepEqual(board.map((r) => r.name), ["Sharp", "Steady"]);
  assert.equal(board[0].rate, 0.9);
  assert.equal(board[1].decided, 10, "the two pushes are in neither column");
});

test("the dashboard is a 404 without the token", async () => {
  const env = { PICKS_DB: fakeD1(), DASHBOARD_TOKEN: "a-long-random-string" };
  for (const query of ["", "?token=", "?token=guess"]) {
    const res = await worker.fetch(
      new Request(`https://edge.example/v1/dashboard${query}`), env);
    assert.equal(res.status, 404, query);
  }
  // And a 404 when no token is configured at all, rather than wide open.
  const unset = await worker.fetch(
    new Request("https://edge.example/v1/dashboard?token=anything"),
    { PICKS_DB: fakeD1() });
  assert.equal(unset.status, 404);
});

test("the dashboard never lets itself be cached", async () => {
  const env = { PICKS_DB: fakeD1(), DASHBOARD_TOKEN: "a-long-random-string" };
  const res = await worker.fetch(new Request(
    "https://edge.example/v1/dashboard?token=a-long-random-string&season=2026"), env);
  assert.equal(res.status, 200);
  assert.match(res.headers.get("cache-control"), /no-store/);
  assert.match(await res.text(), /Pickers · 2026/);
});

test("a picker's name cannot put markup on the dashboard", async () => {
  const PICKS_DB = fakeD1();
  PICKS_DB.prepare = (sql) => ({
    sql,
    bind: () => ({
      async all() {
        return { results: [{ picker: "c".repeat(16), wins: 1, losses: 0,
                             pushes: 0, pending: 0, late: 0, total: 1,
                             name: '<script>alert(1)</script>' }] };
      },
    }),
  });
  const res = await worker.fetch(new Request(
    "https://edge.example/v1/dashboard?token=t"), { PICKS_DB, DASHBOARD_TOKEN: "t" });
  const html = await res.text();
  assert.equal(html.includes("<script>alert(1)</script>"), false);
  assert.match(html, /&lt;script&gt;/);
});

test("the season and week a date belongs to", () => {
  // The case that makes this a function: January is last season.
  assert.deepEqual(nflWeek(new Date("2027-01-05T12:00:00Z")).season, 2026);
  assert.equal(nflWeek(new Date("2026-09-10T12:00:00Z")).week, 1);
  assert.equal(nflWeek(new Date("2026-09-17T12:00:00Z")).week, 2);
  assert.equal(nflWeek(new Date("2026-06-01T12:00:00Z")).week, 1, "clamped, not negative");
});

// ------------------------------------------------- picks need a subscription
//
// This endpoint shipped without a licence check: anybody with the URL could
// write to the picks table, under any id they invented. These are the tests
// that keep it shut.

test("a valid key with a matching picker id is stored", async () => {
  const PICKS_DB = fakeD1();
  const key = "KEY-GOOD-1001";
  const calls = fakeWhop({ ...live });
  const response = await worker.fetch(picksPost({
    picker: await pickerIdFor(key),
    license_key: key,
    picks: [{ game_id: "g1", kind: "winner", side: "KC", season: 2026, week: 5 }],
  }), picksEnv(PICKS_DB));

  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { ok: true, stored: 1 });
  assert.ok(calls.some((c) => c.url.includes("/memberships/")), "Whop was asked");
  // No machine is registered by sharing a pick: that would quietly spend one
  // of the customer's two computer slots.
  assert.equal(calls.some((c) => c.method === "PATCH"), false);
});

test("an invalid key is refused and nothing is written", async () => {
  const PICKS_DB = fakeD1();
  const key = "KEY-DEAD-1002";
  fakeWhop({ ...live, status: "canceled" });
  const response = await worker.fetch(picksPost({
    picker: await pickerIdFor(key),
    license_key: key,
    picks: [{ game_id: "g1", kind: "winner", side: "KC", season: 2026, week: 5 }],
  }), picksEnv(PICKS_DB));

  assert.equal(response.status, 403);
  assert.equal((await response.json()).reason, "unlicensed");
  assert.deepEqual(PICKS_DB.calls, [], "not one statement ran");
});

test("no key at all is refused", async () => {
  // The shape the endpoint shipped in: a picker id and some picks.
  const PICKS_DB = fakeD1();
  fakeWhop({ ...live });
  const response = await worker.fetch(picksPost({
    picker: A_PICKER,
    picks: [{ game_id: "g1", kind: "winner", side: "KC", season: 2026, week: 5 }],
  }), picksEnv(PICKS_DB));

  assert.equal(response.status, 403);
  assert.equal((await response.json()).reason, "unlicensed");
  assert.deepEqual(PICKS_DB.calls, []);
});

test("a valid key cannot write under somebody else's picker id", async () => {
  // Otherwise one subscription is a licence to write the whole leaderboard:
  // somebody else's row, or a thousand invented ones.
  const PICKS_DB = fakeD1();
  const key = "KEY-GOOD-1003";
  const calls = fakeWhop({ ...live });
  const response = await worker.fetch(picksPost({
    picker: await pickerIdFor("SOMEBODY-ELSES-KEY"),
    license_key: key,
    picks: [{ game_id: "g1", kind: "winner", side: "KC", season: 2026, week: 5 }],
  }), picksEnv(PICKS_DB));

  assert.equal(response.status, 403);
  assert.equal((await response.json()).reason, "picker_mismatch");
  assert.deepEqual(PICKS_DB.calls, []);
  assert.equal(calls.length, 0, "refused before Whop is even asked");
});

test("Whop being down takes the batch rather than punishing the customer", async () => {
  const PICKS_DB = fakeD1();
  const key = "KEY-OUTAGE-1004";
  fakeWhop({ ...live }, { status: 503 });
  const logged = [];
  const realLog = console.log;
  console.log = (...args) => logged.push(args.join(" "));
  let body;
  try {
    const response = await worker.fetch(picksPost({
      picker: await pickerIdFor(key),
      license_key: key,
      picks: [{ game_id: "g1", kind: "winner", side: "KC", season: 2026, week: 5 }],
    }), picksEnv(PICKS_DB));
    assert.equal(response.status, 200);
    body = await response.json();
  } finally {
    console.log = realLog;
  }
  assert.equal(body.ok, true);
  assert.equal(body.unchecked, true, "and it says the check was skipped");
  assert.ok(logged.some((l) => /Whop unreachable/.test(l)), "and it is logged");
  assert.equal(logged.join(" ").includes(key), false, "without the key in it");
});

test("an outage is not cached, so the next batch asks Whop again", async () => {
  const key = "KEY-OUTAGE-1005";
  const picker = await pickerIdFor(key);
  const batch = { picker, license_key: key,
                  picks: [{ game_id: "g1", kind: "winner", side: "KC",
                            season: 2026, week: 5 }] };
  const realLog = console.log;
  console.log = () => {};
  try {
    const down = fakeWhop({ ...live }, { status: 503 });
    await worker.fetch(picksPost(batch), picksEnv(fakeD1()));
    assert.equal(down.length, 1);
    const up = fakeWhop({ ...live }, { status: 503 });
    await worker.fetch(picksPost(batch), picksEnv(fakeD1()));
    assert.equal(up.length, 1, "asked again rather than trusting the outage");
  } finally {
    console.log = realLog;
  }
});

test("the licence check is cached: two batches, one Whop call", async () => {
  const key = "KEY-CACHE-1006";
  const picker = await pickerIdFor(key);
  const calls = fakeWhop({ ...live });
  const batch = (gameId) => picksPost({
    picker, license_key: key,
    picks: [{ game_id: gameId, kind: "winner", side: "KC", season: 2026, week: 5 }],
  });

  const first = await worker.fetch(batch("g1"), picksEnv(fakeD1()));
  const second = await worker.fetch(batch("g2"), picksEnv(fakeD1()));
  assert.equal(first.status, 200);
  assert.equal(second.status, 200);
  assert.equal(calls.filter((c) => c.url.includes("/memberships/")).length, 1,
               "a full slate must not be one Whop call per pick");
});

test("a refusal is cached too, so a dead key cannot be used to hammer Whop", async () => {
  const key = "KEY-DEAD-1007";
  const picker = await pickerIdFor(key);
  const calls = fakeWhop({ ...live, status: "expired" });
  const batch = picksPost({ picker, license_key: key,
    picks: [{ game_id: "g1", kind: "winner", side: "KC", season: 2026, week: 5 }] });

  assert.equal((await worker.fetch(batch, picksEnv(fakeD1()))).status, 403);
  assert.equal((await worker.fetch(picksPost({ picker, license_key: key,
    picks: [{ game_id: "g2", kind: "winner", side: "KC", season: 2026, week: 5 }] }),
    picksEnv(fakeD1()))).status, 403);
  assert.equal(calls.filter((c) => c.url.includes("/memberships/")).length, 1);
});

test("the batch after the cap is refused", async () => {
  const key = "KEY-FLOOD-1008";
  const picker = await pickerIdFor(key);
  fakeWhop({ ...live });
  const PICKS_RL = fakeKv();
  const env2 = { ...picksEnv(fakeD1()), PICKS_RL };
  const batch = () => picksPost({ picker, license_key: key,
    picks: [{ game_id: "g1", kind: "winner", side: "KC", season: 2026, week: 5 }] });

  for (let i = 0; i < PICKS_RATE; i += 1) {
    assert.equal((await worker.fetch(batch(), env2)).status, 200, `batch ${i}`);
  }
  const blocked = await worker.fetch(batch(), env2);
  assert.equal(blocked.status, 429);
  assert.equal((await blocked.json()).reason, "rate_limited");

  // Per key, not global: somebody else's week is not affected.
  const other = "KEY-OTHER-1009";
  const fine = await worker.fetch(picksPost({
    picker: await pickerIdFor(other), license_key: other,
    picks: [{ game_id: "g1", kind: "winner", side: "KC", season: 2026, week: 5 }],
  }), env2);
  assert.equal(fine.status, 200);
});

test("a normal week of picking never trips the cap", async () => {
  // Sixteen games, changed a few times each, is nowhere near it.
  assert.ok(PICKS_RATE >= 50, `${PICKS_RATE} batches an hour is not generous`);
});

test("no picks response ever carries the licence key", async () => {
  const good = "KEY-ECHO-1010";
  const dead = "KEY-ECHO-1011";
  const cases = [
    [{ picker: await pickerIdFor(good), license_key: good,
       picks: [{ game_id: "g1", kind: "winner", side: "KC", season: 2026, week: 5 }] },
     () => fakeWhop({ ...live })],
    [{ picker: await pickerIdFor(dead), license_key: dead,
       picks: [{ game_id: "g1", kind: "winner", side: "KC", season: 2026, week: 5 }] },
     () => fakeWhop({ ...live, status: "canceled" })],
    [{ picker: await pickerIdFor("MISMATCH-1012"), license_key: good, picks: [] },
     () => fakeWhop({ ...live })],
    [{ picker: A_PICKER, license_key: good, picks: [] },
     () => fakeWhop({ ...live })],
  ];
  for (const [body, arrange] of cases) {
    arrange();
    const response = await worker.fetch(picksPost(body), picksEnv(fakeD1()));
    const text = await response.text();
    assert.equal(text.includes(body.license_key), false,
                 `the key came back in ${text}`);
  }
});
