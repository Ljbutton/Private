/* Minimal SVG chart helpers.
 *
 * Hand-rolled rather than pulled from a CDN so the dashboard works offline and
 * ships nothing it does not use. Every chart here follows the same rules:
 * one value axis (never two), thin marks, hairline recessive grid, a legend
 * whenever more than one series is drawn, and a hover layer by default.
 */

const NS = "http://www.w3.org/2000/svg";

function el(name, attrs = {}, parent = null) {
  const node = document.createElementNS(NS, name);
  for (const [k, v] of Object.entries(attrs)) {
    if (v !== null && v !== undefined) node.setAttribute(k, v);
  }
  if (parent) parent.appendChild(node);
  return node;
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/**
 * Reduce a series to at most `target` points, keeping its shape.
 *
 * A line sampled far more often than it is drawn is not detail, it is ink. The
 * model's projection is written on every recompute -- hundreds of points across
 * two days -- and a wobble of half a point between consecutive runs then fills
 * the plot with a solid sawtooth that hides the very trend the chart exists to
 * show. Drawing one mark per several pixels cannot show more than one value
 * anyway; it just picks arbitrarily which one.
 *
 * So each bin reports its mean, which is what the line was *around* during that
 * span, plus the range it covered, which the caller can draw as a band. That
 * keeps the variation visible instead of smoothing it away -- the honest
 * version of quieter.
 *
 * The mean specifically, not the median, which was the first thing tried and
 * did not work. Median is robust to an outlier but not to symmetric two-valued
 * noise: with an odd number of samples in a bin it can only return one of the
 * two values, so it snaps to whichever happened to appear more often and the
 * line still saws, bin to bin, exactly as before. Averaging pulls the noise
 * down by the square root of the bin size instead.
 *
 * The default target is deliberately well under the pixel width. Drawing a
 * point every eight pixels still left the flicker visible, because a bin of
 * seven samples only divides noise by its square root; a bin of a dozen or
 * more is what makes the line read as a level rather than a texture.
 *
 * Returns {points, band}. A series already below the target is returned
 * untouched, with no band: there is nothing being hidden to disclose.
 */
export function condense(points, target = 48) {
  const live = (points || []).filter(
    (p) => p && p.y !== null && p.y !== undefined && isFinite(p.y));
  if (live.length <= target) return { points: live, band: [] };

  const at = (p) => (typeof p.x === "number" ? p.x : new Date(p.x).getTime());
  const x0 = at(live[0]);
  const x1 = at(live[live.length - 1]);
  const span = x1 - x0 || 1;

  const bins = new Map();
  for (const p of live) {
    // Clamped so the final point lands in the last bin rather than one past it.
    const i = Math.min(target - 1, Math.floor(((at(p) - x0) / span) * target));
    if (!bins.has(i)) bins.set(i, []);
    bins.get(i).push(p);
  }

  const out = [];
  const band = [];
  for (const i of [...bins.keys()].sort((a, b) => a - b)) {
    const group = bins.get(i);
    const ys = group.map((p) => p.y).sort((a, b) => a - b);
    const mid = ys.reduce((sum, y) => sum + y, 0) / ys.length;
    // The bin's own midpoint in time, not its first sample: placing a summary
    // of a span at the instant the span started shifts the whole line left.
    const x = x0 + ((i + 0.5) / target) * span;
    out.push({ x, y: mid, n: group.length });
    if (ys[0] !== ys[ys.length - 1]) {
      band.push({ x, lo: ys[0], hi: ys[ys.length - 1] });
    }
  }
  return { points: out, band };
}

/** Nice round tick values covering [lo, hi]. */
function ticks(lo, hi, count = 5) {
  if (!isFinite(lo) || !isFinite(hi)) return [0];
  if (lo === hi) return [lo];
  const span = hi - lo;
  const rawStep = span / Math.max(1, count);
  const mag = Math.pow(10, Math.floor(Math.log10(rawStep)));
  const norm = rawStep / mag;
  const step = (norm >= 5 ? 5 : norm >= 2 ? 2 : 1) * mag;
  const out = [];
  for (let t = Math.ceil(lo / step) * step; t <= hi + step * 1e-9; t += step) {
    out.push(Math.abs(t) < step * 1e-9 ? 0 : t);
  }
  return out.length ? out : [lo, hi];
}

function fmtNum(v, digits = 1) {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  const n = Number(v);
  return Math.abs(n) >= 1000 ? n.toFixed(0) : n.toFixed(digits).replace(/\.0$/, "");
}

function shortTime(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" })
    + " " + d.toLocaleTimeString(undefined, { hour: "numeric" });
}

/** Tooltip element bound to a chart wrapper. */
function makeTooltip(wrap) {
  let tip = wrap.querySelector(".tooltip");
  if (!tip) {
    tip = document.createElement("div");
    tip.className = "tooltip";
    wrap.appendChild(tip);
  }
  return {
    show(html, x, y) {
      tip.innerHTML = html;
      tip.style.opacity = "1";
      const box = wrap.getBoundingClientRect();
      const tw = tip.offsetWidth;
      let left = x + 14;
      if (left + tw > box.width) left = Math.max(4, x - tw - 14);
      tip.style.left = `${left}px`;
      tip.style.top = `${Math.max(2, y - 12)}px`;
    },
    hide() { tip.style.opacity = "0"; },
  };
}

/**
 * Multi-series time/line chart with a crosshair.
 * series: [{ name, color, points: [{x, y}], dashed?, muted?, label? }]
 * x values may be dates (ISO strings) or numbers.
 */
export function lineChart(wrap, series, opts = {}) {
  wrap.innerHTML = "";
  wrap.classList.add("chart-wrap");
  const W = opts.width || wrap.clientWidth || 640;
  const H = opts.height || 220;
  const m = { top: 12, right: opts.marginRight ?? 54, bottom: 26, left: opts.marginLeft ?? 44 };
  const live = series.filter((s) => s.points && s.points.length);
  if (!live.length) {
    wrap.innerHTML = '<div class="empty">No history recorded yet.</div>';
    return;
  }

  const toX = (p) => (typeof p.x === "number" ? p.x : new Date(p.x).getTime());
  const xs = live.flatMap((s) => s.points.map(toX));
  const ys = live
    .flatMap((s) => [
      ...s.points.map((p) => p.y),
      // A band drawn outside the domain would be clipped at the axis and read
      // as a flat edge, which is worse than not drawing it at all.
      ...(s.band || []).flatMap((b) => [b.lo, b.hi]),
    ])
    .filter((v) => v !== null && v !== undefined && isFinite(v));
  let [x0, x1] = [Math.min(...xs), Math.max(...xs)];
  if (x0 === x1) { x0 -= 1; x1 += 1; }
  let [y0, y1] = opts.yDomain || [Math.min(...ys), Math.max(...ys)];
  if (y0 === y1) { y0 -= 1; y1 += 1; }
  const pad = (y1 - y0) * 0.12;
  y0 -= pad; y1 += pad;
  if (opts.includeZero) { y0 = Math.min(y0, 0); y1 = Math.max(y1, 0); }

  const px = (v) => m.left + ((v - x0) / (x1 - x0)) * (W - m.left - m.right);
  const py = (v) => m.top + (1 - (v - y0) / (y1 - y0)) * (H - m.top - m.bottom);

  const svg = el("svg", { class: "chart", viewBox: `0 0 ${W} ${H}`,
    role: "img", "aria-label": opts.ariaLabel || "line chart" }, wrap);

  // Hairline grid + value axis. Recessive by design: the data is the ink.
  const yTicks = ticks(y0, y1, opts.yTicks || 4);
  for (const t of yTicks) {
    el("line", { x1: m.left, x2: W - m.right, y1: py(t), y2: py(t),
      stroke: cssVar("--grid"), "stroke-width": 1 }, svg);
    const label = el("text", { x: m.left - 7, y: py(t) + 3, "text-anchor": "end",
      fill: cssVar("--text-muted"), "font-size": 10 }, svg);
    label.textContent = opts.yFormat ? opts.yFormat(t) : fmtNum(t);
  }
  if (opts.zeroLine && y0 < 0 && y1 > 0) {
    el("line", { x1: m.left, x2: W - m.right, y1: py(0), y2: py(0),
      stroke: cssVar("--axis"), "stroke-width": 1 }, svg);
  }

  // Time axis: first, middle and last only — dense date ticks are noise.
  const xTickVals = [x0, (x0 + x1) / 2, x1];
  for (const t of xTickVals) {
    const label = el("text", { x: px(t), y: H - 8,
      "text-anchor": t === x0 ? "start" : t === x1 ? "end" : "middle",
      fill: cssVar("--text-muted"), "font-size": 10 }, svg);
    label.textContent = opts.xFormat ? opts.xFormat(t) : shortTime(new Date(t).toISOString());
  }

  // Bands first, all of them, so a later series' fill never lies over an
  // earlier series' line.
  for (const s of live) {
    const band = (s.band || []).filter(
      (b) => isFinite(b.lo) && isFinite(b.hi));
    if (band.length < 2) continue;
    const top = band.map((b, i) => `${i ? "L" : "M"}${px(toX(b)).toFixed(2)},${py(b.hi).toFixed(2)}`);
    const bottom = band.slice().reverse().map(
      (b) => `L${px(toX(b)).toFixed(2)},${py(b.lo).toFixed(2)}`);
    el("path", {
      d: `${top.join("")}${bottom.join("")}Z`,
      fill: s.color || cssVar("--series-1"),
      // Faint on purpose: it is context for the line, not a second series.
      opacity: 0.16,
      stroke: "none",
    }, svg);
  }

  const endLabels = [];
  for (const s of live) {
    const pts = s.points.filter((p) => p.y !== null && isFinite(p.y));
    if (!pts.length) continue;

    // A series with a single observation has no shape to draw. Rendering it as
    // a one-pixel stub looks like a bug; as a dashed rule across the plot it
    // reads correctly as "this is the current value, we have no history yet".
    if (pts.length === 1 && !s.muted) {
      const y = py(pts[0].y);
      el("line", { x1: m.left, x2: W - m.right, y1: y, y2: y,
        stroke: s.color || cssVar("--series-1"), "stroke-width": 2,
        "stroke-dasharray": "5 4", opacity: 0.9 }, svg);
      const tag = el("text", { x: px(toX(pts[0])) + 6, y: y - 5,
        fill: s.color || cssVar("--series-1"), "font-size": 10, "font-weight": 600,
        "text-anchor": "end" }, svg);
      tag.setAttribute("x", W - m.right - 2);
      tag.textContent = s.short || s.name;
      continue;
    }

    const d = pts.map((p, i) => `${i ? "L" : "M"}${px(toX(p)).toFixed(2)},${py(p.y).toFixed(2)}`).join("");
    el("path", {
      d, fill: "none",
      stroke: s.color || cssVar("--series-1"),
      "stroke-width": s.muted ? 1 : 2,
      "stroke-linejoin": "round", "stroke-linecap": "round",
      "stroke-dasharray": s.dashed ? "5 4" : null,
      opacity: s.muted ? 0.3 : 1,
    }, svg);

    // Direct label at the series end: identity without relying on colour alone.
    if (!s.muted && s.label !== false) {
      const last = pts[pts.length - 1];
      endLabels.push({
        y: py(last.y) + 3,
        x: px(toX(last)) + 6,
        text: s.short || s.name,
        color: s.color || cssVar("--series-1"),
      });
    }
  }

  /* Nudge end labels apart when two series finish at nearly the same value.
     They are what carries identity without relying on colour alone, so two of
     them stacked on the same pixel is not a cosmetic problem -- it destroys the
     one part of the chart a colourblind reader depends on. Seen with a
     projected total landing within half a point of the market's. */
  endLabels.sort((a, b) => a.y - b.y);
  const GAP = 11;
  for (let i = 1; i < endLabels.length; i++) {
    if (endLabels[i].y - endLabels[i - 1].y < GAP) {
      endLabels[i].y = endLabels[i - 1].y + GAP;
    }
  }
  // Pushing down can run past the axis, so the whole group slides back up.
  const overshoot = endLabels.length
    ? Math.max(0, endLabels[endLabels.length - 1].y - (H - m.bottom)) : 0;
  for (const label of endLabels) {
    const node = el("text", { x: label.x, y: label.y - overshoot,
      fill: label.color, "font-size": 10, "font-weight": 600 }, svg);
    node.textContent = label.text;
  }

  // ---- hover crosshair
  const tooltip = makeTooltip(wrap);
  const crosshair = el("line", { y1: m.top, y2: H - m.bottom, stroke: cssVar("--axis"),
    "stroke-width": 1, opacity: 0 }, svg);
  const markers = live.map((s) => el("circle", { r: 4, fill: s.color || cssVar("--series-1"),
    stroke: cssVar("--surface"), "stroke-width": 2, opacity: 0 }, svg));

  const overlay = el("rect", { x: m.left, y: m.top, width: Math.max(1, W - m.left - m.right),
    height: Math.max(1, H - m.top - m.bottom), fill: "transparent" }, svg);

  overlay.addEventListener("pointermove", (ev) => {
    const box = svg.getBoundingClientRect();
    const scale = W / box.width;
    const mx = (ev.clientX - box.left) * scale;
    const xValue = x0 + ((mx - m.left) / (W - m.left - m.right)) * (x1 - x0);
    crosshair.setAttribute("x1", px(xValue));
    crosshair.setAttribute("x2", px(xValue));
    crosshair.setAttribute("opacity", 1);

    const rows = [];
    live.forEach((s, i) => {
      const pts = s.points.filter((p) => p.y !== null && isFinite(p.y));
      if (!pts.length) { markers[i].setAttribute("opacity", 0); return; }
      let best = pts[0];
      for (const p of pts) {
        if (Math.abs(toX(p) - xValue) < Math.abs(toX(best) - xValue)) best = p;
      }
      markers[i].setAttribute("cx", px(toX(best)));
      markers[i].setAttribute("cy", py(best.y));
      markers[i].setAttribute("opacity", s.muted ? 0 : 1);
      if (!s.muted) {
        // "n" is present only on a condensed point, and saying so matters:
        // otherwise the reader takes a bin's median for a reading that was
        // actually taken at that moment.
        const value = opts.yFormat ? opts.yFormat(best.y) : fmtNum(best.y, 1);
        const note = best.n > 1 ? ` <small>mean of ${best.n}</small>` : "";
        rows.push(`<div class="t-row"><span><i style="background:${s.color}"></i>${s.name}</span>` +
          `<span>${value}${note}</span></div>`);
      }
    });
    const title = opts.xFormat ? opts.xFormat(xValue) : shortTime(new Date(xValue).toISOString());
    tooltip.show(`<div class="t-title">${title}</div>${rows.join("")}`,
      (ev.clientX - box.left), (ev.clientY - box.top));
  });
  overlay.addEventListener("pointerleave", () => {
    tooltip.hide();
    crosshair.setAttribute("opacity", 0);
    markers.forEach((mk) => mk.setAttribute("opacity", 0));
  });
}

/**
 * Vertical bar chart. One series, one colour — bar length already encodes
 * magnitude, so colouring by value would burn the free channel.
 * data: [{ label, value, highlight? }]
 */
export function barChart(wrap, data, opts = {}) {
  wrap.innerHTML = "";
  wrap.classList.add("chart-wrap");
  if (!data.length) { wrap.innerHTML = '<div class="empty">No data.</div>'; return; }
  const W = opts.width || wrap.clientWidth || 520;
  const H = opts.height || 180;
  const m = { top: 10, right: 8, bottom: 26, left: 38 };
  const maxV = Math.max(...data.map((d) => d.value), opts.minMax || 0) || 1;
  const innerW = W - m.left - m.right;
  const innerH = H - m.top - m.bottom;
  const slot = innerW / data.length;
  const barW = Math.max(2, slot - 3);   // 2px+ surface gap between adjacent bars

  const svg = el("svg", { class: "chart", viewBox: `0 0 ${W} ${H}`,
    role: "img", "aria-label": opts.ariaLabel || "bar chart" }, wrap);
  for (const t of ticks(0, maxV, 3)) {
    const y = m.top + innerH * (1 - t / maxV);
    el("line", { x1: m.left, x2: W - m.right, y1: y, y2: y,
      stroke: cssVar("--grid"), "stroke-width": 1 }, svg);
    const lab = el("text", { x: m.left - 6, y: y + 3, "text-anchor": "end",
      fill: cssVar("--text-muted"), "font-size": 10 }, svg);
    lab.textContent = opts.yFormat ? opts.yFormat(t) : fmtNum(t);
  }

  const tooltip = makeTooltip(wrap);
  data.forEach((d, i) => {
    const h = Math.max(0, (d.value / maxV) * innerH);
    const x = m.left + i * slot + (slot - barW) / 2;
    const y = m.top + innerH - h;
    const rect = el("rect", {
      x, y, width: barW, height: h, rx: Math.min(4, barW / 2),   // 4px rounded data-end
      fill: d.highlight ? cssVar("--series-2") : (opts.color || cssVar("--series-1")),
      opacity: d.highlight ? 1 : 0.92,
    }, svg);
    rect.addEventListener("pointermove", (ev) => {
      const box = svg.getBoundingClientRect();
      tooltip.show(
        `<div class="t-title">${d.label}</div><div class="t-row"><span>${opts.valueName || "Value"}</span>` +
        `<span>${opts.valueFormat ? opts.valueFormat(d.value) : fmtNum(d.value, 2)}</span></div>`,
        ev.clientX - box.left, ev.clientY - box.top);
    });
    rect.addEventListener("pointerleave", () => tooltip.hide());

    if (data.length <= 24) {
      const lab = el("text", { x: x + barW / 2, y: H - 9, "text-anchor": "middle",
        fill: cssVar("--text-muted"), "font-size": 10 }, svg);
      lab.textContent = d.label;
    }
  });
}

/**
 * Calibration plot: predicted probability against observed frequency, with the
 * y = x reference. Points on the line mean the stated confidence is honest.
 */
export function calibrationChart(wrap, buckets, opts = {}) {
  wrap.innerHTML = "";
  wrap.classList.add("chart-wrap");
  if (!buckets || !buckets.length) {
    wrap.innerHTML = '<div class="empty">Not enough graded games yet.</div>';
    return;
  }
  const W = opts.width || wrap.clientWidth || 420;
  const H = opts.height || 240;
  const m = { top: 12, right: 14, bottom: 30, left: 40 };

  /* The axis has to cover the data, not a range someone expected the data to
     sit in. This was pinned to 0.4-1.0, which is where *predicted* confidence
     lives -- but `observed` is a hit rate over a handful of games and is
     routinely 0. Those points were drawn far below the plot, and because the
     svg does not clip they landed in the table underneath, reading as stray
     marks in a column of numbers rather than as data that did not fit. */
  const values = buckets.flatMap((b) => [b.predicted, b.observed])
    .filter((v) => v !== null && v !== undefined && isFinite(v));
  const floor = Math.min(0.4, ...values);
  const ceil = Math.max(1.0, ...values);
  const lo = Math.max(0, Math.floor(floor * 10) / 10);
  const hi = Math.min(1, Math.ceil(ceil * 10) / 10);
  const px = (v) => m.left + ((v - lo) / (hi - lo)) * (W - m.left - m.right);
  const py = (v) => m.top + (1 - (v - lo) / (hi - lo)) * (H - m.top - m.bottom);

  const svg = el("svg", { class: "chart", viewBox: `0 0 ${W} ${H}`,
    role: "img", "aria-label": "calibration chart" }, wrap);
  const ticks = [];
  for (let t = lo; t <= hi + 1e-9; t += 0.1) ticks.push(Math.round(t * 10) / 10);
  for (const t of ticks) {
    el("line", { x1: m.left, x2: W - m.right, y1: py(t), y2: py(t),
      stroke: cssVar("--grid"), "stroke-width": 1 }, svg);
    const lab = el("text", { x: m.left - 6, y: py(t) + 3, "text-anchor": "end",
      fill: cssVar("--text-muted"), "font-size": 10 }, svg);
    lab.textContent = `${Math.round(t * 100)}%`;
  }
  el("line", { x1: px(lo), y1: py(lo), x2: px(hi), y2: py(hi),
    stroke: cssVar("--axis"), "stroke-width": 2, "stroke-dasharray": "5 4" }, svg);
  const refLabel = el("text", { x: px(0.93), y: py(0.87), fill: cssVar("--text-muted"),
    "font-size": 10, "text-anchor": "end" }, svg);
  refLabel.textContent = "perfect";

  const tooltip = makeTooltip(wrap);
  for (const b of buckets) {
    const r = Math.max(4, Math.min(11, Math.sqrt(b.n) * 0.9));
    const dot = el("circle", { cx: px(b.predicted), cy: py(b.observed), r,
      fill: cssVar("--series-1"), stroke: cssVar("--surface"), "stroke-width": 2 }, svg);
    dot.addEventListener("pointermove", (ev) => {
      const box = svg.getBoundingClientRect();
      tooltip.show(
        `<div class="t-title">${b.range}</div>` +
        `<div class="t-row"><span>Predicted</span><span>${(b.predicted * 100).toFixed(0)}%</span></div>` +
        `<div class="t-row"><span>Observed</span><span>${(b.observed * 100).toFixed(0)}%</span></div>` +
        `<div class="t-row"><span>Games</span><span>${b.n}</span></div>`,
        ev.clientX - box.left, ev.clientY - box.top);
    });
    dot.addEventListener("pointerleave", () => tooltip.hide());
  }
  const xlab = el("text", { x: (W + m.left) / 2, y: H - 8, "text-anchor": "middle",
    fill: cssVar("--text-muted"), "font-size": 10 }, svg);
  xlab.textContent = "predicted confidence →";
}

/** Tiny inline sparkline for a game card. No axes, no hover. */
export function sparkline(points, opts = {}) {
  const W = opts.width || 96, H = opts.height || 22;
  const ys = points.filter((v) => v !== null && isFinite(v));
  if (ys.length < 2) return "";
  let lo = Math.min(...ys), hi = Math.max(...ys);
  if (lo === hi) { lo -= 0.5; hi += 0.5; }
  const d = ys.map((v, i) => {
    const x = (i / (ys.length - 1)) * (W - 2) + 1;
    const y = H - 2 - ((v - lo) / (hi - lo)) * (H - 4);
    return `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`;
  }).join("");
  const color = opts.color || "var(--series-2)";
  return `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" aria-hidden="true">` +
    `<path d="${d}" fill="none" stroke="${color}" stroke-width="1.5" ` +
    `stroke-linejoin="round" stroke-linecap="round"/></svg>`;
}

export { cssVar, fmtNum, shortTime };
