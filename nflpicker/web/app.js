import { barChart, calibrationChart, lineChart, sparkline } from "./charts.js";

const state = { season: null, week: null, weeks: [], tab: "home", meta: null, busy: false };

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) throw new Error(`${path} → ${res.status}`);
  return res.json();
}

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const pct = (v, d = 0) => (v === null || v === undefined || Number.isNaN(v))
  ? "–" : `${(Number(v) * 100).toFixed(d)}%`;
const num = (v, d = 1) => (v === null || v === undefined || Number.isNaN(v))
  ? "–" : Number(v).toFixed(d);
const signed = (v, d = 1) => (v === null || v === undefined || Number.isNaN(v))
  ? "–" : `${Number(v) > 0 ? "+" : ""}${Number(v).toFixed(d)}`;
const american = (v) => (v === null || v === undefined) ? "–"
  : `${Number(v) > 0 ? "+" : ""}${Math.round(Number(v))}`;

function when(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" })
    + ", " + d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

function ago(iso) {
  if (!iso) return "never";
  const secs = (Date.now() - new Date(iso).getTime()) / 1000;
  if (!isFinite(secs)) return "never";
  if (secs < 90) return "just now";
  if (secs < 5400) return `${Math.round(secs / 60)}m ago`;
  if (secs < 172800) return `${Math.round(secs / 3600)}h ago`;
  return `${Math.round(secs / 86400)}d ago`;
}

/* A compact "Q3 · 4:05 · 2nd & 7 · red zone" for a game in progress. */
function liveLabel(live) {
  const parts = [];
  if (live.period) parts.push(live.period > 4 ? `OT${live.period - 4}` : `Q${live.period}`);
  if (live.clock) parts.push(live.clock);
  if (live.down) {
    const ord = { 1: "1st", 2: "2nd", 3: "3rd", 4: "4th" }[live.down] || `${live.down}`;
    parts.push(`${ord} & ${live.distance ?? "?"}`);
  }
  if (live.red_zone) parts.push("red zone");
  return parts.join(" · ") || "Live";
}

/* The diagnostic gap: what the edge would be if we quoted the market-blind
   model straight against the line, with no shrinking toward the market.

   It is derived here rather than read from the payload because it was read
   from `components`, where it has never existed -- so the subtitle rendered
   "raw gap – before shrinking" and the dash read as punctuation rather than as
   a missing number. Deriving it needs no new column: it is exactly the model
   margin plus the posted home line, the same arithmetic the predictor does. */
function rawSpreadEdge(game) {
  const model = game.prediction?.margin_home;
  const spread = game.market?.spread_home;
  if (model === null || model === undefined) return null;
  if (spread === null || spread === undefined) return null;
  return Number(model) + Number(spread);
}

/* The one place the sign convention is turned into words. A home line of -3.5
   means the home team lays 3.5 points. */
function spreadText(card) {
  const line = card.market?.spread_home;
  if (line === null || line === undefined) return "–";
  if (Math.abs(line) < 0.05) return "PK";
  return line < 0 ? `${card.home} ${num(line, 1)}` : `${card.away} ${num(-line, 1)}`;
}

function modelLineText(card) {
  const margin = card.prediction?.margin_home;
  if (margin === null || margin === undefined) return "–";
  if (Math.abs(margin) < 0.05) return "PK";
  return margin > 0 ? `${card.home} ${num(-margin, 1)}` : `${card.away} ${num(margin, 1)}`;
}

/* Edge colour is diverging: blue when it favours home, red when away, neutral
   when there is nothing there. The number is always shown, so colour is never
   the only channel. */
function edgePill(edge) {
  if (edge === null || edge === undefined) return '<span class="muted">no line</span>';
  const v = Number(edge);
  const strong = Math.abs(v) >= 1.5;
  if (!strong) {
    return `<span class="edge-pill quiet">${signed(v)} pts</span>`;
  }
  // Tinted, not filled. A solid red block on a dark card reads as an error
  // rather than as a number worth reading, and there are sixteen of them.
  const hue = v > 0 ? "var(--div-pos)" : "var(--div-neg)";
  return `<span class="edge-pill" style="color:${hue};` +
    `background:color-mix(in srgb, ${hue} 14%, transparent);` +
    `box-shadow:inset 0 0 0 1px color-mix(in srgb, ${hue} 30%, transparent)">` +
    `${signed(v)} pts</span>`;
}

// ------------------------------------------------------------------ status
function renderStatus(meta) {
  const rows = [];
  const conn = (cls, name, status, title = "") =>
    `<div class="conn ${cls}" title="${esc(title)}"><span class="dot"></span>` +
    `<span class="nm">${esc(name)}</span><span class="st">${esc(status)}</span></div>`;

  rows.push(conn(meta.demo ? "warn" : "ok", meta.demo ? "Demo data" : "Live sources",
    meta.demo ? "synthetic" : "live"));
  rows.push(conn(meta.model.trained ? "ok" : "warn", "Model", esc(meta.model.version)));
  for (const s of meta.sources || []) {
    rows.push(conn(s.ok ? "ok" : "bad", s.source, ago(s.ts), s.detail || ""));
  }
  if (meta.odds_usage) {
    const u = meta.odds_usage;
    rows.push(conn(u.remaining_budget < 40 ? "warn" : "ok", "Odds API",
      `${u.used}/${u.budget}`, "requests used this month"));
  } else if (!meta.has_odds_key && !meta.demo) {
    rows.push(conn("warn", "Odds API", "no key", "single consensus line only"));
  }
  $("#statusbar").innerHTML = rows.join("");
}

/* ------------------------------------------------------------------ hero */

function greetingFor(date) {
  const h = date.getHours();
  if (h < 12) return "Good morning";
  if (h < 18) return "Good afternoon";
  return "Good evening";
}

function renderHero(meta) {
  const now = new Date();
  $("#clock").textContent = now.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  $("#clockdate").textContent = now
    .toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" })
    .toUpperCase();
  $("#greeting").textContent = `${greetingFor(now)}.`;

  // The cadence is read from the scheduler rather than written here, so the
  // line cannot drift away from what the app is actually doing.
  const jobs = (meta.scheduler && meta.scheduler.jobs) || [];
  const fastest = jobs.reduce((min, j) => {
    const s = j.next_interval_seconds || j.interval_seconds;
    return s && (!min || s < min) ? s : min;
  }, 0);
  $("#cadence").textContent = fastest
    ? `Live · updates every ${fastest >= 60 ? `${Math.round(fastest / 60)} min` : `${fastest}s`}`
    : "Live";

  const day = now.toLocaleDateString([], { weekday: "long", month: "long", day: "numeric" });
  // Reads the viewed season rather than the current one, so browsing 2024 does
  // not leave a line at the top insisting it is 2026.
  const season = state.season || meta.season;
  const week = state.week || meta.week;
  $("#herosub").textContent = season === meta.season && week === meta.week
    ? `${day} — week ${week} of the ${season} season`
    : `${day} — viewing week ${week} of ${season}`;
}

/* How serious an injury status is, for colour. Out and IR are settled; a
   questionable is a coin flip that still moves a line by a point. */
function statusClass(status) {
  const v = String(status || "").toLowerCase();
  if (/(out|injured reserve|\bir\b|pup|physically unable|suspended|nfi)/.test(v)) return "out";
  if (v.includes("doubtful")) return "doubtful";
  if (v.includes("questionable") || v.includes("limited")) return "questionable";
  return "";
}

/* "Sun 1:00" — the board has sixteen rows and no width to spare for a date
   that is the same on most of them. */
function kickoffShort(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString(undefined, { weekday: "short" }) + " " +
    d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

/* Fold the panels on a page into collapsible sections.
   Home and the Scoreboard are boards: everything on them is meant to be read
   at once. Every other page is reference material you consult one question at
   a time, and four full tables stacked down a page turns finding the one you
   came for into a scrolling exercise.

   Done to the rendered DOM rather than in each template. The panels are built
   inside nested template literals, and rewriting those to emit <details> meant
   re-quoting markup that already contains its own backticks -- a transformation
   with nothing to catch a mistake except the page going blank. Restructuring
   afterwards touches one function and cannot corrupt a template it never
   parses. <details> is used so keyboard support, find-in-page and open state
   all come for free. */
const FOLDING_TABS = new Set(["teams", "picks", "edge", "performance"]);

function foldPanels(root) {
  if (!FOLDING_TABS.has(state.tab)) return;
  const panels = $$(":scope > .panel, :scope > .grid-2 > .panel", root);
  panels.forEach((panel, index) => {
    const header = $("header", panel);
    if (!header || panel.closest("details")) return;
    // Some panels are the point of their page rather than reference material
    // behind it. Folding the ranking comparison hid the comparison.
    if (panel.closest("[data-nofold]")) return;

    const details = document.createElement("details");
    details.className = panel.className + " fold";
    const summary = document.createElement("summary");
    // The first section on a page opens; the rest are a click away. Reopening
    // everything on each refresh would undo the point, so a section the reader
    // has opened is remembered for the session.
    const key = `${state.tab}:${index}`;
    // Open the first section on a page the reader has not touched yet. Keyed
    // per tab: a set shared across tabs meant opening something on one page
    // left every other page fully closed.
    details.open = openFolds.has(key)
      || (index === 0 && !touchedTabs.has(state.tab));
    // Recorded from the click rather than the toggle event, because setting
    // `open` above fires toggle too -- the page would mark itself as read by
    // the reader before they had done anything.
    summary.addEventListener("click", () => {
      touchedTabs.add(state.tab);
      setTimeout(() => {
        if (details.open) openFolds.add(key); else openFolds.delete(key);
      }, 0);
    });

    summary.innerHTML =
      '<svg class="chev" viewBox="0 0 24 24" aria-hidden="true">' +
      '<path d="M9 6l6 6-6 6"/></svg>';
    while (header.firstChild) summary.appendChild(header.firstChild);
    header.remove();

    const body = document.createElement("div");
    body.className = "fold-body";
    while (panel.firstChild) body.appendChild(panel.firstChild);

    details.append(summary, body);
    panel.replaceWith(details);
  });
}

/* Which sections the reader has opened, kept for the session so a refresh does
   not fold the thing they are reading, and which tabs they have touched at all
   -- an untouched page still opens its first section. */
const openFolds = new Set();
const touchedTabs = new Set();

// ------------------------------------------------------------------ alerts
/* Alerts are per-game and live inside the game's own dialog rather than in a
   strip over the board. They are something you go looking for once a game has
   your attention, not a queue demanding to be cleared, and a banner that
   pushed sixteen rows off the screen was charging the whole board for news
   about two games. */
function alertList(rows) {
  if (!rows || !rows.length) return "";
  return `<div class="panel">
    <header><h2>What changed</h2>
      <span class="hint">${rows.length} for this game</span></header>
    ${rows.map((a) => `<div class="alert ${esc(a.severity)}">
      <span class="dot"></span>
      <div><b>${esc(a.title)}</b>${a.detail ? `<div class="sub">${esc(a.detail)}</div>` : ""}</div>
      <span class="when">${ago(a.created_at)}</span>
    </div>`).join("")}
  </div>`;
}

// -------------------------------------------------------------- scoreboard
/* Who is actually picking these best. Straight-up winners only: every source
   here names a favourite, so it is the one question all of them can be asked. */
async function renderScoreboard() {
  const root = $("#view");
  const d = await api(`/api/scoreboard?season=${state.season}`);
  const pickers = d.pickers || [];
  if (!d.weeks.length) {
    root.innerHTML = `<div class="panel"><div class="empty">
      Nothing graded yet — this fills in as games finish and you record picks on the board.
    </div></div>`;
    return;
  }

  const cell = (t) => (t.n
    ? `<td class="num"><b>${pct(t.rate)}</b><span class="rec">${t.correct}-${t.wrong}</span></td>`
    : '<td class="num muted">–</td>');

  /* A source with no record at all gets a reason rather than a dash. Empty
     columns here are not a broken fetch: odds cannot be bought for a week that
     has already been played, so any week that finished before this app was
     running has none and never will. A dash says none of that. */
  const cover = d.coverage || {};

  const totalRow = (key, label) => {
    const all = d.totals.all[key];
    const common = d.totals.common[key];
    const lead = common.rate !== null && common.rate === Math.max(
      ...pickers.map((p) => d.totals.common[p].rate ?? -1));
    // Games from before the app existed show the book's pick for the model.
    // Saying how many keeps a record that is mostly borrowed from reading as
    // one the model earned.
    const borrowed = (d.totals.inherited || {})[key] || 0;
    const note = (cover[key] || {}).note;
    return `<tr class="${lead ? "lead" : ""}">
      <td class="who">${esc(label)}${borrowed
        ? `<span class="rec" title="games from before the model existed, shown with the sportsbook's pick">${borrowed} inherited</span>`
        : ""}
        <div class="who-sub">${esc((d.descriptions || {})[key] || "")}</div>
        ${note ? `<div class="who-note">${esc(note)}</div>` : ""}</td>
      ${cell(all)}${cell(common)}
    </tr>`;
  };

  root.innerHTML = `<div class="panel">
    <header><h2>Season ${d.season}</h2>
      <span class="hint">Straight-up winners · "same games" scores only games every
        picker had a view on</span></header>
    <table class="slate totals">
      <thead><tr><th>Picker</th><th class="num">All their picks</th>
        <th class="num">Same games</th></tr></thead>
      <tbody>${pickers.map((p) => totalRow(p, d.labels[p])).join("")}</tbody>
    </table>
  </div>

  <div class="panel">
    <header><h2>By team</h2>
      <span class="hint">how often each picker called that team's games right ·
        sorted by our model, best first</span></header>
    <div class="table-scroll"><table class="slate">
      <thead><tr><th>Team</th><th class="num">Games</th>${(d.pickers || []).map((p) =>
        `<th class="num">${esc(d.labels[p])}</th>`).join("")}</tr></thead>
      <tbody>${(d.teams || []).map((t) => `<tr>
        <td class="who">${esc(t.team)}</td>
        <td class="num muted">${t.games}</td>
        ${d.pickers.map((p) => {
          const v = t.tallies[p];
          if (!v.n) return '<td class="num muted">–</td>';
          // Above half is being read well, below it badly; the midpoint is
          // where a coin would sit, so it is the only sensible split.
          const tone = v.rate > 0.5 ? " hit" : (v.rate < 0.5 ? " miss" : "");
          return `<td class="num${tone}">${pct(v.rate)}<span class="rec">${v.correct}-${v.wrong}</span></td>`;
        }).join("")}
      </tr>`).join("")}</tbody>
    </table></div>
  </div>

  <div class="panel">
    <header><h2>Week by week</h2><span class="hint">correct out of picked</span></header>
    <table class="slate">
      <thead><tr><th>Week</th>${pickers.map((p) =>
        `<th class="num">${esc(d.labels[p])}</th>`).join("")}</tr></thead>
      <tbody>${d.weeks.map((w) => `<tr>
        <td class="who">Week ${w.week}</td>
        ${pickers.map((p) => {
          const t = w.tallies[p];
          return t.n
            ? `<td class="num">${t.correct}<span class="rec">/${t.n}</span></td>`
            : '<td class="num muted">–</td>';
        }).join("")}
      </tr>`).join("")}</tbody>
    </table>
  </div>`;
}

// -------------------------------------------------------------------- home
/* The whole slate, one card per game.

   This replaced an eleven-column table. The table fit everything, but every
   game was a single dense line and reading one meant counting columns across
   to find which number belonged to which team — the two teams shared one row,
   so nothing on it could be attributed to a side by position alone.

   A card gives each team its own line, and each line is only half the card
   wide: the left half identifies the team, the right half is the same three
   sources the rest of the app uses, one column each, read straight down.

     blind  -- margin_home: the model before it is ever shown the line. This
               is the only column that is genuinely independent of the market.
     blend  -- fair_margin and the win probability built from it, so the pick
               and the spread can never disagree. What the app actually claims.
     book   -- the sportsbook consensus.
     market -- prediction markets, shown but never mixed into either.

   Blind and blend sit next to each other deliberately: the gap between them
   is the market's contribution, and with a fitted weight of 0.98 that gap is
   most of the number. Seeing it is the point.

   The tick marks the side a source picked, which is what makes the card
   scannable: four ticks in a column is agreement, a split is a game worth
   opening. Once a game is final the tick turns green or red, so the card is
   its own scorecard. */

const LOGO_BASE = "https://a.espncdn.com/i/teamlogos/nfl/500/";

/* A team's mark. The abbreviation in the team's own colour is drawn first and
   the logo replaces it only once it has actually loaded, so a blocked network
   or a slow CDN degrades to a readable badge rather than to a broken image. */
function teamMark(abbr) {
  const t = (state.meta?.teams || {})[abbr] || {};
  const slug = t.espn || String(abbr || "").toLowerCase();
  return `<span class="tbadge" style="--team:${esc(t.color || "#64748b")}">
    <span class="mono">${esc(abbr)}</span>
    <img class="tlogo" alt="" src="${esc(LOGO_BASE + slug)}.png" />
  </span>`;
}

function wireLogos(root) {
  $$("img.tlogo", root).forEach((img) => {
    const badge = img.closest(".tbadge");
    if (!badge) return;
    const ok = () => badge.classList.add("hasimg");
    if (img.complete && img.naturalWidth > 0) ok();
    img.addEventListener("load", ok);
  });
}

/* "Final · 9/14", "LIVE · Q3 4:05", "Sun 1:00" — the one line that says where
   in its life the game is. */
function gameStamp(g) {
  if (g.status === "in_progress") {
    return `<span class="live-dot"></span>LIVE${g.live ? ` · ${esc(liveLabel(g.live))}` : ""}`;
  }
  const d = g.kickoff ? new Date(g.kickoff) : null;
  const date = d && !Number.isNaN(d.getTime())
    ? d.toLocaleDateString(undefined, { month: "numeric", day: "numeric" }) : "";
  if (g.status === "final") return `Final${date ? ` · ${date}` : ""}`;
  return esc(kickoffShort(g.kickoff)) || "Scheduled";
}

async function renderHome() {
  const root = $("#view");
  const data = await api(`/api/games?week=${state.week}&season=${state.season}`);
  if (!data.games.length) {
    root.innerHTML = '<div class="panel"><div class="empty">No games stored for this week yet.</div></div>';
    return;
  }

  // Prediction-market prices ride along with the picks payload. A failure here
  // must not cost the board: the column simply reads "–".
  const picks = await api(`/api/picks?week=${state.week}&season=${state.season}`)
    .catch(() => ({}));
  const pmByGame = {};
  for (const row of picks.prediction_markets?.games || []) pmByGame[row.game_id] = row;

  const mine = await api(`/api/my-picks?season=${state.season}&week=${state.week}`)
    .catch(() => ({ picks: [] }));
  const myPick = {};
  for (const row of mine.picks || []) myPick[row.game_id] = row.selection;

  const games = [...data.games].sort((a, b) => {
    const rank = (g) => (g.status === "in_progress" ? 0 : g.status === "final" ? 2 : 1);
    return rank(a) - rank(b) || String(a.kickoff).localeCompare(String(b.kickoff));
  });

  let anyInherited = false;

  const card = (g) => {
    const p = g.prediction;
    const ownProb = p && p.home_win_prob !== null && p.home_win_prob !== undefined
      ? p.home_win_prob : null;
    const blindHome = p && p.blind_win_prob !== null && p.blind_win_prob !== undefined
      ? p.blind_win_prob : null;
    const bookHome = g.market?.home_win_prob ?? null;

    // A finished game the model never saw -- anything from before the app was
    // running -- borrows the sportsbook's pick rather than showing a blank.
    // Only finished games: for an upcoming one the model has its own view, and
    // lending it the book's would be inventing an opinion.
    const inherited = ownProb === null && g.status === "final" && bookHome !== null;
    if (inherited) anyInherited = true;
    const ourHome = ownProb !== null ? ownProb : (inherited ? bookHome : null);

    const mkt = pmByGame[g.game_id];
    const mktHome = mkt && mkt.venue_prob !== null && mkt.venue_prob !== undefined
      ? mkt.venue_prob : null;

    // Model margins are home-positive; posted spreads are the home team's line.
    // One negation apart, and getting it wrong would flip every number on the
    // card, so it is done once here rather than per cell.
    const ourLineHome = p && p.fair_margin !== null && p.fair_margin !== undefined
      ? -Number(p.fair_margin) : null;
    const blindLineHome = p && p.margin_home !== null && p.margin_home !== undefined
      ? -Number(p.margin_home) : null;
    const bookLineHome = g.market?.spread_home ?? null;

    const actualWinner = g.status === "final" && g.home_score !== null
      && g.away_score !== null && g.home_score !== g.away_score
      ? (g.home_score > g.away_score ? g.home : g.away)
      : null;

    const lineText = (homeLine, side) => {
      if (homeLine === null || homeLine === undefined) return "–";
      const v = side === "home" ? Number(homeLine) : -Number(homeLine);
      return Math.abs(v) < 0.05 ? "PK" : signed(v);
    };

    // One source's opinion about one team: its line for that side, its
    // probability for that side, and whether that is the side it picked.
    const cell = (kind, homeProb, homeLine, side) => {
      const picked = homeProb !== null && Math.abs(Number(homeProb) - 0.5) > 1e-9
        && ((Number(homeProb) > 0.5) === (side === "home"));
      const verdict = picked && actualWinner
        ? (g[side] === actualWinner ? " hit" : " miss") : "";
      const prob = homeProb === null ? null
        : (side === "home" ? Number(homeProb) : 1 - Number(homeProb));
      // A near-coin-flip rounds to "50%" on both sides, and a tick on one of
      // them then reads as a contradiction rather than as a close call. One
      // decimal, only where the rounding is what hides the difference.
      const digits = prob !== null && Math.abs(prob - 0.5) < 0.005 ? 1 : 0;
      const borrowed = kind === "ours" && inherited;
      return `<div class="gcell ${kind}${picked ? " picked" : ""}${verdict}${
        borrowed ? " borrowed" : ""}"${borrowed
        ? ' title="This game finished before the app was running, so the model has no number of its own. Its pick is the sportsbook\'s; the spread and total are left blank rather than copied, which would read as the model agreeing on them."'
        : ""}>
        <span class="gline">${homeLine === undefined ? "" : esc(lineText(homeLine, side))}</span>
        <span class="gprob">${prob === null ? "–" : pct(prob, digits)}${
          borrowed ? "*" : ""}${picked ? `
          <svg class="tick" viewBox="0 0 24 24" aria-hidden="true"><path d="M9 16.2 4.8 12l-1.4 1.4L9 19 21 7l-1.4-1.4z"/></svg>` : ""}</span>
      </div>`;
    };

    const yourPick = myPick[g.game_id];
    const teamRow = (side) => {
      const abbr = g[side];
      const t = (state.meta?.teams || {})[abbr] || {};
      const score = g[`${side}_score`];
      const mineHere = yourPick === abbr;
      // Blue while the game is undecided, so your pick still reads as yours
      // rather than as a result you have not earned yet.
      const yourVerdict = !mineHere || actualWinner === null
        ? "" : (abbr === actualWinner ? " hit" : " miss");
      const beaten = actualWinner !== null && abbr !== actualWinner;
      return `<div class="gteam${beaten ? " beaten" : ""}">
        <button class="pickdot${mineHere ? " on" : ""}${yourVerdict}" data-pick="${esc(g.game_id)}"
          data-team="${esc(abbr)}" title="${mineHere ? "Your pick — click to clear" : `Pick ${esc(abbr)}`}"
          aria-label="${mineHere ? "Your pick" : `Pick ${esc(abbr)}`}">${
            mineHere ? (yourVerdict === " miss" ? "✕" : "✓") : ""}</button>
        ${teamMark(abbr)}
        <span class="tname">${esc(t.name || abbr)}<span class="tsub">${
          esc(t.location || "")} ${side === "away" ? "" : "· home"}</span></span>
        <span class="tscore">${score === null || score === undefined ? "" : score}</span>
      </div>
      ${cell("blind", blindHome, blindLineHome, side)}
      ${cell("ours", ourHome, ourLineHome, side)}
      ${cell("book", bookHome, bookLineHome, side)}
      ${cell("pmkt", mktHome, undefined, side)}`;
    };

    const moved = g.movement?.toward_us;
    const movedBadge = moved === null || moved === undefined || Math.abs(moved) < 0.05
      ? ""
      : `<span class="gmoved ${moved > 0 ? "good" : "bad"}"
           title="Points the line has moved toward our side since it opened"
           >${signed(moved)} ${moved > 0 ? "to us" : "against us"}</span>`;

    const ourTotal = p && p.fair_total ? num(p.fair_total, 1) : null;
    const blindTotal = p && p.total_points ? num(p.total_points, 1) : null;
    const bookTotal = g.market?.total_points;

    return `<article class="gcard" data-game="${esc(g.game_id)}" tabindex="0">
      <div class="gcard-top">
        <span class="gstate">${gameStamp(g)}</span>
        ${movedBadge}
        <span class="gopen">View game info<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg></span>
      </div>
      <div class="gcard-grid">
        <div class="ghead you">You</div>
        <div class="ghead blind" title="Blind model — the projection before it is ever shown the line. The only column here independent of the market.">Blind</div>
        <div class="ghead ours" title="Our blend — that same model blended with the line. This is what the app actually claims.">Blend</div>
        <div class="ghead book" title="Sportsbook consensus, with the vig removed">Book</div>
        <div class="ghead pmkt" title="Prediction markets — Kalshi and Polymarket contract prices">Market</div>
        ${teamRow("away")}
        ${teamRow("home")}
      </div>
      <div class="gcard-foot">
        <span class="flabel">Total points</span>
        <span class="fval blind">${blindTotal === null ? "–" : blindTotal}</span>
        <span class="fval ours">${ourTotal === null ? "–" : ourTotal}</span>
        <span class="fval book">${num(bookTotal, 1)}</span>
        <span class="fval pmkt" title="Prediction markets quote who wins, not a total"></span>
      </div>
    </article>`;
  };

  root.innerHTML = `<div class="panel board">
    <header><h2>${data.season} · Week ${data.week} — the whole slate</h2>
      <span class="hint">Blind = before the line · Blend = what we claim ·
        Book = sportsbook · Market = Kalshi/Polymarket · a tick marks each
        source's pick</span></header>
    <div class="gboard">${games.map(card).join("")}</div>
    ${anyInherited ? `<p class="note">* These games finished before the app was
      running, so the model has no pick of its own and the sportsbook's number is
      shown in its place.</p>` : ""}
  </div>`;

  wireLogos(root);

  $$(".gcard", root).forEach((node) => {
    const open = () => openGame(node.dataset.game);
    node.addEventListener("click", open);
    node.addEventListener("keydown", (e) => { if (e.key === "Enter") open(); });
  });

  // The pick button sits inside a card that opens a dialog, so its click must
  // not reach the card -- otherwise recording a pick also opens the detail view.
  $$(".pickdot", root).forEach((dot) => {
    dot.addEventListener("click", async (event) => {
      event.stopPropagation();
      const gameId = dot.dataset.pick;
      // Clicking the team you already have selected clears it; clicking the
      // other one switches. Two buttons behaving like a radio group you can
      // also turn off, which is what picking a game actually is.
      const next = myPick[gameId] === dot.dataset.team ? "" : dot.dataset.team;
      await api("/api/my-picks", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ game_id: gameId, selection: next }),
      });
      await renderHome();
    });
  });
}

