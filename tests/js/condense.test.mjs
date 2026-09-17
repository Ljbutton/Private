/* Condensing a series that is sampled far more often than it is drawn.
 *
 * The model's projection is written on every recompute, so two days of running
 * produce hundreds of points across a chart a few hundred pixels wide. Half a
 * point of wobble between consecutive runs then fills the plot with a solid
 * sawtooth and buries the trend. These tests pin the two things that makes
 * safe: the line has to stay where the data was, and the variation has to stay
 * visible rather than be smoothed away.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { condense } from "../../nflpicker/web/charts.js";

const HOUR = 3600_000;
const T0 = Date.UTC(2026, 8, 15, 9, 0, 0);

/** `n` samples an hour apart, y from `fn`. */
const series = (n, fn) =>
  Array.from({ length: n }, (_, i) => ({ x: T0 + i * HOUR, y: fn(i) }));

describe("condense", () => {
  it("leaves a series that is already sparse completely alone", () => {
    const points = series(20, (i) => i);
    const out = condense(points, 90);
    assert.deepEqual(out.points, points);
    assert.deepEqual(out.band, [], "nothing was hidden, so nothing to disclose");
  });

  it("caps the number of drawn points", () => {
    const out = condense(series(600, (i) => i), 60);
    assert.ok(out.points.length <= 60, `got ${out.points.length}`);
    assert.ok(out.points.length >= 50, "and does not over-thin either");
  });

  it("reports the mean, so a two-value flicker reads as its centre", () => {
    // The real shape: alternating between two values on consecutive runs.
    const out = condense(series(400, (i) => (i % 2 ? 1 : 2)), 40);
    for (const p of out.points) {
      assert.equal(p.y, 1.5, "the line sits between the two values it alternates over");
    }
  });

  it("averages rather than taking a median, which does not survive real noise", () => {
    /* The mistake this pins. A median can only return a value that is in the
       data, so with an odd number of samples in a bin and noise that takes two
       values, it returns one of them -- and which one depends on a coin flip.
       The line then saws from bin to bin exactly as it did before condensing,
       which is what the first version of this actually did on real data. */
    // mulberry32. A textbook LCG written the obvious way overflows 2^53 in
    // JavaScript and degenerates, which had this test failing against correct
    // code -- Math.imul keeps the multiply in 32-bit range.
    let seed = 7;
    const rand = () => {
      seed = (seed + 0x6d2b79f5) | 0;
      let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
    const raw = series(600, () => (rand() < 0.5 ? -0.5 : 0.5));
    const out = condense(raw, 48);
    const spread = (values) => {
      const mean = values.reduce((a, b) => a + b, 0) / values.length;
      return Math.sqrt(
        values.reduce((a, b) => a + (b - mean) ** 2, 0) / values.length);
    };
    /* Measured as spread rather than as max-minus-min, which is what the first
       version of this test used and got wrong: a bin of a dozen coin flips
       lands on all-heads often enough that the extremes still touch the raw
       range across fifty bins, even though the typical point is far tighter.
       The claim worth making is that the line is quieter, not that it is
       incapable of reaching an edge. */
    const quieter = spread(out.points.map((p) => p.y));
    const noisy = spread(raw.map((p) => p.y));
    assert.ok(quieter < noisy / 2,
      `condensed spread ${quieter.toFixed(3)} vs raw ${noisy.toFixed(3)}`);
  });

  it("keeps the range as a band rather than discarding it", () => {
    const out = condense(series(400, (i) => (i % 2 ? -3 : 1)), 40);
    assert.ok(out.band.length > 0, "the variation has to remain visible");
    for (const b of out.band) {
      assert.equal(b.lo, -3);
      assert.equal(b.hi, 1);
    }
  });

  it("draws no band where the value genuinely did not move", () => {
    const out = condense(series(400, () => 7), 40);
    assert.deepEqual(out.band, [], "a flat line must not gain a decorative band");
    for (const p of out.points) assert.equal(p.y, 7);
  });

  it("follows a trend rather than flattening it", () => {
    const out = condense(series(500, (i) => i / 10), 50);
    const ys = out.points.map((p) => p.y);
    assert.ok(ys[0] < ys[ys.length - 1], "a rising series still rises");
    for (let i = 1; i < ys.length; i++) {
      assert.ok(ys[i] >= ys[i - 1], `bin ${i} went backwards`);
    }
  });

  it("places each bin at the middle of the span it summarises", () => {
    /* Not at its first sample: a summary of an hour pinned to the instant the
       hour began shifts the whole line left by half a bin, which on a two-day
       chart is a visible lie about when the model changed its mind. */
    const out = condense(series(400, (i) => i), 40);
    const first = out.points[0].x;
    const last = out.points[out.points.length - 1].x;
    const span = T0 + 399 * HOUR - T0;
    assert.ok(first > T0, "the first bin is not pinned to the very first sample");
    assert.ok(last < T0 + 399 * HOUR, "nor the last to the very last");
    assert.ok(Math.abs((first - T0) - span / 80) < span / 200,
      "the first bin sits about half a bin in");
  });

  it("records how many samples each point stands for", () => {
    const out = condense(series(400, (i) => i), 40);
    const total = out.points.reduce((sum, p) => sum + p.n, 0);
    assert.equal(total, 400, "every sample is accounted for, none counted twice");
  });

  it("ignores gaps and nulls rather than plotting them as zero", () => {
    const points = series(300, (i) => (i % 3 === 0 ? null : 5));
    const out = condense(points, 30);
    for (const p of out.points) assert.equal(p.y, 5);
  });

  it("survives a series with a single point", () => {
    assert.deepEqual(condense([{ x: T0, y: 1 }], 90).points, [{ x: T0, y: 1 }]);
    assert.deepEqual(condense([], 90).points, []);
    assert.deepEqual(condense(undefined, 90).points, []);
  });

  it("accepts ISO timestamps, which is what the API returns", () => {
    const iso = Array.from({ length: 300 }, (_, i) => ({
      x: new Date(T0 + i * HOUR).toISOString(),
      y: i % 2 ? 0 : 2,
    }));
    const out = condense(iso, 30);
    assert.ok(out.points.length <= 30);
    for (const p of out.points) {
      assert.equal(typeof p.x, "number", "bins come back as epoch millis");
      assert.equal(p.y, 1);
    }
  });
});
