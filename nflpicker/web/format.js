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

/* Where a clock's hands point at a moment, in degrees clockwise from twelve.
   The hour hand creeps through the hour rather than jumping at the top of it,
   which is the difference between a clock and a diagram of one. There is no
   second hand: a mark that redraws itself sixty times a minute in the corner
   of a page is a distraction, not a feature. */
export function clockAngles(date) {
  const minute = date.getMinutes();
  return { hour: ((date.getHours() % 12) + minute / 60) * 30, minute: minute * 6 };
}

/* The angle to write for a hand currently sitting at `prev`.

   Always the forward way round, even when that means writing 366 rather than
   6: hands are animated, and the shortest path from 354 to 0 is backwards.
   Without this the minute hand unwinds through the whole dial at the top of
   every hour. */
export function advanceHand(prev, target) {
  if (prev === null || prev === undefined) return target;
  return prev + ((((target - (prev % 360)) % 360) + 360) % 360);
}

/* "Good morning, Luke." -- or the same line without a name, when the computer
   could not tell us one worth using. The comma exists only when something
   follows it; "Good morning, ." is the kind of detail that reads as a bug. */
export function greetingLine(date, name) {
  const who = String(name || "").trim();
  return who ? `${greetingFor(date)}, ${who}.` : `${greetingFor(date)}.`;
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

/* The small amount of markdown a chat model actually emits.

   It writes `**bold**`, `### headings`, `- lists` and `1.` lists whether or
   not it is asked to, and those were being escaped and shown as literal
   asterisks and hashes -- which reads as the model being broken rather than as
   the app not rendering it.

   Hand-written rather than a library: this is nine constructs, the input is a
   few hundred words, and the alternative is shipping a markdown parser inside
   a desktop app to format one panel. Everything is escaped first and only
   these patterns are turned back into tags, so nothing the model writes can
   put markup on the page. */
export function markdown(text) {
  const lines = esc(String(text || "")).split("\n");
  const out = [];
  let list = null;                      // "ul" | "ol" | null
  const closeList = () => { if (list) { out.push(`</${list}>`); list = null; } };
  const inline = (t) => t
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,;:!?]|$)/g, "$1<em>$2</em>")
    .replace(/(^|[\s(])_([^_\n]+)_(?=[\s).,;:!?]|$)/g, "$1<em>$2</em>");

  for (const raw of lines) {
    const line = raw.trimEnd();
    if (!line.trim()) { closeList(); continue; }

    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      closeList();
      // Capped at h4: these sit inside a chat bubble, and a model's "###" is a
      // paragraph label rather than a document structure.
      const level = Math.min(heading[1].length + 2, 4);
      out.push(`<h${level}>${inline(heading[2])}</h${level}>`);
      continue;
    }
    const bullet = line.match(/^\s*[-*+]\s+(.*)$/);
    if (bullet) {
      if (list !== "ul") { closeList(); out.push("<ul>"); list = "ul"; }
      out.push(`<li>${inline(bullet[1])}</li>`);
      continue;
    }
    const numbered = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (numbered) {
      if (list !== "ol") { closeList(); out.push("<ol>"); list = "ol"; }
      out.push(`<li>${inline(numbered[1])}</li>`);
      continue;
    }
    closeList();
    out.push(`<p>${inline(line)}</p>`);
  }
  closeList();
  return out.join("");
}