// ------------------------------------------------- one game, in detail
/* The dialog behind every card on the board. It outlived the Games page:
   that page was a second rendering of the same week the board already
   shows, but this is the only place a single game explains itself --
   line movement, every book's current number, and the alerts raised for
   it. */
async function openGame(gameId) {
  const dlg = $("#detail");
  const body = $(".dialog-body", dlg);
  $(".dialog-title", dlg).textContent = "Loading…";
  body.innerHTML = '<div class="empty">Loading…</div>';
  dlg.showModal();

  const [d, alertsFor] = await Promise.all([
    api(`/api/game/${encodeURIComponent(gameId)}`),
    // A game with no alerts is the normal case, so a failure here must not
    // cost the dialog everything else it was going to show.
    api(`/api/alerts?game_id=${encodeURIComponent(gameId)}`).catch(() => ({ alerts: [] })),
  ]);
  const g = d.game;
  $(".dialog-title", dlg).textContent = `${g.away_name} at ${g.home_name}`;

  const books = d.latest_books || [];
  const bookRows = books.map((b) => `<tr><td class="team">${esc(b.book)}</td>
    <td>${num(b.spread, 1)}</td><td>${american(b.spread_price_home)}</td>
    <td>${num(b.total, 1)}</td><td>${american(b.over_price)}</td>
    <td>${american(b.ml_home)}</td><td>${american(b.ml_away)}</td></tr>`).join("");

  const history = d.prediction_history || [];
  body.innerHTML = `
    <div class="tiles" style="margin-bottom:16px">
      <div class="tile"><div class="label">Market spread</div>
        <div class="value">${esc(spreadText(g))}</div>
        <div class="sub">${g.market?.n_books || 0} books · opened ${num(d.movement.spread.open, 1)}</div></div>
      <div class="tile"><div class="label">Our model</div>
        <div class="value">${esc(modelLineText(g))}</div>
        <div class="sub">market-blind projection</div></div>
      <div class="tile"><div class="label">Actionable edge</div>
        <div class="value">${signed(g.prediction?.spread_edge)}</div>
        <div class="sub">raw gap ${signed(rawSpreadEdge(g))} before shrinking</div></div>
      <div class="tile"><div class="label">Win probability</div>
        <div class="value">${pct(g.prediction?.home_win_prob)}</div>
        <div class="sub">${esc(g.home)} · market ${pct(g.market?.home_win_prob)}</div></div>
    </div>

    <div class="panel" style="background:var(--surface-sunken)">
      <header><h2>Spread movement</h2>
        <span class="hint">consensus against our number; thin grey lines are individual books</span></header>
      <div id="chart-spread" style="height:230px"></div>
      <div class="legend" style="margin-top:8px">
        <span class="key"><i style="background:var(--series-2)"></i>Market consensus</span>
        <span class="key"><i style="background:var(--series-1)"></i>Our projection</span>
        <span class="key"><i class="dash" style="color:var(--series-1)"></i>Current number (no history yet)</span>
        <span class="key"><i style="background:var(--text-muted)"></i>Individual books</span>
      </div>
    </div>

    <div class="panel" style="background:var(--surface-sunken)">
      <header><h2>Total movement</h2></header>
      <div id="chart-total" style="height:200px"></div>
    </div>

    <div class="panel" style="background:var(--surface-sunken)">
      <header><h2>Every book, right now</h2>
        <span class="hint">best available number is what an edge is priced against</span></header>
      <div class="table-scroll"><table>
        <thead><tr><th>Book</th><th>Spread</th><th>Price</th><th>Total</th>
          <th>Over</th><th>${esc(g.home)} ML</th><th>${esc(g.away)} ML</th></tr></thead>
        <tbody>${bookRows || '<tr><td colspan="7" class="muted">No book data.</td></tr>'}</tbody>
      </table></div>
    </div>

    <div class="panel" style="background:var(--surface-sunken)">
      <header><h2>Our prediction history</h2>
        <span class="hint">${history.length} snapshots</span></header>
      <div class="table-scroll"><table>
        <thead><tr><th>Captured</th><th>Model margin</th><th>Market</th><th>Edge</th>
          <th>Win prob</th><th>Version</th></tr></thead>
        <tbody>${history.slice(-12).reverse().map((h) => `<tr>
          <td>${when(h.captured_at)}</td><td>${signed(h.margin_home)}</td>
          <td>${num(h.market_spread, 1)}</td><td>${signed(h.spread_edge)}</td>
          <td>${pct(h.home_win_prob)}</td><td class="muted">${esc(h.model_version)}</td>
        </tr>`).join("") || '<tr><td colspan="6" class="muted">No history yet.</td></tr>'}</tbody>
      </table></div>
    </div>

    ${alertList(alertsFor.alerts)}

    ${["home", "away"].some((s) => g.availability?.[s]?.missing?.length)
      ? `<div class="panel" style="background:var(--surface-sunken)">
      <header><h2>Availability</h2>
        <span class="hint">applied to our projection; the market already prices this</span></header>
      <div class="table-scroll"><table>
        <thead><tr><th>Team</th><th>Player</th><th>Pos</th><th>Status</th><th>Cost</th></tr></thead>
        <tbody>${["away", "home"].flatMap((side) => {
          const team = side === "home" ? g.home : g.away;
          const a = g.availability?.[side];
          if (!a || !a.missing?.length) return [];
          return a.missing.map((m, i) => `<tr>
            <td class="team">${i === 0 ? `${esc(team)} <span class="muted">(${signed(a.adjustment)})</span>` : ""}</td>
            <td>${esc(m.player || "–")}</td><td>${esc(m.position || "–")}</td>
            <td>${esc(m.status || "–")}</td><td>${m.cost ? `−${num(m.cost, 2)}` : "–"}</td></tr>`);
        }).join("")}</tbody>
      </table></div>
      <p class="note">Injury history is not in the training data, so this is applied to the
        projection rather than learned. It mostly removes false disagreement — a model that
        has not noticed a ruled-out starter claims its biggest edge on the game it understands
        least. A quarterback's cost is the measured gap to his backup, not a flat constant.</p>
    </div>` : ""}
    ${(g.news || []).length ? `<div class="panel" style="background:var(--surface-sunken)">
      <header><h2>News touching this game</h2></header>
      ${g.news.map((n) => `<div style="padding:6px 0;border-bottom:1px solid var(--grid)">
        <span class="badge ${esc(n.category)}">${esc(n.category)}</span>
        ${n.url ? `<a href="${esc(n.url)}" target="_blank" rel="noopener">${esc(n.title)}</a>`
          : esc(n.title)}
        ${n.line_impact ? `<span class="muted"> · est. ${signed(n.line_impact)} pts</span>` : ""}
      </div>`).join("")}</div>` : ""}
  `;

  // Books as a muted group rather than eight categorical hues: their identity
  // is "some book", and the story is consensus versus our number.
  const bookSeries = Object.entries(d.books.spread || {}).map(([book, pts]) => ({
    name: book, color: "var(--text-muted)", muted: true, label: false,
    points: pts.map((p) => ({ x: p.captured_at, y: p.value })),
  }));
  const consensusPts = (d.movement.spread.points || []).map((p) => ({ x: p.captured_at, y: p.value }));
  const modelPts = history.map((h) => ({
    x: h.captured_at,
    y: h.margin_home === null ? null : -h.margin_home,   // model's implied home line
  }));
  lineChart($("#chart-spread"), [
    ...bookSeries,
    { name: "Consensus", short: "market", color: "var(--series-2)", points: consensusPts },
    { name: "Our line", short: "model", color: "var(--series-1)", points: modelPts },
  ], { height: 230, yFormat: (v) => signed(v, 1), ariaLabel: "spread movement over time" });

  lineChart($("#chart-total"), [
    { name: "Market total", short: "market", color: "var(--series-2)",
      points: (d.movement.total.points || []).map((p) => ({ x: p.captured_at, y: p.value })) },
    { name: "Our total", short: "model", color: "var(--series-1)",
      points: history.map((h) => ({ x: h.captured_at, y: h.total_points })) },
  ], { height: 200, ariaLabel: "total movement over time" });
}

