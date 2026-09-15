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
  $("#herosub").textContent = `${day} — week ${meta.week} of the ${meta.season} season`;
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

// -------------------------------------------------------------------- home
/* The whole slate on one screen. The card board is better for reading one
   game closely; this is for answering "what does the model think, and is the
   market coming to it" across sixteen games without scrolling.

   Three numbers per game, and they are deliberately the *same* three the rest
   of the app uses:
     ours   -- fair_margin, the blend. This is what the win probability is
               built from, so the pick and the spread can never disagree.
     book   -- the sportsbook consensus.
     market -- prediction markets, shown but never mixed into either. */
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

  const games = [...data.games].sort((a, b) => {
    const rank = (g) => (g.status === "in_progress" ? 0 : g.status === "final" ? 2 : 1);
    return rank(a) - rank(b) || String(a.kickoff).localeCompare(String(b.kickoff));
  });

  const rows = games.map((g) => {
    const p = g.prediction;
    const fair = p ? p.fair_margin : null;
    const prob = p ? p.home_win_prob : null;
    const homePick = prob !== null && prob >= 0.5;
    const winner = homePick ? g.home : g.away;
    const loser = homePick ? g.away : g.home;

    // Every probability on the row is quoted for the *same* side -- the one we
    // picked -- so they can be compared straight across. Flipping some to the
    // home team and others to our pick would make the row unreadable.
    const ourProb = prob === null ? null : (homePick ? prob : 1 - prob);
    const bookHome = g.market?.home_win_prob;
    const bookProb = bookHome === null || bookHome === undefined
      ? null : (homePick ? bookHome : 1 - bookHome);

    const line = (margin) => {
      if (margin === null || margin === undefined) return "–";
      if (Math.abs(margin) < 0.05) return "PK";
      return margin > 0 ? `${g.home} ${num(-margin, 1)}` : `${g.away} ${num(margin, 1)}`;
    };
    const bookSpread = g.market?.spread_home;

    const moved = g.movement?.toward_us;
    const movedCls = moved === null || moved === undefined || Math.abs(moved) < 0.05
      ? "flat" : (moved > 0 ? "good" : "bad");
    const movedText = moved === null || moved === undefined ? "–" : signed(moved);

    const mkt = pmByGame[g.game_id];
    const mktProb = mkt && mkt.venue_prob !== null && mkt.venue_prob !== undefined
      ? (homePick ? mkt.venue_prob : 1 - mkt.venue_prob) : null;

    const state_ = g.status === "in_progress"
      ? `<span class="live-dot"></span>LIVE`
      : (g.status === "final" ? "Final" : kickoffShort(g.kickoff));

    return `<tr data-game="${esc(g.game_id)}" tabindex="0">
      <td class="when">${state_}</td>
      <td class="match">
        <b>${esc(winner)}</b><span class="beat">over</span><span class="lose">${esc(loser)}</span>
      </td>
      <td class="ours edge-col">${ourProb === null ? "–" : pct(ourProb)}</td>
      <td class="ours">${esc(line(fair))}</td>
      <td class="ours">${p && p.fair_total ? num(p.fair_total, 1) : "–"}</td>
      <td class="book edge-col">${bookProb === null ? "–" : pct(bookProb)}</td>
      <td class="book">${esc(line(bookSpread === null || bookSpread === undefined
        ? null : -bookSpread))}</td>
      <td class="book">${num(g.market?.total_points, 1)}</td>
      <td class="pmkt edge-col">${mktProb === null ? "–" : pct(mktProb)}</td>
      <td class="moved ${movedCls}">${movedText}</td>
    </tr>`;
  }).join("");

  root.innerHTML = `<div class="panel board">
    <header><h2>Week ${data.week} — the whole slate</h2>
      <span class="hint">Every probability is for the side we picked ·
        click a row for detail</span></header>
    <table class="slate">
      <thead>
        <tr class="groups">
          <th colspan="2"></th>
          <th colspan="3" class="g-ours">Our model</th>
          <th colspan="3" class="g-book">Sportsbook</th>
          <th class="g-pmkt">Pred. mkt</th>
          <th></th>
        </tr>
        <tr>
          <th></th><th>Pick</th>
          <th class="ours edge-col">Win</th><th class="ours">Spread</th><th class="ours">Total</th>
          <th class="book edge-col">Win</th><th class="book">Spread</th><th class="book">Total</th>
          <th class="pmkt edge-col">Win</th>
          <th class="moved" title="Points the line has moved toward our side since it opened">Moved to us</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
  </div>`;

  $$("tr[data-game]", root).forEach((node) => {
    const open = () => openGame(node.dataset.game);
    node.addEventListener("click", open);
    node.addEventListener("keydown", (e) => { if (e.key === "Enter") open(); });
  });
}

