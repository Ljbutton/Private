/* The small amount of markdown a chat model actually emits.
 *
 * It writes bold, headings and lists whether or not it is asked to, and those
 * were reaching the page escaped -- literal asterisks and hashes, which reads
 * as the model being broken rather than as the app not rendering it. The
 * renderer is hand-written, so the thing worth pinning hardest is that it
 * cannot be talked into emitting markup of its own.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { markdown } from "../../nflpicker/web/format.js";

test("bold, italics and code become tags", () => {
  assert.equal(markdown("**Buffalo** is *clearly* the `best`"),
    "<p><strong>Buffalo</strong> is <em>clearly</em> the <code>best</code></p>");
});

test("headings become headings, capped inside a bubble", () => {
  assert.equal(markdown("# Top\n### Deeper"), "<h3>Top</h3><h4>Deeper</h4>");
});

test("both kinds of list close themselves", () => {
  assert.equal(markdown("- one\n- two"), "<ul><li>one</li><li>two</li></ul>");
  assert.equal(markdown("1. one\n2. two"), "<ol><li>one</li><li>two</li></ol>");
});

test("a list ends when the prose starts again", () => {
  assert.equal(markdown("- one\n\nAfter"), "<ul><li>one</li></ul><p>After</p>");
});

test("a stray asterisk is left alone", () => {
  // The bug was showing these; over-correcting into <em> on a lone asterisk
  // would be the same bug wearing the other hat.
  assert.equal(markdown("3 * 4 = 12"), "<p>3 * 4 = 12</p>");
});

test("markup in the model's text cannot reach the page", () => {
  const out = markdown('<img src=x onerror="alert(1)"> **safe**');
  assert.ok(!out.includes("<img"), out);
  assert.ok(out.includes("&lt;img"), out);
  assert.ok(out.includes("<strong>safe</strong>"), out);
});

test("plain prose is just paragraphs", () => {
  assert.equal(markdown("One line.\n\nAnother."), "<p>One line.</p><p>Another.</p>");
});