// ------------------------------------------------------------------- teams
/* Ours against everybody else's, side by side.

   The consensus is the average of published top-32s from ESPN, NFL.com, CBS and
   the rest, pooled over weeks 1 and 2. It is opinion rather than measurement,
   so it is not a scoreboard -- but where our rating and a *tight* consensus
   disagree by a dozen places, one of us has found something and it is worth
   knowing which. A team the sources themselves cannot place is not evidence of
   anything, so the spread between them is shown beside every row. */
function comparisonBlock(con, data) {
  const hasData = con && con.n_lists > 0 && (con.comparison || []).length;
  const sources = hasData
    ? con.sources.map((k) => esc(con.source_names[k] || k)).join(", ") : "";

  const left = `<div class="panel">
    <header><h2>Biggest disagreements</h2>
      <span class="hint">${hasData
        ? `us vs ${con.n_lists} published list${con.n_lists === 1 ? "" : "s"} ·
           mean gap ${con.mean_abs_gap} places`
        : "nothing imported yet"}</span></header>
    ${hasData ? `<table class="slate">
      <thead><tr><th>Team</th><th class="num">Ours</th><th class="num">Them</th>
        <th class="num">Gap</th><th class="num" title="How far apart the sources are on this team">Spread</th></tr></thead>
      <tbody>${con.comparison.slice(0, 12).map((r) => {
        const strong = Math.abs(r.gap) >= 8 && r.spread <= 8;
        return `<tr class="${strong ? "flag" : ""}">
          <td class="who">${esc(r.team)}</td>
          <td class="num">${r.our_rank}</td>
          <td class="num">${r.consensus_rank}</td>
          <td class="num ${r.gap > 0 ? "hit" : (r.gap < 0 ? "miss" : "")}">${
            r.gap > 0 ? "+" : ""}${r.gap}</td>
          <td class="num muted">${r.spread}</td>
        </tr>`;
      }).join("")}</tbody></table>
      <p class="note">A positive gap means we rate a team higher than the
        published lists do. Rows marked in colour are the ones worth arguing
        about: a gap of eight or more places on a team the sources themselves
        agree about (spread of eight or less).</p>`
      : '<div class="empty">Import a published top-32 to compare against.</div>'}
  </div>`;

  const right = `<div class="panel">
    <header><h2>Outside consensus</h2>
      <span class="hint">${hasData
        ? `weeks ${con.weeks.join(" & ")} · ${sources}`
        : "paste a published ranking"}</span></header>
    ${hasData ? `<div class="table-scroll tall"><table class="slate">
      <thead><tr><th>#</th><th>Team</th><th class="num">Avg</th>
        <th class="num">Range</th><th class="num">Ours</th></tr></thead>
      <tbody>${con.comparison.slice().sort((a, b) => a.consensus_rank - b.consensus_rank)
        .map((r) => `<tr>
          <td class="num muted">${r.consensus_rank}</td>
          <td class="who">${esc(r.team)} <span class="muted">${esc(r.name)}</span></td>
          <td class="num">${r.mean_rank.toFixed(1)}</td>
          <td class="num muted">${r.best}–${r.worst}</td>
          <td class="num">${r.our_rank}</td>
        </tr>`).join("")}</tbody></table></div>`
      : ""}
    <div class="import-box">
      <h3>Add a list</h3>
      <div class="controls">
        <select id="rank-source">${(con?.available_sources || [])
          .map((s) => `<option value="${esc(s.key)}">${esc(s.name)}</option>`).join("")}</select>
        <select id="rank-week">${[1, 2, 3, 4, 5].map((w) =>
          `<option value="${w}">Week ${w}</option>`).join("")}</select>
        <button class="btn primary" id="rank-save">Import</button>
        <span id="rank-msg" class="muted"></span>
      </div>
      <textarea id="rank-text" rows="4" placeholder="Paste a published top 32 — &#10;1. Seattle Seahawks&#10;2. Philadelphia Eagles&#10;…"></textarea>
      <p class="note">Copy the list straight off the page; numbering, full team
        names and trailing commentary are all fine. It is stored only if it
        parses to a complete 1&ndash;32 — a half-read list would quietly drag
        the average toward whichever teams happened to come through.</p>
    </div>
  </div>`;

  return `<div class="grid-2 rank-split" data-nofold>${left}${right}</div>`;
}

