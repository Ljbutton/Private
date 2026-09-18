import { barChart, condense, lineChart, sparkline } from "./charts.js";
import {
  advanceHand, ago, american, clockAngles, esc, greetingLine, kickoffShort,
  liveLabel, num, pct,
  signed, statusClass, when,
} from "./format.js";

const state = { season: null, week: null, weeks: [], tab: "home", meta: null,
  busy: false, trackTeam: null };

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) throw new Error(`${path} → ${res.status}`);
  return res.json();
}


/* A compact "Q3 · 4:05 · 2nd & 7 · red zone" for a game in progress. */
/* `short` drops the down and distance. On the board the stamp shares one
   narrow strip with the three totals, and a four-segment label is the thing
   that pushed the totals out of it -- quarter and clock are what a card is
   scanned for, and the situation is one click away in the game itself. */

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
    // Amber, not red, for a feed the app is built to run without: the Market
    // column reads "–" and everything else is unaffected. Red should mean
    // something is broken. The reason is on the tooltip either way.
    const cls = s.ok ? "ok" : (s.optional ? "warn" : "bad");
    const note = s.ok ? (s.detail || "")
      : `${s.optional ? "Optional feed unavailable" : "Failed"} — ${s.detail || "no detail"}`;
    rows.push(conn(cls, s.source, ago(s.ts), note));
  }
  if (meta.odds_usage) {
    const u = meta.odds_usage;
    // All three windows, because which one is about to stop you is the only
    // useful thing this line can say. The month is the bill; the day and week
    // are burst ceilings. Whichever is tightest is the one worth warning on.
    const tight = Math.min(
      u.day_budget ? u.day_remaining / u.day_budget : 1,
      u.week_budget ? u.week_remaining / u.week_budget : 1,
      u.budget ? u.remaining_budget / u.budget : 1);
    const perPoll = 3;   // h2h + spreads + totals, one credit each
    rows.push(conn(tight < 0.15 ? "warn" : "ok", "Odds API",
      `${u.used}/${u.budget}`,
      `Today ${u.day_used ?? 0}/${u.day_budget ?? "–"} · `
      + `week ${u.week_used ?? 0}/${u.week_budget ?? "–"} · `
      + `month ${u.used}/${u.budget}. Each poll costs ${perPoll} credits `
      + `(spread, total, moneyline), so the month allows about `
      + `${Math.round(u.budget / 30 / perPoll)} polls a day.`));
  } else if (!meta.has_odds_key && !meta.demo) {
    rows.push(conn("warn", "Odds API", "no key", "single consensus line only"));
  }
  $("#statusbar").innerHTML = rows.join("");
}

/* ------------------------------------------------------------------ hero */


/* The brandmark keeps the time.

   Angles are written as CSS transforms rather than as the SVG attribute so
   they can be transitioned, and they are remembered between calls because
   `advanceHand` needs to know where the hand already was. */
const handAt = { hour: null, minute: null };

function setBrandClock(now) {
  const want = clockAngles(now);
  for (const hand of ["hour", "minute"]) {
    const el = $(`#bm-${hand}`);
    if (!el) continue;
    handAt[hand] = advanceHand(handAt[hand], want[hand]);
    el.style.transform = `rotate(${handAt[hand]}deg)`;
  }
}

function renderHero(meta) {
  const now = new Date();
  setBrandClock(now);
  $("#clock").textContent = now.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  $("#clockdate").textContent = now
    .toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" })
    .toUpperCase();
  // The name comes off this computer's account unless Settings overrides it,
  // and the tooltip says which -- a guessed name someone cannot see how to
  // change is worse than no name at all.
  const user = meta.user || {};
  const greet = $("#greeting");
  greet.textContent = greetingLine(now, user.name);
  greet.title = user.name && user.source !== "settings"
    ? "Read from this computer's account. Settings → Your name changes it."
    : "";

  /* Just "Live". The refresh cadence used to be spelled out beside it, which
     is a fact about the app's plumbing rather than about the season, and it
     made the top-left corner read like a status page. It moves to the tooltip,
     still read from the scheduler so it cannot drift from what is actually
     happening. */
  const jobs = (meta.scheduler && meta.scheduler.jobs) || [];
  const fastest = jobs.reduce((min, j) => {
    const s = j.next_interval_seconds || j.interval_seconds;
    return s && (!min || s < min) ? s : min;
  }, 0);
  const cadence = $("#cadence");
  cadence.textContent = "Live";
  cadence.title = fastest
    ? `Updates every ${fastest >= 60 ? `${Math.round(fastest / 60)} min` : `${fastest}s`}`
    : "";

  // What you are looking at *is* the headline. The date used to sit here too
  // and again in the clock two inches to the right, so it said nothing twice.
  // Reads the viewed season rather than the current one, so browsing 2024 does
  // not leave a line at the top insisting it is 2026.
  const season = state.season || meta.season;
  const week = state.week || meta.week;
  const live = season === meta.season && week === meta.week;
  const line = $("#whenline");
  line.textContent = `Week ${week} · ${season} season`;
  line.classList.toggle("past", !live);
  line.title = live ? "The current week" : "Not the current week";
}

/* How serious an injury status is, for colour. Out and IR are settled; a
   questionable is a coin flip that still moves a line by a point. */

/* "Sun 1:00" — the board has sixteen rows and no width to spare for a date
   that is the same on most of them. */

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
// Picks is no longer folded: both contests are meant to be answered in one
// look, and a collapsed Survivor panel is the opposite of putting them on one
// page.
// Teams no longer folds at all: its two panels are the page. Performance still
// does, but only the closing-line value at the bottom -- the season table and
// the by-team table are marked data-nofold, because folding the thing a page
// exists to show is how a page ends up looking empty.
const FOLDING_TABS = new Set(["performance"]);

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

/* Settings opens with every section shut -- four headings you can read at a
   glance beat two panels of fields you have to scroll past -- and remembers
   what you opened, so a refresh mid-edit does not fold the box you are in. */
const openSettings = new Set();

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

/* Did the line move toward your picks after you made them?

   A win rate needs hundreds of games before it says anything, and you will get
   a few dozen a season. The market's own revision is far less noisy and answers
   a question results cannot: whether you saw something before the price did.
   It is also how a sportsbook decides you are sharp, and it does not care
   whether the game then went your way. */
function clvBlock(clv) {
  if (!clv) return "";
  if (!clv.n) {
    return `<div class="panel">
      <header><h2>Your closing-line value</h2></header>
      <div class="empty">${esc(clv.note || "Nothing to measure yet.")}</div>
    </div>`;
  }
  const good = clv.average > 0;
  return `<div class="panel">
    <header><h2>Your closing-line value</h2>
      <span class="hint">${clv.n} pick${clv.n === 1 ? "" : "s"} the line moved after</span></header>
    <div class="tiles">
      <div class="tile"><div class="label">Points vs the close</div>
        <div class="value ${good ? "pos" : "neg"}">${signed(clv.average, 2)}</div>
        <div class="sub">per pick, averaged</div></div>
      <div class="tile"><div class="label">Beat the close</div>
        <div class="value ${clv.beat_rate > 0.5 ? "pos" : ""}">${pct(clv.beat_rate, 0)}</div>
        <div class="sub">${clv.beat} of ${clv.n} picks</div></div>
    </div>
    <div class="table-scroll"><table class="slate">
      <thead><tr><th>Wk</th><th>Pick</th><th>Game</th>
        <th class="num">You got</th><th class="num">Closed</th>
        <th class="num">Value</th></tr></thead>
      <tbody>${clv.picks.map((p) => `<tr>
        <td class="num muted">${p.week}</td>
        <td class="who">${esc(p.selection)}</td>
        <td class="muted">${esc(p.matchup)}</td>
        <td class="num">${signed(p.line_at_pick, 1)}</td>
        <td class="num muted">${signed(p.line_at_close, 1)}</td>
        <td class="num ${p.clv > 0 ? "hit" : (p.clv < 0 ? "miss" : "")}">${signed(p.clv, 1)}</td>
      </tr>`).join("")}</tbody></table></div>
    <p class="note">Positive means you took a better number than the one that closed —
      you backed a team at &minus;3 and it closed &minus;5, so you have two points of value
      whether or not they covered. This is the one honest early read on whether
      <em>you</em> are any good: it needs a fraction of the sample a win rate does, because
      it measures the market agreeing with you rather than the game going your way.</p>
  </div>`;
}