// ------------------------------------------------------------------- games
async function renderGames() {
  const root = $("#view");
  const data = await api(`/api/games?week=${state.week}&season=${state.season}`);
  if (!data.games.length) {
    root.innerHTML = '<div class="panel"><div class="empty">No games stored for this week yet.</div></div>';
    return;
  }

  // Games in progress lead: they are the ones changing while you look at them.
  data.games.sort((a, b) => {
    const rank = (g) => (g.status === "in_progress" ? 0 : g.status === "final" ? 2 : 1);
    return rank(a) - rank(b) || String(a.kickoff).localeCompare(String(b.kickoff));
  });
  const cards = data.games.map((card) => {
    const p = card.prediction;
    const final = card.status === "final";
    const live = card.status === "in_progress" ? card.live : null;
    const movePoints = (card.movement.points || []).map((pt) => pt.value);
    const spark = movePoints.length > 1
      ? sparkline(movePoints, { color: "var(--axis)" }) : "";
    const homeProb = p ? p.home_win_prob : null;

    const teamRow = (side) => {
      const isHome = side === "home";
      const abbr = isHome ? card.home : card.away;
      const name = isHome ? card.home_name : card.away_name;
      const color = isHome ? card.home_color : card.away_color;
      const score = isHome ? card.home_score : card.away_score;
      const prob = homeProb === null ? null : (isHome ? homeProb : 1 - homeProb);
      // While a game is live the score is the headline and the probability is
      // the live one, not the pregame projection.
      const liveProb = live && live.win_prob_home !== null && live.win_prob_home !== undefined
        ? (isHome ? live.win_prob_home : 1 - live.win_prob_home) : null;
      const right = (final || live)
        ? `<span class="score">${score ?? "–"}</span>` +
          (liveProb !== null ? `<span class="prob" style="margin-left:10px">${pct(liveProb)}</span>` : "")
        : `<span class="prob">${pct(prob)}</span>`;
      const hasBall = live && live.possession === abbr
        ? '<span class="ball" title="has possession"></span>' : "";
      // The bar behind the row *is* the win probability. Thirty-two team
      // colours across a sixteen-game board read as confetti, and most NFL
      // palettes are dark navies that disappear on a near-black card anyway --
      // so the one thing worth colouring is the one thing that carries meaning.
      const shown = liveProb !== null ? liveProb : prob;
      const lead = shown !== null && shown >= 0.5;
      const fill = shown === null ? "" :
        `<span class="pbar${lead ? " lead" : ""}" style="width:${(shown * 100).toFixed(1)}%"></span>`;
      return `<div class="row-team" style="--team:${esc(color || "transparent")}">${fill}` +
        `<span class="nm">${esc(abbr)}</span>${hasBall}` +
        `<span class="rec">${esc(name.replace(abbr, "").trim())}</span>${right}</div>`;
    };

    const news = (card.news || []).slice(0, 2).map((n) =>
      `<span class="badge ${esc(n.category)}">${esc(n.category)}</span>`).join(" ");
    // Only surface weather when it is the kind that moves a total. A mild
    // afternoon is not information.
    const w = card.weather;
    const weatherFlag = (w && !w.indoor && (
      (w.wind_mph ?? 0) >= 15 || (w.temp_f ?? 50) <= 32 || (w.precip_pct ?? 0) >= 60))
      ? `<span class="badge weather">${(w.wind_mph ?? 0) >= 15
          ? `${Math.round(w.wind_mph)} mph wind` : ""}${
          (w.wind_mph ?? 0) >= 15 && (w.temp_f ?? 50) <= 32 ? " · " : ""}${
          (w.temp_f ?? 50) <= 32 ? `${Math.round(w.temp_f)}°F` : ""}${
          (w.precip_pct ?? 0) >= 60 ? ` · ${Math.round(w.precip_pct)}% precip` : ""}</span>`
      : "";
    const hits = ["away", "home"]
      .map((side) => [side === "home" ? card.home : card.away, card.availability?.[side]])
      .filter(([, a]) => a && a.adjustment <= -1.0)
      .map(([team, a]) => `<span class="badge injury">${esc(team)} ${signed(a.adjustment)}` +
        `${a.qb_change ? " QB" : ""}</span>`).join(" ");

    return `<article class="card" data-game="${esc(card.game_id)}" tabindex="0">
      <div class="kick">
        <span>${live
          ? `<span class="live-dot"></span>${esc(liveLabel(live))}`
          : (final ? "Final" : when(card.kickoff))}</span>
        <span>${card.movement.steam ? "⚡ steam move" : (spark || "")}</span></div>
      ${live && live.last_play ? `<div class="lastplay">${esc(live.last_play)}</div>` : ""}
      <div class="teams">${teamRow("away")}${teamRow("home")}</div>
      <div class="lines">
        <div class="line">
          <span class="lbl">Spread</span>
          <span class="pair" title="Market line, then our blended estimate">
            <b class="mkt">${esc(spreadText(card))}</b>
            <i class="to" aria-hidden="true"></i>
            <b class="mdl">${esc(modelLineText(card))}</b>
          </span>
          ${edgePill(p?.spread_edge)}
        </div>
        <div class="line">
          <span class="lbl">Total</span>
          <span class="pair" title="Market total, then our blended estimate">
            <b class="mkt">${num(card.market?.total_points, 1)}</b>
            <i class="to" aria-hidden="true"></i>
            <b class="mdl">${num(p?.total_points, 1)}</b>
          </span>
          <span class="move" title="How far the line has moved since it opened"
            >${signed(card.movement.spread_move)}</span>
        </div>
      </div>
      ${weatherFlag ? `<div class="newsline">${weatherFlag}<span class="muted">conditions at kickoff</span></div>` : ""}
      ${hits ? `<div class="newsline">${hits}<span class="muted">injury adjustment applied</span></div>` : ""}
      ${news ? `<div class="newsline">${news}<span class="muted">news affecting this game</span></div>` : ""}
    </article>`;
  }).join("");

  root.innerHTML = `<div class="panel">
    <header><h2>Week ${data.week} — ${data.games.length} games</h2>
      <span class="hint">Click a game for line movement, every book, and prediction history</span>
    </header>
    <div class="legend" style="margin-bottom:12px">
      <span class="key"><i style="background:var(--div-pos)"></i>Edge favours home</span>
      <span class="key"><i style="background:var(--div-neg)"></i>Edge favours away</span>
      <span class="key muted">Each pair reads market \u2192 our model. Edge is the blended
        estimate against the line, not the raw model gap</span>
    </div>
    <div class="cards">${cards}</div></div>`;

  $$(".card", root).forEach((node) => {
    const open = () => openGame(node.dataset.game);
    node.addEventListener("click", open);
    node.addEventListener("keydown", (e) => { if (e.key === "Enter") open(); });
  });
}