async function renderTeams() {
  const root = $("#view");
  const data = await api("/api/teams");
  const con = await api("/api/rankings?weeks=1,2").catch(() => null);
  // Season win totals only exist when the odds feed publishes futures. Two
  // columns of dashes read as a bug, so drop them when nothing has one.
  const hasWinTotals = data.teams.some(
    (t) => t.win_total_line !== null && t.win_total_line !== undefined);
  /* Where the projection disagrees with the table. A team rated well above
     its record is one the model thinks has been unlucky -- that gap is the
     single most useful column here, and it is what a ranking sorted by record
     can never show. */
  const drift = (t) => {
    if (!t.record_rank || !t.rank) return "";
    const d = t.record_rank - t.rank;
    if (Math.abs(d) < 3) return "";
    return `<span class="drift ${d > 0 ? "up" : "down"}">${d > 0 ? "▲" : "▼"}${Math.abs(d)}</span>`;
  };
  const rows = data.teams.map((t) => `<tr data-team="${esc(t.team)}" style="cursor:pointer">
    <td class="team">${t.rank}. ${esc(t.team)} <span class="muted">${esc(t.name)}</span>${drift(t)}</td>
    <td>${t.record.wins ?? 0}-${t.record.losses ?? 0}${t.record.ties ? `-${t.record.ties}` : ""}</td>
    <td><b>${num(t.exp_wins, 1)}</b></td>
    <td class="muted">${t.pythagorean === null || t.pythagorean === undefined
      ? "–" : pct(t.pythagorean)}</td>
    <td>${signed(t.power)}</td>
    <td>${num(t.elo, 0)}</td>
    <td class="muted">${num(t.wins_p10, 0)}–${num(t.wins_p90, 0)}</td>
    ${hasWinTotals ? `<td>${num(t.win_total_line, 1)}</td><td>${pct(t.over_prob)}</td>` : ""}
    <td>${pct(t.playoff_prob)}</td>
    <td>${pct(t.division_prob)}</td>
    <td>${pct(t.sb_prob, 1)}</td>
  </tr>`).join("");

  // Only render the situational block when play-by-play has actually been
  // loaded; a table of dashes is worse than no table.
  const hasSituational = data.teams.some((t) => (t.situational || {}).games);
  const sitRows = !hasSituational ? "" : data.teams
    .filter((t) => (t.situational || {}).games)
    .sort((a, b) => (b.situational.third_down_rate || 0) - (a.situational.third_down_rate || 0))
    .map((t) => {
      const s2 = t.situational;
      return `<tr>
        <td class="team">${esc(t.team)} <span class="muted">${esc(t.name)}</span></td>
        <td>${pct(s2.third_down_rate, 1)}</td>
        <td>${pct(s2.def_third_down_rate, 1)}</td>
        <td>${pct(s2.red_zone_td_rate, 1)}</td>
        <td>${pct(s2.explosive_rate, 1)}</td>
        <td>${pct(s2.def_explosive_rate, 1)}</td>
        <td>${pct(s2.sack_rate, 1)}</td>
        <td>${pct(s2.sack_rate_forced, 1)}</td>
        <td>${signed(s2.turnover_margin, 1)}</td>
        <td>${num(s2.penalty_yards, 0)}</td>
      </tr>`;
    }).join("");

  root.innerHTML = `${comparisonBlock(con, data)}
  <div class="panel">
    <header><h2>Power ranking</h2>
      <span class="hint">Ranked by projected finish, not by record · ▲▼ is how
        far a team sits from where its record would put it · rating is points
        better than average on a neutral field</span></header>
    <div class="table-scroll"><table>
      <thead><tr><th>Team</th><th>Record</th>
        <th title="Expected wins from 20,000 simulations of the remaining schedule — what the ranking is sorted by">Proj. wins</th>
        <th title="Win expectation implied by points scored and allowed. Point differential predicts the rest of the season better than the record does.">Pythag</th>
        <th title="Points better than an average team on a neutral field">Rating</th>
        <th>Elo</th>
        <th>80% range</th>${hasWinTotals ? "<th>Win total</th><th>Over</th>" : ""}
        <th>Playoff</th><th>Division</th><th>Title</th></tr></thead>
      <tbody>${rows}</tbody></table></div>
    <p class="note">${hasWinTotals
      ? "Over probabilities come from the simulated win distribution against the posted line."
      : "No season win-total lines are available from the odds feed right now, so those "
        + "columns are hidden. The simulated win distribution below is unaffected."}</p>
  </div>
  ${hasSituational ? `<div class="panel">
    <header><h2>How teams are actually playing</h2>
      <span class="hint">season to date, from play-by-play</span></header>
    <div class="table-scroll"><table>
      <thead><tr><th>Team</th><th>3rd down</th><th>3rd down allowed</th>
        <th>Red zone TD</th><th>Explosive</th><th>Explosive allowed</th>
        <th>Sacks taken</th><th>Sacks forced</th><th>Turnover margin</th>
        <th>Penalty yds</th></tr></thead>
      <tbody>${sitRows}</tbody></table></div>
    <p class="note">Rates rather than counts throughout, because counts mostly measure how
      many possessions a team happened to get. These carry only a small amount of extra
      predictive power over the efficiency ratings above — measured at about 0.002 points of
      margin error — so they are here to be read rather than to drive the model.
      An "explosive" play gains 20 yards or more.</p>
  </div>` : ""}

  <div class="panel" id="team-detail-panel">
    <header><h2>Simulated win distribution</h2>
      <span class="hint">Select a team above</span></header>
    <div id="team-dist" style="height:200px"></div>
  </div>`;

  const show = (abbr) => {
    const team = data.teams.find((t) => t.team === abbr);
    if (!team) return;
    $("#team-detail-panel .hint").textContent =
      `${team.name} — ${num(team.exp_wins, 1)} expected wins`;
    const dist = team.distribution || {};
    const bars = [];
    for (let w = 0; w <= 17; w++) {
      bars.push({ label: String(w), value: dist[String(w)] || 0,
        highlight: Math.round(team.exp_wins ?? -1) === w });
    }
    barChart($("#team-dist"), bars, {
      height: 200, valueName: "Probability",
      valueFormat: (v) => pct(v, 1), yFormat: (v) => pct(v, 0),
      ariaLabel: `${team.name} simulated win distribution`,
    });
  };
  $$("tbody tr", root).forEach((tr) => tr.addEventListener("click", () => show(tr.dataset.team)));
  if (data.teams.length) show(data.teams[0].team);
}