// ------------------------------------------------------------- performance
/* Who is actually picking these best. Straight-up winners only: every source
   here names a favourite, so it is the one question all of them can be asked.

   This was the Scoreboard tab, and there was a separate Performance tab
   grading the model against the spread. Two pages answering "is this any
   good" is one page too many, and of the two this is the one that answers it
   in the terms a pool player thinks in: you, the model, the book, side by
   side, on games everybody called. */
async function renderPerformance(ticket) {
  const root = $("#view");
  const d = await api(`/api/scoreboard?season=${state.season}`);
  if (stale(ticket)) return;
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
        ${note ? `<button class="why" type="button" title="${esc(note)}"
          aria-label="${esc(note)}">?</button>` : ""}
        <div class="who-sub">${esc((d.descriptions || {})[key] || "")}</div></td>
      ${cell(all)}${cell(common)}
    </tr>`;
  };

  /* Sorting the team table. Every column is a different question -- which
     teams *we* read best, which ones the market reads best, where the two
     disagree -- and the answer to each is one click, not a different page. */
  const sort = state.sbSort && (state.sbSort.key === "team" || state.sbSort.key === "games"
    || pickers.includes(state.sbSort.key))
    ? state.sbSort
    // Opens on the blend -- what the app actually claims -- rather than on the
    // first column, which is "you" and is empty until picks have been graded.
    : { key: ["model", "blind", "book"].find((k) => pickers.includes(k))
             || pickers[0] || "team", dir: "desc" };

  const sortValue = (t, key) => {
    if (key === "team") return t.team;
    if (key === "games") return t.games;
    const v = t.tallies[key];
    // A picker with no view on a team sorts last either way rather than
    // landing at the top of an ascending sort as if it scored zero.
    return v && v.n ? v.rate : null;
  };
  const sortedTeams = [...(d.teams || [])].sort((a, b) => {
    const av = sortValue(a, sort.key);
    const bv = sortValue(b, sort.key);
    if (av === null && bv === null) return a.team.localeCompare(b.team);
    if (av === null) return 1;
    if (bv === null) return -1;
    const cmp = typeof av === "string" ? av.localeCompare(bv) : av - bv;
    return sort.dir === "desc" ? -cmp : cmp;
  });

  const sortHead = (key, label, cls) => {
    const on = key === sort.key;
    return `<th class="${cls}${on ? " sorted" : ""}" data-sort="${esc(key)}"
      aria-sort="${on ? (sort.dir === "desc" ? "descending" : "ascending") : "none"}"
      title="Sort by ${esc(label)}" tabindex="0" role="button"
      >${esc(label)}<span class="sort-arrow">${on ? (sort.dir === "desc" ? "▾" : "▴") : "⇅"}</span></th>`;
  };

  root.innerHTML = `<div class="panel" data-nofold>
    <header><h2>Season ${d.season}</h2>
      <span class="hint">Straight-up winners · "same games" scores only games every
        picker had a view on</span></header>
    <div class="table-scroll"><table class="slate totals">
      <thead><tr><th>Picker</th><th class="num">All their picks</th>
        <th class="num">Same games</th></tr></thead>
      <tbody>${pickers.map((p) => totalRow(p, d.labels[p])).join("")}</tbody>
    </table></div>

    <h3 class="sub-head">Week by week<span class="hint"> · correct out of picked</span></h3>
    <div class="table-scroll"><table class="slate">
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
    </table></div>
  </div>

  <div class="panel" data-nofold>
    <header><h2>By team</h2>
      <span class="hint">how often each picker called that team's games right ·
        click a column to sort by it</span></header>
    <div class="table-scroll"><table class="slate sortable">
      <thead><tr>
        ${sortHead("team", "Team", "")}
        ${sortHead("games", "Games", "num")}
        ${(d.pickers || []).map((p) => sortHead(p, d.labels[p], "num")).join("")}
      </tr></thead>
      <tbody>${sortedTeams.map((t) => `<tr>
        <td class="who">${esc(t.team)}</td>
        <td class="num muted">${t.games}</td>
        ${d.pickers.map((p) => {
          const v = t.tallies[p];
          if (!v.n) return `<td class="num muted${p === sort.key ? " sorted" : ""}">–</td>`;
          // Above half is being read well, below it badly; the midpoint is
          // where a coin would sit, so it is the only sensible split.
          const tone = v.rate > 0.5 ? " hit" : (v.rate < 0.5 ? " miss" : "");
          return `<td class="num${tone}${p === sort.key ? " sorted" : ""}">${
            pct(v.rate)}<span class="rec">${v.correct}-${v.wrong}</span></td>`;
        }).join("")}
      </tr>`).join("")}</tbody>
    </table></div>
  </div>

  ${clvBlock(d.clv)}`;

  /* Re-sorting is a re-render of this view, not a reload: the payload is
     already here and the server has no opinion about column order. */
  $$("th[data-sort]", root).forEach((th) => {
    th.addEventListener("click", () => {
      const key = th.dataset.sort;
      state.sbSort = key === sort.key
        ? { key, dir: sort.dir === "desc" ? "asc" : "desc" }
        // A new column starts on its most useful end: best first for a rate,
        // A-Z for the team name.
        : { key, dir: key === "team" ? "asc" : "desc" };
      renderScoreboard();
    });
  });
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
    return `<span class="live-dot"></span>LIVE${
      g.live ? ` · ${esc(liveLabel(g.live, true))}` : ""}`;
  }
  const d = g.kickoff ? new Date(g.kickoff) : null;
  const date = d && !Number.isNaN(d.getTime())
    ? d.toLocaleDateString(undefined, { month: "numeric", day: "numeric" }) : "";
  if (g.status === "final") return `Final${date ? ` · ${date}` : ""}`;
  return esc(kickoffShort(g.kickoff)) || "Scheduled";
}

async function renderHome(ticket) {
  const root = $("#view");
  const data = await api(`/api/games?week=${state.week}&season=${state.season}`);
  if (stale(ticket)) return;
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
      const borrowed = kind === "ours" && inherited;
      return `<div class="gcell ${kind}${picked ? " picked" : ""}${verdict}${
        borrowed ? " borrowed" : ""}"${borrowed
        ? ' title="This game finished before the app was running, so the model has no number of its own. Its pick is the sportsbook\'s; the spread and total are left blank rather than copied, which would read as the model agreeing on them."'
        : ""}>
        <span class="gline">${homeLine === undefined ? "" : esc(lineText(homeLine, side))}</span>
        <span class="gprob">${prob === null ? "–" : pct(prob)}${
          borrowed ? '<i class="est">*</i>' : ""}${picked ? `
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
        <span class="tname" title="${esc(t.full_name || abbr)}${
          side === "home" ? " (home)" : " (away)"}"><span class="nick">${
            esc(t.name || abbr)}</span><span class="abbr">${esc(abbr)}</span></span>
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
           title="${signed(moved)} points: how far the line has moved ${
             moved > 0 ? "toward" : "away from"} our side since it opened"
           >${signed(moved)}</span>`;

    const ourTotal = p && p.fair_total ? num(p.fair_total, 1) : null;
    const blindTotal = p && p.total_points ? num(p.total_points, 1) : null;
    const bookTotal = g.market?.total_points;

    return `<article class="gcard" data-game="${esc(g.game_id)}" tabindex="0">
      <div class="gcard-top">
        <span class="gstate">${gameStamp(g)}</span>
        ${movedBadge}
        <span class="gopen" title="Open this game">&rsaquo;</span>
      </div>
      <div class="gcard-grid">
        <div class="ghead you">You</div>
        <div class="ghead blind" title="Blind model — the projection before it is ever shown the line. The only column here independent of the market.">Blind</div>
        <div class="ghead ours" title="Our blend — that same model blended with the line. This is what the app actually claims.">Blend</div>
        <div class="ghead book" title="Sportsbook consensus, with the vig removed">Book</div>
        <div class="ghead pmkt" title="Prediction markets — Kalshi and Polymarket contract prices">Mkt</div>
        ${teamRow("away")}
        ${teamRow("home")}
        <!-- The projected total, as its own row under the two teams rather
             than squeezed into the corner of the top strip. It belongs in the
             grid: each figure then sits under the column it came from, so
             "which of these three is the book's" is answered by position
             instead of by remembering the order in a tooltip. -->
        <div class="gtot-label" title="Projected total points for the game">Total</div>
        <div class="gtot" title="Blind model's projected total">${
          blindTotal === null ? "–" : blindTotal}</div>
        <div class="gtot" title="Our blend's projected total">${
          ourTotal === null ? "–" : ourTotal}</div>
        <div class="gtot" title="Sportsbook total">${num(bookTotal, 1)}</div>
        <div class="gtot"></div>
      </div>
    </article>`;
  };

  root.innerHTML = `<div class="panel board">
    <header><h2>${data.season} · Week ${data.week} — the whole slate</h2>
      <span class="hint">Home team listed second · Blind = before the line ·
        Blend = what we claim · Book = sportsbook · Market = Kalshi/Polymarket ·
        a tick marks each source's pick</span></header>
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
      await render();
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
        <span class="hint">consensus against our number · the band is how far our
          line moved inside each step, since it is recomputed far more often than
          the market moves · thin grey lines are individual books</span></header>
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
    // Condensed too, but with no band: these are already background context at
    // 30% opacity, and a band behind each of eight books is a grey wash.
    points: condense(pts.map((p) => ({ x: p.captured_at, y: p.value }))).points,
  }));
  const consensusPts = (d.movement.spread.points || []).map((p) => ({ x: p.captured_at, y: p.value }));

  /* The two lines are sampled on completely different clocks, and that is why
     the model's used to read as a solid block of sawtooth.

     The market changes when a book moves its number -- a handful of times in a
     week, each one real -- so its series is a step function and every vertex
     means something. The model is written on every recompute, which is a timer,
     so two days produce hundreds of points whose *spacing* carries no
     information at all. Half a point of wobble between consecutive runs then
     fills the plot edge to edge and buries the trend underneath it.

     So the model series is condensed to roughly one point per few pixels,
     plotted at each bin's median, with the range it covered drawn as a faint
     band behind it. The market is left alone: binning a step function would
     round off the corners, which are the only part of it worth seeing. */
  const modelSpread = condense(history.map((h) => ({
    x: h.captured_at,
    y: h.margin_home === null ? null : -h.margin_home,   // model's implied home line
  })));
  lineChart($("#chart-spread"), [
    ...bookSeries,
    { name: "Consensus", short: "market", color: "var(--series-2)", points: consensusPts },
    { name: "Our line", short: "model", color: "var(--series-1)",
      points: modelSpread.points, band: modelSpread.band },
  ], { height: 230, yFormat: (v) => signed(v, 1), ariaLabel: "spread movement over time" });

  const modelTotal = condense(history.map((h) => ({ x: h.captured_at, y: h.total_points })));
  lineChart($("#chart-total"), [
    { name: "Market total", short: "market", color: "var(--series-2)",
      points: (d.movement.total.points || []).map((p) => ({ x: p.captured_at, y: p.value })) },
    { name: "Our total", short: "model", color: "var(--series-1)",
      points: modelTotal.points, band: modelTotal.band },
  ], { height: 200, ariaLabel: "total movement over time" });
}

// ------------------------------------------------------------------- teams
/* Ours against everybody else's, side by side.

   The consensus is the average of published top-32s, pooled over weeks 1 and 2.
   It is opinion rather than measurement, so it is not a scoreboard — but where
   our ranking and a *tight* consensus differ by a dozen places, one of us has
   found something. Both tables carry the same signed gap so a team can be
   followed across: +4 on the left means the published lists put that team four
   places lower than we do, and the same team reads −4 on the right. */

/* The shared delta. "=" rather than "0" because a zero in a column of signed
   numbers reads as a missing value, and agreeing exactly is worth seeing. */

async function renderTeams(ticket) {
  const root = $("#view");
  const [data, history] = await Promise.all([
    api("/api/teams"),
    // Movement against the previous week we actually hold. Its own request
    // because a missing history must cost the ranking nothing.
    api("/api/power/history").catch(() => ({ teams: [], compared_to: null })),
  ]);
  if (stale(ticket)) return;

  // Season win totals only exist when the odds feed publishes futures.
  const hasWinTotals = data.teams.some(
    (t) => t.win_total_line !== null && t.win_total_line !== undefined);

  /* How far a team has moved since the last week we hold a ranking for.

     Blank when there is nothing to compare against, for every team, rather
     than a column of zeroes. A zero is a claim -- "this team held its place"
     -- and in week one nobody has held anything yet. `compared_to` is null
     exactly when no earlier week exists, which is what makes the distinction
     available at all. */
  const moved = {};
  if (history.compared_to !== null && history.compared_to !== undefined) {
    for (const row of history.teams || []) {
      if (row.move !== null && row.move !== undefined) moved[row.team] = row.move;
    }
  }
  const moveCell = (abbr) => {
    const m = moved[abbr];
    if (m === undefined) return '<td class="move"></td>';
    if (m === 0) return '<td class="move muted">—</td>';
    return `<td class="move ${m > 0 ? "up" : "down"}">${m > 0 ? "▲" : "▼"}${
      Math.abs(m)}</td>`;
  };

  /* Four columns and a mark. Everything else -- the rating, the Pythagorean,
     the 80% range, division and title odds, the win total -- moves into the
     card a row opens. The table had eleven columns of numbers and was, in the
     reader's words, hard to read; the answer to that is not a smaller font. */
  const row = (t) => `<tr data-team="${esc(t.team)}" tabindex="0">
    <td class="rk">${t.rank}</td>
    <td class="team">${teamMark(t.team)}<span class="tname">${esc(t.name)}</span></td>
    <td class="num">${t.record.wins ?? 0}-${t.record.losses ?? 0}${
      t.record.ties ? `-${t.record.ties}` : ""}</td>
    <td class="num"><b>${num(t.exp_wins, 1)}</b></td>
    <td class="num">${pct(t.playoff_prob)}</td>
    ${moveCell(t.team)}
  </tr>`;

  const head = `<thead><tr><th class="rk">#</th><th>Team</th>
    <th class="num">Rec</th>
    <th class="num" title="Expected wins from 20,000 simulations of the remaining schedule">Proj</th>
    <th class="num" title="Chance of reaching the playoffs">Playoff</th>
    <th class="move" title="Places moved since the last week held">Move</th></tr></thead>`;

  // Top sixteen beside bottom sixteen: the whole league on one screen, which
  // is the only way the bottom half is ever looked at.
  const half = Math.ceil(data.teams.length / 2);
  const table = (teams) => `<table class="rank-table">${head}
    <tbody>${teams.map(row).join("")}</tbody></table>`;

  root.innerHTML = `
  <div class="panel" data-nofold>
    <header><h2>Power rankings</h2>
      <span class="hint">by projected finish, the Pythagorean, the rating and
        title odds — not by record · click a team for the rest</span></header>
    <div class="rank-split">
      <div class="table-scroll">${table(data.teams.slice(0, half))}</div>
      <div class="table-scroll">${table(data.teams.slice(half))}</div>
    </div>
    ${hasWinTotals ? "" : `<p class="note">No season win-total lines are
      available from the odds feed right now, so the market columns in each
      card are blank. Nothing else is affected.</p>`}
  </div>
  <div class="panel" id="team-detail-panel" data-nofold>
    <header><h2>Simulated win distribution</h2>
      <span class="hint" id="dist-who"></span></header>
    <div class="team-card" id="team-card"></div>
    <div id="team-dist" style="height:200px"></div>
  </div>`;

  const card = (t) => {
    const cell = (label, value, hint) => `<div class="fact"${
      hint ? ` title="${esc(hint)}"` : ""}>
      <div class="fact-label">${esc(label)}</div>
      <div class="fact-value">${value}</div></div>`;
    return [
      cell("Rating", signed(t.power),
           "Points better than an average team on a neutral field"),
      /* A dash here could mean "no games yet" or "this field never got
         written", and those are different answers. The number is worked out
         from the season's scores server-side when the rating row does not
         carry it, so a blank now means only the first one. */
      cell("Pythag", t.pythagorean === null || t.pythagorean === undefined
        ? '<span class="muted">no games yet</span>' : pct(t.pythagorean),
           "Win expectation implied by points scored and allowed"),
      cell("80% range", `${num(t.wins_p10, 0)}–${num(t.wins_p90, 0)}`,
           "Where four seasons in five finish"),
      cell("Division", pct(t.division_prob)),
      cell("Title", pct(t.sb_prob, 1)),
      ...(hasWinTotals ? [
        cell("Win total", num(t.win_total_line, 1), "The posted market line"),
        cell("Over", pct(t.over_prob),
             "Chance of finishing above the posted line"),
      ] : []),
    ].join("");
  };

  const show = (abbr) => {
    const team = data.teams.find((t) => t.team === abbr);
    if (!team) return;
    $("#dist-who").textContent = `click any team above · showing ${team.name}`;
    $("#team-card").innerHTML =
      `<div class="card-head">${teamMark(team.team)}<b>${esc(team.name)}</b>
        <span class="muted">#${team.rank} · ${num(team.exp_wins, 1)} expected wins</span>
      </div><div class="facts">${card(team)}</div>`;
    $$("#view tr[data-team]").forEach(
      (tr) => tr.classList.toggle("on", tr.dataset.team === abbr));
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

  // No team picker: the ranking above *is* the picker. A second control that
  // selects the same thing as the row you just clicked is a second place for
  // the two to disagree, and one more thing to look at on a page whose whole
  // complaint was that there was too much to look at.
  wireLogos(root);
  $$("#view tr[data-team]", root).forEach((tr) => {
    tr.addEventListener("click", () => show(tr.dataset.team));
    tr.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); show(tr.dataset.team); }
    });
  });
  if (data.teams.length) show(data.teams[0].team);
}

// ------------------------------------------------------------------- picks
async function renderPicks(ticket) {
  const root = $("#view");
  const data = await api(`/api/picks?week=${state.week}&season=${state.season}`);
  if (stale(ticket)) return;
  const pickem = data.pickem || {};
  const survivor = data.survivor || {};

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
  /* This week's options as one list, the recommendation included and marked,
     rather than a pick in one panel and a table of "alternatives" in another.
     Deviating is not a separate subject from choosing -- it is the same choice
     -- and splitting them meant reading the cost of a switch two panels away
     from the thing it would replace. */
  const rec = survivor.recommendation;
  const altRows = [
    ...(rec ? [{
      team: rec.team, opponent: rec.opponent, win_prob: rec.win_prob,
      path_survival: survivor.survival_prob, cost: 0, picked: true,
    }] : []),
    ...(survivor.alternatives || []).filter((a) => !rec || a.team !== rec.team),
  ].map((a) => `<div class="alt-row${a.picked ? " picked" : ""}">
    <span class="alt-team">${teamMark(a.team)}<strong>${esc(a.team)}</strong>
      <span class="muted">vs ${esc(a.opponent)}</span></span>
    <span class="alt-win">${pct(a.win_prob, 1)}</span>
    <span class="alt-cost ${a.picked ? "muted" : (a.cost > 0 ? "neg" : "pos")}">${
      a.picked ? "the pick"
        : (a.cost > 0 ? `−${pct(a.cost, 2)}` : `+${pct(-a.cost, 2)}`)}</span>
  </div>`).join("");

  root.classList.add("fit-screen");
  root.innerHTML = `
  <div class="grid-2 pick-split">
  <div class="panel">
    <header><h2>ESPN pick'em</h2>
      <span class="hint" title="Confidence points are assigned highest-to-most-likely, which maximises expected score. Leverage mode deliberately gives some of that up to differentiate from a field that picks close to the market — the right trade only when finishing first is what pays.">most confident first</span>
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
    </div>
    <div class="pickem-list">${pickRows || '<div class="empty">No games to pick.</div>'}</div>
  </div>

  <div class="pick-col">
  <div class="panel survivor-now">
    <header><h2>Survivor</h2>
      <span class="hint">${survivor.horizon ? `planned ${survivor.horizon} weeks ahead` : ""}</span></header>
    ${survivor.recommendation ? `
      <div class="tiles">
        <div class="tile"><div class="label">This week</div>
          <div class="value">${esc(survivor.recommendation.team)}</div>
          <div class="sub">vs ${esc(survivor.recommendation.opponent)} ·
            ${pct(survivor.recommendation.win_prob, 1)} to win</div></div>
        <div class="tile"><div class="label">Path survival</div>
          <div class="value">${pct(survivor.survival_prob, 1)}</div>
          <div class="sub">through week ${survivor.through_week
            || ((survivor.week || 0) + (survivor.horizon || 1) - 1)}</div></div>
      </div>
      <div class="alt-block">
        <h3>This week's options<span class="hint">win chance, then what taking
          that team instead costs across the whole remaining path</span></h3>
        <div class="alt-list">${altRows
          || '<div class="empty">No alternatives.</div>'}</div>
      </div>
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
    ` : `<div class="empty">${esc(survivor.note || "No survivor plan available.")}</div>`}
  </div>

  ${survivor.recommendation ? `
  <div class="panel survivor-run">
    <header><h2>The rest of the run</h2>
      <span class="hint" title="The recommendation is not always this week's safest team. Spending a strong team now can cost more later than it gains today, so the optimiser solves the whole remaining path — which is why the cost of switching, shown beside this week's options, is measured over this run rather than over Sunday.">every week from here · scroll ↓</span></header>
    <div class="table-scroll"><table class="slate">
      <thead><tr><th>Week</th><th>Team</th><th>Opponent</th>
        <th class="num">Win prob</th></tr></thead>
      <tbody>${path}</tbody></table></div>
  </div>` : ""}
  </div>
  </div>`;

  const modeSelect = $("#pickem-mode");
  modeSelect.value = state.pickemMode || "ev";
  modeSelect.addEventListener("change", () => {
    state.pickemMode = modeSelect.value;
    render();
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
      await render();
    });
  });
}

// -------------------------------------------------------------------- news
/* Two columns, not folded. This page is read by scanning rather than by
   looking one thing up: the question is "has anything changed that I should
   know before I pick", and the injury report is the half that answers it most
   often. Side by side, one scan covers both; stacked behind summaries it took
   two clicks to learn there was nothing new. */
async function renderNews(ticket) {
  const root = $("#view");
  const data = await api("/api/news?limit=80");
  if (stale(ticket)) return;
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

  /* Four columns, because four things are being asked: who, what, how long,
     how serious. The position and the last-updated timestamp came out -- the
     first is on the name for anyone who follows the team, and the second is a
     fact about our polling rather than about the player. The source's prose
     comment stays too, but as a tooltip: it is the fallback when the feed did
     not break the injury out into fields, not a column of its own. */
  const shown = (data.injuries || []).filter((i) => i.team === state.injuryTeam);
  const injuries = shown.map((i) => `<tr${i.detail
      ? ` title="${esc(i.detail)}"` : ""}>
    <td class="team">${esc(i.player)}${i.position
      ? ` <span class="muted">${esc(i.position)}</span>` : ""}</td>
    <td>${esc(i.injury || "–")}</td>
    <td class="muted">${esc(i.how_long || "–")}</td>
    <td><span class="inj ${esc(statusClass(i.status))}">${esc(i.status || "–")}</span></td>
    </tr>`).join("");

  root.innerHTML = `<div class="grid-2 news-split">
    <div class="panel">
      <header><h2>Injury report</h2>
        <span class="hint">questionable, doubtful, out, IR and PUP only —
          not the whole roster</span></header>
      <div class="team-picker">${picker}</div>
      ${injuries
        ? `<div class="table-scroll tall"><table class="slate roster">
            <thead><tr><th>Player</th>
              <th title="What is hurt, when the feed breaks it out. Hover a row for the full note.">Injury</th>
              <th title="How long they have been listed, and when they are expected back">How long</th>
              <th>Status</th></tr></thead>
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

  // The marks are drawn by the same teamMark() the board uses, and that draws
  // the logo at opacity 0 until it has actually loaded -- so without this the
  // badges rendered as bare abbreviations here while working on Home.
  wireLogos(root);

  // And the buttons had no listener at all: the picker was drawn, styled and
  // given a selected state that nothing could ever change.
  $$(".team-pick[data-team]", root).forEach((button) => {
    button.addEventListener("click", () => {
      state.injuryTeam = button.dataset.team;
      render();
    });
  });
}

// --------------------------------------------------------------- settings
/* What you have to tell this app, and where it goes.

   Everything here was an environment variable, which is fine in a terminal and
   useless in a packaged app: there is no shell to export from. Each field says
   what breaks without it, because "Odds API key" answers nothing on its own —
   the question being asked is "what do I need to fill in, and what happens if
   I don't". */
async function renderSettings(ticket) {
  const root = $("#view");
  // The backup list is not worth failing the whole page over: settings still
  // need editing on a machine where the directory cannot be read.
  const [data, backups] = await Promise.all([
    api("/api/settings"),
    api("/api/settings/backups").catch(() => ({ backups: [], directory: "", keep: 10 })),
  ]);
  if (stale(ticket)) return;

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

  const saveBar = (where) => `<div class="save-bar ${where}">
    <button class="btn primary" data-save>Save settings</button>
    <span class="save-result muted"></span>
    <span class="muted tiny">Written to <code>${esc(data.path)}</code> · takes
      effect on the next refresh, no restart</span>
  </div>`;

  /* Each group collapses, and two sit side by side. Open, stacked and full
     width, this was a very long page to scroll past to reach the one box you
     came for -- and the save button was stranded in the middle of it, which is
     the one place a save button should never be.
     One bar, at the top. There was a second at the foot of the page for when
     the groups were open and long; with everything closed by default the page
     is shorter than the screen, and a duplicate of the only button on it was
     costing the height that made that true. */
  /* The theme, where someone looking for a setting would look for it. The
     header toggle stays: one is for flipping it, the other is for finding it.
     It is not a stored setting like the rest -- the browser remembers the
     choice -- so it saves itself on change rather than waiting for the bar. */
  const themeRow = `<div class="setting">
    <div class="set-head"><label for="set-theme">Appearance</label>
      <span class="set-state on">saved here</span></div>
    <select id="set-theme">
      <option value="dark">Dark</option>
      <option value="light">Light</option>
    </select>
    <div class="set-help">The same switch as the one in the corner of every
      page, kept here because this is where a setting is looked for.</div>
  </div>`;

  root.innerHTML = `${saveBar("top")}
  <div class="settings-grid">
  ${(data.groups || []).map((g) => `<details class="panel set-group"${
    openSettings.has(g.name) ? " open" : ""} data-group="${esc(g.name)}">
    <summary><h2>${esc(g.name)}</h2>
      <span class="hint">${g.settings.length} setting${
        g.settings.length === 1 ? "" : "s"}</span>
      <svg class="chev" viewBox="0 0 24 24" aria-hidden="true">
        <path d="M9 6l6 6-6 6"/></svg></summary>
    <div class="settings">
      ${g.name === "General" ? themeRow : ""}
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
  </details>`).join("")}
  </div>
  <p class="note">A secret is never sent back to this page, so an empty box
    means "leave it alone", not "clear it"; to remove a key, type a space and
    save.</p>

  <div class="grid-2 tool-row">
  <div class="panel tool-panel">
    <header><h2>Prediction markets</h2>
      <span class="hint" title="Asks Kalshi and Polymarket directly and reports each one. The status dot in the sidebar can only say a feed failed; &quot;no NFL games right now&quot; and &quot;this machine cannot reach the host&quot; look identical from the outside and need opposite responses.">an optional feed — the board reads "–" without it</span>
      <div class="controls" style="margin-left:auto">
        <button class="btn" id="test-pmkt">Test connection</button>
      </div></header>
    <div class="tool-out"><span id="pmkt-result" class="muted"></span>
      <div id="pmkt-venues" class="backup-list"></div></div>
  </div>

  <div class="panel tool-panel">
    <header><h2>Backup</h2>
      <span class="hint">your picks, results and settings are one file —
        this copies it</span>
      <button class="why" type="button" aria-label="About backups"
        title="The app already writes a copy before it changes the database's shape, but that is one file per version and a second upgrade from the same version overwrites it — a safety net for the app's own changes, not a backup you should rely on. This one you asked for. The newest ${
          backups.keep ?? 10} are kept; older ones are removed so a growing database cannot quietly fill the disk. Copies live in ${
          esc(backups.directory || "")} — that folder is inside the data directory, so copy it somewhere else if you want it to survive losing this machine.">?</button>
      <div class="controls" style="margin-left:auto">
        <button class="btn" id="make-backup">Back up now</button>
      </div></header>
    <div class="tool-out"><span id="backup-result" class="muted"></span>
      <div id="backup-list" class="backup-list"></div></div>
  </div>
  </div>`;

  /* A refresh re-renders this whole page, which used to close every section
     the reader had opened -- including the one they were halfway through
     filling in. What is open is remembered for the session instead. */
  $$("details.set-group", root).forEach((d) => {
    d.addEventListener("toggle", () => {
      if (d.open) openSettings.add(d.dataset.group);
      else openSettings.delete(d.dataset.group);
    });
  });

  const paintBackups = (rows) => {
    const list = $("#backup-list");
    if (!list) return;
    list.innerHTML = (rows || []).length
      ? rows.map((b) => `<div class="backup-row">
          <span class="nm">${esc(b.name)}</span>
          <span class="muted">${ago(b.made_at)}</span>
          <span class="muted num">${(b.bytes / 1048576).toFixed(1)} MB</span>
        </div>`).join("")
      : '<div class="empty">No backups yet.</div>';
  };
  paintBackups(backups.backups);

  $("#test-pmkt")?.addEventListener("click", async (ev) => {
    const out = $("#pmkt-result");
    const venues = $("#pmkt-venues");
    out.textContent = "asking both venues…";
    out.className = "muted";
    venues.innerHTML = "";
    ev.target.disabled = true;
    try {
      const r = await api("/api/settings/test-prediction-markets", { method: "POST" });
      out.textContent = r.summary;
      out.className = r.ok ? "pos" : "neg";
      /* The per-series attempts are shown when a venue answers with nothing.
         "Quoting no NFL games" and "we asked under a ticker they renamed" are
         the same sentence from outside and need opposite fixes, so the ticker
         asked and the count returned go on screen rather than into a log. */
      venues.innerHTML = (r.venues || []).map((v) => `<div class="backup-row">
        <span class="nm">${v.ok ? "✓" : "✕"} ${esc(v.venue)}</span>
        <span class="muted">${esc(v.message)}</span>
      </div>${(v.attempts || []).map((a) => `<div class="backup-row probe">
        <span class="nm">${esc(a.series)}</span>
        <span class="muted">${a.error
          ? esc(a.error)
          : `${a.events} events · ${a.markets} markets${
              a.sample ? ` · e.g. ${esc(a.sample)}` : ""}`}</span>
      </div>`).join("")}`).join("");
    } catch (err) {
      out.textContent = String(err);
      out.className = "neg";
    } finally {
      ev.target.disabled = false;
    }
  });

  $("#make-backup")?.addEventListener("click", async (ev) => {
    const out = $("#backup-result");
    out.textContent = "copying…";
    out.className = "muted";
    ev.target.disabled = true;
    try {
      const r = await api("/api/settings/backup", { method: "POST" });
      out.textContent = `Saved ${r.name} (${(r.bytes / 1048576).toFixed(1)} MB)`
        + (r.pruned ? ` · removed ${r.pruned} older` : "");
      out.className = "pos";
      paintBackups(r.backups);
    } catch (err) {
      out.textContent = String(err);
      out.className = "neg";
    } finally {
      ev.target.disabled = false;
    }
  });

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

  const themeSelect = $("#set-theme");
  if (themeSelect) {
    themeSelect.value = document.documentElement.getAttribute("data-theme") || "dark";
    themeSelect.addEventListener("change", () => setTheme(themeSelect.value));
  }

  // Both bars save the same thing. A collapsed group still has its inputs in
  // the document, so a setting you cannot currently see is still saved rather
  // than silently dropped.
  $$("[data-save]", root).forEach((button) => {
    button.addEventListener("click", async () => {
      const values = {};
      $$("[data-key]", root).forEach((el) => {
        if (el.type === "checkbox") values[el.dataset.key] = el.checked ? "true" : "false";
        // An untouched secret box is empty, and sending that would clear a key
        // the page was never shown. Absent means "leave it".
        else if (el.value !== "") values[el.dataset.key] = el.value;
        else if (el.type !== "password") values[el.dataset.key] = "";
      });
      $$("[data-save]", root).forEach((b) => { b.disabled = true; });
      const outs = $$(".save-result", root);
      const say = (text, cls) => outs.forEach((o) => {
        o.textContent = text; o.className = `save-result ${cls}`;
      });
      try {
        const r = await api("/api/settings", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ values }),
        });
        say(`Saved ${r.saved.length} setting${r.saved.length === 1 ? "" : "s"}.`, "pos");
        await loadState();
        await render();
      } catch (err) {
        say(String(err), "neg");
      } finally {
        $$("[data-save]", root).forEach((b) => { b.disabled = false; });
      }
    });
  });
}

// -------------------------------------------------------------- assistant
/* A local model, given this app's own numbers.

   It is not a football oracle and is not asked to be one: it gets the board,
   the model's measured record and the scoreboard as JSON, and answers about
   those. Nothing leaves the machine — the app refuses any endpoint that is not
   loopback, so "offline" is enforced rather than promised. */
/* The assistant, as a chat app rather than a single running log.

   One log meant every question shared one context -- asking about week 3
   after twenty lines about week 2 fed the model all twenty -- and there was
   no way to put a thread aside and come back to it. Conversations live in the
   database, not the browser, so they survive an update and land in a backup.
*/
const chat = { id: null, chats: [], messages: [], busy: false, loaded: false };

async function loadChats() {
  const r = await api("/api/assistant/chats").catch(() => ({ chats: [] }));
  chat.chats = r.chats || [];
  if (chat.id && !chat.chats.some((c) => c.id === chat.id)) chat.id = null;
  if (!chat.id && chat.chats.length) chat.id = chat.chats[0].id;
  chat.messages = chat.id
    ? (await api(`/api/assistant/chats/${chat.id}`).catch(() => ({ messages: [] }))).messages
    : [];
  chat.loaded = true;
}

/* Setting the assistant up, rather than explaining how to.

   What used to be here was three numbered steps: install Ollama, run a
   command, paste an address into Settings. Every one of them is a place to
   give up, and the last two are a terminal -- which is the thing a desktop
   app exists to avoid. Now it is a button, and the work happens where the
   user can watch it.

   The server is installed into this app's own directory on its own port, so
   an Ollama the user already runs keeps its models and its settings. */
let setupPoll = null;

function bytes(n) {
  if (!n) return "";
  const mb = n / 1e6;
  return mb >= 1000 ? `${(mb / 1000).toFixed(1)} GB` : `${Math.round(mb)} MB`;
}

async function renderAssistantSetup(status) {
  const root = $("#view");
  const setup = await api("/api/assistant/setup").catch(() => null);
  const job = setup?.progress || {};
  const offered = status.setup_offered !== false && setup;

  const choices = (setup?.choices || []).map((c) => `<label class="model-choice">
    <input type="radio" name="setup-model" value="${esc(c.name)}"
      ${c.name === (setup.default_model) ? "checked" : ""} />
    <span><b>${esc(c.name)}</b> <span class="muted">· ${esc(c.size)}</span>
      <span class="model-note">${esc(c.note)}</span></span>
  </label>`).join("");

  const bar = job.running || job.phase === "done" || job.phase === "error" ? `
    <div class="setup-progress">
      <div class="bar"><div class="bar-fill${job.percent === null ? " indeterminate" : ""}"
        style="width:${job.percent === null ? 100 : job.percent}%"></div></div>
      <div class="setup-status">
        <span>${esc(job.message || "")}</span>
        <span class="muted">${job.total
          ? `${bytes(job.done)} of ${bytes(job.total)}` : ""}</span>
      </div>
      ${job.error ? `<p class="note warn">${esc(job.error)}</p>` : ""}
    </div>` : "";

  root.innerHTML = `<div class="panel">
    <header><h2>Assistant</h2><span class="hint">${
      job.running ? "setting up" : "not set up yet"}</span></header>
    <div class="setup-pane">
      <p>${esc(status.message)}</p>
      <p class="note">The assistant runs a model on this machine and talks to it
        over loopback only — an endpoint anywhere else is refused, so nothing you
        ask it can leave the computer. It sees this week's board, the model's
        measured record and the scoreboard, and nothing else.</p>
      ${offered ? `
        <div class="model-choices"${job.running ? " hidden" : ""}>${choices}</div>
        ${bar}
        <div class="controls setup-actions">
          <button class="btn primary" id="setup-go" ${job.running ? "disabled" : ""}>
            ${job.running ? "Setting up…"
              : (job.error ? "Try again" : "Set up the assistant")}</button>
          <span class="muted tiny">Downloads a model server and a model into
            ${esc(setup.directory)}. Nothing is installed anywhere else, and
            deleting The Edge takes it with it.</span>
        </div>
      ` : `<p class="note">Change it on the <b>Settings</b> page.</p>`}
    </div>
  </div>`;

  if (!offered) return;

  $("#setup-go")?.addEventListener("click", async () => {
    const chosen = $('input[name="setup-model"]:checked')?.value;
    await api("/api/assistant/setup", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: chosen }),
    }).catch(() => null);
    await renderAssistantSetup(status);
  });

  // Poll while it runs. Cleared on the way out of the tab, because a timer
  // that outlives its page is a timer that rewrites somebody else's.
  clearInterval(setupPoll);
  if (job.running) {
    setupPoll = setInterval(async () => {
      if (state.tab !== "assistant") { clearInterval(setupPoll); return; }
      const now = await api("/api/assistant/setup").catch(() => null);
      if (!now) return;
      if (now.progress?.phase === "done") {
        clearInterval(setupPoll);
        chat.loaded = false;
        await render();
        return;
      }
      await renderAssistantSetup(status);
    }, 1200);
  }
}

async function renderAssistant(ticket) {
  const root = $("#view");
  // The status probe starts the managed server if it is not running, which can
  // take twenty seconds. That is the longest await on any tab, and it is why
  // this is the tab that used to land on top of whichever one you switched to.
  const status = await api("/api/assistant/status").catch((e) => ({
    ready: false, message: String(e) }));
  if (stale(ticket)) return;

  if (!status.ready) {
    await renderAssistantSetup(status);
    return;
  }

  if (!chat.loaded) await loadChats();
  if (stale(ticket)) return;

  const list = chat.chats.map((c) => `<button class="chat-item${
    c.id === chat.id ? " on" : ""}" data-chat="${esc(c.id)}">
    <span class="ci-title">${esc(c.title)}</span>
    <span class="ci-sub">${esc(ago(c.updated_at))} · ${c.n || 0} message${
      c.n === 1 ? "" : "s"}</span>
    <span class="ci-actions">
      <span class="ci-act" data-rename="${esc(c.id)}" title="Rename" role="button">✎</span>
      <span class="ci-act" data-delete="${esc(c.id)}" title="Delete" role="button">✕</span>
    </span>
  </button>`).join("");

  const bubbles = chat.messages.map((m) => `<div class="msg ${esc(m.role)}">
    <div class="msg-body">${esc(m.content)}</div></div>`).join("");

  root.innerHTML = `<div class="chat-shell">
    <aside class="chat-side">
      <div class="chat-side-head">
        <button class="btn primary tiny" id="chat-new">New chat</button>
      </div>
      <div class="chat-list">${list || '<div class="empty tiny">No chats yet.</div>'}</div>
    </aside>

    <div class="panel chat">
      <header><h2>${esc(chat.chats.find((c) => c.id === chat.id)?.title || "Assistant")}</h2>
        <span class="hint">${esc(status.model)} · on this machine · sees week
          ${state.week} of ${state.season}</span></header>
      <div class="chat-log" id="chat-log">${bubbles || `<div class="empty">
        Ask about this week's board, where the model disagrees with the market, or
        what its record actually says. It only knows what this app has.</div>`}${
        chat.busy ? '<div class="msg assistant pending"><div class="msg-body">…</div></div>' : ""}</div>
      <div class="controls chat-input">
        <input type="text" id="chat-q" ${chat.busy ? "disabled" : ""}
          placeholder="e.g. where does the blind model disagree most with the book this week?" />
        <button class="btn primary" id="chat-send" ${chat.busy ? "disabled" : ""}>Ask</button>
      </div>
      <div class="chat-suggest">
        ${["Which games does the blind model disagree with the market on?",
           "Is this model actually any good? Be blunt.",
           "Summarise this week in five lines."].map((q) =>
          `<button class="btn tiny" data-q="${esc(q)}">${esc(q)}</button>`).join("")}
      </div>
    </div>
  </div>`;

  const scroll = () => {
    const log = $("#chat-log");
    if (log) log.scrollTop = log.scrollHeight;
  };

  const send = async (question) => {
    if (!question.trim() || chat.busy) return;
    // Shown immediately, and kept on screen while the model thinks: a 4B model
    // takes seconds, and a question that vanishes into a still page reads as a
    // dropped click.
    chat.messages = [...chat.messages, { role: "user", content: question }];
    chat.busy = true;
    await render();
    try {
      const r = await api("/api/assistant/ask", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ chat_id: chat.id, question,
                               season: state.season, week: state.week }),
      });
      chat.id = r.chat_id;
      chat.messages = r.messages;
    } catch (err) {
      chat.messages = [...chat.messages,
        { role: "assistant", content: `Could not answer: ${err}` }];
    } finally {
      chat.busy = false;
    }
    await loadChats().catch(() => {});
    await render();
    scroll();
  };

  $("#chat-new").addEventListener("click", async () => {
    const created = await api("/api/assistant/chats", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: "New chat" }),
    });
    chat.id = created.id;
    await loadChats();
    await render();
  });

  $$("[data-chat]", root).forEach((b) => b.addEventListener("click", async (e) => {
    if (e.target.closest("[data-rename],[data-delete]")) return;
    chat.id = b.dataset.chat;
    await loadChats();
    await render();
  }));

  $$("[data-rename]", root).forEach((b) => b.addEventListener("click", async (e) => {
    e.stopPropagation();
    const current = chat.chats.find((c) => c.id === b.dataset.rename);
    const title = prompt("Rename this chat", current?.title || "");
    if (title === null) return;
    await api(`/api/assistant/chats/${b.dataset.rename}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    }).catch(() => {});
    await loadChats();
    await render();
  }));

  $$("[data-delete]", root).forEach((b) => b.addEventListener("click", async (e) => {
    e.stopPropagation();
    const current = chat.chats.find((c) => c.id === b.dataset.delete);
    if (!confirm(`Delete "${current?.title || "this chat"}"? This cannot be undone.`)) return;
    await api(`/api/assistant/chats/${b.dataset.delete}`, { method: "DELETE" }).catch(() => {});
    if (chat.id === b.dataset.delete) chat.id = null;
    await loadChats();
    await render();
  }));

  $("#chat-send").addEventListener("click", () => send($("#chat-q").value));
  $("#chat-q").addEventListener("keydown", (e) => {
    if (e.key === "Enter") send(e.target.value);
  });
  $$("[data-q]", root).forEach((b) =>
    b.addEventListener("click", () => send(b.dataset.q)));
  if (!chat.busy) $("#chat-q")?.focus();
  scroll();
}

