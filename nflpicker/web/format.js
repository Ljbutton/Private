/* Formatting and small pure helpers.

   Pulled out of app.js so they can be tested without a browser. Everything in
   here takes values and returns a string: no DOM, no fetch, no module state.
   That is the whole selection rule -- if a function needs the page, it stays
   in app.js and is covered by the screenshot sweep instead.

   These are the functions that turn model output into what you actually read,
   so their edge cases are the ones that show: a null probability has to render
   as a dash rather than "NaN%", and a value that came back as a string has to
   survive the trip. */

/* Anything from the model, the feeds or the user goes through here before it
   touches innerHTML. Team names come from a data file, but news headlines and
   injury notes come off the wire. */
export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* A dash, never "NaN". Missing is a normal state here -- a game with no line,
   a picker with no record -- and it must not read like a bug. */
const missing = (v) => v === null || v === undefined || v === ""
  || Number.isNaN(Number(v));

export const pct = (v, d = 0) => missing(v) ? "–" : `${(Number(v) * 100).toFixed(d)}%`;
export const num = (v, d = 1) => missing(v) ? "–" : Number(v).toFixed(d);
export const signed = (v, d = 1) => missing(v)
  ? "–" : `${Number(v) > 0 ? "+" : ""}${Number(v).toFixed(d)}`;
export const american = (v) => missing(v)
  ? "–" : `${Number(v) > 0 ? "+" : ""}${Math.round(Number(v))}`;

export function when(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" })
    + ", " + d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

export function ago(iso, now = Date.now()) {
  if (!iso) return "never";
  const secs = (now - new Date(iso).getTime()) / 1000;
  if (!isFinite(secs)) return "never";
  if (secs < 90) return "just now";
  if (secs < 5400) return `${Math.round(secs / 60)}m ago`;
  if (secs < 172800) return `${Math.round(secs / 3600)}h ago`;
  return `${Math.round(secs / 86400)}d ago`;
}

/* "Sun 1:00" — the board has sixteen rows and no width to spare for a date
   that is the same on most of them. */
export function kickoffShort(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString(undefined, { weekday: "short" }) + " " +
    d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

/* `short` drops the down and distance. On the board the stamp shares one
   narrow strip with the three totals, and a four-segment label is the thing
   that pushed the totals out of it -- quarter and clock are what a card is
   scanned for, and the situation is one click away in the game itself. */
export function liveLabel(live, short = false) {
  const parts = [];
  if (live.period) parts.push(live.period > 4 ? `OT${live.period - 4}` : `Q${live.period}`);
  if (live.clock) parts.push(live.clock);
  if (live.down && !short) {
    const ord = { 1: "1st", 2: "2nd", 3: "3rd", 4: "4th" }[live.down] || `${live.down}`;
    parts.push(`${ord} & ${live.distance ?? "?"}`);
  }
  if (live.red_zone) parts.push(short ? "RZ" : "red zone");
  return parts.join(" · ") || "Live";
}

export function greetingFor(date) {
  const h = date.getHours();
  if (h < 12) return "Good morning";
  if (h < 18) return "Good afternoon";
  return "Good evening";
}

/* How serious an injury status is, for colour. Out and IR are settled; a
   questionable is a coin flip that still moves a line by a point. */
export function statusClass(status) {
  const v = String(status || "").toLowerCase();
  if (/(out|injured reserve|\bir\b|pup|physically unable|suspended|nfi)/.test(v)) return "out";
  if (v.includes("doubtful")) return "doubtful";
  if (v.includes("questionable") || v.includes("limited")) return "questionable";
  return "";
}