// ------------------------------------------------------------------- picks
async function renderPicks() {
  const root = $("#view");
  const data = await api(`/api/picks?week=${state.week}&season=${state.season}`);
  const edges = data.ats?.edges || [];
  const markets = data.prediction_markets?.games || [];
  const pickem = data.pickem || {};
  const survivor = data.survivor || {};

  const edgeRows = edges.map((e) => `<tr>
    <td class="team">${esc(e.selection)}</td>
    <td>${esc(e.market)}</td>
    <td>${esc(e.book || "–")}</td>
    <td>${american(e.price)}</td>
    <td>${pct(e.win_prob, 1)}</td>
    <td>${american(e.fair_price)}</td>
    <td>${e.edge_points === null ? "–" : signed(e.edge_points)}</td>
    <td>${(e.expected_value * 100).toFixed(1)}%</td>
    <td>${(e.kelly * 100).toFixed(1)}%</td>
    <td><span class="badge ${e.confidence === "suspect" ? "qb" : ""}">${esc(e.confidence)}</span></td>
  </tr>`).join("");

  const board = pickem[state.pickemMode || "ev"] || pickem.ev || {};
  const pickRows = (board.picks || []).map((p) => `<div class="pick-row">
    <span class="conf">${p.confidence}</span>
    <span><strong>${esc(p.pick)}</strong> <span class="muted">over ${esc(p.opponent)}</span>
      ${p.note ? `<div class="muted" style="font-size:11px">${esc(p.note)}</div>` : ""}</span>
    <span>${pct(p.win_prob, 1)}</span>
    <span class="muted">${p.edge === null ? "" : `${signed(p.edge * 100, 1)}pp vs mkt`}</span>
  </div>`).join("");

  const path = (survivor.path || []).map((s) => `<tr>
    <td class="team">Week ${s.week}</td><td>${esc(s.team)}</td>
    <td class="muted">vs ${esc(s.opponent)}</td><td>${pct(s.win_prob, 1)}</td></tr>`).join("");
  const alts = (survivor.alternatives || []).map((a) => `<tr>
    <td class="team">${esc(a.team)}</td><td class="muted">vs ${esc(a.opponent)}</td>
    <td>${pct(a.win_prob, 1)}</td><td>${pct(a.path_survival, 1)}</td>
    <td>${a.cost > 0 ? `−${pct(a.cost, 2)}` : `+${pct(-a.cost, 2)}`}</td></tr>`).join("");

  // Venue columns are built from whatever actually priced this week, so a
  // venue being down removes its column rather than filling it with dashes.
  const venueKeys = [];
  for (const g of markets) {
    for (const v of g.venues || []) {
      if (!venueKeys.some((k) => k.venue === v.venue)) {
        venueKeys.push({ venue: v.venue, label: v.label });
      }
    }
  }
  const marketRows = markets.map((g) => {
    const byVenue = Object.fromEntries((g.venues || []).map((v) => [v.venue, v]));
    const cells = venueKeys.map((k) => {
      const v = byVenue[k.venue];
      return `<td>${v ? pct(v.home_prob, 0) : '<span class="muted">–</span>'}</td>`;
    }).join("");
    const gapStyle = g.notable
      ? `color:${(g.gap || 0) > 0 ? "var(--div-pos)" : "var(--div-neg)"};font-weight:600`
      : "color:var(--text-muted)";
    return `<tr>
      <td class="team">${esc(g.away)} <span class="muted">@</span> ${esc(g.home)}</td>
      <td>${pct(g.book_prob, 0)} <span class="muted">(${g.n_books})</span></td>
      <td>${pct(g.model_prob, 0)}</td>
      ${cells}
      <td style="${gapStyle}">${signed((g.gap || 0) * 100, 1)}pp</td>
      <td>${g.leans ? `<span class="badge">${esc(g.leans)}</span>`
          : '<span class="muted">agrees</span>'}</td>
    </tr>`;
  }).join("");

  root.innerHTML = `
  <div class="panel">
    <header><h2>Prediction markets</h2>
      <span class="hint">All probabilities are for the home team, vig removed</span></header>
    <div class="table-scroll"><table>
      <thead><tr><th>Game</th><th>Sportsbooks</th><th>Our model</th>
        ${venueKeys.map((k) => `<th>${esc(k.label)}</th>`).join("")}
        <th>Gap vs books</th><th>Leans</th></tr></thead>
      <tbody>${marketRows || `<tr><td colspan="${5 + venueKeys.length}" class="muted">
        No prediction-market prices for this week yet.</td></tr>`}</tbody>
    </table></div>
    <p class="note">Shown for comparison only — nothing here changes the suggestions below,
      and none of it feeds the model. Prediction markets draw on a different crowd than the
      sportsbooks, so a gap is worth a second look rather than an instruction: it may mean the
      thinner venue is lagging, or that it has priced news the books have not. "Gap" is the
      venue average minus the sportsbook consensus, in percentage points, and a game is only
      marked as leaning once that reaches five.</p>
  </div>

  <div class="panel">
    <header><h2>Best bets — week ${data.week}</h2>
      <span class="hint">Priced against the best available number, sized at quarter Kelly</span></header>
    <div class="table-scroll"><table>
      <thead><tr><th>Selection</th><th>Market</th><th>Book</th><th>Price</th><th>Our prob</th>
        <th>Fair price</th><th>Edge</th><th>EV</th><th>Stake</th><th>Rating</th></tr></thead>
      <tbody>${edgeRows || '<tr><td colspan="10" class="muted">No qualifying edges this week — that is a normal result, not a failure.</td></tr>'}</tbody>
    </table></div>
    <p class="note">Edges are the blended estimate against the line, already shrunk toward the
      market. A rating of <strong>suspect</strong> means the disagreement is so large it is more
      likely our blind spot than the market's — treat it as a prompt to investigate, not a bet.</p>
  </div>

  <div class="panel">
    <header><h2>ESPN pick'em</h2>
      <div class="controls" style="margin-left:auto">
        <select id="pickem-mode">
          <option value="ev">Maximise expected points</option>
          <option value="leverage">Leverage (large pools)</option>
        </select>
      </div></header>
    <div class="tiles" style="margin-bottom:12px">
      <div class="tile"><div class="label">Expected correct</div>
        <div class="value">${num(board.expected_correct, 1)}<span class="sub"> of ${board.n_games ?? 0}</span></div></div>
      <div class="tile"><div class="label">Expected points</div>
        <div class="value">${num(board.expected_points, 1)}</div>
        <div class="sub">of ${board.max_points ?? 0} possible</div></div>
      <div class="tile"><div class="label">Versus the field</div>
        <div class="value ${(board.expected_points - board.field_expected_points) >= 0 ? "pos" : "neg"}">
          ${signed(board.expected_points - board.field_expected_points, 2)}</div>
        <div class="sub">points, under our own probabilities</div></div>
    </div>
    <div class="pickem-list">${pickRows || '<div class="empty">No games to pick.</div>'}</div>
    <p class="note">Confidence points are assigned highest-to-most-likely, which maximises
      expected score. Leverage mode deliberately gives some of that up to differentiate
      from a field that picks close to the market — the right trade only when finishing
      first is what pays.</p>
  </div>

  <div class="panel">
    <header><h2>Survivor</h2>
      <span class="hint">${survivor.horizon ? `planned ${survivor.horizon} weeks ahead` : ""}</span></header>
    ${survivor.recommendation ? `
      <div class="tiles" style="margin-bottom:12px">
        <div class="tile"><div class="label">This week</div>
          <div class="value">${esc(survivor.recommendation.team)}</div>
          <div class="sub">vs ${esc(survivor.recommendation.opponent)} ·
            ${pct(survivor.recommendation.win_prob, 1)} to win</div></div>
        <div class="tile"><div class="label">Path survival</div>
          <div class="value">${pct(survivor.survival_prob, 1)}</div>
          <div class="sub">through week ${(survivor.week || 0) + (survivor.horizon || 1) - 1}</div></div>
      </div>
      <div class="grid-2">
        <div><h3 style="font-size:12px;margin-bottom:6px">Planned path</h3>
          <table><thead><tr><th>Week</th><th>Team</th><th>Opponent</th><th>Win prob</th></tr></thead>
          <tbody>${path}</tbody></table></div>
        <div><h3 style="font-size:12px;margin-bottom:6px">If you deviate this week</h3>
          <table><thead><tr><th>Team</th><th>Opp</th><th>Win prob</th><th>Path</th><th>Cost</th></tr></thead>
          <tbody>${alts || '<tr><td colspan="5" class="muted">No alternatives.</td></tr>'}</tbody></table></div>
      </div>
      <p class="note">The recommendation is not always this week's safest team. Spending a strong
        team now can cost more later than it gains today, so the optimiser solves the whole
        remaining path — the cost column is what deviating actually costs over that path.</p>
    ` : `<div class="empty">${esc(survivor.note || "No survivor plan available.")}</div>`}
    <div class="used-block">
      <h3>Teams you have already used<span class="hint">click a mark to use or
        release it — the plan replans itself</span></h3>
      <div class="team-picker used">${(state.meta?.teams
        ? Object.keys(state.meta.teams).sort() : []).map((t) => {
          const used = (data.survivor_used || []).includes(t);
          return `<button class="team-pick${used ? " used" : ""}" data-used="${esc(t)}"
            title="${esc(t)} — ${used ? "used, click to release" : "available, click to mark used"}"
            aria-pressed="${used}">${teamMark(t)}</button>`;
        }).join("")}</div>
    </div>
  </div>`;

  const modeSelect = $("#pickem-mode");
  modeSelect.value = state.pickemMode || "ev";
  modeSelect.addEventListener("change", () => {
    state.pickemMode = modeSelect.value;
    renderPicks();
  });

  wireLogos(root);
  /* Clicking a mark toggles it and replans immediately. Typing a
     comma-separated list meant naming a team from memory, spelling its
     abbreviation the way this app happens to spell it, and pressing a second
     button before anything happened. */
  $$("[data-used]", root).forEach((button) => {
    button.addEventListener("click", async () => {
      const team = button.dataset.used;
      const used = new Set(data.survivor_used || []);
      if (used.has(team)) used.delete(team); else used.add(team);
      button.classList.toggle("used");
      await api("/api/survivor/used", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ teams: [...used] }),
      });
      await renderPicks();
    });
  });
}