// -------------------------------------------------------------------- shell
const VIEWS = { home: renderHome, teams: renderTeams,
  picks: renderPicks, news: renderNews,
  performance: renderPerformance,
  assistant: renderAssistant, settings: renderSettings };

/* Which render is allowed to write to the page.

   Every view fetches before it draws, and nothing stopped a slow one from
   finishing after the reader had moved on -- so clicking Teams and then Picks
   left you on Picks for a moment and then dropped Teams on top of it. It looked
   like a sluggish tab; it was the wrong tab arriving late. Worst while the
   assistant is downloading or answering, because that is when the server is
   busiest and the gap is widest.

   Each render takes a ticket. A render that comes back holding a stale ticket
   has been overtaken and says nothing. */
let renderTicket = 0;

async function render() {
  const ticket = ++renderTicket;
  const view = VIEWS[state.tab] || renderHome;
  // Only Picks asks for the viewport's height; every other page is as tall as
  // it needs to be. Cleared here so a class one view sets cannot outlive it.
  $("#view").classList.remove("fit-screen");
  // The hero reports which season and week are on screen, so it has to follow
  // the selectors rather than only the last state load.
  if (state.meta) renderHero(state.meta);
  try {
    await view(renderTicket);
    if (ticket !== renderTicket) return;
    foldPanels($("#view"));
    markScrollFades($("#view"));
  } catch (err) {
    if (ticket !== renderTicket) return;
    $("#view").innerHTML = `<div class="panel"><div class="empty">
      Could not load this view: ${esc(err.message)}</div></div>`;
  }
}