async function openGame(gameId) {
  const dlg = $("#detail");
  const body = $(".dialog-body", dlg);
  $(".dialog-title", dlg).textContent = "Loading…";
  body.innerHTML = '<div class="empty">Loading…</div>';
  dlg.showModal();

  const d = await api(`/api/game/${encodeURIComponent(gameId)}`);
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
async function renderTeams() {
  const root = $("#view");
  const data = await api("/api/teams");
  // Season win totals only exist when the odds feed publishes futures. Two
  // columns of dashes read as a bug, so drop them when nothing has one.
  const hasWinTotals = data.teams.some(
    (t) => t.win_total_line !== null && t.win_total_line !== undefined);
  const rows = data.teams.map((t) => `<tr data-team="${esc(t.team)}" style="cursor:pointer">
    <td class="team">${t.rank}. ${esc(t.team)} <span class="muted">${esc(t.name)}</span></td>
    <td>${t.record.wins ?? 0}-${t.record.losses ?? 0}${t.record.ties ? `-${t.record.ties}` : ""}</td>
    <td>${signed(t.power)}</td>
    <td>${num(t.elo, 0)}</td>
    <td>${num(t.exp_wins, 1)}</td>
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

  root.innerHTML = `<div class="panel">
    <header><h2>Power ratings &amp; season projections</h2>
      <span class="hint">Power is points better than an average team on a neutral field.
        Projections come from 20,000 simulated seasons.</span></header>
    <div class="table-scroll"><table>
      <thead><tr><th>Team</th><th>Record</th><th>Power</th><th>Elo</th><th>Exp. wins</th>
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
    <div style="margin-top:14px">
      <h3 style="font-size:12px;margin-bottom:6px">Teams you have already used</h3>
      <div class="controls">
        <input type="text" id="used-teams" style="flex:1;min-width:220px"
          value="${esc((data.survivor_used || []).join(", "))}"
          placeholder="e.g. KC, SF, BAL" />
        <button class="btn primary" id="save-used">Save &amp; replan</button>
      </div>
    </div>
  </div>`;

  const modeSelect = $("#pickem-mode");
  modeSelect.value = state.pickemMode || "ev";
  modeSelect.addEventListener("change", () => {
    state.pickemMode = modeSelect.value;
    renderPicks();
  });

  $("#save-used").addEventListener("click", async (ev) => {
    const teams = $("#used-teams").value.split(",").map((t) => t.trim().toUpperCase()).filter(Boolean);
    ev.target.disabled = true;
    ev.target.textContent = "Replanning…";
    try {
      await api("/api/survivor/used", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ teams }),
      });
      await renderPicks();
    } catch (err) {
      alert(`Could not save: ${err.message}`);
      ev.target.disabled = false;
      ev.target.textContent = "Save & replan";
    }
  });
}

// -------------------------------------------------------------------- news
async function renderNews() {
  const root = $("#view");
  const data = await api("/api/news?limit=80");
  const items = data.items.map((n) => `<div style="padding:9px 0;border-bottom:1px solid var(--grid)">
    <div style="display:flex;gap:8px;align-items:baseline;flex-wrap:wrap">
      <span class="badge ${esc(n.category)}">${esc(n.category)}</span>
      ${(n.teams || []).map((t) => `<span class="badge">${esc(t)}</span>`).join("")}
      ${n.line_impact ? `<span class="badge" style="border-color:var(--serious);color:var(--serious)">
        est. ${signed(n.line_impact)} pts</span>` : ""}
      <span class="muted" style="margin-left:auto;font-size:11px">${esc(n.source)} · ${ago(n.published_at)}</span>
    </div>
    <div style="margin-top:3px">${n.url
      ? `<a href="${esc(n.url)}" target="_blank" rel="noopener">${esc(n.title)}</a>`
      : esc(n.title)}</div>
    ${n.summary ? `<div class="muted" style="font-size:12px;margin-top:2px">${esc(n.summary.slice(0, 220))}</div>` : ""}
  </div>`).join("");

  const injuries = (data.injuries || []).slice(0, 60).map((i) => `<tr>
    <td class="team">${esc(i.team)}</td><td>${esc(i.player)}</td>
    <td>${esc(i.position || "–")}</td><td>${esc(i.status || "–")}</td>
    <td class="muted">${ago(i.updated_at)}</td></tr>`).join("");

  root.innerHTML = `<div class="panel">
    <header><h2>News &amp; changes</h2>
      <span class="hint">Sorted by estimated relevance to picks, not by recency</span></header>
    ${items || '<div class="empty">No news stored yet.</div>'}
    <p class="note">The points estimate is a coarse prior from position and availability —
      a starting quarterback is worth two to three points, a backup almost nothing. It is a
      triage signal for what to look at, never a substitute for the market's own reaction.</p>
  </div>
  ${injuries ? `<div class="panel"><header><h2>Injury report</h2></header>
    <div class="table-scroll"><table>
      <thead><tr><th>Team</th><th>Player</th><th>Pos</th><th>Status</th><th>Updated</th></tr></thead>
      <tbody>${injuries}</tbody></table></div></div>` : ""}`;
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
      <div class="tiles" style="margin-top:14px">
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

// -------------------------------------------------------------------- shell
const VIEWS = { home: renderHome, games: renderGames, teams: renderTeams,
  picks: renderPicks, news: renderNews, edge: renderEdge, performance: renderPerformance };

async function render() {
  const view = VIEWS[state.tab] || renderHome;
  try {
    await view();
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