// -------------------------------------------------------------------- news
/* Two columns, not folded. This page is read by scanning rather than by
   looking one thing up: the question is "has anything changed that I should
   know before I pick", and the injury report is the half that answers it most
   often. Side by side, one scan covers both; stacked behind summaries it took
   two clicks to learn there was nothing new. */
async function renderNews() {
  const root = $("#view");
  const data = await api("/api/news?limit=80");
  const items = data.items.map((n) => `<div class="news-item">
    <div class="news-tags">
      <span class="badge ${esc(n.category)}">${esc(n.category)}</span>
      ${(n.teams || []).map((t) => `<span class="badge">${esc(t)}</span>`).join("")}
      ${n.line_impact ? `<span class="badge" style="border-color:var(--serious);color:var(--serious)">
        est. ${signed(n.line_impact)} pts</span>` : ""}
      <span class="muted news-src">${esc(n.source)} · ${ago(n.published_at)}</span>
    </div>
    <div class="news-title">${n.url
      ? `<a href="${esc(n.url)}" target="_blank" rel="noopener">${esc(n.title)}</a>`
      : esc(n.title)}</div>
    ${n.summary ? `<div class="muted news-sum">${esc(n.summary.slice(0, 220))}</div>` : ""}
  </div>`).join("");

  /* One team at a time, chosen by its mark. A league-wide list is four hundred
     rows you scroll past to find the one team you are about to pick, and the
     status filter happens server-side: "Active" is not an injury report. */
  const teams = data.injury_teams || [];
  const counts = data.injury_counts || {};
  if (!state.injuryTeam || !teams.includes(state.injuryTeam)) {
    state.injuryTeam = teams[0] || null;
  }
  const picker = (state.meta?.teams ? Object.keys(state.meta.teams).sort() : teams)
    .map((t) => {
      const n = counts[t] || 0;
      return `<button class="team-pick${t === state.injuryTeam ? " on" : ""}${
        n ? "" : " empty"}" data-team="${esc(t)}"
        title="${esc(t)} — ${n ? `${n} listed` : "nobody listed"}">
        ${teamMark(t)}<span class="tp-count">${n || ""}</span></button>`;
    }).join("");

  const shown = (data.injuries || []).filter((i) => i.team === state.injuryTeam);
  const injuries = shown.map((i) => `<tr>
    <td class="team">${esc(i.player)}</td>
    <td>${esc(i.position || "–")}</td>
    <td><span class="inj ${esc(statusClass(i.status))}">${esc(i.status || "–")}</span></td>
    <td class="muted">${esc(i.detail || "")}</td>
    <td class="muted">${ago(i.updated_at)}</td></tr>`).join("");

  root.innerHTML = `<div class="grid-2 news-split">
    <div class="panel">
      <header><h2>Injury report</h2>
        <span class="hint">questionable, doubtful, out, IR and PUP only —
          not the whole roster</span></header>
      <div class="team-picker">${picker}</div>
      ${injuries
        ? `<div class="table-scroll tall"><table class="slate roster">
            <thead><tr><th>Player</th><th>Pos</th><th>Status</th><th>Detail</th><th>Updated</th></tr></thead>
            <tbody>${injuries}</tbody></table></div>`
        : `<div class="empty">${state.injuryTeam
            ? `Nobody listed for ${esc(state.injuryTeam)} — everyone is available.`
            : "No injury report stored yet."}</div>`}
    </div>

    <div class="panel">
      <header><h2>News &amp; changes</h2>
        <span class="hint">by estimated relevance, not recency</span></header>
      <div class="news-feed">${items || '<div class="empty">No news stored yet.</div>'}</div>
      <p class="note">The points estimate is a coarse prior from position and availability —
        a starting quarterback is worth two to three points, a backup almost nothing. It is a
        triage signal for what to look at, never a substitute for the market's own reaction.</p>
    </div>
  </div>`;
}