/* Every box on the page that fades its bottom edge while more is below.
   Kept in one place because the rule is the same everywhere and the bug was
   that it had been written three times as static CSS. */
const FADE_BOXES = ".table-scroll.tall, .news-feed, .survivor-run .table-scroll";

function markScrollFades(root = document) {
  $$(FADE_BOXES, root).forEach((box) => {
    const update = () => box.classList.toggle(
      "scroll-fade", box.scrollHeight - box.clientHeight - box.scrollTop > 2);
    if (!box.dataset.fadeWired) {
      box.addEventListener("scroll", update, { passive: true });
      box.dataset.fadeWired = "1";
    }
    update();
  });
}

/* True when this render has been overtaken by a newer one.

   Views call it after every await and before they touch the page. The ticket
   check in `render` is not enough on its own: a view writes to #view itself,
   partway through, long before it returns. */
function stale(ticket) {
  /* No ticket means nobody is racing this render, so let it paint. Twelve
     handlers used to call their view directly -- the injury picker, every
     assistant button, the picks controls -- and every one of them fetched,
     came back holding `undefined`, compared it to the current ticket, decided
     it had been overtaken and drew nothing. The page only changed when you
     left the tab and came back, which is the bug that was reported. They all
     go through render() now; this is so that the next one to forget degrades
     into painting anyway rather than into doing nothing at all. */
  return ticket !== undefined && ticket !== renderTicket;
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

function setTab(tab, { fromHash = false } = {}) {
  if (!VIEWS[tab]) tab = "home";
  state.tab = tab;
  $$(".tab").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === tab)));
  // The week selector is shown on every page. Hiding it made changing week a
  // two-step move -- go to Home, change it, come back -- on the pages most
  // likely to raise the question.
  //
  // The tab also lives in the address, which it did not before: reloading
  // dropped you back on Home, and there was no way to reopen the app on the
  // page you were last reading. It is what makes the page addressable at all.
  if (!fromHash && location.hash.slice(1) !== tab) {
    history.replaceState(null, "", `#${tab}`);
  }
  // Clear the old page immediately rather than leaving it up until the new
  // one has fetched. A tab that responds at once and then fills in reads as
  // fast; a tab that sits on the last page for two seconds reads as broken.
  $("#view").innerHTML = '<div class="panel"><div class="empty">Loading…</div></div>';
  render();
}

