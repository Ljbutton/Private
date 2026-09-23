// node --test license-server/worker.test.mjs
import assert from "node:assert/strict";
import { test } from "node:test";

import { IMAGE_BYTES_MAX, IMAGE_MAX, PICKS_RATE, SUPPORT_MAX, SUPPORT_RATE,
  captureConsensus, captureFirst, crowdClv, ensureSchema, gradeCrowd,
  RESULTS_QUORUM, canonicalGameId, cleanResult, dropIdPrefix, fetchResults,
  gradePick, gradeWeek, health, leaderboard, nflWeek, promoteResults,
  resultsQuorum, scoreboardHeaders, support, validate, weeksToGrade }
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

test("a seat that cannot be recorded does not lock out a paid key", async () => {
  // The bug this covers: an API key that can read but not write turned every
  // first run on a new computer into "Can't reach the license server", for a
  // subscription Whop had just confirmed as active. Bookkeeping is ours; the
  // verdict is the customer's.
  fakeWhop(base, { patchStatus: 403 });
  const out = await validate({ license_key: "ABC-123", machine_id: "m1" }, env);
  assert.equal(out.valid, true);
  assert.equal(out.reason, "ok");
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
  // Every prepare, bound or not. `calls` only records statements that go
  // through bind(), and a statement with no parameters never does.
  const prepared = [];
  const rows = { picks: [], results: [] };
  const db = {
    calls,
    prepared,
    rows,
    prepare(sql) {
      prepared.push(sql);
      const stmt = {
        sql,
        args: [],
        bind(...args) { stmt.args = args; calls.push({ sql, args }); return stmt; },
        async run() { return { meta: { changes: 7 } }; },
        async all() {
          if (/DISTINCT season, week FROM picks/.test(sql)) {
            return { results: rows.backlog || [] };
          }
          if (/FROM result_reports/.test(sql)) return { results: rows.agreed || [] };
          if (/FROM results/.test(sql)) return { results: rows.results };
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
  assert.equal(args[11], "1999-01-01T00:00:00Z", "kept, for the record");
  assert.match(args[12], /^20\d\d-/, "but received_at is ours");
  assert.notEqual(args[12], args[11]);
});

test("the model's own side is actually stored, not just read", async () => {
  // It was not. The column, the dashboard column that reads it and the
  // capture that averages it all shipped; the INSERT never carried it, so
  // every row had NULL there and the page had a permanently blank column.
  const PICKS_DB = fakeD1();
  const key = "KEY-MODELSIDE-01";
  fakeWhop({ ...live });
  await worker.fetch(picksPost({
    picker: await pickerIdFor(key),
    license_key: key,
    picks: [{ game_id: "g1", kind: "winner", side: "KC", season: 2026, week: 5,
              model_side: "DEN" }],
  }), picksEnv(PICKS_DB));
  const insert = PICKS_DB.calls.find((c) => /INSERT INTO picks/.test(c.sql));
  assert.match(insert.sql, /model_side/);
  assert.equal(insert.args[10], "DEN");
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

// ---------------------------------------------------------------- dashboard
//
// A fake D1 that answers each of the dashboard's queries from plain arrays.
// It is not a SQL engine: it recognises the handful of statements the views
// actually run and computes the same thing in JavaScript, which is enough to
// pin the arithmetic and the markup without pretending to test D1.

function fakeDash(tables = {}) {
  const t = { results: [], picks: [], pickers: [], consensus: [], ...tables };
  const writes = [];
  const rowsFor = (sql, args) => {
    const where = (list, fn) => list.filter(fn);
    if (/FROM results WHERE season = \? AND week = \?/.test(sql)) {
      return where(t.results, (r) => r.season === args[0] && r.week === args[1]);
    }
    if (/SELECT game_id, kickoff FROM results/.test(sql)) {
      return where(t.results, (r) => r.season === args[0] && r.week === args[1]);
    }
    if (/COUNT\(\*\) AS n FROM pickers/.test(sql)) return [{ n: t.pickers.length }];
    if (/FROM pickers WHERE picker = \?/.test(sql)) {
      return where(t.pickers, (k) => k.picker === args[0]);
    }
    if (/FROM picks WHERE result IN \('win','loss'\) GROUP BY picker/.test(sql)) {
      const by = new Map();
      for (const p of t.picks) {
        if (p.result !== "win" && p.result !== "loss") continue;
        const row = by.get(p.picker) || { picker: p.picker, wins: 0, losses: 0 };
        row[p.result === "win" ? "wins" : "losses"] += 1;
        by.set(p.picker, row);
      }
      return [...by.values()];
    }
    if (/FROM picks p LEFT JOIN pickers k .* GROUP BY p\.picker/s.test(sql)) {
      const by = new Map();
      for (const p of t.picks.filter((x) => x.season === args[0])) {
        const row = by.get(p.picker) || {
          picker: p.picker,
          name: (t.pickers.find((k) => k.picker === p.picker) || {}).name
            || `Picker #${p.picker.slice(0, 4)}`,
          wins: 0, losses: 0, pushes: 0, pending: 0, late: 0, total: 0,
          last_at: "",
        };
        row.total += 1;
        if (p.result === "win") row.wins += 1;
        else if (p.result === "loss") row.losses += 1;
        else if (p.result === "push") row.pushes += 1;
        else if (p.result === "late") row.late += 1;
        else row.pending += 1;
        if ((p.received_at || "") > row.last_at) row.last_at = p.received_at || "";
        by.set(p.picker, row);
      }
      return [...by.values()];
    }
    if (/FROM picks p LEFT JOIN pickers k/.test(sql)) {
      return t.picks
        .filter((p) => p.season === args[0] && p.week === args[1])
        .map((p) => ({ ...p,
          name: (t.pickers.find((k) => k.picker === p.picker) || {}).name
            || `Picker #${p.picker.slice(0, 4)}` }))
        .sort((a, b) => String(b.received_at).localeCompare(String(a.received_at)));
    }
    if (/FROM picks p LEFT JOIN results r ON r\.game_id = p\.game_id\s+WHERE p\.picker = \?/s.test(sql)) {
      return t.picks.filter((p) => p.picker === args[0]).map((p) => ({
        ...p, ...(t.results.find((r) => r.game_id === p.game_id) || {}),
        side: p.side, result: p.result,
      }));
    }
    if (/MAX\(received_at\) AS m FROM picks GROUP BY game_id/.test(sql)) {
      const by = new Map();
      for (const p of t.picks) {
        const seen = by.get(p.game_id);
        if (!seen || String(p.received_at) > String(seen.received_at)) {
          by.set(p.game_id, p);
        }
      }
      return [...by.values()].map((p) => ({ game_id: p.game_id, line: p.line }));
    }
    if (/FROM consensus c LEFT JOIN results r/.test(sql)) {
      return t.consensus.filter((c) => c.season === args[0]).map((c) => ({
        ...c, ...(t.results.find((r) => r.game_id === c.game_id) || {}),
        side: c.side, result: c.result,
      }));
    }
    if (/FROM consensus c JOIN results r/.test(sql)) {
      return t.consensus
        .filter((c) => c.season === args[0] && c.week === args[1] && !c.graded_at)
        .map((c) => ({ ...c, ...(t.results.find((r) => r.game_id === c.game_id) || {}),
                       side: c.side, phase: c.phase, avg_line: c.avg_line }))
        .filter((c) => c.home);
    }
    if (/FROM consensus WHERE phase = 'first' AND season = \?/.test(sql)) {
      return t.consensus.filter((c) => c.phase === "first" && c.season === args[0]);
    }
    if (/FROM consensus WHERE season = \? AND week = \?/.test(sql)) {
      return t.consensus.filter((c) => c.season === args[0] && c.week === args[1]
        && c.phase === "prekick");
    }
    if (/FROM picks p JOIN results r ON r\.game_id = p\.game_id\s+WHERE r\.kickoff > \?/s.test(sql)) {
      return t.picks
        .map((p) => ({ ...p, ...(t.results.find((r) => r.game_id === p.game_id) || {}),
                       side: p.side, line: p.line, kind: p.kind,
                       received_at: p.received_at, model_side: p.model_side }))
        .filter((p) => p.kickoff && p.kickoff > args[0]);
    }
    if (/FROM picks p\s+WHERE p\.season = \? AND p\.week = \? AND p\.line IS NOT NULL/s.test(sql)) {
      return t.picks.filter((p) => p.season === args[0] && p.week === args[1]
        && p.line !== null && p.line !== undefined);
    }
    if (/pragma_table_info/.test(sql)) {
      return (t.consensusColumns || ["game_id", "phase"]).map((name) => ({ name }));
    }
    if (/FROM picks WHERE season = \? AND kind IN/.test(sql)) {
      const by = new Map();
      for (const p of t.picks) {
        if (p.season !== args[0]) continue;
        if (p.kind !== "winner" && p.kind !== "survivor") continue;
        const row = by.get(p.side) || { team: p.side, picked: 0, wins: 0, losses: 0 };
        row.picked += 1;
        if (p.result === "win") row.wins += 1;
        if (p.result === "loss") row.losses += 1;
        by.set(p.side, row);
      }
      return [...by.values()];
    }
    if (/FROM picks WHERE season = \? AND week = \?/.test(sql)) {
      return t.picks.filter((p) => p.season === args[0] && p.week === args[1]);
    }
    return [];
  };
  return {
    tables: t,
    writes,
    prepare(sql) {
      const stmt = {
        sql,
        args: [],
        bind(...a) { stmt.args = a; return stmt; },
        async run() { writes.push({ sql, args: stmt.args }); return { meta: { changes: 1 } }; },
        async all() { return { results: rowsFor(sql, stmt.args) }; },
      };
      return stmt;
    },
    async batch(statements) {
      for (const st of statements) writes.push({ sql: st.sql, args: st.args });
      return statements.map(() => ({ success: true }));
    },
    async exec() { return { count: 1 }; },
  };
}

const TOKEN = "a-long-random-dashboard-token";
const dashEnv = (PICKS_DB) => ({ PICKS_DB, DASHBOARD_TOKEN: TOKEN });
const dashGet = (query = "") => new Request(
  `https://edge.example/v1/dashboard?token=${TOKEN}${query}`);

// This season's current week, so the "default week" test is not pinned to a
// date that will stop being current.
const nowWeek = nflWeek(new Date());

function aGame(over = {}) {
  return { game_id: "g1", season: nowWeek.season, week: nowWeek.week,
           home: "KC", away: "DEN", kickoff: "2099-10-11T17:00:00Z",
           home_score: null, away_score: null, ...over };
}
function aPick(over = {}) {
  return { picker: "a".repeat(16), game_id: "g1", kind: "winner", side: "KC",
           season: nowWeek.season, week: nowWeek.week, line: -3.5, price: -180,
           model_side: null, received_at: "2099-10-10T12:00:00Z",
           result: null, ...over };
}

test("every dashboard view is a 404 without the token", async () => {
  const env = dashEnv(fakeDash());
  for (const url of [
    "https://edge.example/v1/dashboard",
    "https://edge.example/v1/dashboard?token=",
    "https://edge.example/v1/dashboard?token=guess",
    "https://edge.example/v1/dashboard?token=guess&view=board",
    `https://edge.example/v1/dashboard?token=guess&picker=${"a".repeat(16)}`,
  ]) {
    const res = await worker.fetch(new Request(url), env);
    assert.equal(res.status, 404, url);
    assert.equal(await res.text(), "Not found");
  }
  // And with no token configured at all, rather than wide open.
  const unset = await worker.fetch(dashGet(), { PICKS_DB: fakeDash() });
  assert.equal(unset.status, 404);
});

test("this week: two picks read as a count, never as a percentage alone", async () => {
  const db = fakeDash({
    results: [aGame()],
    picks: [aPick({ picker: "a".repeat(16) }),
            aPick({ picker: "b".repeat(16), side: "DEN" })],
  });
  const res = await worker.fetch(dashGet(), dashEnv(db));
  const html = await res.text();
  assert.equal(res.status, 200);
  assert.match(html, /1 of 2/, "the raw count sits beside the share");
  assert.equal(/100% on KC/.test(html), false);
});

test("this week: the weighted split is blank under three proven pickers", async () => {
  // Two proven pickers on a game is not a weighted opinion, and "0%" would
  // read as one.
  const career = [];
  for (const id of ["a", "b"]) {
    for (let i = 0; i < 30; i += 1) {
      career.push(aPick({ picker: id.repeat(16), game_id: `old${id}${i}`,
                          week: 1, result: i < 25 ? "win" : "loss" }));
    }
  }
  const db = fakeDash({
    results: [aGame()],
    picks: [...career,
            aPick({ picker: "a".repeat(16) }),
            aPick({ picker: "b".repeat(16) })],
  });
  const html = await (await worker.fetch(dashGet(), dashEnv(db))).text();
  assert.match(html, /Proven pickers \(n=2\)/);
  // The cell under that heading is an em dash, not a figure.
  const cell = html.split("Proven pickers (n=2)")[1].slice(0, 120);
  assert.match(cell, /—/);
  assert.equal(/\d+%/.test(cell), false, "no percentage on two pickers");
});

test("this week: three proven pickers do get a weighted split", async () => {
  const career = [];
  for (const id of ["a", "b", "c"]) {
    for (let i = 0; i < 30; i += 1) {
      career.push(aPick({ picker: id.repeat(16), game_id: `old${id}${i}`,
                          week: 1, result: i < 25 ? "win" : "loss" }));
    }
  }
  const db = fakeDash({
    results: [aGame()],
    picks: [...career, ...["a", "b", "c"].map((id) => aPick({ picker: id.repeat(16) }))],
  });
  const html = await (await worker.fetch(dashGet(), dashEnv(db))).text();
  assert.match(html, /Proven pickers \(n=3\)/);
  assert.match(html, /3 of 3/);
});

test("this week: a pick that arrived after kickoff is marked late", async () => {
  const db = fakeDash({
    results: [aGame({ kickoff: "2026-10-11T17:00:00Z" })],
    picks: [aPick({ received_at: "2026-10-11T19:30:00Z" })],
  });
  const html = await (await worker.fetch(dashGet(), dashEnv(db))).text();
  assert.match(html, /<span class="late">late<\/span>/);
});

test("this week: a game nobody picked still appears, with zeros", async () => {
  const db = fakeDash({ results: [aGame({ game_id: "lonely", home: "SEA", away: "SF" })] });
  const html = await (await worker.fetch(dashGet(), dashEnv(db))).text();
  assert.match(html, /SF at SEA/);
  assert.match(html, /no picks/);
  assert.match(html, /Proven pickers \(n=0\)/);
});

test("this week: the week defaults to the current one and &week= overrides", async () => {
  // A week that cannot be the current one, whenever this suite is run.
  const other = nowWeek.week + 5;
  const db = fakeDash({
    results: [aGame(), aGame({ game_id: "g9", week: other, home: "BUF", away: "NYJ" })],
  });
  const now = await (await worker.fetch(dashGet(), dashEnv(db))).text();
  assert.match(now, new RegExp(`Week ${nowWeek.week} · ${nowWeek.season}`));
  assert.match(now, /DEN at KC/);

  const later = await (await worker.fetch(dashGet(`&week=${other}`), dashEnv(db))).text();
  assert.match(later, new RegExp(`Week ${other} ·`));
  assert.match(later, /NYJ at BUF/);
  assert.equal(/DEN at KC/.test(later), false, "the default week is not still showing");
});

test("this week: the sample's thinness is on the page", async () => {
  const db = fakeDash({
    results: [aGame()],
    pickers: [{ picker: "a".repeat(16), name: "One" },
              { picker: "b".repeat(16), name: "Two" },
              { picker: "c".repeat(16), name: "Three" }],
    picks: [aPick()],
  });
  const html = await (await worker.fetch(dashGet(), dashEnv(db))).text();
  assert.match(html, /Shared this week/);
  assert.match(html, /Have ever shared/);
  assert.match(html, /Active subscriptions/);
  // One of three, and the subscription count is an em dash rather than a
  // number nobody should trust when Whop cannot be asked.
  assert.match(html, /<div class="value">1<span class="sub"> picker<\/span>/);
  assert.match(html, /<div class="value">3<\/div>/);
});

test("a display name containing markup comes out escaped", async () => {
  const nasty = '<script>alert(1)</script>';
  const db = fakeDash({
    results: [aGame()],
    pickers: [{ picker: "a".repeat(16), name: nasty }],
    picks: [aPick()],
  });
  for (const query of ["", "&view=board", `&picker=${"a".repeat(16)}`]) {
    const html = await (await worker.fetch(dashGet(query), dashEnv(db))).text();
    assert.equal(html.includes(nasty), false, `raw markup survived on ${query}`);
    assert.match(html, /&lt;script&gt;/);
  }
});

test("the dashboard never lets itself be cached", async () => {
  const res = await worker.fetch(dashGet(), dashEnv(fakeDash()));
  assert.equal(res.status, 200);
  assert.match(res.headers.get("cache-control"), /no-store/);
});

// ------------------------------------------------------------ picker view

test("picker: an unknown id is a 404 rather than a crash", async () => {
  const res = await worker.fetch(dashGet(`&picker=${"f".repeat(16)}`),
                                 dashEnv(fakeDash()));
  assert.equal(res.status, 404);
});

test("picker: an id that is not sixteen hex characters is a 404", async () => {
  for (const id of ["", "nope", "ABCDEF0123456789", "0123456789abcde",
                    "0123456789abcdef0", "../../etc"]) {
    const res = await worker.fetch(dashGet(`&picker=${encodeURIComponent(id)}`),
                                   dashEnv(fakeDash()));
    assert.equal(res.status, 404, id);
  }
});

test("picker: a 3-1 week with a push and a late pick adds up", async () => {
  // The late one is counted on its own and stays outside the percentage: it is
  // not a wrong prediction, it is not a prediction.
  const me = "a".repeat(16);
  const games = [];
  const picks = [];
  const outcomes = ["win", "win", "win", "loss", "push", "late"];
  outcomes.forEach((result, i) => {
    games.push(aGame({ game_id: `g${i}`, week: 5, home: `H${i}`, away: `A${i}`,
                       home_score: 20, away_score: 17 }));
    picks.push(aPick({ picker: me, game_id: `g${i}`, week: 5, side: `H${i}`,
                       result }));
  });
  const db = fakeDash({ results: games, picks,
                        pickers: [{ picker: me, name: "Tester" }] });
  const html = await (await worker.fetch(dashGet(`&picker=${me}`), dashEnv(db))).text();

  assert.match(html, /<td>Week 5<\/td>\s*<td class="n">3-1-1<\/td>/);
  assert.match(html, /<td class="n">75\.0%<\/td>/, "the push is outside the rate");
  // Late has its own column and is not in the record.
  assert.match(html, /<td class="n dim">0<\/td>\s*<td class="n dim">1<\/td>/);
});

test("picker: weeks come out newest first whatever order the rows arrive in", async () => {
  const me = "a".repeat(16);
  const weeksOut = [2, 5, 3];
  const db = fakeDash({
    results: weeksOut.map((w) => aGame({ game_id: `g${w}`, week: w,
      home: "KC", away: "DEN", home_score: 27, away_score: 20 })),
    picks: weeksOut.map((w) => aPick({ picker: me, game_id: `g${w}`, week: w,
      side: "KC", result: "win" })),
    pickers: [{ picker: me, name: "Tester" }],
  });
  const html = await (await worker.fetch(dashGet(`&picker=${me}`), dashEnv(db))).text();
  const order = [...html.matchAll(/<td>Week (\d+)<\/td>/g)].map((m) => Number(m[1]));
  assert.deepEqual(order, [5, 3, 2]);
});

test("picker: picking a team and picking against it land in the right columns", async () => {
  const me = "a".repeat(16);
  const db = fakeDash({
    results: [
      aGame({ game_id: "g1", week: 5, home: "KC", away: "DEN",
              home_score: 27, away_score: 20 }),
      aGame({ game_id: "g2", week: 6, home: "DEN", away: "KC",
              home_score: 10, away_score: 31 }),
    ],
    picks: [
      // Took KC, KC won.
      aPick({ picker: me, game_id: "g1", week: 5, side: "KC", result: "win" }),
      // Took KC again, against DEN, and KC won again.
      aPick({ picker: me, game_id: "g2", week: 6, side: "KC", result: "win" }),
    ],
    pickers: [{ picker: me, name: "Tester" }],
  });
  const html = await (await worker.fetch(dashGet(`&picker=${me}`), dashEnv(db))).text();
  const table = html.split('<h2>By team</h2>')[1];
  // KC: picked twice, 2-0 with. DEN: never picked, 0-0 with, 2-0 against.
  assert.match(table, /<td>KC<\/td>\s*<td class="n">2<\/td>\s*<td class="n">2-0<\/td>/);
  assert.match(table, /<td>DEN<\/td>\s*<td class="n">0<\/td>\s*<td class="n">0-0<\/td>/);
  assert.match(table, /<td class="n dim">2-0<\/td>/, "and 2-0 in the against column");
});

test("picker: a game with no result shows as pending, and the skips are counted", async () => {
  const me = "a".repeat(16);
  const db = fakeDash({
    results: [
      aGame({ game_id: "g1", week: 5, home: "KC", away: "DEN",
              home_score: 27, away_score: 20 }),
      aGame({ game_id: "g2", week: 5, home: "SEA", away: "SF" }),  // not played
    ],
    picks: [
      aPick({ picker: me, game_id: "g1", week: 5, side: "KC", result: "win" }),
      aPick({ picker: me, game_id: "g2", week: 5, side: "SEA", result: null }),
    ],
    pickers: [{ picker: me, name: "Tester" }],
  });
  const html = await (await worker.fetch(dashGet(`&picker=${me}`), dashEnv(db))).text();
  assert.match(html, /<span class="dim">pending<\/span>/);
  assert.match(html, /1 pick left out — no result yet/);
});

// ----------------------------------------------------------- leaderboard

test("leaderboard: each name links to that picker's page", async () => {
  const me = "a".repeat(16);
  const db = fakeDash({
    picks: [aPick({ picker: me, result: "win" })],
    pickers: [{ picker: me, name: "Tester" }],
  });
  const html = await (await worker.fetch(dashGet("&view=board"), dashEnv(db))).text();
  assert.match(html, new RegExp(`picker=${me}`));
  assert.match(html, /Tester<\/a>/);
});

test("leaderboard: the crowd gets a row of its own, and so do proven pickers", async () => {
  const db = fakeDash({
    results: [
      aGame({ game_id: "g1", home: "KC", away: "DEN", home_score: 27, away_score: 20 }),
      aGame({ game_id: "g2", home: "SEA", away: "SF", home_score: 10, away_score: 24 }),
    ],
    consensus: [
      { game_id: "g1", phase: "prekick", season: nowWeek.season,
        week: nowWeek.week, side: "KC", proven_side: "KC", model_side: "KC",
        result: "win" },
      { game_id: "g2", phase: "prekick", season: nowWeek.season,
        week: nowWeek.week, side: "SEA", proven_side: "SF", model_side: "SF",
        result: "loss" },
    ],
  });
  const html = await (await worker.fetch(dashGet("&view=board"), dashEnv(db))).text();
  assert.match(html, /<b>The crowd<\/b>/);
  assert.match(html, /<b>Proven pickers only<\/b>/);
  // The crowd went 1-1; the proven subset went 2-0 on the same games.
  const crowdRow = html.split("<b>The crowd</b>")[1].split("</tr>")[0];
  assert.match(crowdRow, /<td class="n">1-1<\/td>/);
  const provenRow = html.split("<b>Proven pickers only</b>")[1].split("</tr>")[0];
  assert.match(provenRow, /<td class="n">2-0<\/td>/);
});

test("leaderboard: by team puts the crowd next to the model", async () => {
  const db = fakeDash({
    results: [aGame({ game_id: "g1", home: "KC", away: "DEN",
                      home_score: 27, away_score: 20 })],
    picks: [aPick({ result: "win" }), aPick({ picker: "b".repeat(16), result: "win" })],
    consensus: [{ game_id: "g1", phase: "prekick", season: nowWeek.season,
                  week: nowWeek.week, side: "KC", proven_side: null,
                  model_side: "KC", result: "win" }],
  });
  const html = await (await worker.fetch(dashGet("&view=board"), dashEnv(db))).text();
  const table = html.split("<h2>By team</h2>")[1];
  assert.match(table, /<td>KC<\/td>\s*<td class="n">2<\/td>\s*<td class="n">2-0<\/td>/);
  assert.match(table, /<td class="n dim">1-0<\/td>/, "the model's record on the same team");
});

// ------------------------------------------------------- consensus capture

test("consensus: a game kicking off within the hour is frozen", async () => {
  const now = new Date("2026-10-11T16:20:00Z");
  const db = fakeDash({
    results: [aGame({ game_id: "soon", kickoff: "2026-10-11T17:00:00Z",
                      season: 2026, week: 5 })],
    picks: [aPick({ game_id: "soon", season: 2026, week: 5, side: "KC",
                    model_side: "KC" }),
            aPick({ picker: "b".repeat(16), game_id: "soon", season: 2026,
                    week: 5, side: "KC" })],
  });
  const out = await captureConsensus({ PICKS_DB: db }, 2026, 5, now);
  assert.equal(out.captured, 1);
  const write = db.writes.find((w) => /INSERT INTO consensus/.test(w.sql));
  assert.ok(write, "a row was written");
  assert.equal(write.args[0], "soon");
  assert.equal(write.args[1], "prekick", "the phase");
  assert.equal(write.args[4], "KC", "the side");
  assert.equal(write.args[5], 2, "on that side");
  assert.equal(write.args[6], 2, "out of that many");
  assert.equal(write.args[10], "KC", "and what the model said");
});

test("consensus: a game five hours out is not frozen yet", async () => {
  const now = new Date("2026-10-11T12:00:00Z");
  const db = fakeDash({
    results: [aGame({ game_id: "later", kickoff: "2026-10-11T17:00:00Z",
                      season: 2026, week: 5 })],
    picks: [aPick({ game_id: "later", season: 2026, week: 5 })],
  });
  const out = await captureConsensus({ PICKS_DB: db }, 2026, 5, now);
  assert.equal(out.captured, 0);
  assert.equal(db.writes.length, 0);
});

test("consensus: a game already under way is too late to freeze", async () => {
  // Whatever is in the table now includes picks made after the ball was
  // kicked, so it is not what the crowd said beforehand.
  const now = new Date("2026-10-11T17:30:00Z");
  const db = fakeDash({
    results: [aGame({ game_id: "gone", kickoff: "2026-10-11T17:00:00Z",
                      season: 2026, week: 5 })],
    picks: [aPick({ game_id: "gone", season: 2026, week: 5 })],
  });
  const out = await captureConsensus({ PICKS_DB: db }, 2026, 5, now);
  assert.equal(out.captured, 0);
  assert.equal(db.writes.length, 0);
});

test("consensus: a second run does not overwrite what is already frozen", async () => {
  const now = new Date("2026-10-11T16:20:00Z");
  const db = fakeDash({
    results: [aGame({ game_id: "soon", kickoff: "2026-10-11T17:00:00Z",
                      season: 2026, week: 5 })],
    picks: [aPick({ game_id: "soon", season: 2026, week: 5 })],
    consensus: [{ game_id: "soon", phase: "prekick", season: 2026, week: 5,
                  side: "DEN", picks: 1, total_picks: 1 }],
  });
  const out = await captureConsensus({ PICKS_DB: db }, 2026, 5, now);
  assert.equal(out.captured, 0);
  assert.equal(out.skipped, 1);
  assert.equal(db.writes.length, 0, "the earlier capture stands");
});

test("consensus: the crowd's record grades, pushes included", async () => {
  const db = fakeDash({
    results: [
      aGame({ game_id: "g1", season: 2026, week: 5, home: "KC", away: "DEN",
              home_score: 27, away_score: 20 }),
      aGame({ game_id: "g2", season: 2026, week: 5, home: "SEA", away: "SF",
              home_score: 17, away_score: 24 }),
      aGame({ game_id: "g3", season: 2026, week: 5, home: "BUF", away: "NYJ",
              home_score: 20, away_score: 20 }),
    ],
    consensus: [
      { game_id: "g1", phase: "prekick", season: 2026, week: 5, side: "KC" },
      { game_id: "g2", phase: "prekick", season: 2026, week: 5, side: "SEA" },
      { game_id: "g3", phase: "prekick", season: 2026, week: 5, side: "BUF" },
    ],
  });
  const out = await gradeCrowd({ PICKS_DB: db }, 2026, 5);
  assert.equal(out.graded, 3);
  const verdicts = db.writes
    .filter((w) => /UPDATE consensus SET result/.test(w.sql))
    .map((w) => [w.args[2], w.args[0]]);
  assert.deepEqual(verdicts.sort(),
                   [["g1", "win"], ["g2", "loss"], ["g3", "push"]]);
});

test("consensus: a game nobody picked is not graded as a loss", async () => {
  // Silence is not a wrong answer.
  const db = fakeDash({
    results: [aGame({ game_id: "g1", season: 2026, week: 5, home: "KC",
                      away: "DEN", home_score: 27, away_score: 20 })],
    consensus: [{ game_id: "g1", phase: "prekick", season: 2026, week: 5,
                  side: null }],
  });
  const out = await gradeCrowd({ PICKS_DB: db }, 2026, 5);
  assert.equal(out.graded, 0);
});

test("no dashboard page ever carries a licence key", async () => {
  // The tables do not hold one, so this is really a test that no view has
  // started reaching for somewhere that does.
  const me = "a".repeat(16);
  const key = "EDGE-SECRET-KEY-9999";
  const db = fakeDash({
    results: [aGame({ home_score: 27, away_score: 20 })],
    picks: [aPick({ picker: me, result: "win" })],
    pickers: [{ picker: me, name: "Tester" }],
    consensus: [{ game_id: "g1", phase: "prekick", season: nowWeek.season,
                  week: nowWeek.week, side: "KC", model_side: "KC",
                  result: "win" }],
  });
  for (const query of ["", "&view=board", `&picker=${me}`]) {
    const html = await (await worker.fetch(dashGet(query), dashEnv(db))).text();
    assert.equal(html.includes(key), false);
    assert.equal(/license_key/i.test(html), false);
  }
});

// ------------------------------------------------- the first look, and CLV
//
// Two phases per game, and the closing-line figure that only the early one can
// carry. The record and the number are separate facts: the crowd can be right
// at a bad price and wrong at a good one, and the whole reason for measuring
// both is that the second is the one that predicts the first.

const DAY = 24 * 3600 * 1000;
const iso = (ms) => new Date(ms).toISOString();

test("a game picked five days out is frozen on that run", async () => {
  const now = new Date("2026-10-06T12:00:00Z");
  const db = fakeDash({
    results: [aGame({ game_id: "early", season: 2026, week: 5,
                      kickoff: iso(now.getTime() + 5 * DAY) })],
    picks: [aPick({ game_id: "early", season: 2026, week: 5, side: "KC",
                    line: -3.5, model_side: "KC" }),
            aPick({ picker: "b".repeat(16), game_id: "early", season: 2026,
                    week: 5, side: "KC", line: -2.5 })],
  });
  const out = await captureFirst({ PICKS_DB: db }, 2026, now);
  assert.equal(out.captured, 1);

  const write = db.writes.find((w) => /INSERT INTO consensus/.test(w.sql));
  assert.equal(write.args[0], "early");
  assert.equal(write.args[1], "first", "the phase");
  assert.equal(write.args[4], "KC", "the side");
  assert.equal(write.args[5], 2);
  assert.equal(write.args[9], -3, "the early number, averaged");
});

test("a later run does not overwrite the first look", async () => {
  // The early number is the whole point of the row; a second capture would be
  // a later number wearing its name.
  const now = new Date("2026-10-08T12:00:00Z");
  const db = fakeDash({
    results: [aGame({ game_id: "early", season: 2026, week: 5,
                      kickoff: iso(now.getTime() + 3 * DAY) })],
    picks: [aPick({ game_id: "early", season: 2026, week: 5, line: -7 })],
    consensus: [{ game_id: "early", phase: "first", season: 2026, week: 5,
                  side: "KC", avg_line: -3 }],
  });
  const out = await captureFirst({ PICKS_DB: db }, 2026, now);
  assert.equal(out.captured, 0);
  assert.equal(db.writes.length, 0);
});

test("a game with no picks yet gets no first row", async () => {
  const now = new Date("2026-10-06T12:00:00Z");
  const db = fakeDash({
    results: [aGame({ game_id: "quiet", season: 2026, week: 5,
                      kickoff: iso(now.getTime() + 5 * DAY) })],
  });
  assert.equal((await captureFirst({ PICKS_DB: db }, 2026, now)).captured, 0);
});

test("the same game still gets its own prekick row near kickoff", async () => {
  // Both rows coexist: one says what the crowd first thought, the other what
  // it went in with.
  const kickoff = "2026-10-11T17:00:00Z";
  const near = new Date("2026-10-11T16:20:00Z");
  const db = fakeDash({
    results: [aGame({ game_id: "early", season: 2026, week: 5, kickoff })],
    picks: [aPick({ game_id: "early", season: 2026, week: 5, side: "KC",
                    line: -6 })],
    // The first row is already on file from five days ago.
    consensus: [{ game_id: "early", phase: "first", season: 2026, week: 5,
                  side: "KC", avg_line: -3 }],
  });
  const out = await captureConsensus({ PICKS_DB: db }, 2026, 5, near);
  assert.equal(out.captured, 1, "the first row does not block the prekick one");
  const write = db.writes.find((w) => /INSERT INTO consensus/.test(w.sql));
  assert.equal(write.args[1], "prekick");
  assert.equal(write.args[9], -6, "and it carries the late number");
});

test("CLV is positive when the crowd beat the close, on either side", () => {
  // Lines are the home team's spread throughout, so the sign has to be read
  // through the side the crowd was on.
  //
  // Favourite: took KC laying 3.5, the game closed laying 7. Three points in
  // hand, so positive.
  assert.equal(crowdClv("KC", "KC", -3.5, -7), 3.5);
  // The same favourite the other way round: laid 7, closed at 3.5. Worse.
  assert.equal(crowdClv("KC", "KC", -7, -3.5), -3.5);
  // Underdog: took DEN getting 3.5 (home -3.5), closed getting 7 (home -7).
  // The dog got longer without them, so they got the worse of it.
  assert.equal(crowdClv("DEN", "KC", -3.5, -7), -3.5);
  // And the dog that shortened: took DEN +7, closed +3.5. Better.
  assert.equal(crowdClv("DEN", "KC", -7, -3.5), 3.5);
  // No movement is no value, which is zero rather than blank.
  assert.equal(crowdClv("KC", "KC", -3, -3), 0);
});

test("CLV is blank, not zero, when there is nothing to measure against", () => {
  assert.equal(crowdClv("KC", "KC", -3.5, null), null);
  assert.equal(crowdClv("KC", "KC", null, -3.5), null);
  assert.equal(crowdClv(null, "KC", -3.5, -7), null, "a game nobody picked");
  assert.equal(crowdClv("KC", "KC", -3.5, undefined), null);
});

test("grading writes the closing line and the value onto the first row", async () => {
  const kickoff = "2026-10-11T17:00:00Z";
  const db = fakeDash({
    results: [aGame({ game_id: "g1", season: 2026, week: 5, kickoff,
                      home: "KC", away: "DEN",
                      home_score: 27, away_score: 20 })],
    picks: [
      // The early number, and the last one before kickoff.
      aPick({ game_id: "g1", season: 2026, week: 5, line: -3,
              received_at: "2026-10-06T12:00:00Z" }),
      aPick({ picker: "b".repeat(16), game_id: "g1", season: 2026, week: 5,
              line: -7, received_at: "2026-10-11T16:50:00Z" }),
      // And one that landed after the ball was kicked, which is not a close.
      aPick({ picker: "c".repeat(16), game_id: "g1", season: 2026, week: 5,
              line: -13, received_at: "2026-10-11T19:00:00Z" }),
    ],
    consensus: [{ game_id: "g1", phase: "first", season: 2026, week: 5,
                  side: "KC", proven_side: "KC", avg_line: -3 }],
  });
  const out = await gradeCrowd({ PICKS_DB: db }, 2026, 5);
  assert.equal(out.valued, 1);

  const write = db.writes.find((w) => /SET close_line/.test(w.sql));
  assert.equal(write.args[0], -7, "the last line before kickoff, not after");
  assert.equal(write.args[1], 4, "KC at -3 against a -7 close is four points");
  assert.equal(write.args[2], 4, "and the proven side agreed");
  assert.match(write.sql, /phase = 'first'/);
});

test("a right pick at a worse number is a win with negative CLV", async () => {
  // The record and the number are independent, which is the point of having
  // both: a crowd that is right at bad prices is not a crowd to follow.
  const kickoff = "2026-10-11T17:00:00Z";
  const db = fakeDash({
    results: [aGame({ game_id: "g1", season: 2026, week: 5, kickoff,
                      home: "KC", away: "DEN",
                      home_score: 27, away_score: 20 })],
    picks: [aPick({ game_id: "g1", season: 2026, week: 5, line: -3,
                    received_at: "2026-10-11T16:00:00Z" })],
    consensus: [
      // Took KC laying 9 early; it closed at 3. Right team, bad number.
      { game_id: "g1", phase: "first", season: 2026, week: 5, side: "KC",
        proven_side: "KC", avg_line: -9 },
      { game_id: "g1", phase: "prekick", season: 2026, week: 5, side: "KC" },
    ],
  });
  const out = await gradeCrowd({ PICKS_DB: db }, 2026, 5);
  assert.equal(out.graded, 1, "the prekick row got the verdict");
  assert.equal(out.valued, 1, "and the first row got the number");

  const verdict = db.writes.find((w) => /SET result/.test(w.sql));
  assert.equal(verdict.args[0], "win");
  const value = db.writes.find((w) => /SET close_line/.test(w.sql));
  assert.equal(value.args[1], -6, "nine laid against a three close is six lost");
});

test("a first row with no closing line is finished, not rescanned for ever", async () => {
  const db = fakeDash({
    results: [aGame({ game_id: "g1", season: 2026, week: 5,
                      kickoff: "2026-10-11T17:00:00Z",
                      home: "KC", away: "DEN", home_score: 27, away_score: 20 })],
    // Nobody's pick carried a line, so there is no close to measure against.
    picks: [aPick({ game_id: "g1", season: 2026, week: 5, line: null })],
    consensus: [{ game_id: "g1", phase: "first", season: 2026, week: 5,
                  side: "KC", avg_line: null }],
  });
  const out = await gradeCrowd({ PICKS_DB: db }, 2026, 5);
  assert.equal(out.valued, 0);
  const write = db.writes.find((w) => /SET close_line/.test(w.sql));
  assert.equal(write.args[1], null, "blank");
  assert.ok(write.args[3], "but graded_at is set, so it is done");
});

test("the leaderboard shows the crowd's average CLV beside its record", async () => {
  const db = fakeDash({
    results: [
      aGame({ game_id: "g1", home: "KC", away: "DEN", home_score: 27, away_score: 20 }),
      aGame({ game_id: "g2", home: "SEA", away: "SF", home_score: 10, away_score: 24 }),
    ],
    consensus: [
      { game_id: "g1", phase: "prekick", season: nowWeek.season,
        week: nowWeek.week, side: "KC", proven_side: "KC", result: "win" },
      { game_id: "g2", phase: "prekick", season: nowWeek.season,
        week: nowWeek.week, side: "SEA", proven_side: "SEA", result: "loss" },
      { game_id: "g1", phase: "first", season: nowWeek.season,
        week: nowWeek.week, side: "KC", clv: 3, proven_clv: 3 },
      { game_id: "g2", phase: "first", season: nowWeek.season,
        week: nowWeek.week, side: "SEA", clv: -1, proven_clv: -1 },
    ],
  });
  const html = await (await worker.fetch(dashGet("&view=board"), dashEnv(db))).text();
  const crowdRow = html.split("<b>The crowd</b>")[1].split("</tr>")[0];
  assert.match(crowdRow, /1-1/, "the record comes off the prekick rows");
  assert.match(crowdRow, /\+1\.00/, "and the value off the first ones");
  assert.match(crowdRow, /of 2/, "with how many it is an average of");
  // Counting the two phases together would have made this 2-2.
  assert.equal(/2-2/.test(crowdRow), false);
});

test("the leaderboard leaves CLV blank rather than calling an unknown zero", async () => {
  const db = fakeDash({
    results: [aGame({ game_id: "g1", home: "KC", away: "DEN",
                      home_score: 27, away_score: 20 })],
    consensus: [
      { game_id: "g1", phase: "prekick", season: nowWeek.season,
        week: nowWeek.week, side: "KC", result: "win" },
      { game_id: "g1", phase: "first", season: nowWeek.season,
        week: nowWeek.week, side: "KC", clv: null, proven_clv: null },
    ],
  });
  const html = await (await worker.fetch(dashGet("&view=board"), dashEnv(db))).text();
  const crowdRow = html.split("<b>The crowd</b>")[1].split("</tr>")[0];
  assert.match(crowdRow, /<td class="n dim">—<\/td>/);
  assert.equal(/0\.00/.test(crowdRow), false, "zero would be a claim");
});

test("a fresh database reaches the new shape through ensureSchema alone", async () => {
  const run = [];
  const fresh = {
    async exec(sql) { run.push(sql); return { count: 1 }; },
    prepare(sql) {
      return { sql, bind: () => ({ async all() { return { results: [] }; } }) };
    },
  };
  assert.equal(await ensureSchema({ PICKS_DB: fresh }), true);
  const ddl = run.find((sql) => /CREATE TABLE IF NOT EXISTS consensus/.test(sql));
  assert.ok(ddl, "the table is created");
  assert.match(ddl, /PRIMARY KEY \(game_id, phase\)/);
  for (const column of ["phase", "close_line", "clv", "proven_clv"]) {
    assert.match(ddl, new RegExp(`\\b${column}\\b`), `${column} is in the DDL`);
  }
  assert.ok(run.some((sql) => /ALTER TABLE picks ADD COLUMN model_side/.test(sql)));
});

test("the old table shape is reported rather than silently left alone", async () => {
  // CREATE TABLE IF NOT EXISTS cannot migrate a table that already exists, and
  // a no-op is how somebody finds out three weeks later with a season missing.
  const logged = [];
  const realLog = console.log;
  console.log = (...args) => logged.push(args.join(" "));
  let ok;
  try {
    const old = {
      async exec() { return { count: 1 }; },
      prepare(sql) {
        return { sql, bind: () => ({ async all() { return { results: [] }; } }),
                 async all() {
                   return { results: [{ name: "game_id" }, { name: "side" }] };
                 } };
      },
    };
    ok = await ensureSchema({ PICKS_DB: old });
  } finally {
    console.log = realLog;
  }
  assert.equal(ok, false, "and it says it did not get there");
  assert.ok(logged.some((l) => /old single-row shape/.test(l)));
  assert.ok(logged.some((l) => /README/.test(l)), "pointing at the fix");
});

test("the leaderboard says so rather than 500ing before the migration", async () => {
  const db = fakeDash();
  db.prepare = (sql) => {
    if (/FROM consensus/.test(sql)) {
      return { sql, bind: () => ({ async all() {
        throw new Error("no such column: c.phase");
      } }) };
    }
    return { sql, bind: () => ({ async all() { return { results: [] }; } }),
             async all() { return { results: [] }; } };
  };
  const realLog = console.log;
  console.log = () => {};
  let res;
  try {
    res = await worker.fetch(dashGet("&view=board"), dashEnv(db));
  } finally {
    console.log = realLog;
  }
  assert.equal(res.status, 200, "the page still renders");
  assert.match(await res.text(), /still needs its migration/);
});


// --------------------------------------------------------------- health

test("health names the missing key rather than just failing", async () => {
  const out = await health({});
  assert.equal(out.ok, false);
  assert.equal(out.whop_api_key, false);
  assert.equal(out.whop, null);
  assert.match(out.notes.join(" "), /WHOP_API_KEY is not set/);
});

test("health tells a rejected key apart from a working one", async () => {
  fakeWhop(null, { status: 401 });
  const bad = await health({ WHOP_API_KEY: "k" });
  assert.equal(bad.ok, false);
  assert.deepEqual(bad.whop, { reachable: true, status: 401 });
  assert.match(bad.notes.join(" "), /rejected the API key \(401\)/);

  // 404 for a membership id that cannot exist is the *good* answer: the key
  // was accepted and the id simply is not there.
  fakeWhop(null, { status: 404 });
  const good = await health({ WHOP_API_KEY: "k" });
  assert.equal(good.ok, true);
  assert.deepEqual(good.whop, { reachable: true, status: 404 });
});

test("health never reports a secret's value", async () => {
  fakeWhop(null, { status: 404 });
  const out = await health({
    WHOP_API_KEY: "sk_live_do_not_print_me", DASHBOARD_TOKEN: "tok_secret",
    RESEND_API_KEY: "re_secret", SUPPORT_EMAIL_TO: "me@example.com",
  });
  const text = JSON.stringify(out);
  for (const secret of ["sk_live_do_not_print_me", "tok_secret", "re_secret",
                        "me@example.com"]) {
    assert.ok(!text.includes(secret), `health leaked ${secret}`);
  }
});

test("health is not readable without the dashboard token", async () => {
  const e = { DASHBOARD_TOKEN: "tok", WHOP_API_KEY: "k" };
  const miss = await worker.fetch(new Request("https://x/v1/health"), e);
  assert.equal(miss.status, 404);
  const wrong = await worker.fetch(new Request("https://x/v1/health?token=nope"), e);
  assert.equal(wrong.status, 404);

  fakeWhop(null, { status: 404 });
  const ok = await worker.fetch(new Request("https://x/v1/health?token=tok"), e);
  assert.equal(ok.status, 200);
  assert.equal((await ok.json()).ok, true);
});


// ------------------------------------------------- the scoreboard's refusals

// console.log, captured. The Worker says what went wrong by logging it, so
// the log line is the behaviour under test, not a side effect of it.
async function capturingLogs(fn) {
  const lines = [];
  const real = console.log;
  console.log = (...args) => lines.push(args.map(String).join(" "));
  try {
    return { value: await fn(), lines };
  } finally {
    console.log = real;
  }
}

const A_403 = () => new Response(
  "<html><head><title>Access Denied</title></head><body>You don't have "
  + "permission to access this resource on this server.</body></html>",
  { status: 403, headers: { "content-type": "text/html" } });

test("the scoreboard request carries the headers the app sends", async () => {
  let seen = null;
  const espn = async (url, init) => {
    seen = { url: String(url), init };
    return new Response(JSON.stringify({ events: [] }), { status: 200 });
  };
  await gradeWeek({ PICKS_DB: fakeD1() }, 2026, 5, espn);

  assert.ok(seen, "the fetcher was called");
  const headers = seen.init.headers;
  // Verbatim from nflpicker/config.py -- the same identity, not a second one.
  assert.equal(headers["User-Agent"],
               "nflpicker/0.1 (+https://github.com/Ljbutton/ESPNpicem)");
  assert.equal(headers.Accept, "application/json");
  assert.equal(headers["Accept-Language"], "en-US,en;q=0.9");
  assert.ok(!("Referer" in headers), "no header that claims to be a browser");
});

test("a different User-Agent can be set without a code change", () => {
  const headers = scoreboardHeaders({ SCOREBOARD_USER_AGENT: "Mozilla/5.0 (test)" });
  assert.equal(headers["User-Agent"], "Mozilla/5.0 (test)");
  assert.equal(headers.Accept, "application/json", "the rest is unchanged");
});

test("a 403 logs the status and the body, and does not throw", async () => {
  const { value: out, lines } = await capturingLogs(
    () => gradeWeek({ PICKS_DB: fakeD1() }, 2026, 5, A_403));

  assert.equal(out.ok, false, "the week reports that it did not run");
  assert.equal(out.status, 403);
  assert.equal(out.graded, 0);

  const refusal = lines.find((l) => l.includes("scoreboard refused"));
  assert.ok(refusal, `no refusal logged; got ${JSON.stringify(lines)}`);
  assert.match(refusal, /"status":403/);
  assert.match(refusal, /"season":2026/);
  assert.match(refusal, /"week":5/);
  // The part the old `espn 403` never carried: what ESPN actually said.
  assert.match(refusal, /Access Denied/);
  assert.match(refusal, /permission to access this resource/);
});

test("a refusal with an unreadable body still logs the status", async () => {
  const broken = async () => ({
    ok: false, status: 429,
    text: async () => { throw new Error("stream already consumed"); },
  });
  const { value: out, lines } = await capturingLogs(
    () => gradeWeek({ PICKS_DB: fakeD1() }, 2026, 5, broken));
  assert.equal(out.ok, false);
  assert.equal(out.status, 429);
  assert.match(lines.find((l) => l.includes("scoreboard refused")), /"status":429/);
});

test("a long body is cut down rather than logged whole", async () => {
  const huge = async () => new Response("x".repeat(5000), { status: 403 });
  const { lines } = await capturingLogs(
    () => gradeWeek({ PICKS_DB: fakeD1() }, 2026, 5, huge));
  const body = JSON.parse(lines.find((l) => l.includes("scoreboard refused"))
    .replace("picks: scoreboard refused ", "")).body;
  assert.equal(body.length, 300);
});

// --------------------------------------------------- coming back to a week

test("grading resumes on the next run after a failure", async () => {
  // Week 5 failed two runs ago; the season has moved on to week 8, so the
  // old [week, week - 1] pair would never look at it again.
  const db = fakeD1();
  db.rows.backlog = [{ season: 2026, week: 5 }];
  const weeks = await weeksToGrade({ PICKS_DB: db }, 2026, 8);

  assert.deepEqual(weeks, [
    { season: 2026, week: 8 },
    { season: 2026, week: 7 },
    { season: 2026, week: 5 },
  ]);
});

test("a week that graded cleanly is not queued again", async () => {
  const db = fakeD1();
  db.rows.backlog = [];                       // nothing left ungraded
  assert.deepEqual(await weeksToGrade({ PICKS_DB: db }, 2026, 8),
                   [{ season: 2026, week: 8 }, { season: 2026, week: 7 }]);
});

test("the backlog asks only for weeks that have been played", async () => {
  const db = fakeD1();
  await weeksToGrade({ PICKS_DB: db }, 2026, 8);
  const q = db.calls.find((c) => /DISTINCT season, week FROM picks/.test(c.sql));
  assert.ok(q, "the backlog was read");
  assert.match(q.sql, /result IS NULL/);
  // A pick made a fortnight early is ungraded for a good reason, and its week
  // is not worth a request yet.
  assert.match(q.sql, /week <= \?/);
  assert.deepEqual(q.args, [2026, 2026, 8, 6]);
});

test("the backlog cannot make one run unbounded", async () => {
  const db = fakeD1();
  db.rows.backlog = Array.from({ length: 30 }, (_, i) => ({ season: 2026, week: i + 1 }));
  const weeks = await weeksToGrade({ PICKS_DB: db }, 2026, 18);
  assert.equal(weeks.length, 6);
  assert.deepEqual(weeks[0], { season: 2026, week: 18 }, "this week is never crowded out");
});

test("a database that will not answer still grades this week and last", async () => {
  const db = fakeD1();
  db.prepare = () => { throw new Error("D1 unavailable"); };
  const { value: weeks } = await capturingLogs(
    () => weeksToGrade({ PICKS_DB: db }, 2026, 8));
  assert.deepEqual(weeks, [{ season: 2026, week: 8 }, { season: 2026, week: 7 }]);
});

test("week 1 has no week 0 behind it", async () => {
  const db = fakeD1();
  assert.deepEqual(await weeksToGrade({ PICKS_DB: db }, 2026, 1),
                   [{ season: 2026, week: 1 }]);
});

test("one refused week does not take the other weeks down with it", async () => {
  // The run must still grade what it can: a 403 on one week is not a reason
  // to leave the rest of the season alone.
  const db = fakeD1();
  db.rows.backlog = [{ season: 2026, week: 5 }];
  const asked = [];
  const espn = async (url) => {
    const week = Number(new URL(url).searchParams.get("week"));
    asked.push(week);
    if (week === 8) return A_403();
    return new Response(JSON.stringify({ events: [] }), { status: 200 });
  };

  const { lines } = await capturingLogs(async () => {
    for (const { season, week } of await weeksToGrade({ PICKS_DB: db }, 2026, 8)) {
      const out = await gradeWeek({ PICKS_DB: db }, season, week, espn);
      if (!out.ok) console.log("picks: week not graded, will retry", JSON.stringify(out));
      else console.log("picks: graded", JSON.stringify(out));
    }
  });

  assert.deepEqual(asked, [8, 7, 5], "every week was attempted");
  assert.equal(lines.filter((l) => l.includes("will retry")).length, 1);
  assert.equal(lines.filter((l) => l.includes("picks: graded")).length, 2);
});

test("the scheduled run survives a scoreboard that refuses everything", async () => {
  const db = fakeD1();
  db.rows.backlog = [{ season: 2026, week: 5 }];
  const realFetch = globalThis.fetch;
  globalThis.fetch = A_403;
  try {
    const waits = [];
    const { lines } = await capturingLogs(async () => {
      await worker.scheduled({}, { PICKS_DB: db }, { waitUntil: (p) => waits.push(p) });
      await Promise.all(waits);               // rejects here if the run threw
    });
    assert.ok(lines.some((l) => l.includes("scoreboard refused")),
              "the run said why, not just that it failed");
    assert.ok(lines.some((l) => l.includes("will retry")),
              "and that the week is coming back");
  } finally {
    globalThis.fetch = realFetch;
  }
});


// -------------------------------------------------- results, reported by the app

const FINAL = {
  game_id: "401", season: 2026, week: 5, kickoff: "2026-10-11T17:00:00Z",
  home: "KC", away: "DEN", home_score: 27, away_score: 20,
};

function resultsPost(body) {
  return new Request("https://edge.example/v1/results", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

test("a reported score needs a live subscription, like a pick", async () => {
  const db = fakeD1();
  const key = "KEY-RESULTS-0001";
  // No key at all.
  const bare = await worker.fetch(resultsPost({
    picker: await pickerIdFor(key), results: [FINAL],
  }), picksEnv(db));
  assert.equal(bare.status, 403);
  assert.equal((await bare.json()).reason, "unlicensed");

  // A key, but an id that is not the one it hashes to -- which is what stops
  // one subscription reaching a quorum by itself.
  fakeWhop({ ...live });
  const wrong = await worker.fetch(resultsPost({
    picker: "aaaaaaaaaaaaaaaa", license_key: key, results: [FINAL],
  }), picksEnv(db));
  assert.equal(wrong.status, 403);
  assert.equal((await wrong.json()).reason, "picker_mismatch");
  assert.equal(db.calls.length, 0, "nothing was written");
});

test("a lapsed subscription cannot report", async () => {
  const key = "KEY-RESULTS-0002";
  fakeWhop({ ...base, status: "canceled" });
  const res = await worker.fetch(resultsPost({
    picker: await pickerIdFor(key), license_key: key, results: [FINAL],
  }), picksEnv(fakeD1()));
  assert.equal(res.status, 403);
});

test("a report is stored against the picker who sent it", async () => {
  const db = fakeD1();
  const key = "KEY-RESULTS-0003";
  const picker = await pickerIdFor(key);
  fakeWhop({ ...live });
  const res = await worker.fetch(resultsPost({
    picker, license_key: key, results: [FINAL],
  }), picksEnv(db));

  assert.equal(res.status, 200);
  const out = await res.json();
  assert.equal(out.ok, true);
  assert.equal(out.reported, 1);
  const insert = db.calls.find((c) => /INSERT INTO result_reports/.test(c.sql));
  assert.ok(insert, "the report was written");
  assert.equal(insert.args[0], "401");
  assert.equal(insert.args[1], picker, "under their own id, not one they chose");
  // One row per picker per game: reporting twice replaces, never adds.
  assert.match(insert.sql, /ON CONFLICT\(game_id, picker\) DO UPDATE/);
});

test("nothing becomes a result until enough pickers agree", async () => {
  const db = fakeD1();
  db.rows.agreed = [];                        // the HAVING found no quorum
  const written = await promoteResults(db.prepare ? { PICKS_DB: db } : null,
                                       ["401"], "2026-10-11T22:00:00Z");
  assert.equal(written, 0);
  assert.ok(!db.calls.some((c) => /INSERT INTO results/.test(c.sql)),
            "no score was promoted on one person's word");
});

test("a quorum is counted per score, so disagreement is not agreement", async () => {
  const db = fakeD1();
  await promoteResults({ PICKS_DB: db }, ["401"], "2026-10-11T22:00:00Z");
  const q = db.calls.find((c) => /FROM result_reports/.test(c.sql));
  // Grouping by the score is what makes two people saying 27-20 and two
  // saying 28-20 four reports and no quorum.
  assert.match(q.sql, /GROUP BY game_id, kickoff, home, away, home_score, away_score/);
  assert.match(q.sql, /HAVING reporters >= \?/);
  assert.equal(q.args[q.args.length - 1], RESULTS_QUORUM);
});

test("an agreed score is written, and never over one ESPN gave us", async () => {
  const db = fakeD1();
  db.rows.agreed = [{ ...FINAL, reporters: 3 }];
  const written = await promoteResults({ PICKS_DB: db }, ["401"],
                                       "2026-10-11T22:00:00Z");
  assert.equal(written, 1);
  const insert = db.calls.find((c) => /INSERT INTO results/.test(c.sql));
  assert.match(insert.sql, /'crowd'/);
  // The clause that keeps a vote from overwriting a fact.
  assert.match(insert.sql, /WHERE results\.home_score IS NULL/);
});

test("a refused week still grades from scores that were reported", async () => {
  // The whole point of the fallback: ESPN says no, and the week grades anyway.
  const db = fakeD1();
  db.rows.results = [FINAL];                  // promoted from agreeing reports
  db.rows.picks = [
    { picker: A_PICKER, game_id: "401", kind: "winner", side: "KC",
      received_at: "2026-10-11T12:00:00Z" },
  ];
  const { value: out } = await capturingLogs(
    () => gradeWeek({ PICKS_DB: db }, 2026, 5, A_403));

  assert.equal(out.ok, false, "the refusal is still reported");
  assert.equal(out.status, 403);
  assert.equal(out.known, 1, "but a score was known anyway");
  assert.equal(out.graded, 1, "and the pick was graded");
  const update = db.calls.find((c) => /UPDATE picks SET result/.test(c.sql));
  assert.equal(update.args[0], "win", "KC won 27-20");
});

test("a refused week with nothing reported grades nothing and says so", async () => {
  const db = fakeD1();
  db.rows.picks = [
    { picker: A_PICKER, game_id: "401", kind: "winner", side: "KC",
      received_at: "2026-10-11T12:00:00Z" },
  ];
  const { value: out } = await capturingLogs(
    () => gradeWeek({ PICKS_DB: db }, 2026, 5, A_403));
  assert.equal(out.ok, false);
  assert.equal(out.known, 0);
  assert.equal(out.graded, 0);
});

test("ESPN wins where it has an answer", async () => {
  const db = fakeD1();
  db.rows.results = [{ ...FINAL, home_score: 3, away_score: 0 }];   // a bad vote
  db.rows.picks = [
    { picker: A_PICKER, game_id: "401", kind: "winner", side: "DEN",
      received_at: "2026-10-11T12:00:00Z" },
  ];
  const espn = async () => new Response(JSON.stringify({
    events: [{
      id: "401", date: "2026-10-11T17:00:00Z",
      competitions: [{
        status: { type: { completed: true } },
        competitors: [
          { homeAway: "home", score: "20", team: { abbreviation: "KC" } },
          { homeAway: "away", score: "27", team: { abbreviation: "DEN" } },
        ],
      }],
    }],
  }), { status: 200 });

  const out = await gradeWeek({ PICKS_DB: db }, 2026, 5, espn);
  assert.equal(out.ok, true);
  const update = db.calls.find((c) => /UPDATE picks SET result/.test(c.sql));
  assert.equal(update.args[0], "win", "DEN won 27-20 by ESPN, whatever the vote said");
  const insert = db.calls.find((c) => /INSERT INTO results/.test(c.sql));
  assert.match(insert.sql, /'espn'/);
});

// ------------------------------------------------- what a report may contain

test("a report that is not a finished game is dropped", () => {
  assert.equal(cleanResult({ ...FINAL, home_score: null }), null, "no score");
  assert.equal(cleanResult({ ...FINAL, home_score: "" }), null);
  assert.equal(cleanResult({ ...FINAL, home_score: -1 }), null);
  assert.equal(cleanResult({ ...FINAL, home_score: 1e9 }), null);
  assert.equal(cleanResult({ ...FINAL, home_score: 20.5 }), null);
  assert.equal(cleanResult({ ...FINAL, home: "KC", away: "KC" }), null);
  assert.equal(cleanResult({ ...FINAL, home: "" }), null);
  assert.equal(cleanResult({ ...FINAL, game_id: "../../etc" }), null);
  assert.equal(cleanResult({ ...FINAL, week: 99 }), null);
  assert.equal(cleanResult({ ...FINAL, season: 1900 }), null);
  assert.equal(cleanResult(null), null);
});

test("a kickoff in the future is dropped, and the report is kept", () => {
  // A game cannot be final before it starts. Dropping the kickoff rather than
  // the whole report is deliberate: a missing kickoff only skips the late-pick
  // check, and grading a late pick beats voiding an honest one on a timestamp
  // a stranger chose.
  const ahead = cleanResult({ ...FINAL, kickoff: "2099-01-01T00:00:00Z" });
  assert.equal(ahead.kickoff, null);
  assert.equal(ahead.home_score, 27, "the score is still there");
  assert.equal(cleanResult({ ...FINAL, kickoff: "not a date" }).kickoff, null);
  assert.equal(cleanResult(FINAL, Date.parse("2026-10-12T00:00:00Z")).kickoff,
               "2026-10-11T17:00:00Z", "a kickoff in the past is kept");
});

test("a batch is capped and an empty one is refused", async () => {
  const db = fakeD1();
  const key = "KEY-RESULTS-0004";
  fakeWhop({ ...live });
  const empty = await worker.fetch(resultsPost({
    picker: await pickerIdFor(key), license_key: key, results: [],
  }), picksEnv(db));
  assert.equal(empty.status, 400);
  assert.equal((await empty.json()).reason, "empty");

  const many = Array.from({ length: 500 }, (_, i) => ({ ...FINAL, game_id: `g${i}` }));
  const res = await worker.fetch(resultsPost({
    picker: await pickerIdFor(key), license_key: key, results: many,
  }), picksEnv(db));
  assert.equal((await res.json()).reported, 64);
});


// ------------------------------------------------------ a settable quorum
//
// Three is right for a customer base and impossible for one person, and a
// fallback that cannot fire is not a fallback.

test("the quorum comes from the environment when it is set", () => {
  assert.equal(resultsQuorum({ RESULTS_QUORUM: "1" }), 1);
  assert.equal(resultsQuorum({ RESULTS_QUORUM: "2" }), 2);
  assert.equal(resultsQuorum({ RESULTS_QUORUM: "10" }), 10);
  // A dashboard field is a string with whatever whitespace was pasted in.
  assert.equal(resultsQuorum({ RESULTS_QUORUM: " 4 " }), 4);
  assert.equal(resultsQuorum({ RESULTS_QUORUM: 2 }), 2, "a number works too");
});

test("anything that is not a positive whole number falls back to the default", () => {
  // Ignored rather than interpreted: a typo must not quietly switch off the
  // agreement rule, which is the only thing between the leaderboard and one
  // client's word.
  for (const bad of ["0", "-1", "", "   ", "two", "1.5", "1e3", "abc", "3x",
                     null, undefined, {}, [], true, NaN]) {
    assert.equal(resultsQuorum({ RESULTS_QUORUM: bad }), RESULTS_QUORUM,
                 `${JSON.stringify(String(bad))} should fall back`);
  }
  assert.equal(resultsQuorum({}), RESULTS_QUORUM, "unset");
  assert.equal(resultsQuorum(), RESULTS_QUORUM, "no env at all");
});

test("quorum 1 promotes a single report", async () => {
  const db = fakeD1();
  db.rows.agreed = [{ ...FINAL, reporters: 1 }];
  const written = await promoteResults({ PICKS_DB: db, RESULTS_QUORUM: "1" },
                                       ["401"], "2026-10-11T22:00:00Z");
  assert.equal(written, 1);
  const q = db.calls.find((c) => /FROM result_reports/.test(c.sql));
  assert.equal(q.args[q.args.length - 1], 1, "one report is enough");
  assert.ok(db.calls.some((c) => /INSERT INTO results/.test(c.sql)));
});

test("quorum 3 still needs three matching reports", async () => {
  const db = fakeD1();
  // The HAVING does the counting, so what the test pins is the number that
  // reaches it -- and that an unset variable does not lower it.
  await promoteResults({ PICKS_DB: db }, ["401"], "2026-10-11T22:00:00Z");
  let q = db.calls.find((c) => /FROM result_reports/.test(c.sql));
  assert.equal(q.args[q.args.length - 1], 3);

  const bad = fakeD1();
  await promoteResults({ PICKS_DB: bad, RESULTS_QUORUM: "0" }, ["401"], "x");
  q = bad.calls.find((c) => /FROM result_reports/.test(c.sql));
  assert.equal(q.args[q.args.length - 1], 3, "zero does not mean zero");

  // And two reports on a three-quorum deployment promote nothing.
  const two = fakeD1();
  two.rows.agreed = [];                       // the HAVING matched nothing
  assert.equal(await promoteResults({ PICKS_DB: two }, ["401"], "x"), 0);
  assert.ok(!two.calls.some((c) => /INSERT INTO results/.test(c.sql)));
});

test("the scheduled run says which quorum is in force", async () => {
  const db = fakeD1();
  const realFetch = globalThis.fetch;
  globalThis.fetch = A_403;
  try {
    for (const [env, expect] of [
      [{ PICKS_DB: db }, { quorum: 3, configured: false, shouts: false }],
      [{ PICKS_DB: db, RESULTS_QUORUM: "1" }, { quorum: 1, configured: true, shouts: true }],
      [{ PICKS_DB: db, RESULTS_QUORUM: "5" }, { quorum: 5, configured: true, shouts: false }],
    ]) {
      const waits = [];
      const { lines } = await capturingLogs(async () => {
        await worker.scheduled({}, env, { waitUntil: (p) => waits.push(p) });
        await Promise.all(waits);
      });
      const said = lines.find((l) => l.includes("quorum in effect"));
      assert.ok(said, "the run states the quorum");
      assert.match(said, new RegExp(`"quorum":${expect.quorum}`));
      assert.match(said, new RegExp(`"configured":${expect.configured}`));
      assert.equal(lines.some((l) => l.includes("QUORUM IS 1")), expect.shouts,
                   `a quorum of ${expect.quorum} should${
                     expect.shouts ? "" : " not"} be shouted about`);
    }
  } finally {
    globalThis.fetch = realFetch;
  }
});

// ------------------------------------------- saying so on the dashboard

const CROWD_GAME = {
  game_id: "401", season: 2026, week: 5, kickoff: "2026-10-11T17:00:00Z",
  home: "KC", away: "DEN", home_score: 27, away_score: 20, source: "crowd",
};

async function dashHtml(query, env) {
  const res = await worker.fetch(
    new Request(`https://edge.example/v1/dashboard?token=${TOKEN}${query}`), env);
  assert.equal(res.status, 200);
  return res.text();
}

test("the single-reporter warning shows only at a quorum of 1", async () => {
  const db = () => fakeDash({
    results: [CROWD_GAME],
    consensus: [{ game_id: "401", phase: "prekick", season: 2026, week: 5,
                  side: "KC", result: "win", graded_at: "2026-10-12T00:00:00Z" }],
  });
  const WARNING = /single reporter/;

  const alone = await dashHtml("&view=board",
                               { PICKS_DB: db(), DASHBOARD_TOKEN: TOKEN,
                                 RESULTS_QUORUM: "1" });
  assert.match(alone, WARNING);
  assert.match(alone, /RESULTS_QUORUM = 1/);
  // Beside the crowd's record, not tucked in a corner of the page.
  assert.ok(alone.indexOf("single reporter") > alone.indexOf("The crowd"),
            "the note sits inside the crowd row");

  for (const env of [
    { PICKS_DB: db(), DASHBOARD_TOKEN: TOKEN },                   // default 3
    { PICKS_DB: db(), DASHBOARD_TOKEN: TOKEN, RESULTS_QUORUM: "3" },
    { PICKS_DB: db(), DASHBOARD_TOKEN: TOKEN, RESULTS_QUORUM: "0" }, // → 3
  ]) {
    assert.doesNotMatch(await dashHtml("&view=board", env), WARNING);
  }
});

test("a score that came from a vote is marked wherever it is shown", async () => {
  const picker = "a".repeat(16);
  const tables = {
    results: [CROWD_GAME],
    pickers: [{ picker, name: "Me", first_at: "2026-10-01T00:00:00Z",
                last_at: "2026-10-11T00:00:00Z" }],
    picks: [{ picker, game_id: "401", kind: "winner", side: "KC", season: 2026,
              week: 5, result: "win", received_at: "2026-10-11T12:00:00Z" }],
  };
  const env = () => ({ PICKS_DB: fakeDash(tables), DASHBOARD_TOKEN: TOKEN });

  const week = await dashHtml("&week=5&season=2026", env());
  assert.match(week, /class="src"/, "this week marks it");
  assert.match(week, /reported/);

  const page = await dashHtml(`&picker=${picker}`, env());
  assert.match(page, /class="src"/, "the picker page marks it too");
});

test("a score ESPN gave us carries no marker", async () => {
  const tables = { results: [{ ...CROWD_GAME, source: "espn" }] };
  const html = await dashHtml("&week=5&season=2026",
                              { PICKS_DB: fakeDash(tables), DASHBOARD_TOKEN: TOKEN });
  assert.doesNotMatch(html, /class="src"/);

  // Nor does a row written before the column existed. It is not a vote either.
  const legacy = { results: [{ ...CROWD_GAME, source: null }] };
  assert.doesNotMatch(
    await dashHtml("&week=5&season=2026",
                   { PICKS_DB: fakeDash(legacy), DASHBOARD_TOKEN: TOKEN }),
    /class="src"/);
});


// ------------------------------------------------- one game, one id
//
// The app writes ESPN's event id behind an "espn-" prefix that records where
// it came from; this server writes the bare id. Five picks and sixteen games
// sat in the same database joining to nothing.
//
// EVENT_ID is ESPN's own, copied from the recorded scoreboard payload in the
// app's test suite (tests/test_source_parsers.py) rather than invented here,
// so both spellings below are the ones the two programs really produce.
const EVENT_ID = "401671789";
const APP_ID = `espn-${EVENT_ID}`;

test("the two spellings of one game come from the same event", async () => {
  // The server's own parser, over ESPN's shape, produces the bare id...
  const espn = async () => new Response(JSON.stringify({
    events: [{
      id: EVENT_ID, date: "2025-09-21T17:00Z",
      competitions: [{
        status: { type: { completed: true } },
        competitors: [
          { homeAway: "home", score: "27", team: { abbreviation: "KC" } },
          { homeAway: "away", score: "20", team: { abbreviation: "DEN" } },
        ],
      }],
    }],
  }), { status: 200 });
  const [game] = await fetchResults(2025, 3, espn);
  assert.equal(game.game_id, EVENT_ID);
  // ...and the app's id is that, prefixed. Which is the whole bug.
  assert.equal(APP_ID, `espn-${game.game_id}`);
  assert.notEqual(APP_ID, game.game_id);
});

test("the prefix is taken off, and only where it is a prefix", () => {
  assert.equal(canonicalGameId(APP_ID), EVENT_ID);
  assert.equal(canonicalGameId("ESPN-401671789"), EVENT_ID, "case does not matter");
  assert.equal(canonicalGameId(` ${APP_ID} `), EVENT_ID);
  // Idempotent: a client that one day sends the bare id already works.
  assert.equal(canonicalGameId(EVENT_ID), EVENT_ID);
  assert.equal(canonicalGameId(canonicalGameId(APP_ID)), EVENT_ID);
  // Anything that is not the prefix followed by digits is left exactly alone,
  // so it shows up as unmatched rather than being mangled into a near-miss.
  for (const odd of ["espn-", "espn-abc", "espn-401a", "espnx-401", "demo-1",
                     "401671789-espn", "", "  "]) {
    assert.equal(canonicalGameId(odd), odd.trim(), JSON.stringify(odd));
  }
  assert.equal(canonicalGameId(null), "");
  assert.equal(canonicalGameId(undefined), "");
});

test("a pick sent with the app's id is stored against the ESPN row", async () => {
  const PICKS_DB = fakeD1();
  const key = "KEY-GAMEID-0001";
  fakeWhop({ ...live });
  const res = await worker.fetch(picksPost({
    picker: await pickerIdFor(key),
    license_key: key,
    picks: [{ game_id: APP_ID, kind: "winner", side: "KC", season: 2025, week: 3 }],
  }), picksEnv(PICKS_DB));
  assert.equal(res.status, 200);

  const insert = PICKS_DB.calls.find((c) => /INSERT INTO picks/.test(c.sql));
  assert.equal(insert.args[1], EVENT_ID, "stored bare, so it joins the results row");
});

test("a reported score sent with the app's id is stored the same way", () => {
  // The app reports from its own games table, so these arrive prefixed too --
  // and a prefixed result row would clash with the one ESPN wrote.
  const r = cleanResult({
    game_id: APP_ID, season: 2025, week: 3, home: "KC", away: "DEN",
    home_score: 27, away_score: 20, kickoff: "2025-09-21T17:00:00Z",
  }, Date.parse("2025-09-22T00:00:00Z"));
  assert.equal(r.game_id, EVENT_ID);
});

// ------------------------------------------------ the rows already stored

test("picks already stored are renamed, across every table that holds an id", async () => {
  const db = fakeD1();
  const moved = await dropIdPrefix({ PICKS_DB: db });

  const updates = db.prepared.filter((sql) => /UPDATE OR IGNORE/.test(sql));
  assert.deepEqual(updates.map((sql) => /UPDATE OR IGNORE (\w+)/.exec(sql)[1]),
                   ["picks", "consensus", "result_reports", "results"]);
  for (const u of updates.map((sql) => ({ sql }))) {
    assert.match(u.sql, /SET game_id = substr\(game_id, 6\)/);
    // Only "espn-" followed by digits and nothing else. A prefix on something
    // odder is left alone to be counted as unmatched, not silently rewritten.
    assert.match(u.sql, /game_id LIKE 'espn-%'/);
    assert.match(u.sql, /length\(game_id\) > 5/);
    assert.match(u.sql, /substr\(game_id, 6\) NOT GLOB '\*\[\^0-9\]\*'/);
  }
  // fakeD1 reports 7 changes per statement, so every table is counted.
  assert.deepEqual(moved, { picks: 7, consensus: 7, result_reports: 7, results: 7 });
});

test("the rename is reported, and a clean database says nothing", async () => {
  const quiet = fakeD1();
  quiet.prepare = (sql) => ({
    sql, args: [], bind(...a) { this.args = a; return this; },
    async run() { return { meta: { changes: 0 } }; },
    async all() { return { results: [] }; },
  });
  const { lines } = await capturingLogs(() => dropIdPrefix({ PICKS_DB: quiet }));
  assert.ok(!lines.some((l) => l.includes("normalised game ids")),
            "nothing to say when nothing moved");

  const dirty = await capturingLogs(() => dropIdPrefix({ PICKS_DB: fakeD1() }));
  assert.match(dirty.lines.find((l) => l.includes("normalised game ids")),
               /"picks":7/);
});

// ------------------------------------------- never silent again

test("a pick that matches no game on file is logged", async () => {
  const db = fakeD1();
  // The week's card is on file, and this pick is not on it.
  db.rows.results = [{ game_id: EVENT_ID, season: 2025, week: 3 }];
  const key = "KEY-GAMEID-0002";
  fakeWhop({ ...live });
  const picker = await pickerIdFor(key);
  const { lines } = await capturingLogs(() => worker.fetch(picksPost({
    picker,
    license_key: key,
    picks: [{ game_id: "nfl-9999", kind: "winner", side: "KC",
              season: 2025, week: 3 }],
  }), picksEnv(db)));

  const said = lines.find((l) => l.includes("match no game on file"));
  assert.ok(said, `nothing logged; got ${JSON.stringify(lines)}`);
  assert.match(said, /"count":1/);
  assert.match(said, /nfl-9999/);
});

test("a pick is stored even when it matches nothing", async () => {
  // Counted and complained about, never dropped: a pick thrown away is a
  // pick that cannot be recovered once the ids are reconciled.
  const db = fakeD1();
  db.rows.results = [{ game_id: EVENT_ID, season: 2025, week: 3 }];
  const key = "KEY-GAMEID-0003";
  fakeWhop({ ...live });
  const res = await worker.fetch(picksPost({
    picker: await pickerIdFor(key), license_key: key,
    picks: [{ game_id: "nfl-9999", kind: "winner", side: "KC", season: 2025, week: 3 }],
  }), picksEnv(db));
  assert.equal((await res.json()).stored, 1);
  assert.ok(db.calls.some((c) => /INSERT INTO picks/.test(c.sql)));
});

test("an early pick on a week with no card yet is not called unmatched", async () => {
  const db = fakeD1();
  db.rows.results = [];                       // that week never fetched yet
  const key = "KEY-GAMEID-0004";
  fakeWhop({ ...live });
  const picker = await pickerIdFor(key);
  const { lines } = await capturingLogs(() => worker.fetch(picksPost({
    picker, license_key: key,
    picks: [{ game_id: EVENT_ID, kind: "winner", side: "KC",
              season: 2025, week: 18 }],
  }), picksEnv(db)));
  assert.ok(!lines.some((l) => l.includes("match no game on file")),
            "a fortnight-early pick is not a mismatch");
});

test("the dashboard counts unmatched picks for the week", async () => {
  const tables = {
    results: [{ game_id: EVENT_ID, season: 2025, week: 3, home: "KC",
                away: "DEN", kickoff: "2025-09-21T17:00:00Z",
                home_score: 27, away_score: 20 }],
    picks: [
      { picker: "a".repeat(16), game_id: EVENT_ID, kind: "winner", side: "KC",
        season: 2025, week: 3, received_at: "2025-09-21T12:00:00Z" },
      { picker: "a".repeat(16), game_id: APP_ID, kind: "winner", side: "KC",
        season: 2025, week: 3, received_at: "2025-09-21T12:00:00Z" },
    ],
  };
  const html = await dashHtml("&week=3&season=2025",
                              { PICKS_DB: fakeDash(tables), DASHBOARD_TOKEN: TOKEN });
  assert.match(html, /1 pick matched no game on this week's card/);
  assert.match(html, /espn-401671789/, "and says which id");

  // The matched one alone leaves no warning at all.
  const clean = { ...tables, picks: [tables.picks[0]] };
  assert.doesNotMatch(
    await dashHtml("&week=3&season=2025",
                   { PICKS_DB: fakeDash(clean), DASHBOARD_TOKEN: TOKEN }),
    /matched no game/);
});