// ------------------------------------------------------------- performance
async function renderPerformance() {
  const root = $("#view");
  const r = await api("/api/performance");
  if (!r.n_games) {
    root.innerHTML = `<div class="panel"><div class="empty">${esc(r.note ||
      "No graded games yet. Results appear once games this app predicted have finished.")}</div></div>`;
    return;
  }
  const acc = r.accuracy || {};
  const beatsMarket = acc.margin_mae !== null && acc.market_margin_mae !== null
    && acc.margin_mae < acc.market_margin_mae;

  // When most graded games were backfilled through a model that was trained on
  // them, these figures are in-sample and will flatter the model — often by a
  // lot. Say so on the numbers themselves, not only in a footnote nobody reads,
  // and put the walk-forward result beside them as the honest benchmark.
  const wf = r.walk_forward || null;
  const inSample = r.n_games > 0 && (r.backfilled || 0) / r.n_games > 0.5;
  const flag = inSample
    ? '<span class="badge" style="border-color:var(--warning);color:var(--warning)">in-sample</span>'
    : "";
  const tone = (good) => (inSample ? "" : (good ? "pos" : "neg"));

  root.innerHTML = `<div class="panel">
    <header><h2>How the model is actually doing</h2>
      <span class="hint">${r.n_games} graded games</span></header>
    <div class="tiles">
      <div class="tile"><div class="label">Against the spread ${flag}</div>
        <div class="value ${tone((r.ats.rate ?? 0) > 0.524)}">${pct(r.ats.rate, 1)}</div>
        <div class="sub">${r.ats.wins}-${r.ats.losses}-${r.ats.pushes} · break-even 52.4%${
          wf && wf.ats_rate ? `<br><strong>walk-forward ${pct(wf.ats_rate, 1)}</strong>` : ""}</div></div>
      <div class="tile"><div class="label">Return on risk ${flag}</div>
        <div class="value ${tone((r.ats.roi ?? 0) >= 0)}">${pct(r.ats.roi, 1)}</div>
        <div class="sub">${signed(r.ats.units, 1)} units at −110</div></div>
      <div class="tile"><div class="label">Closing-line value</div>
        <div class="value ${(r.clv.spread_avg ?? 0) >= 0 ? "pos" : "neg"}">${
          r.clv.spread_avg === null ? "–" : signed(r.clv.spread_avg, 2)}</div>
        <div class="sub">${r.clv.spread_n} bets · points vs close</div></div>
      <div class="tile"><div class="label">Straight up</div>
        <div class="value">${pct(r.straight_up.rate, 1)}</div>
        <div class="sub">${r.straight_up.correct} of ${r.straight_up.n}</div></div>
      <div class="tile"><div class="label">Brier score</div>
        <div class="value">${num(r.calibration.brier, 3)}</div>
        <div class="sub">lower is better · 0.25 = coin flip</div></div>
      <div class="tile"><div class="label">Margin error ${flag}</div>
        <div class="value ${inSample ? "" : (beatsMarket ? "pos" : "")}">${num(acc.margin_mae, 2)}</div>
        <div class="sub">market ${num(acc.market_margin_mae, 2)}${
          beatsMarket ? " — we're closer" : " — market is closer"}${
          wf && wf.margin_mae ? `<br><strong>walk-forward ${num(wf.margin_mae, 2)}</strong> vs ${
            num(wf.market_margin_mae, 2)}` : ""}</div></div>
    </div>
    ${inSample ? `<p class="note" style="border-left-color:var(--warning)">
      <strong>These headline figures are in-sample.</strong> ${r.backfilled} of ${r.n_games}
      graded games were replayed through a model trained on those same seasons, which
      flatters every one of them. The walk-forward figures shown beneath each tile are
      the honest measure — they train only on earlier seasons and test on later ones.
      Once this app has watched real games before kickoff, the top-line numbers become
      genuine out-of-sample results and this warning goes away.</p>` : ""}
    ${r.clv.note ? `<p class="note">${esc(r.clv.note)}</p>` : ""}
    ${!inSample && r.backfill_note ? `<p class="note">${esc(r.backfill_note)}</p>` : ""}
  </div>

  <div class="grid-2">
    <div class="panel"><header><h2>Cumulative units</h2>
      <span class="hint">flat stakes at −110</span></header>
      <div id="chart-units" style="height:230px"></div></div>
    <div class="panel"><header><h2>Calibration</h2>
      <span class="hint">dot size is sample count</span></header>
      <div id="chart-calib" style="height:240px"></div>
      <div class="table-scroll" style="margin-top:10px"><table>
        <thead><tr><th>Confidence</th><th>Predicted</th><th>Observed</th><th>Games</th></tr></thead>
        <tbody>${(r.calibration.buckets || []).map((b) => `<tr><td class="team">${esc(b.range)}</td>
          <td>${pct(b.predicted, 1)}</td><td>${pct(b.observed, 1)}</td><td>${b.n}</td></tr>`).join("")}
        </tbody></table></div>
    </div>
  </div>

  <div class="panel"><header><h2>Week by week</h2></header>
    <div class="table-scroll"><table>
      <thead><tr><th>Season</th><th>Week</th><th>ATS</th><th>Straight up</th>
        <th>CLV</th><th>Cumulative units</th></tr></thead>
      <tbody>${(r.weekly || []).slice().reverse().map((w) => `<tr>
        <td class="team">${w.season}</td><td>${w.week}</td>
        <td>${w.ats_wins}-${w.ats_losses}</td><td>${w.su_correct}/${w.n}</td>
        <td>${w.clv === null ? "–" : signed(w.clv, 2)}</td>
        <td>${signed(w.cumulative_units, 1)}</td></tr>`).join("")}</tbody>
    </table></div></div>`;

  lineChart($("#chart-units"), [{
    name: "Units", short: "units", color: "var(--series-1)",
    points: (r.weekly || []).map((w, i) => ({ x: i, y: w.cumulative_units })),
  }], {
    height: 230, includeZero: true, zeroLine: true,
    xFormat: (v) => {
      const w = (r.weekly || [])[Math.round(v)];
      return w ? `${w.season} wk ${w.week}` : "";
    },
    ariaLabel: "cumulative units over time",
  });
  calibrationChart($("#chart-calib"), r.calibration.buckets || [], { height: 240 });
}

// -------------------------------------------------------------------- edge
async function renderEdge() {
  const root = $("#view");
  const data = await api("/api/edge");
  const m = data.movement || {};
  const cov = data.coverage || {};

  if (!m.n) {
    root.innerHTML = `<div class="panel">
      <header><h2>Does the line move toward us?</h2></header>
      <div class="empty">${esc(m.note || "Not enough observations yet.")}</div>
      <div class="tiles">
        <div class="tile"><div class="label">Games with a line</div>
          <div class="value">${cov.games_with_any_line ?? 0}</div></div>
        <div class="tile"><div class="label">Games with movement</div>
          <div class="value">${cov.games_with_movement ?? 0}</div>
          <div class="sub">need two observations each</div></div>
        <div class="tile"><div class="label">Watching since</div>
          <div class="value" style="font-size:15px">${cov.watching_since
            ? when(cov.watching_since) : "–"}</div></div>
      </div></div>`;
    return;
  }

  const verdict = m.beats_coin_flip
    ? "The line moves toward us more often than chance."
    : "Not yet distinguishable from a coin flip.";

  root.innerHTML = `<div class="panel">
    <header><h2>Does the line move toward us?</h2>
      <span class="hint">${m.n} games where we had a view before the market settled</span></header>
    <div class="tiles">
      <div class="tile"><div class="label">Line moved our way</div>
        <div class="value ${m.beats_coin_flip ? "pos" : ""}">${pct(m.agreement_rate, 1)}</div>
        <div class="sub">${m.agreed} of ${m.n} · 95% CI ${pct(m.ci_low, 1)}–${pct(m.ci_high, 1)}</div></div>
      <div class="tile"><div class="label">Closing-line value</div>
        <div class="value ${(m.avg_clv ?? 0) > 0 ? "pos" : "neg"}">${signed(m.avg_clv, 2)}</div>
        <div class="sub">points per game vs the close</div></div>
      <div class="tile"><div class="label">Positive CLV</div>
        <div class="value">${pct(m.positive_clv_rate, 1)}</div>
        <div class="sub">share of games</div></div>
      <div class="tile"><div class="label">Average move</div>
        <div class="value">${num(m.avg_move, 2)}</div>
        <div class="sub">points, open to close</div></div>
    </div>
    <p class="note"><strong>${esc(verdict)}</strong> This is the sharpest test available,
      and a different question from "did the pick win". If our number carries information the
      opening market lacks, the line should drift toward us more often than away — that is
      measurable without any opinion about the final score, and line movement is far less noisy
      than results, so it needs a fraction of the sample an ATS record would.
      A 50% rate is the coin flip: the line was always going to move one way or the other.</p>
  </div>

  <div class="grid-2">
    <div class="panel"><header><h2>By how early we formed the view</h2>
      <span class="hint">an edge should be largest before the market has worked</span></header>
      <table><thead><tr><th>Lead time</th><th>Games</th><th>Moved our way</th><th>CLV</th></tr></thead>
      <tbody>${(m.by_lead_time || []).map((b) => `<tr>
        <td class="team">${esc(b.window)}</td><td>${b.n}</td>
        <td>${b.agreement_rate === null ? "–" : pct(b.agreement_rate, 1)}</td>
        <td>${b.avg_clv === null ? "–" : signed(b.avg_clv, 2)}</td></tr>`).join("")}</tbody>
      </table></div>
    <div class="panel"><header><h2>What we have witnessed</h2>
      <span class="hint">this measure cannot be backfilled</span></header>
      <div class="tiles">
        <div class="tile"><div class="label">Games with a line</div>
          <div class="value">${cov.games_with_any_line ?? 0}</div></div>
        <div class="tile"><div class="label">With movement</div>
          <div class="value">${cov.games_with_movement ?? 0}</div></div>
        <div class="tile"><div class="label">Snapshots</div>
          <div class="value">${(cov.snapshots ?? 0).toLocaleString()}</div></div>
      </div>
      <p class="note">Nobody publishes a history of intraday NFL line movement, so this
        accumulates only while the app is running before kickoff. Leaving it off between
        Sundays is the one thing that stops this page from ever filling in.</p>
    </div>
  </div>

  <div class="panel"><header><h2>Biggest moves</h2>
    <span class="hint">where the market revised most after we had formed a view</span></header>
    <div class="table-scroll"><table>
      <thead><tr><th>Game</th><th>Opened</th><th>Closed</th><th>Moved</th>
        <th>Our lean</th><th>Agreed</th><th>CLV</th></tr></thead>
      <tbody>${(m.biggest_moves || []).map((c) => `<tr>
        <td class="team">${esc(c.away)} <span class="muted">@</span> ${esc(c.home)}</td>
        <td>${num(c.open_spread, 1)}</td><td>${num(c.close_spread, 1)}</td>
        <td>${signed(c.movement, 1)}</td><td>${signed(c.lean, 1)}</td>
        <td>${c.agreed ? '<span class="badge" style="border-color:var(--good);color:var(--success-text)">yes</span>'
             : '<span class="badge">no</span>'}</td>
        <td>${signed(c.clv, 1)}</td></tr>`).join("")}</tbody>
    </table></div></div>`;
}

// --------------------------------------------------------------- settings
/* What you have to tell this app, and where it goes.

   Everything here was an environment variable, which is fine in a terminal and
   useless in a packaged app: there is no shell to export from. Each field says
   what breaks without it, because "Odds API key" answers nothing on its own —
   the question being asked is "what do I need to fill in, and what happens if
   I don't". */
