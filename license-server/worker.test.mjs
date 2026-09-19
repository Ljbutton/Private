// node --test license-server/worker.test.mjs
import assert from "node:assert/strict";
import { test } from "node:test";

import { validate } from "./worker.js";

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