function initRouting() {
  addEventListener("hashchange", () => {
    const tab = location.hash.slice(1);
    if (tab && tab !== state.tab) setTab(tab, { fromHash: true });
  });
}

/* Set the theme and remember it. Pulled out of the toggle's handler because
   there are two ways to change it now -- the corner button and the Settings
   page -- and both have to do exactly the same thing. */
function setTheme(next) {
  document.documentElement.setAttribute("data-theme", next);
  try { localStorage.setItem("theedge-theme", next); } catch { /* not fatal */ }
  render();
}

function initTheme() {
  // Dark unless told otherwise. A stored choice still wins in both directions,
  // so someone who picked light keeps light; only the unset case changes.
  let saved = null;
  try { saved = localStorage.getItem("theedge-theme") || localStorage.getItem("nflpicker-theme"); }
  catch { /* private window, blocked storage */ }
  document.documentElement.setAttribute("data-theme", saved || "dark");
  $("#theme").addEventListener("click", () => {
    const current = document.documentElement.getAttribute("data-theme");
    const isDark = current === "dark" ||
      (!current && matchMedia("(prefers-color-scheme: dark)").matches);
    setTheme(isDark ? "light" : "dark");
  });
}

/* Full screen. The window has no browser chrome to hide, so this is the only
   way to give the board the whole display -- which is what it is for.

   Two routes, because the obvious one does not work in the packaged app.
   `requestFullscreen()` asks the *host* to take the window full screen, and an
   embedded webview has no standing to do that: WebView2 passes the request to
   the application and pywebview does not implement it, so the promise rejected,
   the catch below swallowed it, and the button did nothing. In the app the
   window toggles itself through the bridge; in a browser tab the DOM API is
   the one that works, so it stays as the fallback. */