async function renderSettings() {
  const root = $("#view");
  const data = await api("/api/settings");

  const field = (s) => {
    const id = `set-${s.key}`;
    if (s.kind === "bool") {
      const on = String(s.value).toLowerCase() === "true";
      return `<label class="switch"><input type="checkbox" id="${id}"
        data-key="${esc(s.key)}" ${on ? "checked" : ""} /><span>Enabled</span></label>`;
    }
    // A secret is never sent to the browser, so the box starts empty with the
    // stored key's last four characters as its placeholder: enough to see that
    // something is saved, useless to anyone reading over your shoulder.
    const type = s.kind === "secret" ? "password" : (s.kind === "number" ? "number" : "text");
    const placeholder = s.kind === "secret" && s.is_set
      ? `saved — ${s.masked} — type to replace` : s.placeholder;
    return `<input type="${type}" id="${id}" data-key="${esc(s.key)}"
      value="${esc(s.kind === "secret" ? "" : s.value)}"
      placeholder="${esc(placeholder)}" autocomplete="off" spellcheck="false" />`;
  };

  root.innerHTML = `${(data.groups || []).map((g) => `<div class="panel">
    <header><h2>${esc(g.name)}</h2></header>
    <div class="settings">
      ${g.settings.map((s) => `<div class="setting">
        <div class="set-head">
          <label for="set-${esc(s.key)}">${esc(s.label)}</label>
          <span class="set-state ${s.explicit ? "on" : ""}">${
            s.explicit
              ? (s.source === "file" ? "saved" : "from environment")
              : (s.is_set ? "default" : "not set")}</span>
        </div>
        ${field(s)}
        <div class="set-help">${esc(s.help)}${s.link
          ? ` <a href="${esc(s.link)}" target="_blank" rel="noopener">${esc(s.link)}</a>` : ""}</div>
        ${s.needed_for ? `<div class="set-need"><b>Needed for:</b> ${esc(s.needed_for)}</div>` : ""}
        ${s.key === "ODDS_API_KEY"
          ? `<div class="controls"><button class="btn" id="test-odds">Test this key</button>
             <span id="odds-result" class="muted"></span></div>` : ""}
      </div>`).join("")}
    </div>
  </div>`).join("")}

  <div class="panel">
    <div class="controls">
      <button class="btn primary" id="save-settings">Save settings</button>
      <span id="save-result" class="muted"></span>
    </div>
    <p class="note">Written to <code>${esc(data.path)}</code>. Saved settings take
      effect on the next refresh — no restart. A secret is never sent back to this
      page, so an empty box means "leave it alone", not "clear it"; to remove a key,
      type a space and save.</p>
  </div>`;

  $("#test-odds")?.addEventListener("click", async (ev) => {
    const key = $("#set-ODDS_API_KEY").value.trim();
    const out = $("#odds-result");
    out.textContent = "checking…";
    ev.target.disabled = true;
    try {
      const r = await api("/api/settings/test-odds-key", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key }),
      });
      out.textContent = r.message;
      out.className = r.ok ? "pos" : "neg";
    } catch (err) {
      out.textContent = String(err);
      out.className = "neg";
    } finally {
      ev.target.disabled = false;
    }
  });

  $("#save-settings").addEventListener("click", async (ev) => {
    const values = {};
    $$("[data-key]", root).forEach((el) => {
      if (el.type === "checkbox") values[el.dataset.key] = el.checked ? "true" : "false";
      // An untouched secret box is empty, and sending that would clear a key
      // the page was never shown. Absent means "leave it".
      else if (el.value !== "") values[el.dataset.key] = el.value;
      else if (el.type !== "password") values[el.dataset.key] = "";
    });
    ev.target.disabled = true;
    const out = $("#save-result");
    try {
      const r = await api("/api/settings", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ values }),
      });
      out.textContent = `Saved ${r.saved.length} setting${r.saved.length === 1 ? "" : "s"}.`;
      out.className = "pos";
      await loadState();
      await renderSettings();
    } catch (err) {
      out.textContent = String(err);
      out.className = "neg";
    } finally {
      ev.target.disabled = false;
    }
  });
}

// -------------------------------------------------------------- assistant
/* A local model, given this app's own numbers.

   It is not a football oracle and is not asked to be one: it gets the board,
   the model's measured record and the scoreboard as JSON, and answers about
   those. Nothing leaves the machine — the app refuses any endpoint that is not
   loopback, so "offline" is enforced rather than promised. */
const chatLog = [];

async function renderAssistant() {
  const root = $("#view");
  const state_ = await api("/api/assistant/status").catch((e) => ({
    ready: false, message: String(e) }));

  if (!state_.ready) {
    root.innerHTML = `<div class="panel">
      <header><h2>Assistant</h2><span class="hint">not configured</span></header>
      <div class="empty" style="text-align:left;max-width:64ch;margin:0 auto">
        <p>${esc(state_.message)}</p>
        <p class="note" style="margin-top:14px">The assistant runs a model on this
          machine and talks to it over loopback only — an endpoint anywhere else is
          refused, so nothing you ask it can leave the computer. It sees this week's
          board, the model's measured record and the scoreboard, and nothing else.</p>
        <ol class="setup">
          <li>Install <a href="https://ollama.com/download" target="_blank" rel="noopener">Ollama</a>.</li>
          <li>Run <code>ollama pull qwen3.5:4b</code> once. About 2.5&nbsp;GB; a 4B
            model is plenty for reading a page of numbers and runs on a laptop.</li>
          <li>On <b>Settings</b>, set the endpoint to <code>http://127.0.0.1:11434/v1</code>
            and the model to <code>qwen3.5:4b</code>.</li>
        </ol>
      </div>
    </div>`;
    return;
  }

  const bubbles = chatLog.map((m) => `<div class="msg ${esc(m.role)}">
    <div class="msg-body">${esc(m.content)}</div></div>`).join("");

  root.innerHTML = `<div class="panel chat">
    <header><h2>Assistant</h2>
      <span class="hint">${esc(state_.model)} · on this machine · sees week
        ${state.week} of ${state.season}</span></header>
    <div class="chat-log" id="chat-log">${bubbles || `<div class="empty">
      Ask about this week's board, where the model disagrees with the market, or
      what its record actually says. It only knows what this app has.</div>`}</div>
    <div class="controls chat-input">
      <input type="text" id="chat-q" placeholder="e.g. where does the blind model disagree most with the book this week?" />
      <button class="btn primary" id="chat-send">Ask</button>
    </div>
    <div class="chat-suggest">
      ${["Which games does the blind model disagree with the market on?",
         "Is this model actually any good? Be blunt.",
         "Summarise this week in five lines."].map((q) =>
        `<button class="btn tiny" data-q="${esc(q)}">${esc(q)}</button>`).join("")}
    </div>
  </div>`;

  const send = async (question) => {
    if (!question.trim()) return;
    chatLog.push({ role: "user", content: question });
    await renderAssistant();
    const log = $("#chat-log");
    if (log) log.scrollTop = log.scrollHeight;
    try {
      const r = await api("/api/assistant/ask", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages: chatLog, season: state.season, week: state.week }),
      });
      chatLog.push({ role: "assistant", content: r.reply });
    } catch (err) {
      chatLog.push({ role: "assistant", content: `Could not answer: ${err}` });
    }
    await renderAssistant();
    const after = $("#chat-log");
    if (after) after.scrollTop = after.scrollHeight;
  };

  $("#chat-send").addEventListener("click", () => send($("#chat-q").value));
  $("#chat-q").addEventListener("keydown", (e) => {
    if (e.key === "Enter") send(e.target.value);
  });
  $$("[data-q]", root).forEach((b) =>
    b.addEventListener("click", () => send(b.dataset.q)));
  const log = $("#chat-log");
  if (log) log.scrollTop = log.scrollHeight;
}

// -------------------------------------------------------------------- shell
const VIEWS = { home: renderHome, teams: renderTeams,
  picks: renderPicks, news: renderNews, edge: renderEdge,
  scoreboard: renderScoreboard, performance: renderPerformance,
  assistant: renderAssistant, settings: renderSettings };

async function render() {
  const view = VIEWS[state.tab] || renderHome;
  // The hero reports which season and week are on screen, so it has to follow
  // the selectors rather than only the last state load.
  if (state.meta) renderHero(state.meta);
  try {
    await view();
    foldPanels($("#view"));
  } catch (err) {
    $("#view").innerHTML = `<div class="panel"><div class="empty">
      Could not load this view: ${esc(err.message)}</div></div>`;
  }
}

async function loadState() {
  const meta = await api("/api/state");
  state.meta = meta;
  if (state.season === null) state.season = meta.season;
  if (state.week === null) state.week = meta.week;
  state.weeks = meta.weeks && meta.weeks.length ? meta.weeks
    : Array.from({ length: 18 }, (_, i) => i + 1);
  renderStatus(meta);

  const sel = $("#week");
  if (sel.options.length !== state.weeks.length) {
    sel.innerHTML = state.weeks.map((w) => `<option value="${w}">Week ${w}</option>`).join("");
  }
  sel.value = String(state.week);

  // Seasons the app has games for. A season it was never running for is simply
  // absent until it is backfilled, so the list is what exists rather than a
  // range of years that mostly lead to empty boards.
  const seasons = meta.seasons && meta.seasons.length ? meta.seasons : [state.season];
  const seasonSel = $("#season");
  seasonSel.innerHTML = seasons
    .slice().reverse()
    .map((y) => `<option value="${y}">${y}</option>`).join("");
  seasonSel.value = String(state.season);
  $("#refreshed").textContent = `Updated ${ago(meta.last_recompute)}`;
  renderHero(meta);
}

function setTab(tab) {
  state.tab = tab;
  $$(".tab").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === tab)));
  $("#week-wrap").classList.toggle("hidden", !["home", "games", "picks"].includes(tab));
  render();
}

function initTheme() {
  const saved = localStorage.getItem("nflpicker-theme");
  if (saved) document.documentElement.setAttribute("data-theme", saved);
  $("#theme").addEventListener("click", () => {
    const current = document.documentElement.getAttribute("data-theme");
    const isDark = current === "dark" ||
      (!current && matchMedia("(prefers-color-scheme: dark)").matches);
    const next = isDark ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem("nflpicker-theme", next);
    render();
  });
}

async function main() {
  initTheme();
  $$(".tab").forEach((b) => b.addEventListener("click", () => setTab(b.dataset.tab)));
  $("#week").addEventListener("change", (e) => { state.week = Number(e.target.value); render(); });
  $("#season").addEventListener("change", async (e) => {
    state.season = Number(e.target.value);
    // Week numbers are per season, and the one being viewed may not exist in
    // the season being switched to, so the week list is reloaded rather than
    // carried across.
    const meta = await api(`/api/state?season=${state.season}`).catch(() => null);
    if (meta && meta.weeks?.length) {
      state.week = meta.weeks.includes(state.week) ? state.week : meta.weeks[0];
    }
    await render();
  });
  $("#close-detail").addEventListener("click", () => $("#detail").close());

  const refreshBtn = $("#refresh");
  refreshBtn.addEventListener("click", async () => {
    if (state.busy) return;
    state.busy = true;
    // The button is an icon now, so progress is shown by spinning it rather
    // than by replacing its label -- writing text into it would delete the SVG.
    refreshBtn.disabled = true;
    refreshBtn.classList.add("spinning");
    try {
      await api("/api/refresh", { method: "POST" });
      await loadState();
      await render();
    } catch (err) {
      alert(`Refresh failed: ${err.message}`);
    } finally {
      state.busy = false;
      refreshBtn.disabled = false;
      refreshBtn.classList.remove("spinning");
    }
  });

  // The clock is the one thing on the page that must not wait for a refresh.
  setInterval(() => { if (state.meta) renderHero(state.meta); }, 30000);

  await loadState();
  await render();

  // The server refreshes on its own schedule; poll so an open tab reflects it
  // without the user reaching for reload.
  setInterval(async () => {
    if (state.busy || document.hidden) return;
    const before = state.meta?.last_recompute;
    try {
      await loadState();
      if (state.meta?.last_recompute !== before) await render();
    } catch { /* transient: the next tick retries */ }
  }, 30000);
}

main();
