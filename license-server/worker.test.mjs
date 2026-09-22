// node --test license-server/worker.test.mjs
import assert from "node:assert/strict";
import { test } from "node:test";

import { IMAGE_BYTES_MAX, IMAGE_MAX, SUPPORT_MAX, SUPPORT_RATE, support, validate }
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