function initFullscreen() {
  const button = $("#fullscreen");
  if (!button) return;
  let native = false;   // what the bridge last told us, when there is one
  const bridge = () => window.pywebview?.api?.toggle_fullscreen;
  const sync = () => button.classList.toggle(
    "on", bridge() ? native : !!document.fullscreenElement);
  button.addEventListener("click", async () => {
    try {
      const toggle = bridge();
      if (toggle) {
        // The DOM never reports fullscreen in this environment, so the bridge
        // returns the new state rather than leaving it to be inferred.
        native = await toggle();
      } else if (document.fullscreenElement) {
        await document.exitFullscreen();
      } else {
        await document.documentElement.requestFullscreen();
      }
    } catch { /* refused by the platform; the button simply does nothing */ }
    sync();
  });
  document.addEventListener("fullscreenchange", sync);
  // F11 is what people already press, and a webview does not handle it itself.
  document.addEventListener("keydown", (e) => {
    if (e.key === "F11") { e.preventDefault(); button.click(); }
  });
}

async function main() {
  initTheme();
  initFullscreen();
  initRouting();
  // Open on the page the address names, so a reload or a saved link lands
  // where it says it will.
  const initial = location.hash.slice(1);
  if (initial && VIEWS[initial]) state.tab = initial;
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

  // The clock is the one thing on the page that must not wait for a refresh --
  // including the one in the logo, which is why it ticks whether or not there
  // is any state to render around it.
  // A narrower window rewraps rows, which changes whether a box still has
  // anything below the fold.
  addEventListener("resize", () => markScrollFades(), { passive: true });

  setBrandClock(new Date());
  setInterval(() => {
    setBrandClock(new Date());
    if (state.meta) renderHero(state.meta);
  }, 30000);

  await loadState();
  setTab(state.tab, { fromHash: true });

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
