/* The formatting layer, which is what turns model output into what you read.
 *
 * Run with `node --test tests/js`. No dependencies and no browser: these are
 * the functions that take values and return strings, which is exactly the part
 * a screenshot sweep cannot check. The sweep proves a page renders; it cannot
 * prove that a missing probability renders as a dash rather than "NaN%".
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  ago, american, esc, greetingFor, kickoffShort, liveLabel, num, pct,
  signed, statusClass, when,
} from "../../nflpicker/web/format.js";

describe("esc", () => {
  it("escapes every character that can break out of markup", () => {
    assert.equal(esc(`<script>"x"&'y'</script>`),
      "&lt;script&gt;&quot;x&quot;&amp;&#39;y&#39;&lt;/script&gt;");
  });

  it("escapes the ampersand first, so entities are not double-decoded", () => {
    // Getting this order wrong turns &lt; into &amp;lt; and renders as text.
    assert.equal(esc("&lt;"), "&amp;lt;");
  });

  it("renders null and undefined as empty, not as the words", () => {
    assert.equal(esc(null), "");
    assert.equal(esc(undefined), "");
  });

  it("survives a news headline, which is the untrusted case", () => {
    assert.equal(esc(`QB "out" <b>indefinitely</b>`),
      "QB &quot;out&quot; &lt;b&gt;indefinitely&lt;/b&gt;");
  });
});

describe("numbers", () => {
  it("shows a dash for missing rather than NaN", () => {
    for (const v of [null, undefined, NaN, ""]) {
      assert.equal(pct(v), "–", `pct(${String(v)})`);
      assert.equal(num(v), "–", `num(${String(v)})`);
      assert.equal(signed(v), "–", `signed(${String(v)})`);
      assert.equal(american(v), "–", `american(${String(v)})`);
    }
  });

  it("keeps zero, which is a real value and not missing", () => {
    assert.equal(pct(0), "0%");
    assert.equal(num(0), "0.0");
    assert.equal(signed(0), "0.0");
    assert.equal(american(0), "0");
  });

  it("formats probabilities at the asked precision", () => {
    assert.equal(pct(0.5124), "51%");
    assert.equal(pct(0.5124, 1), "51.2%");
    assert.equal(pct(1), "100%");
  });

  it("signs only positives, because minus is already there", () => {
    assert.equal(signed(3.5), "+3.5");
    assert.equal(signed(-3.5), "-3.5");
    assert.equal(signed(-0.04, 1), "-0.0");
  });

  it("rounds american prices to whole numbers", () => {
    assert.equal(american(-110.4), "-110");
    assert.equal(american(145.6), "+146");
  });

  it("accepts numbers that arrived as strings", () => {
    // JSON from the API is typed, but sqlite and the odds feed are not always.
    assert.equal(pct("0.5"), "50%");
    assert.equal(signed("2.5"), "+2.5");
  });
});

describe("ago", () => {
  const T = Date.parse("2026-09-16T12:00:00Z");
  const at = (secs) => new Date(T - secs * 1000).toISOString();

  it("says never when there is no timestamp", () => {
    assert.equal(ago(null, T), "never");
    assert.equal(ago("", T), "never");
    assert.equal(ago("not a date", T), "never");
  });

  it("walks up the units as the gap grows", () => {
    assert.equal(ago(at(30), T), "just now");
    assert.equal(ago(at(300), T), "5m ago");
    assert.equal(ago(at(7200), T), "2h ago");
    assert.equal(ago(at(259200), T), "3d ago");
  });

  it("does not report a future timestamp as days ago", () => {
    // Clock skew between the machine and a feed is normal and must not print
    // "-1d ago", which reads as a bug in the app.
    assert.equal(ago(new Date(T + 5000).toISOString(), T), "just now");
  });
});

describe("liveLabel", () => {
  it("builds the full situation for the game drawer", () => {
    assert.equal(
      liveLabel({ period: 3, clock: "11:49", down: 1, distance: 10 }),
      "Q3 · 11:49 · 1st & 10");
  });

  it("drops the down and distance for the board", () => {
    // The long form is what pushed the three totals out of the card header.
    assert.equal(
      liveLabel({ period: 3, clock: "11:49", down: 1, distance: 10 }, true),
      "Q3 · 11:49");
  });

  it("names overtime rather than calling it Q5", () => {
    assert.equal(liveLabel({ period: 5, clock: "4:00" }), "OT1 · 4:00");
  });

  it("abbreviates the red zone in short form", () => {
    assert.equal(liveLabel({ period: 4, red_zone: true }), "Q4 · red zone");
    assert.equal(liveLabel({ period: 4, red_zone: true }, true), "Q4 · RZ");
  });

  it("falls back to Live rather than an empty string", () => {
    assert.equal(liveLabel({}), "Live");
  });

  it("marks an unknown distance rather than printing undefined", () => {
    assert.equal(liveLabel({ down: 2 }), "2nd & ?");
  });
});

describe("statusClass", () => {
  it("treats every settled absence as out", () => {
    for (const s of ["Out", "Injured Reserve", "IR", "PUP",
                     "Physically Unable to Perform", "Suspended", "NFI"]) {
      assert.equal(statusClass(s), "out", s);
    }
  });

  it("separates doubtful from questionable", () => {
    assert.equal(statusClass("Doubtful"), "doubtful");
    assert.equal(statusClass("Questionable"), "questionable");
    assert.equal(statusClass("Limited Participation"), "questionable");
  });

  it("gives active and unknown no class at all", () => {
    assert.equal(statusClass("Active"), "");
    assert.equal(statusClass(null), "");
  });
});

describe("dates", () => {
  it("returns empty for a missing or unparseable timestamp", () => {
    for (const fn of [when, kickoffShort]) {
      assert.equal(fn(null), "");
      assert.equal(fn(""), "");
      assert.equal(fn("nonsense"), "");
    }
  });

  it("formats a real kickoff without throwing", () => {
    assert.match(kickoffShort("2026-09-20T17:00:00Z"), /\w{3} \d/);
  });
});

describe("greetingFor", () => {
  const at = (h) => new Date(2026, 8, 16, h, 0, 0);

  it("changes at noon and six", () => {
    assert.equal(greetingFor(at(0)), "Good morning");
    assert.equal(greetingFor(at(11)), "Good morning");
    assert.equal(greetingFor(at(12)), "Good afternoon");
    assert.equal(greetingFor(at(17)), "Good afternoon");
    assert.equal(greetingFor(at(18)), "Good evening");
    assert.equal(greetingFor(at(23)), "Good evening");
  });
});
