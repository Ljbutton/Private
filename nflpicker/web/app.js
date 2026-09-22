import { barChart, condense, lineChart, sparkline } from "./charts.js";
import {
  advanceHand, ago, american, clockAngles, esc, greetingLine, kickoffShort,
  liveLabel, markdown, num, pct,
  signed, statusClass, when,
} from "./format.js";

const state = { season: null, week: null, weeks: [], tab: "home", meta: null,
  busy: false, trackTeam: null };

/* Which pages take exactly one screen, and when that is decided.

   `render` used to clear `fit-screen` as its first act and wait for the view
   -- which is async -- to put it back. For the frame in between, the board was
   unconstrained: it grew to its full height, the window gained a scrollbar,
   and the scroll position inside it was lost and then restored. One frame, and
   entirely visible, which is what "refresh glitches the bottom of the screen"
   was. Nothing is stripped now; a view says it fits while it renders and the
   class is settled once, after the paint. */
let fitRequested = false;

function fitsOneScreen(root) {
  fitRequested = true;
  root.classList.add("fit-screen");
}

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

async function api(path, options) {
  const res = await fetch(path, options);
  // The subscription lapsed while the app was open: stop and ask for a key
  // rather than letting every panel fail one by one.
  if (res.status === 402) {
    const body = await res.json().catch(() => ({}));
    showLicenseGate(body.license || {}).then(() => location.reload());
    throw new Error("license required");
  }
  if (!res.ok) throw new Error(`${path} → ${res.status}`);
  return res.json();
}

/* ------------------------------------------------------- contact support */

/* One dialog for all three kinds of message -- a bug, an idea, a question --
   opened from Help and from the activation gate.

   The gate matters more than it looks: "my key will not activate" is the
   likeliest message this app will ever receive, and the people sending it
   cannot get past that screen -- so a support flow reachable only from inside
   the app is a support flow for everybody except the users who most need it.
   The endpoint is open for the same reason.

   Everything the message would carry is shown in full before it goes: the
   attachments behind a disclosure, one checkbox each, and the screenshots as
   thumbnails. Unticking a box means the detail is not gathered at all, which
   is decided on the Python side: see support.details. */

const REPORT_LABELS = {
  version: "App version, commit and build date",
  os: "Your operating system and version",
  key_hint: "The last four characters of your licence key (never the key)",
  log: "The last 100 lines of the app's log, with keys removed",
};

/* What each kind of message asks for. The words change; the shape does not.
   Every kind takes text, screenshots and an address, because the person who
   has a screenshot of a bug is the same person who has one of the thing they
   wish worked differently, and a form that only lets one of them attach it is
   a form that decides which of them is worth hearing from. */
const REPORT_KINDS = {
  bug: {
    lede: "Something went wrong. Say what you saw and we'll go looking.",
    what: "What happened?",
    whatHint: "What went wrong, and what you expected instead.",
    doing: "What were you doing when it happened?",
    doingHint: "Which page you were on, what you pressed.",
    email: "Your email, if you'd like a reply",
    send: "Send report",
  },
  suggestion: {
    lede: "An idea for the app. The more concrete the better — a fair bit of "
      + "what's in here started as one of these.",
    what: "What would you like to see?",
    whatHint: "What it would do, and what it would save you.",
    doing: "Where in the app would it go?",
    doingHint: "Home, Picks, the Assistant — wherever you'd look for it.",
    email: "Your email, if you'd like a reply",
    send: "Send suggestion",
  },
  general: {
    lede: "Anything else — buying, installing, or how something is meant to work.",
    what: "How can we help?",
    whatHint: "Ask away.",
    doing: "Anything else that would help? (optional)",
    doingHint: "When it started, what you've already tried.",
    email: "Your email, so we can write back",
    send: "Send message",
  },
};

/* Screenshots. The caps are the app's, matched on the Python side and again
   in the Worker: a public endpoint cannot take the page's word for any of it.
   The long edge is what makes the difference — a 4K screenshot is six
   megabytes of pixels nobody will look at at that size. */
const REPORT_IMAGE_MAX = 3;
const REPORT_IMAGE_BYTES = 2_000_000;
const REPORT_IMAGE_EDGE = 1600;
const REPORT_IMAGE_TYPES = ["image/png", "image/jpeg", "image/webp", "image/gif"];

let reportWiring = false;
let reportImages = [];

const reportKind = () => REPORT_KINDS[$("#report-category")?.value] || REPORT_KINDS.bug;

/* Roughly what the attachment weighs, from the base64 rather than the file:
   four characters carry three bytes, and the base64 is what actually travels. */
function imageBytes(image) {
  const body = String(image.data || "").split(",").pop() || "";
  return Math.round((body.length * 3) / 4);
}

/* A screenshot small enough to email, without asking anyone to resize one.

   Anything already small and lossless is left exactly as it is: a cropped PNG
   of one dialog is the clearest attachment there is, and re-encoding it as
   JPEG would make it blurrier without making it smaller. Everything else is
   capped on the long edge and re-encoded, which takes a full-screen 4K grab
   from six megabytes to a couple of hundred kilobytes. */
async function shrinkImage(file) {
  const original = await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(new Error("could not be read"));
    reader.readAsDataURL(file);
  });
  const picture = await new Promise((resolve) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => resolve(null);
    img.src = original;
  });
  if (!picture) throw new Error("is not an image this app can read");

  const scale = Math.min(1,
    REPORT_IMAGE_EDGE / Math.max(picture.width, picture.height, 1));
  if (scale === 1 && file.size <= 400_000) {
    return { name: file.name, type: file.type, data: original };
  }
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(picture.width * scale));
  canvas.height = Math.max(1, Math.round(picture.height * scale));
  canvas.getContext("2d").drawImage(picture, 0, 0, canvas.width, canvas.height);
  return {
    name: `${file.name.replace(/\.[^.]*$/, "") || "screenshot"}.jpg`,
    type: "image/jpeg",
    data: canvas.toDataURL("image/jpeg", 0.82),
  };
}

function paintThumbs() {
  const wrap = $("#report-thumbs");
  if (!wrap) return;
  wrap.innerHTML = reportImages.map((image, i) => `<figure class="report-thumb">
    <img src="${image.data}" alt="" />
    <button class="thumb-x" type="button" data-drop-image="${i}"
      title="Remove ${esc(image.name)}" aria-label="Remove ${esc(image.name)}">×</button>
    <figcaption>${esc(image.name)} · ${Math.round(imageBytes(image) / 1024)} KB</figcaption>
  </figure>`).join("");
  const note = $("#report-image-note");
  if (note) {
    note.textContent = reportImages.length
      ? `(${reportImages.length} of ${REPORT_IMAGE_MAX} attached)`
      : `(optional, up to ${REPORT_IMAGE_MAX})`;
  }
}

/* Added one at a time and shown, rather than counted. Whatever is on screen
   here is exactly what leaves the machine, which matters more for a picture
   than for anything else on this form: a screenshot shows whatever was on the
   screen, including the parts nobody meant to send. */
async function addImages(files) {
  const msg = $("#report-msg");
  const refused = [];
  for (const file of Array.from(files || [])) {
    if (reportImages.length >= REPORT_IMAGE_MAX) {
      refused.push(`${file.name} (only ${REPORT_IMAGE_MAX} fit)`);
      continue;
    }
    if (!REPORT_IMAGE_TYPES.includes(file.type)) {
      refused.push(`${file.name} (PNG, JPEG, WebP or GIF only)`);
      continue;
    }
    try {
      const image = await shrinkImage(file);
      if (imageBytes(image) > REPORT_IMAGE_BYTES) {
        refused.push(`${file.name} (still too big to send)`);
        continue;
      }
      reportImages.push(image);
    } catch (err) {
      refused.push(`${file.name} (${err.message || "could not be read"})`);
    }
  }
  paintThumbs();
  if (msg && refused.length) {
    msg.className = "report-msg warn";
    msg.textContent = `Not attached: ${refused.join(", ")}.`;
  }
}

function reportBody() {
  const include = {};
  $$("#report-detail-list input[type=checkbox]").forEach((box) => {
    include[box.dataset.detail] = box.checked;
  });
  return {
    category: $("#report-category").value,
    description: $("#report-what").value.trim(),
    doing: $("#report-doing").value.trim(),
    email: $("#report-email").value.trim(),
    include,
    images: reportImages.map((i) => ({ name: i.name, type: i.type, data: i.data })),
  };
}

/* The message as text, for the clipboard. The fallback when sending fails has
   to be something: somebody has just written six paragraphs about a crash and
   losing them to a network error would be its own bug. Screenshots are named
   rather than copied -- the clipboard takes one thing at a time. */
function reportText(body, details) {
  const kind = REPORT_KINDS[body.category] || REPORT_KINDS.bug;
  const lines = [`${kind.what} ${body.description}`, ""];
  if (body.doing) lines.push(`${kind.doing} ${body.doing}`, "");
  if (body.email) lines.push(`Reply to: ${body.email}`, "");
  if (body.images.length) {
    lines.push(`Screenshots attached: ${
      body.images.map((i) => i.name).join(", ")}`, "");
  }
  for (const key of Object.keys(REPORT_LABELS)) {
    if (!body.include[key]) continue;
    const value = (details || {})[key];
    if (value) lines.push(`${REPORT_LABELS[key]}:`, String(value), "");
  }
  return lines.join("\n").trim();
}

/* The dropdown rewrites the form rather than adding a field to it. Three
   entrances would mean choosing the right one before you can type, and the
   people least sure which of the three they have are the ones with the most
   to say. */
function applyKind() {
  const kind = reportKind();
  $("#report-lede").textContent = kind.lede;
  $("#report-what-label").textContent = kind.what;
  $("#report-what").placeholder = kind.whatHint;
  $("#report-doing-label").textContent = kind.doing;
  $("#report-doing").placeholder = kind.doingHint;
  $("#report-email-label").innerHTML =
    `${esc(kind.email)} <span class="muted">(optional)</span>`;
  $("#report-send").textContent = kind.send;
  // The log is the thing worth having about a crash and beside the point on
  // an idea, so it starts ticked for one and unticked for the others. It is a
  // starting position, not a rule: the box is right there either way.
  const log = $("#report-detail-list input[data-detail=log]");
  if (log) log.checked = $("#report-category").value === "bug";
}

async function openReport(category = "") {
  const dlg = $("#report-dialog");
  const list = $("#report-detail-list");
  const msg = $("#report-msg");
  msg.textContent = "";
  msg.className = "report-msg";
  if (category && REPORT_KINDS[category]) $("#report-category").value = category;
  reportImages = [];
  $("#report-images").value = "";
  paintThumbs();

  /* What would be attached, fetched rather than described. This endpoint is
     open before activation, same as the message itself. */
  let info = { keys: Object.keys(REPORT_LABELS), details: {}, can_send: true };
  try {
    const res = await fetch("/api/support/details");
    if (res.ok) info = { ...info, ...(await res.json()) };
  } catch { /* the form still works; the boxes just have nothing to preview */ }

  const shown = info.details || {};
  const preview = {
    version: shown.version
      ? [shown.version.version, shown.version.commit, shown.version.built_at]
        .filter(Boolean).join(" · ") : "",
    os: shown.environment ? shown.environment.os_detail || shown.environment.os : "",
    key_hint: shown.key_hint || "(no key saved)",
    log: shown.log || "(no log yet)",
  };
  list.innerHTML = (info.keys || []).map((key) => `<label class="report-detail">
    <input type="checkbox" data-detail="${esc(key)}" checked />
    <span class="report-detail-label">${esc(REPORT_LABELS[key] || key)}</span>
    <pre class="report-detail-value">${esc(String(preview[key] || "").slice(0, 4000))}</pre>
  </label>`).join("");
  applyKind();

  if (info.can_send === false) {
    msg.className = "report-msg warn";
    msg.textContent = "This build has no support server set up, so Send will "
      + "not work — use Copy message instead.";
  }
  if (!dlg.open) dlg.showModal();
  setTimeout(() => $("#report-what").focus(), 50);
}

function wireReport() {
  if (reportWiring) return;
  reportWiring = true;
  const dlg = $("#report-dialog");

  document.addEventListener("click", (ev) => {
    const open = ev.target.closest("[data-open-report], #gate-report");
    if (open) {
      ev.preventDefault();
      // The gate is where "my key won't activate" comes from, so it opens on
      // the kind of message that is, rather than on a dropdown to read first.
      openReport(open.dataset.openReport || "bug");
      return;
    }
    if (ev.target.closest("[data-close-report]")) dlg.close();
    const drop = ev.target.closest("[data-drop-image]");
    if (drop) {
      reportImages.splice(Number(drop.dataset.dropImage), 1);
      paintThumbs();
    }
  });

  $("#report-category").addEventListener("change", applyKind);
  $("#report-images").addEventListener("change", async (ev) => {
    await addImages(ev.target.files);
    // Cleared so picking the same file twice still fires a change.
    ev.target.value = "";
  });

  $("#report-copy").addEventListener("click", async () => {
    const body = reportBody();
    const details = (await fetch("/api/support/details")
      .then((r) => (r.ok ? r.json() : {})).catch(() => ({}))).details || {};
    const text = reportText(body, {
      version: details.version ? JSON.stringify(details.version) : "",
      os: details.environment ? details.environment.os_detail : "",
      key_hint: details.key_hint, log: details.log,
    });
    const msg = $("#report-msg");
    try {
      await navigator.clipboard.writeText(text);
      msg.className = "report-msg ok";
      msg.textContent = "Copied. Paste it wherever you like.";
    } catch {
      msg.className = "report-msg warn";
      msg.textContent = "Could not reach the clipboard. Select the text and copy it.";
    }
  });

  $("#report-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const send = $("#report-send");
    const msg = $("#report-msg");
    const body = reportBody();
    if (!body.description) {
      msg.className = "report-msg warn";
      msg.textContent = body.category === "bug"
        ? "Tell us what happened first." : "Write your message first.";
      return;
    }
    // Disabled while in flight: a second press would send a second email.
    send.disabled = true;
    const label = send.textContent;
    send.textContent = "Sending…";
    msg.className = "report-msg";
    msg.textContent = "";
    try {
      const res = await fetch("/api/support/report", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const out = await res.json().catch(() => ({}));
      if (res.ok && out.ok) {
        msg.className = "report-msg ok";
        msg.textContent = `Sent, thanks.${out.ref ? ` Reference ${out.ref}.` : ""}`;
        $("#report-what").value = "";
        $("#report-doing").value = "";
        reportImages = [];
        paintThumbs();
      } else {
        msg.className = "report-msg warn";
        msg.textContent = (out.message || "That could not be sent.")
          + " Press Copy message so nothing you wrote is lost.";
      }
    } catch (err) {
      msg.className = "report-msg warn";
      msg.textContent = `Could not send (${err.message}). Press Copy message so `
        + "nothing you wrote is lost.";
    } finally {
      send.disabled = false;
      send.textContent = label;
    }
  });
}

/* ----------------------------------------------------------- pick sharing */

/* The bargain is stated in one place: the notice in index.html.

   It was duplicated here too, for a card above the board, and the card went
   when the notice became a modal -- leaving a second copy of the wording
   behind, still saying the licence key is never sent. It is sent now, to prove
   the subscription is live. One copy, in the markup, is how that stops being
   possible. */

let shareState = null;

async function loadSharing(force = false) {
  if (shareState && !force) return shareState;
  try {
    shareState = await api("/api/sharing");
  } catch {
    shareState = null;
  }
  return shareState;
}

async function setSharing(patch) {
  try {
    shareState = await api("/api/sharing", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    });
  } catch { /* the switch stays where it was; nothing is sent either way */ }
  return shareState;
}

/* The controls that hang off the switch in Settings: who you are on the
   leaderboard, and the way out. Under the switch rather than on a page of
   their own -- "delete what you sent" is worth nothing if it is somewhere
   other than where you just turned the thing off. */
function shareControls(state) {
  if (!state) return "";
  return `<div class="share-extra">
    <div class="share-id">You are
      <b>${esc(state.display_name)}</b>
      <span class="mono">${esc(state.picker_id || "no licence key yet")}</span></div>
    <label class="side-label" for="share-name">Name on the leaderboard
      <span class="muted">(optional)</span></label>
    <input id="share-name" type="text" maxlength="40" spellcheck="false"
      placeholder="${esc(state.default_name)}"
      value="${esc(state.display_name === state.default_name ? "" : state.display_name)}" />
    <div class="controls">
      <button class="btn" id="share-delete" type="button">Delete my shared picks</button>
      <span class="muted" id="share-msg">${state.queued
        ? `${state.queued} pick${state.queued === 1 ? "" : "s"} waiting to send`
        : ""}</span>
    </div>
  </div>`;
}

/* The notice, once, before a single pick is queued.

   Sharing is on by default. The only thing that makes that defensible is this
   modal: it is shown on the first launch after activation, and to anybody
   upgrading into this version on their first launch after it, and nothing is
   collected until one of its two buttons has been pressed. The server enforces
   that as well -- see sharing.may_send -- so a modal somebody managed to skip
   does not quietly turn into consent.

   It cannot be dismissed by clicking away or pressing Escape, which is the one
   place in this app where that is the right call: every other dialog is
   something you opened, and this is something being told to you. */
async function showShareNotice() {
  const state = await loadSharing(true);
  if (!state || !state.needs_notice) return;
  const dlg = $("#share-notice");
  if (!dlg || dlg.open) return;

  const choose = async (on) => {
    await setSharing({ enabled: on, notice_seen: true });
    dlg.close();
    // Whatever page is up may be showing the indicator, or about to.
    render().catch(() => {});
  };
  $$("[data-notice-choice]", dlg).forEach((b) => b.addEventListener("click",
    () => choose(b.dataset.noticeChoice === "on")));
  // Escape is a way to be counted as sharing without having chosen to, so it
  // is not a way out of this one.
  dlg.addEventListener("cancel", (ev) => ev.preventDefault());
  dlg.showModal();
}

/* While sharing is on, say so, on the page where the picks are made.

   A choice made once in a modal is a choice somebody has forgotten by
   November. This is the line that keeps it from being a thing that happens to
   them quietly -- small, permanent, and one click from the switch. */
function shareIndicator(state) {
  if (!state || !state.enabled || state.needs_notice) return "";
  return `<button class="share-flag" type="button" data-open-share
    title="Your picks are shared with The Edge, with the line you took and an id — not your name, email or licence key. Click to change it or to delete what has been shared.">
    <span class="dot"></span>Sharing picks<span class="sep">·</span><b>change</b></button>`;
}

function wireShareIndicator(root) {
  $$("[data-open-share]", root).forEach((b) => b.addEventListener("click", () => {
    // Open the group it lives in first, so the switch is on screen rather than
    // behind a closed section on a page of closed sections.
    openSettings.add("Sharing");
    setTab("settings");
  }));
}

function wireShareControls(root) {
  const name = $("#share-name", root);
  if (name) {
    name.addEventListener("change", async () => {
      await setSharing({ display_name: name.value });
      const msg = $("#share-msg", root);
      if (msg) msg.textContent = `You are ${shareState?.display_name || ""}.`;
    });
  }
  const del = $("#share-delete", root);
  if (del) {
    del.addEventListener("click", async () => {
      const msg = $("#share-msg", root);
      // Asked once, because it cannot be undone: the server keeps no copy.
      if (!window.confirm("Delete every pick you have shared? This removes "
        + "them from The Edge's server and cannot be undone.")) return;
      del.disabled = true;
      const was = del.textContent;
      del.textContent = "Deleting…";
      try {
        const out = await api("/api/sharing/delete", { method: "POST" });
        if (msg) {
          msg.textContent = out.ok
            ? `Deleted${out.deleted ? ` ${out.deleted} pick${
              out.deleted === 1 ? "" : "s"}` : ""}.`
            : (out.message || "That could not be deleted.");
        }
      } finally {
        del.disabled = false;
        del.textContent = was;
      }
      await loadSharing(true);
    });
  }
}

/* ------------------------------------------------------------- licensing */

let gatePromise = null;

/* Full-screen activation form. Resolves once a key has been accepted. */
function showLicenseGate(lic) {
  if (gatePromise) return gatePromise;
  const gate = $("#license-gate");
  const form = $("#license-form");
  const input = $("#license-key");
  const msg = $("#license-msg");
  const submit = $("#license-submit");
  const store = $("#license-store");
  if (lic.store_url) { store.href = lic.store_url; store.hidden = false; }
  msg.className = "license-msg";
  msg.textContent = lic.has_key ? (lic.message || "") : "";
  gate.hidden = false;
  setTimeout(() => input.focus(), 50);
  gatePromise = new Promise((resolve) => {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      submit.disabled = true;
      submit.textContent = "Checking…";
      msg.className = "license-msg";
      msg.textContent = "";
      try {
        const res = await fetch("/api/license/activate", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ key: input.value.trim() }),
        });
        const out = await res.json();
        if (out.valid) {
          msg.className = "license-msg ok";
          msg.textContent = "Activated. Loading The Edge…";
          gate.hidden = true;
          gatePromise = null;
          resolve(out);
          return;
        }
        msg.textContent = out.message || "That key didn't work.";
      } catch {
        msg.textContent = "Couldn't reach The Edge. Try again.";
      } finally {
        submit.disabled = false;
        submit.textContent = "Activate";
      }
    });
  });
  return gatePromise;
}

async function ensureLicensed() {
  const lic = await fetch("/api/license").then((r) => r.json())
    .catch(() => ({ required: false, valid: true }));
  if (lic.required && !lic.valid) await showLicenseGate(lic);
  paintLicense();
}

/* The sidebar line and, when it matters, a banner: offline for days, or a
   subscription that is winding down. */
async function paintLicense() {
  const lic = await fetch("/api/license").then((r) => r.json()).catch(() => null);
  const line = $("#license-line");
  const banner = $("#license-banner");
  if (!lic || !lic.required) { line.hidden = true; banner.hidden = true; return; }
  line.hidden = false;
  line.className = "license-line";
  line.textContent = `Subscription ${lic.valid ? "active" : "inactive"} · key ${lic.key_hint || ""}`;
  let warn = "";
  if (lic.offline && lic.grace_days_left !== null) {
    line.className = "license-line warn";
    const days = Math.max(0, Math.floor(lic.grace_days_left));
    warn = `Can't reach the license server. The Edge keeps working offline for ${days} more day${days === 1 ? "" : "s"}.`;
  } else if (lic.status === "canceling") {
    warn = lic.message || "Your subscription ends at the close of this billing period.";
  }
  if (!warn) { banner.hidden = true; return; }
  banner.innerHTML = `<span class="grow">${esc(warn)}</span>` +
    (lic.store_url ? `<a class="pill" href="${esc(lic.store_url)}" target="_blank" rel="noopener">Manage subscription</a>` : "");
  banner.hidden = false;
}

/* --------------------------------------------------------- update notice */

/* A slot in the corner, not a banner across the top.

   The banner this replaces had a Later button, which is the whole problem
   with it: dismissing the notice dismissed the only thing that ever said a
   newer version existed, and the copy went on being out of date silently.
   The slot beside the version number cannot be dismissed because it never
   demanded anything -- it is blank until there is something to say, and it
   stays lit for as long as that is still true. */
async function checkForUpdate() {
  const slot = $("#update-slot");
  if (!slot) return;
  const up = await fetch("/api/updates").then((r) => r.json()).catch(() => null);
  if (!up || !up.newer || !up.latest) { slot.hidden = true; return; }
  const day = (up.published_at || "").slice(0, 10);
  slot.title = `A newer version was published${day ? ` on ${day}` : ""}.`
    + (up.notes ? ` ${up.notes}` : "")
    + " Install it over this one — your picks and settings are kept.";
  slot.hidden = false;
  slot.onclick = () => {
    if (up.url) window.open(up.url, "_blank", "noopener");
  };
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
/* Shown wherever a number could be read as "bet this".

   What it does not do any more is quote the backtest at the reader. It used
   to lead with the model's measured rate against the closing line, which was
   accurate and was the wrong place for it: a line that sits under every
   number on the page and argues with them is a footnote picking a fight with
   the product. That measurement has a home -- the Performance page, in
   context, with the sample size beside it -- and anyone who wants to know how
   the model does against the market can read it there.

   What stays is the part that is actually a duty of care: these are outputs
   and not recommendations, do not stake what you cannot lose, and the number
   to call if it stops being a game. */
function wagerNotice() {
  return `<p class="note wager-note">
    <b>Not betting advice.</b> These are model outputs, not recommendations,
    and no model is a sure thing. Never stake money you cannot afford to lose.
    If gambling stops being fun, stop: in the US, call or text
    1-800-GAMBLER.</p>`;
}

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
  sideFoot(meta);
}

/* What the bottom of the sidebar shows, which depends on where you are.

   Connections only on Settings: everywhere else it was eight rows of green
   dots reporting that nothing had happened, which is a lot of standing space
   for a question you only ask when something is wrong. The game goes there
   instead -- what is on now, or what is on next. */
function sideFoot(meta) {
  const conns = $("#side-conns");
  const now = $("#side-now");
  if (!conns || !now) return;
  const onSettings = state.tab === "settings";
  conns.hidden = !onSettings;
  now.hidden = onSettings;
  if (onSettings) return;

  // The *current* week, not the week being browsed. Looking at week 3 in
  // October does not mean there is no game on tonight, and the sidebar
  // answering "what is on now" with "nothing" because you clicked back a week
  // is the wrong answer to a question about the clock.
  const games = state.liveSlate || [];
  const live = games.filter((g) => g.status === "in_progress");
  const next = games
    .filter((g) => g.status === "scheduled" && g.kickoff)
    .sort((a, b) => String(a.kickoff).localeCompare(String(b.kickoff)))[0];

  /* A card, not a caption.

     It reads as a matchup on the board, and it is clickable the same ways:
     the card itself goes to Home and the week the game is in, either team
     opens that matchup. It looked like a status line, so nobody tried -- the
     box is what says it can be pressed.

     The percentage is what the model gave that team *before* kickoff, which
     is the number worth having beside a score: it is what the game is being
     measured against. Predictions are written once and kept, so it stays the
     pre-game figure while the game is played rather than drifting into a
     live readout. */
  const line = (g, kicking) => {
    const score = (side) => {
      const v = g[`${side}_score`];
      return v === null || v === undefined ? "" : v;
    };
    const homeProb = g.prediction && g.prediction.home_win_prob;
    const prob = (side) => {
      if (homeProb === null || homeProb === undefined) return "";
      const value = side === "home" ? Number(homeProb) : 1 - Number(homeProb);
      return `<span class="sg-prob" title="What the model gave ${
        esc(g[side])} before kickoff">${pct(value, 0)}</span>`;
    };
    const row = (side) => `<div class="sg-row" data-side-team="${esc(g[side])}"
        role="button" tabindex="0" title="${esc(g[side])} — open this matchup">
      <span class="sg-team">${esc(g[side])}</span>
      ${prob(side)}
      <span class="sg-score">${score(side)}</span></div>`;
    return `<div class="side-game${kicking ? "" : " on"}"
        data-side-game="${esc(g.game_id)}" data-side-week="${esc(String(g.week))}"
        role="button" tabindex="0" title="Open this week on Home">
      ${row("away")}
      ${row("home")}
      <div class="sg-when">${kicking
        ? esc(untilKickoff(g.kickoff))
        /* The quarter and the clock, the same label the board's live cards
           carry. It used to read "in progress", which is the one thing you
           can already see from the scores being there -- and leaves out the
           only part that says whether the score still means anything. The
           live row is the fallback, not the other way round. */
        : `<span class="live-dot"></span>${esc(g.live
            ? liveLabel(g.live, true)
            : (g.clock || "in progress"))}`}</div>
    </div>`;
  };

  if (live.length) {
    now.innerHTML = `<div class="side-label">
      ${live.length > 1 ? `${live.length} games on now` : "On now"}</div>`
      + live.slice(0, 3).map((g) => line(g, false)).join("");
  } else if (next) {
    now.innerHTML = '<div class="side-label">Next up</div>' + line(next, true);
  } else {
    now.innerHTML = '<div class="side-label">Next up</div>'
      + '<div class="side-game"><div class="sg-when">No games scheduled.</div></div>';
  }
  wireSideGames(now);
}

/* Clicks on the sidebar card.

   A team opens its matchup; anywhere else goes to Home on that game's week.
   The team handler stops the event so one press does not do both -- which it
   did, landing you on Home behind a dialog you did not mean to open. */
function wireSideGames(root) {
  const go = (node, fn) => {
    node.addEventListener("click", fn);
    node.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); fn(ev); }
    });
  };
  $$("[data-side-game]", root).forEach((card) => {
    go(card, () => {
      const week = Number(card.dataset.sideWeek);
      if (Number.isFinite(week) && week) state.week = week;
      const sel = $("#week");
      if (sel) sel.value = String(state.week);
      setTab("home");
    });
  });
  $$("[data-side-team]", root).forEach((node) => {
    go(node, (ev) => {
      ev.stopPropagation();
      openGame(node.closest("[data-side-game]").dataset.sideGame);
    });
  });
}

/* "in 2h 14m", and it has to be recomputed rather than rendered once -- the
   whole point of the line is that it counts down. */
function untilKickoff(kickoff) {
  const ms = new Date(kickoff).getTime() - Date.now();
  if (!Number.isFinite(ms)) return "";
  if (ms <= 0) return "kicking off";
  const mins = Math.round(ms / 60000);
  if (mins < 60) return `in ${mins}m`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `in ${hours}h ${mins % 60}m`;
  return `in ${Math.round(hours / 24)}d`;
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
  // The selector's own label, so a postseason round is named rather than
  // numbered: "Divisional · 2026 season", not "Week 20".
  const named = (state.weekOptions || []).find((o) => Number(o.value) === Number(week));
  line.textContent = `${named ? named.label : `Week ${week}`} · ${season} season`;
  line.classList.toggle("past", !live);
  // No colour on the current week, and none on the date.
  //
  // Green was meant to save the reader comparing a week number against the
  // selector. It did, and it cost more than it saved: this line sits directly
  // under the wordmark, so the one green thing on the page was not the pick,
  // the edge or the live game -- it was a label saying today is today. An
  // accent that fires on the default state is not an accent, it is the body
  // colour with extra steps, and it made every genuinely green thing further
  // down the page read as less urgent than the header.
  //
  // The past week keeps its dimming. That one earns its ink: it says you are
  // looking at something other than now, which is the state you can be in
  // without meaning to be.
  line.title = live ? "The current week" : "Not the current week";
}

/* How serious an injury status is, for colour. Out and IR are settled; a
   questionable is a coin flip that still moves a line by a point. */

/* "Sun 1:00" — the board has sixteen rows and no width to spare for a date
   that is the same on most of them. */

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
      <div class="panel-body"><div class="empty">${
        esc(clv.note || "Nothing to measure yet.")}</div></div>
    </div>`;
  }
  const good = clv.average > 0;
  const vs = clv.versus_model;
  /* A small table of the same number cut a different way. One season figure
     says whether you are beating the line; it cannot say where. */
  const cut = (rows, key, label) => !rows || rows.length < 2 ? "" : `
    <div class="clv-cut">
      <h3 class="sub-head">${esc(label)}</h3>
      <div class="table-scroll"><table class="slate">
        <thead><tr><th>${esc(key === "week" ? "Wk" : "Team")}</th>
          <th class="num">Picks</th><th class="num">Beat</th>
          <th class="num">Value</th></tr></thead>
        <tbody>${rows.map((r) => `<tr>
          <td class="who">${key === "week" ? `Week ${r.week}` : esc(r.selection)}</td>
          <td class="num muted">${r.n}</td>
          <td class="num muted">${r.beat}</td>
          <td class="num ${r.average > 0 ? "hit" : (r.average < 0 ? "miss" : "")}">${
            signed(r.average, 2)}</td>
        </tr>`).join("")}</tbody></table></div>
    </div>`;

  const WHAT_IT_MEANS = "Positive means you took a better number than the one "
    + "that closed \u2014 you backed a team at \u22123 and it closed \u22125, so you "
    + "have two points of value whether or not they covered. It needs a fraction of "
    + "the sample a win rate does, because it measures the market coming round to "
    + "your number rather than the game going your way.";

  return `<div class="panel">
    <header><h2>Your closing-line value</h2>
      <button class="why" type="button" title="${esc(WHAT_IT_MEANS)}"
        aria-label="${esc(WHAT_IT_MEANS)}">?</button>
      <span class="hint">${clv.n} pick${clv.n === 1 ? "" : "s"} the line moved after</span></header>
    <div class="panel-body">
    <div class="tiles">
      <div class="tile"><div class="label">Points vs the close</div>
        <div class="value ${good ? "pos" : "neg"}">${signed(clv.average, 2)}</div>
        <div class="sub">per pick, averaged</div></div>
      <div class="tile"><div class="label">Beat the close</div>
        <div class="value ${clv.beat_rate > 0.5 ? "pos" : ""}">${pct(clv.beat_rate, 0)}</div>
        <div class="sub">${clv.beat} of ${clv.n} picks</div></div>
      ${vs && vs.n ? `<div class="tile" title="The same games at the same moment, the only difference being which side was taken. A game you both called the same way cancels out, which is right — you cannot claim credit for agreeing.">
        <div class="label">You vs the model</div>
        <div class="value ${vs.difference > 0 ? "pos" : (vs.difference < 0 ? "neg" : "")}">${
          signed(vs.difference, 2)}</div>
        <div class="sub">you ${signed(vs.yours, 2)} · model ${signed(vs.model, 2)}</div></div>
      <div class="tile"><div class="label">Where you differed</div>
        <div class="value">${vs.disagreed}</div>
        <div class="sub">of ${vs.n} · agreed on ${vs.agreed}</div></div>` : ""}
    </div>
    ${vs && vs.disagreed === 0 && vs.n ? `<p class="note">You have taken the
      model's side on every game it had a view on, so there is nothing yet to
      separate your judgement from its own. The comparison starts saying
      something the first time you disagree with it.</p>` : ""}

    <!-- Four numbers answer the question. Every pick behind them, and the
         same number cut by week and by team, are what you read once the
         answer has made you want to know where it came from. -->
    <details class="more">
      <summary><span class="chev"></span>Every pick<span class="hint">
        ${clv.n} · and the same number by week and by team</span></summary>
      <div class="table-scroll"><table class="slate">
        <thead><tr><th>Wk</th><th>Pick</th><th>Game</th>
          <th class="num">You got</th><th class="num">Closed</th>
          <th class="num">Value</th></tr></thead>
        <tbody>${clv.picks.map((pk) => `<tr>
          <td class="num muted">${pk.week}</td>
          <td class="who">${esc(pk.selection)}</td>
          <td class="muted">${esc(pk.matchup)}</td>
          <td class="num">${signed(pk.line_at_pick, 1)}</td>
          <td class="num muted">${signed(pk.line_at_close, 1)}</td>
          <td class="num ${pk.clv > 0 ? "hit" : (pk.clv < 0 ? "miss" : "")}">${
            signed(pk.clv, 1)}</td>
        </tr>`).join("")}</tbody></table></div>
      <div class="grid-2 clv-cuts">
        ${cut(clv.by_week, "week", "By week")}
        ${cut(clv.by_team, "selection", "By team · worst first")}
      </div>
    </details>
    </div>
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
  /* The prediction-market venue comes out of this page.

     It was a column beside the blind model, the blend and the book, and it
     could not be read the way the other three are: it scores only the games
     Kalshi and Polymarket happened to quote, which is a different and much
     smaller set every week, so its rate sat in a row of rates that are not
     comparable to it. The venues still have their own place in the app; what
     they do not have is a column pretending to be a fourth opinion on the
     same games. */
  const pickers = (d.pickers || []).filter((p) => p !== "market");
  if (!d.weeks.length) {
    paint(root, `<div class="panel"><div class="empty">
      Nothing graded yet — this fills in as games finish and you record picks on the board.
    </div></div>`);
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
      <td class="who" title="${esc((d.descriptions || {})[key] || "")}">${esc(label)}${borrowed
        ? `<span class="rec" title="games from before the model existed, shown with the sportsbook's pick">${borrowed} inherited</span>`
        : ""}
        ${note ? `<button class="why" type="button" title="${esc(note)}"
          aria-label="${esc(note)}">?</button>` : ""}</td>
      ${cell(all)}${cell(common)}
    </tr>`;
  };

  /* Sorting the team table. Every column is a different question -- which
     teams *we* read best, which ones the market reads best, where the two
     disagree -- and the answer to each is one click, not a different page. */
  const sort = state.sbSort && (state.sbSort.key === "team" || state.sbSort.key === "games"
    || pickers.includes(state.sbSort.key))
    ? state.sbSort
    /* Opens alphabetically. A table of thirty-two teams is one you come to
       with a team in mind, and finding it by name is the common errand;
       ranking them by a rate is the question you ask second, which is what
       the column headers are for. It used to open on the blend, which is a
       useful order and a poor index. */
    : { key: "team", dir: "asc" };

  const sortValue = (t, key) => {
    if (key === "team") return t.team;
    if (key === "games") return t.games;
    const v = t.tallies[key];
    // A picker with no view on a team sorts last either way rather than
    // landing at the top of an ascending sort as if it scored zero.
    return v && v.n ? v.rate : null;
  };
  /* How many games are behind a rate, for breaking ties on it. Two perfect
     records are not equally impressive: 2-0 has twice the evidence of 1-0 and
     belongs above it. Applied as a tie-break rather than as a weighting,
     because the column is a rate and re-ranking it by sample size would make
     the numbers on screen stop explaining the order they are in. */
  const sortWeight = (t, key) => {
    if (key === "team" || key === "games") return 0;
    const v = t.tallies[key];
    return v && v.n ? v.n : 0;
  };
  const sortedTeams = [...(d.teams || [])].sort((a, b) => {
    const av = sortValue(a, sort.key);
    const bv = sortValue(b, sort.key);
    if (av === null && bv === null) return a.team.localeCompare(b.team);
    if (av === null) return 1;
    if (bv === null) return -1;
    let cmp = typeof av === "string" ? av.localeCompare(bv) : av - bv;
    // The deeper record first, in whichever direction the column is sorted:
    // more evidence is better either way round, so this one does not flip.
    if (cmp === 0 && typeof av !== "string") {
      const byWeight = sortWeight(a, sort.key) - sortWeight(b, sort.key);
      if (byWeight !== 0) return -byWeight;
    }
    if (cmp === 0) return a.team.localeCompare(b.team);
    return sort.dir === "desc" ? -cmp : cmp;
  });

  const sortHead = (key, label, cls) => {
    const on = key === sort.key;
    return `<th class="${cls}${on ? " sorted" : ""}" data-sort="${esc(key)}"
      aria-sort="${on ? (sort.dir === "desc" ? "descending" : "ascending") : "none"}"
      title="Sort by ${esc(label)}" tabindex="0" role="button"
      >${esc(label)}<span class="sort-arrow">${on ? (sort.dir === "desc" ? "▾" : "▴") : "⇅"}</span></th>`;
  };

  fitsOneScreen(root);
  /* Three answers down the left, the league down the right.

     Two by two gave the by-team table a quarter of the page for thirty-two
     rows, so twenty-five of them sat behind a scroll -- on the one panel
     whose whole point is comparing teams with each other, which you cannot do
     through a seven-row window. It gets a full-height column of its own; the
     three panels that are four rows, four tiles and a run share the other. */
  if (!paint(root, `<div class="perf-grid">
  <div class="perf-stack">
  <div class="panel">
    <header><h2>Season ${d.season}</h2>
      <span class="hint" title="Straight-up winners only, because every source here names a favourite and it is the one question all of them can be asked. &quot;Same games&quot; scores only the games every picker had a view on.">straight-up winners</span></header>
    <div class="panel-body">
    <div class="table-scroll"><table class="slate totals">
      <thead><tr><th>Picker</th><th class="num">All their picks</th>
        <th class="num">Same games</th></tr></thead>
      <tbody>${pickers.map((p) => totalRow(p, d.labels[p])).join("")}</tbody>
    </table></div>

    <!-- The season is the answer; the eighteen weeks behind it are the
         working. Four rows and a scrollbar was the whole panel spent on
         showing that there was more, rather than on the more. -->
    <details class="more">
      <summary><span class="chev"></span>Week by week<span class="hint">
        ${d.weeks.length} week${d.weeks.length === 1 ? "" : "s"} · correct out of picked
      </span></summary>
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
    </details>
    </div>
  </div>

  <div class="panel" id="survivor-track">
    <header><h2>Survivor: the original run</h2>
      <span class="hint">the plan as first made, against the teams you
        actually spent</span></header>
    <div class="panel-body"><div class="empty">Loading…</div></div>
  </div>
  </div>

  <div class="panel perf-league">
    <header><h2>By team</h2>
      <span class="hint">how often each picker called that team's games right ·
        click a column to sort by it, or a row for that team</span></header>
    <div class="panel-body">
    <div class="table-scroll"><table class="slate sortable">
      <thead><tr>
        ${sortHead("team", "Team", "")}
        ${sortHead("games", "Games", "num")}
        ${pickers.map((p) => sortHead(p, d.labels[p], "num")).join("")}
      </tr></thead>
      <tbody>${sortedTeams.map((t) => `<tr data-team-card="${esc(t.team)}"
        tabindex="0" title="${esc((state.meta?.teams || {})[t.team]?.full_name
          || t.team)} — open their season">
        <td class="who perf-team">${teamMark(t.team)}<span>${esc(t.team)}</span></td>
        <td class="num muted">${t.games}</td>
        ${pickers.map((p) => {
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
  </div>
  </div>`)) return;

  /* The plan the optimiser made before any of it had happened, against what
     was actually picked. Fetched after the page is drawn rather than in the
     Promise.all above: it is the last thing on the page, it is behind a fold,
     and the rest of Performance should not wait on it. */
  api(`/api/survivor/tracker?season=${state.season}`)
    .then((t) => {
      const outer = $("#survivor-track", root);
      if (!outer || stale(ticket)) return;
      const panel = $(".panel-body", outer) || outer;
      const mark = { won: "\u2713", lost: "\u2715", tied: "=", pending: "\u00b7" };
      /* One table, not two.

         The plan and the picks were drawn as two lists side by side, which is
         the same shape as the question -- did I follow it? -- and none of the
         answer: the weeks only lined up while both runs covered the same ones,
         and reading across meant counting rows in two columns at once. Sharing
         the week column puts each pair on one row, where a week you deviated on
         is a row with two different names in it. It also halves the width, and
         the pair had been sharing a quarter of the page. */
      const byWeek = new Map();
      const side = (run, key) => (run.weeks || []).forEach((w) => {
        if (!byWeek.has(w.week)) byWeek.set(w.week, { week: w.week });
        byWeek.get(w.week)[key] = w;
      });
      side(t.original || {}, "plan");
      side(t.mine || {}, "mine");
      const weeks = [...byWeek.values()].sort((a2, b2) => a2.week - b2.week);

      /* The score sits in the cell's title rather than its own column. It is
         the widest thing in the run and the least often wanted: the tick has
         already said how the week went. */
      const runCell = (w) => {
        if (!w) return '<td class="muted none">\u2014</td>';
        const detail = w.score || (w.opponent ? `vs ${w.opponent}` : "");
        return `<td class="r-${esc(w.result)}"${detail ? ` title="${esc(detail)}"` : ""}
          ><span class="run-cell">${teamMark(w.team)}<b>${esc(w.team)}</b
          ><span class="res">${mark[w.result] || "\u00b7"}</span></span></td>`;
      };
      const ran = (run) => run.out_week
        ? `out in week ${run.out_week}`
        : `alive \u00b7 ${run.weeks_survived} survived`;

      panel.querySelector(".empty")?.remove();
      panel.querySelector(".track-split")?.remove();
      panel.querySelector(".verdict")?.remove();
      panel.querySelector(".track-foot")?.remove();
      /* A busted plan is not the end of the question.

         Once the plan goes out its column stops being a rival and becomes a
         gravestone: weeks, a cross partway down, and nothing after it. What
         it would do *from here* is still worth seeing, and it is the only way
         to keep judging the optimiser against your own picks for the rest of
         the season. Tucked in the corner rather than opened on the page --
         it is a counterfactual, and a counterfactual laid out beside real
         results is one more thing to mistake for them. */
      const continuation = (t.original || {}).continuation;
      panel.insertAdjacentHTML("beforeend", `
        ${t.verdict ? `<p class="verdict">${esc(t.verdict)}</p>` : ""}
        <div class="table-scroll"><table class="slate run-track track-split">
          <thead><tr><th>Wk</th>
            <th>The plan<span class="hint">${esc(ran(t.original || {}))}</span></th>
            <th>You<span class="hint">${esc(ran(t.mine || {}))}</span></th>
          </tr></thead>
          <tbody>${weeks.map((r) => `<tr>
            <td class="team">W${r.week}</td>
            ${runCell(r.plan)}${runCell(r.mine)}
          </tr>`).join("")}</tbody>
        </table></div>
        ${continuation ? `<div class="track-foot">
          <button class="btn tiny" id="plan-if-alive"
            title="What the optimiser would pick from here if the plan had survived, with the teams you have already spent taken out">
            If the plan had lived →</button>
        </div>` : ""}`);
      wireLogos(panel);
      $("#plan-if-alive", panel)?.addEventListener("click", () => {
        const dlg = $("#detail");
        paintDialogNav(null);
        $(".dialog-title", dlg).textContent =
          `If the plan had lived — from week ${continuation.week}`;
        const rows = (continuation.path || []).map((step) => `<tr>
          <td class="team">W${esc(String(step.week))}</td>
          <td><span class="run-cell">${teamMark(step.team)}<b>${esc(step.team)}</b></span></td>
          <td class="num">${pct(step.win_prob, 1)}</td>
          <td class="muted">${esc(step.opponent || "")}</td></tr>`).join("");
        $(".dialog-body", dlg).innerHTML = `
          <p class="note">The plan went out in week ${esc(String(
            (t.original || {}).out_week))}. This is what the optimiser would
            take from here if it had not — planned around the teams you have
            actually spent, because those are gone either way. It is a
            counterfactual, not a record: nothing below has been played.</p>
          <div class="table-scroll"><table class="slate">
            <thead><tr><th>Wk</th><th>Team</th><th class="num">Win prob</th>
              <th>Opponent</th></tr></thead>
            <tbody>${rows || '<tr><td colspan="4" class="muted">No weeks left to plan.</td></tr>'}</tbody>
          </table></div>`;
        wireLogos($(".dialog-body", dlg));
        if (!dlg.open) dlg.showModal();
      });
    })
    .catch(() => {});


  wireLogos(root);
  // The same card the board opens, from the row that names the team.
  $$("[data-team-card]", root).forEach((node) => {
    const open = (ev) => { ev.stopPropagation(); openTeam(node.dataset.teamCard); };
    node.addEventListener("click", open);
    node.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); open(ev); }
    });
  });

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
      // render(), not the view function: this page was renamed from
      // Scoreboard and the old name was left behind here, so every click on a
      // column heading threw a ReferenceError and sorted nothing.
      render();
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

/* The league's own mark, built exactly like a team's.

   It was a text-only badge, and beside thirty-two real crests a grey word on
   a near-black circle does not read as "all teams" -- it reads as the one
   logo that failed to load. Same CDN, same `hasimg` swap, same fallback: if
   the image does not arrive the lettering stands on the league's navy, which
   looks like a mark rather than like a hole in the row. */
const LEAGUE_LOGO = "https://a.espncdn.com/i/teamlogos/leagues/500/nfl.png";

function leagueMark() {
  return `<span class="tbadge league">
    <span class="mono">NFL</span>
    <img class="tlogo" alt="" src="${LEAGUE_LOGO}" />
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

/* The scoreboard, patched in place.

   A score changes every few minutes on a Sunday and the rest of the page
   does not change at all -- the projections, the lines, the picks and the
   season simulation are all settled until a game *ends*. Redrawing the whole
   board to move one number costs the scroll position, the hover, and a fetch
   of four endpoints, which is why the live tick used to be tied to the same
   minute timer as everything else and still felt late.

   So the tick writes the numbers straight into the cells that hold them and
   touches nothing else. The one thing it cannot do is grading: a game going
   final turns the card green and red, marks your pick won or lost and tallies
   the week, and none of that is a text node. A status change hands over to a
   real render, which is the one moment it is worth one. */
function paintLive(games) {
  let statusChanged = false;
  for (const g of games) {
    const card = $(`.gcard[data-game="${CSS.escape(String(g.game_id))}"]`);
    if (!card) continue;
    for (const side of ["away", "home"]) {
      const cell = $(`.gteam[data-side="${side}"] .tscore`, card);
      if (!cell) continue;
      const v = g[`${side}_score`];
      const text = v === null || v === undefined ? "" : String(v);
      if (cell.textContent !== text) cell.textContent = text;
    }
    const stamp = $(".gstate", card);
    if (stamp) {
      const wasLive = !!$(".live-dot", stamp);
      const html = gameStamp(g);
      if (stamp.innerHTML !== html) stamp.innerHTML = html;
      if (wasLive !== (g.status === "in_progress")) statusChanged = true;
    }
  }
  return statusChanged;
}

/* The slate the sidebar reads, brought up to date without refetching it.
   It holds the full game rows -- predictions, lines, records -- and the live
   payload holds four fields, so this merges rather than replaces. */
function mergeLive(games) {
  const slate = state.liveSlate;
  if (!Array.isArray(slate) || !slate.length) return false;
  const by = new Map(games.map((g) => [String(g.game_id), g]));
  let changed = false;
  for (const row of slate) {
    const g = by.get(String(row.game_id));
    if (!g) continue;
    if (row.status !== g.status) changed = true;
    row.status = g.status;
    row.home_score = g.home_score;
    row.away_score = g.away_score;
    row.live = g.live;
  }
  return changed;
}

/* How often to ask. Fast while something is being played, slow otherwise --
   the slow tick exists only to notice a kickoff, which is the one transition
   nothing else would catch. The fetch behind this is rate-limited on the
   server, so several windows open on the same app cost one request between
   them. */
const LIVE_TICK_FAST = 15000;
const LIVE_TICK_IDLE = 60000;

/* The outcome of a refresh, said where the button that started it is.

   There are two of these buttons -- one on Settings beside a result line, one
   in the sidebar on every page -- and only the first had anywhere to report
   to. So a failure pressed from the sidebar went nowhere at all: the spinner
   stopped and the page looked the same, which is indistinguishable from the
   button doing nothing. Both now say something, and the sidebar's line clears
   itself so it does not become furniture. */
let refreshNoteTimer = null;
function sayRefresh(text, tone) {
  const settings = $("#refresh-result");
  if (settings) {
    settings.textContent = text;
    settings.className = tone;
  }
  const note = $("#side-refresh-note");
  if (!note) return;
  note.textContent = text;
  note.className = `side-note ${tone}`;
  note.hidden = false;
  clearTimeout(refreshNoteTimer);
  // A failure stays up; good news does not need to.
  if (tone === "pos") {
    refreshNoteTimer = setTimeout(() => { note.hidden = true; }, 4000);
  }
}

function startLiveTicker() {
  let timer = null;
  const tick = async () => {
    let next = LIVE_TICK_IDLE;
    /* Not while a refresh is running -- it is about to redraw everything
       anyway -- and not while the window is hidden, which is most of the week
       for an app left open in the background. */
    if (!state.busy && !document.hidden) {
      try {
        const week = state.meta?.week ?? state.week;
        const season = state.meta?.season ?? state.season;
        const data = await api(`/api/live?season=${season}&week=${week}`);
        const games = data.games || [];
        const anyLive = games.some((g) => g.status === "in_progress");
        next = anyLive ? LIVE_TICK_FAST : LIVE_TICK_IDLE;
        // The board is only on screen, and only showing these games, when
        // Home has the live week up.
        const onLiveWeek = state.tab === "home"
          && state.week === week && state.season === season;
        const statusChanged = onLiveWeek ? paintLive(games) : false;
        const slateChanged = mergeLive(games);
        if (slateChanged || anyLive) sideFoot(state.meta);
        // A game has started or finished: the cards need grading, the picks
        // need tallying, and neither is a text node.
        if (statusChanged || (slateChanged && !onLiveWeek)) {
          await render({ keepPlace: true });
        }
      } catch { /* transient: the next tick asks again */ }
    }
    timer = setTimeout(tick, next);
  };
  timer = setTimeout(tick, LIVE_TICK_FAST);
  // A window coming back to the front has been showing a frozen score for as
  // long as it was hidden, so it asks at once rather than waiting out the
  // rest of an interval it spent asleep.
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) return;
    clearTimeout(timer);
    timer = setTimeout(tick, 200);
  });
}

/* Did the favourite win, or did it not.

   Judged against the book rather than against our own number, because "upset"
   is a claim about what the world expected and the book is the closest thing
   to a public answer. A game the market had at a coin flip is neither, so it
   says so instead of calling a 50.4% favourite losing an upset.

   One function because it is read twice: once per card, for the letter in the
   corner, and once across the week for the Chaos meter at the top. Two copies
   of a rule with a threshold in it is two rules waiting to disagree. */
function gameVerdict(g) {
  const book = g.market?.home_win_prob;
  if (book === null || book === undefined) return null;
  if (Math.abs(Number(book) - 0.5) < 0.02) return null;
  if (g.status !== "final" || g.home_score === null || g.away_score === null
      || g.home_score === g.away_score) return null;
  const winner = g.home_score > g.away_score ? g.home : g.away;
  return winner === (Number(book) > 0.5 ? g.home : g.away) ? "expected" : "upset";
}

async function renderHome(ticket) {
  const root = $("#view");
  const data = await api(`/api/games?week=${state.week}&season=${state.season}`);
  if (stale(ticket)) return;
  // Browsing the live week means Home has just fetched exactly what the
  // sidebar wants, so it is handed over rather than fetched twice.
  if (state.week === state.meta?.week && state.season === state.meta?.season) {
    state.liveSlate = data.games || [];
    sideFoot(state.meta);
  }
  if (!data.games.length) {
    paint(root, '<div class="panel"><div class="empty">No games stored for this week yet.</div></div>');
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

  // This week's survivor pick, and every week the others were spent in, so a
  // team already used is shown as unavailable rather than silently moving.
  // Both come down with the games rather than as a second request: they are
  // facts about the same week and would only be fetched together anyway.
  const survivorPick = data.survivor_pick || null;
  const usedWeeks = data.survivor_used_weeks || {};

  /* Broadcast order: the order ESPN and the books list a week in, which is
     simply kickoff time, with anything already finished moved to the back.

     It used to pull live games to the *front*, and that is what put the board
     out of order. A game in progress is not a separate category to be promoted
     -- on a Sunday afternoon most of the board is in progress -- so a live
     Sunday night game leapt above the one o'clock games that started hours
     before it. Live and upcoming now sort together, by when they kick off,
     and only finished games move, which is the part that was worth keeping:
     they are the ones you have stopped needing.

     Compared as instants rather than as strings. `localeCompare` on an ISO
     timestamp is right only while every row carries the same UTC offset, and
     the moment one arrives as `Z`, or as a local `-04:00`, two games at the
     same moment sort apart and an earlier one can sort later. */
  const kickAt = (g) => {
    const t = Date.parse(g.kickoff);
    return Number.isNaN(t) ? Infinity : t;      // undated games sit at the end
  };
  /* Who is not playing, in the hole their absence leaves.

     A bye week takes two or three games off the board and leaves a ragged
     gap at the end of the grid. The teams that made the gap are the obvious
     thing to put in it, and they are genuinely worth a look: their rivals are
     playing, their ranking is about to move without them, and a survivor pick
     cannot use them. Split by conference because that is how anybody reading
     a bye week is already thinking about it. */
  const byeBox = (conf, rows) => rows.length ? `<article class="gcard byecard">
    <div class="byehead">${esc(conf)} on bye</div>
    <div class="byelist">${rows.map((b) => `<button type="button" class="byerow"
      data-team-card="${esc(b.team)}"
      title="${esc(b.full_name || b.team)} — where their season stands">
      ${teamMark(b.team)}
      <span class="tname"><span class="nick">${esc(b.name || b.team)}</span>
        <span class="abbr">${esc(b.team)}</span></span>
      <span class="trec">${esc(b.record || "")}</span>
      <span class="byerank">${b.rank ? `#${b.rank}` : "–"}</span>
      <span class="bypow muted">${b.power === null || b.power === undefined
        ? "" : signed(b.power, 1)}</span>
    </button>`).join("")}</div></article>` : "";
  const byes = data.byes || [];
  const byeCards = byeBox("AFC", byes.filter((b) => b.conference === "AFC"))
    + byeBox("NFC", byes.filter((b) => b.conference === "NFC"));

  const games = [...data.games].sort((a, b) => {
    const done = (g) => (g.status === "final" ? 1 : 0);
    return done(a) - done(b)
      || kickAt(a) - kickAt(b)
      || String(a.game_id).localeCompare(String(b.game_id));
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

    /* How a finished game grades every number on the card.

       The margin from the home side, and the points actually scored. Both are
       null until the game is final, which is what every grader below checks
       first -- an unplayed game has no verdict, not a neutral one. */
    const finalMargin = g.status === "final" && g.home_score !== null
      && g.away_score !== null ? Number(g.home_score) - Number(g.away_score) : null;
    const finalTotal = g.status === "final" && g.home_score !== null
      && g.away_score !== null ? Number(g.home_score) + Number(g.away_score) : null;

    /* A pushed bet is not a wrong one.

       Nothing here used to be able to say "neither": a spread landing exactly
       on the number, a total landing exactly on the line, and a source with
       no opinion at all were all resolved into a green or a red by whichever
       way a floating-point comparison happened to fall. A push is its own
       answer and gets its own colour -- grey, which is the honest one. */
    const PUSH = " push";
    const grade = (right) => right === null ? PUSH : (right ? " hit" : " miss");

    /* Did this side cover its own spread?

       `homeLine` is the home team's number, so the away side's is its
       negative; a side covers when the final margin beats the number it was
       laying. Within a tenth of a point of the line is a push. */
    const coverVerdict = (homeLine, side) => {
      if (finalMargin === null || homeLine === null || homeLine === undefined) return "";
      const margin = side === "home" ? finalMargin : -finalMargin;
      const line = side === "home" ? Number(homeLine) : -Number(homeLine);
      const edge = margin + line;
      return Math.abs(edge) < 0.05 ? PUSH : grade(edge > 0);
    };

    // One source's opinion about one team: its line for that side, its
    // probability for that side, and whether that is the side it picked.
    const cell = (kind, homeProb, homeLine, side) => {
      const picked = homeProb !== null && Math.abs(Number(homeProb) - 0.5) > 1e-9
        && ((Number(homeProb) > 0.5) === (side === "home"));
      /* Graded by who won, not by who this source picked.

         The verdict used to be set only on the side a source had picked, so
         exactly one of the two rows on a card was ever coloured -- the other
         stayed neutral whatever had happened to it. Both rows carry it now:
         the winner's numbers go green and the loser's red, on every column,
         and the tick still says which side each source was on. A game the
         source called a dead heat is greyed rather than assigned. */
      const verdict = actualWinner === null ? ""
        : (homeProb !== null && Math.abs(Number(homeProb) - 0.5) < 1e-9 ? PUSH
          : grade(g[side] === actualWinner));
      const lineVerdict = coverVerdict(homeLine, side);
      const prob = homeProb === null ? null
        : (side === "home" ? Number(homeProb) : 1 - Number(homeProb));
      const borrowed = kind === "ours" && inherited;
      return `<div class="gcell ${kind}${picked ? " picked" : ""}${verdict}${
        borrowed ? " borrowed" : ""}"${borrowed
        ? ' title="This game finished before the app was running, so the model has no number of its own. Its pick is the sportsbook\'s; the spread and total are left blank rather than copied, which would read as the model agreeing on them."'
        : ""}>
        <span class="gline${lineVerdict}"${lineVerdict && !borrowed
          ? ` title="${lineVerdict === PUSH ? "Pushed — the game landed on the number"
            : (lineVerdict === " hit" ? "Covered" : "Did not cover")}"` : ""
          }>${homeLine === undefined ? "" : esc(lineText(homeLine, side))}</span>
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
      /* The survivor pick, made here rather than on a grid of crests two pages
         away. Picking it beside the game means the week comes with it, which
         is the whole reason to move it: a list of teams you have used cannot
         say which week you spent each one in, and that is the only thing that
         makes a run checkable afterwards. */
      const survivorHere = survivorPick === abbr;
      const survivorElsewhere = survivorHere ? null : (usedWeeks[abbr] ?? null);
      const survivorVerdict = !survivorHere || actualWinner === null
        ? "" : (abbr === actualWinner ? " hit" : " miss");
      // Blue while the game is undecided, so your pick still reads as yours
      // rather than as a result you have not earned yet.
      const yourVerdict = !mineHere || actualWinner === null
        ? "" : (abbr === actualWinner ? " hit" : " miss");
      const beaten = actualWinner !== null && abbr !== actualWinner;
      const won = actualWinner !== null && abbr === actualWinner;
      return `<div class="gteam${beaten ? " beaten" : ""}${won ? " won" : ""}"
        data-side="${side}">
        <button class="pickdot${mineHere ? " on" : ""}${yourVerdict}" data-pick="${esc(g.game_id)}"
          data-team="${esc(abbr)}" title="${mineHere ? "Your pick — click to clear" : `Pick ${esc(abbr)}`}"
          aria-label="${mineHere ? "Your pick" : `Pick ${esc(abbr)}`}">${
            mineHere ? (yourVerdict === " miss" ? "✕" : "✓") : ""}</button>
        <span class="tlabel" data-team-card="${esc(abbr)}" role="button"
          tabindex="0" title="${esc(t.full_name || abbr)}${
            side === "home" ? " (home)" : " (away)"} — open their season">
          ${teamMark(abbr)}
          <span class="tname"><span class="nick">${
            esc(t.name || abbr)}</span><span class="abbr">${esc(abbr)}</span></span>
        </span>
        <span class="trec" title="Record going into this week">${
          esc(g[`${side}_record`] || "")}</span>
        <button class="sdot${survivorHere ? " on" : ""}${survivorVerdict}"
          data-survivor="${esc(abbr)}"${survivorElsewhere
            ? ` disabled title="${esc(abbr)} was already used in week ${survivorElsewhere}"`
            : ` title="${survivorHere
                ? `Your survivor pick this week — click to release`
                : `Take ${esc(abbr)} as this week's survivor pick`}"`}
          aria-pressed="${survivorHere}">S</button>
        <span class="tscore">${score === null || score === undefined ? "" : score}</span>
      </div>
      ${cell("blind", blindHome, blindLineHome, side)}
      ${cell("ours", ourHome, ourLineHome, side)}
      ${cell("book", bookHome, bookLineHome, side)}`;
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

    /* A projected total is graded against the book's line, not against the
       score. "45.2 when the game went 44" is not a miss by a point and a bit
       -- there is no prize for being close to a total. What it is is a lean:
       over the book's number or under it, and the game settles which was
       right. Without a line to lean against there is nothing to grade, and a
       game landing exactly on it is a push. */
    const totalVerdict = (value) => {
      if (value === null || finalTotal === null) return "";
      if (bookTotal === null || bookTotal === undefined) return "";
      const lean = Number(value) - Number(bookTotal);
      const result = finalTotal - Number(bookTotal);
      if (Math.abs(result) < 0.05) return PUSH;   // landed on the number
      if (Math.abs(lean) < 0.05) return PUSH;     // no lean to be right about
      return grade((lean > 0) === (result > 0));
    };
    const totalCell = (value, label) => {
      const verdict = totalVerdict(value);
      const said = verdict === PUSH ? "Pushed — the game landed on the line"
        : verdict === " hit" ? "Called the right side of the total"
        : verdict === " miss" ? "Called the wrong side of the total" : label;
      return `<div class="gtot${verdict}" title="${esc(said)}">${
        value === null ? "–" : value}</div>`;
    };

    /* Which way the game went, as an arrow rather than a colour.

       Over or under is a fact about the game, not a verdict on anybody's
       number, and it is the same fact for all three cells in the row -- so it
       is said once, on the row's label, instead of tinting three figures that
       each mean something different. It also frees green and red on this row
       to keep meaning what they mean everywhere else on the card. */
    const overUnder = () => {
      if (finalTotal === null || bookTotal === null || bookTotal === undefined) {
        return "";
      }
      const by = finalTotal - Number(bookTotal);
      if (Math.abs(by) < 0.05) {
        return `<span class="ou push" title="Landed on the total — ${
          esc(String(finalTotal))} against a line of ${esc(num(bookTotal, 1))}"
          aria-label="pushed">=</span>`;
      }
      const over = by > 0;
      return `<span class="ou ${over ? "over" : "under"}" title="${
        over ? "Over" : "Under"} — ${esc(String(finalTotal))} against a line of ${
        esc(num(bookTotal, 1))}" aria-label="${over ? "over" : "under"}">${
        over ? "▲" : "▼"}</span>`;
    };

    /* One letter in the corner. E for the favourite winning, U for the
       underdog.

       It was the word, then the word in grey, and on a board of fifteen cards
       it was still the widest thing in the strip -- spelling out "Expected"
       fifteen times to say the unremarkable thing happened. The letter reads
       as a mark rather than as a label, which is what it is; the tooltip
       carries the sentence for anyone who has not met it before.

       Your own result has gone from here entirely. It was per-card, which is
       the wrong grain: what you want from a board is how the week went, and
       that is now counted once at the top of the page instead of fifteen
       times down it. */
    const verdict = gameVerdict(g);
    const verdictMark = !verdict ? "" : (verdict === "expected"
      ? `<span class="gmark expected"
           title="Expected — the book's favourite won">E</span>`
      : `<span class="gmark upset"
           title="Upset — the underdog won">U</span>`);

    return `<article class="gcard" data-game="${esc(g.game_id)}" tabindex="0">
      <div class="gcard-top">
        <span class="gstate">${gameStamp(g)}</span>
        ${movedBadge}
        <span class="gopen" title="Open this game">&rsaquo;</span>
        ${verdictMark}
      </div>
      <div class="gcard-grid">
        <div class="ghead you">You</div>
        <div class="ghead blind" title="Blind model — the projection before it is ever shown the line. The only column here independent of the market.">Blind</div>
        <div class="ghead ours" title="Our blend — that same model blended with the line. This is what the app actually claims.">Blend</div>
        <div class="ghead book" title="Sportsbook consensus, with the vig removed">Book</div>

        ${teamRow("away")}
        ${teamRow("home")}
        <!-- The projected total, as its own row under the two teams rather
             than squeezed into the corner of the top strip. It belongs in the
             grid: each figure then sits under the column it came from, so
             "which of these three is the book's" is answered by position
             instead of by remembering the order in a tooltip. -->
        <div class="gtot-label" title="Projected total points for the game">Total${
          overUnder()}</div>
        ${totalCell(blindTotal, "Blind model's projected total")}
        ${totalCell(ourTotal, "Our blend's projected total")}
        ${totalCell(bookTotal === null || bookTotal === undefined
          ? null : num(bookTotal, 1), "Sportsbook total")}
      </div>
    </article>`;
  };

  /* The board fits the window, and scrolls inside itself when it cannot.

     The page used to scroll, which on a sixteen-game week meant the header,
     the clock and the week you are looking at slid away as you read down the
     slate -- and the one thing you go back up for is the week. The panel
     takes the height it is given and the grid of cards scrolls within it, the
     same contract Picks and Performance are already on. */
  fitsOneScreen(root);
  /* Your own record, for the week on screen and for the season.

     This used to be a Win or Loss chip on every finished card, which is the
     wrong grain twice over: it answered the same question fifteen times down
     one page, and never answered the question actually being asked, which is
     how the week went. Counted once, at the top, where the week is already
     named. A pick on a game still to be played is in neither column -- it is
     shown as still out, so the record does not move at kickoff. */
  const recordChip = (label, r) => {
    if (!r || !(r.won || r.lost || r.tied || r.pending)) return "";
    const decided = r.won + r.lost + r.tied;
    return `<span class="rec-chip" title="Your straight-up picks ${
      label === "Week" ? `in week ${data.week}` : `across ${data.season}`}: ${
      r.won} right, ${r.lost} wrong${r.tied ? `, ${r.tied} tied` : ""}${
      r.pending ? `, ${r.pending} still to play` : ""}">
      <span class="rec-label">${label}</span>
      <b>${decided ? `${r.won}-${r.lost}${r.tied ? `-${r.tied}` : ""}`
        : "—"}</b></span>`;
  };
  /* The Chaos meter: how much of this week went against the book.

     The letters are already in the corner of every finished card -- U for an
     upset, E for the expected result -- and this is nothing more than the
     count of them, which is the question anybody reading a board of fifteen
     cards is adding up in their head anyway. Same letters in the same colours,
     so the chip is its own legend: you learn what U means by hovering the
     thing that counts them.

     Games the market had at a coin flip are in neither column, so the share is
     out of the games that had a favourite rather than out of the slate. A
     normal NFL week lands somewhere near a third. */
  const chaosChip = () => {
    let upsets = 0;
    let expected = 0;
    for (const g of games) {
      const verdict = gameVerdict(g);
      if (verdict === "upset") upsets += 1;
      else if (verdict === "expected") expected += 1;
    }
    const judged = upsets + expected;
    if (!judged) return "";
    const share = Math.round((upsets / judged) * 100);
    const mood = share >= 50 ? "Nothing went to form"
      : share >= 33 ? "A messy week"
        : share > 0 ? "Mostly to form" : "Chalk all the way down";
    const why = `Chaos — how much of week ${data.week} went against the market. `
      + `${upsets} of ${judged} finished games ${upsets === 1 ? "was" : "were"} `
      + `won by the underdog (${share}%), ${expected} by the favourite. ${mood}. `
      + "Every finished card carries the same letter in its corner: U for an "
      + "upset, E for the expected result. A game the book had at a coin flip "
      + "counts as neither.";
    return `<span class="rec-chip chaos" title="${esc(why)}">
      <span class="rec-label">Chaos</span>
      <span class="gmark upset" aria-hidden="true">U</span><b>${upsets}</b>
      <span class="gmark expected" aria-hidden="true">E</span><b>${expected}</b>
      <i class="chaos-share">${share}%</i></span>`;
  };

  const myRecord = data.my_record || {};
  const records = `${recordChip("Week", myRecord.week)}${
    recordChip("Season", myRecord.season)}${chaosChip()}`;

  if (!paint(root, `<div class="panel board">
    <header><h2>${data.season} · Week ${data.week} — the whole slate</h2>
      ${records ? `<div class="rec-strip">${records}</div>` : ""}
      <span class="hint">Home team listed second · Blind = before the line ·
        Blend = what we claim · Book = sportsbook ·
        a tick marks each source's pick</span></header>
    <div class="gboard">${games.map(card).join("")}${byeCards}</div>
    ${anyInherited ? `<p class="note">* These games finished before the app was
      running, so the model has no pick of its own and the sportsbook's number is
      shown in its place.</p>` : ""}
  </div>`)) return;

  wireLogos(root);

  /* `.gcard` also matches the bye box, which is a card in the grid and not a
     game -- it has no `data-game`, so every click on one asked the server for
     the game called "undefined" and got an error dialog back. The bye rows
     have their own handler below; the box itself opens nothing. */
  $$(".gcard[data-game]", root).forEach((node) => {
    const open = () => openGame(node.dataset.game);
    node.addEventListener("click", open);
    node.addEventListener("keydown", (e) => { if (e.key === "Enter") open(); });
  });

  /* A team's own card, from anywhere its name is written: the bye rows, and
     the two names on every game card. The game card still opens the game --
     that is what the rest of the card is for -- so these stop the click
     before it gets there. */
  $$("[data-team-card]", root).forEach((node) => {
    node.addEventListener("click", (event) => {
      event.stopPropagation();
      openTeam(node.dataset.teamCard);
    });
    node.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      event.stopPropagation();
      openTeam(node.dataset.teamCard);
    });
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

  // The survivor pick, same rule: inside a card that opens a dialog, so the
  // click stops here. Clicking the team you already have releases it.
  $$(".sdot", root).forEach((dot) => {
    dot.addEventListener("click", async (event) => {
      event.stopPropagation();
      if (dot.disabled) return;
      const team = dot.dataset.survivor;
      await api("/api/survivor/pick", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          season: state.season, week: state.week,
          team: survivorPick === team ? null : team,
        }),
      });
      await render();
    });
  });
}

// ------------------------------------------------- the assistant, as a page
/* A tab, not a window over the page.

   It was tried as a dialog and the dialog could not render twice. `paint`
   caches the last markup under `state.tab`, which for a window is whatever
   page is *behind* it -- so the second open found its own markup in Home's
   slot, decided the DOM already matched, and left "Loading..." on screen for
   good. A page renders under its own key and cannot collide with anything. */
/* Whether the Assistant is offered at all. Someone who does not want a
   language model in their football app can turn it off, and then the tab is
   not there -- not greyed out, not asking to be set up. */
function paintAssistantButton() {
  const tab = $('.tab[data-tab="assistant"]');
  if (!tab) return;
  const on = state.meta?.settings?.assistant_button !== false;
  tab.hidden = !on;
  if (!on && state.tab === "assistant") setTab("home");
}

// ------------------------------------------------------- the postseason
/* Where the season is heading, and where it ended up.

   Two things in one card, because they are two halves of one question. The
   seeding is what is true now -- fourteen places, taken in order, with the
   league's own letters for what each team has settled -- and the bracket is
   what has happened to it once January starts. Before the postseason exists
   the bracket half is the seeding read as matchups, which is the version
   worth looking at in November.

   Seeded from results rather than from the simulator. The playoff odds beside
   each team come from twenty thousand simulated seasons and answer "how
   likely is this to hold"; the seed itself has to be the one that is actually
   happening. */
async function openBracket() {
  const dlg = $("#detail");
  const body = $(".dialog-body", dlg);
  dlg.classList.add("wide");
  $(".dialog-title", dlg).textContent = `${state.season} playoffs`;
  body.innerHTML = '<div class="empty">Loading…</div>';
  dlg.showModal();

  let d;
  try {
    d = await api(`/api/playoffs?season=${state.season}`);
  } catch (err) {
    body.innerHTML = `<div class="empty">Could not load the bracket — ${
      esc(err.message)}</div>`;
    return;
  }

  const mark = (row) => row.clinch
    ? `<span class="clinch c-${esc(row.clinch)}" title="${
        esc(d.legend[row.clinch] || "")}">${esc(row.clinch)}</span>`
    : '<span class="clinch"></span>';

  /* How a team is in, or that it is not.

     Every row outside the seven used to read "wild card", which is a place in
     the field and not something a team in eleventh has. What it has is a
     chase, so that is what it says; and a team that is mathematically out has
     neither, so it says so and turns red. */
  const howIn = (row) => {
    if (row.clinch === "e") return '<span class="out-flag">Eliminated</span>';
    if (row.seed > 7) return "in the hunt";
    return esc(row.division_winner ? row.division.split(" ")[1] : "wild card");
  };

  /* Seven in, the rest out, and a line between them that is the whole point
     of a standings table in December. */
  const seedRows = (rows) => rows.map((row) => `<tr class="${
    row.seed === 7 ? "cutline " : ""}${row.seed <= 7 ? "in" : "out"}${
    row.clinch === "e" ? " dead" : ""}"
    data-team-card="${esc(row.team)}" tabindex="0"
    title="${esc(row.name || row.team)} — open their season">
    <td class="num seedno">${row.seed}</td>
    <td class="who perf-team">${teamMark(row.team)}<span>${esc(row.team)}</span>${mark(row)}</td>
    <td class="num">${esc(row.record)}</td>
    <td class="muted small">${howIn(row)}</td>
    <td class="num ${row.playoff_prob >= 0.5 ? "hit" : ""}">${
      row.playoff_prob === null || row.playoff_prob === undefined
        ? "–" : pct(row.playoff_prob, 0)}</td>
  </tr>`).join("");

  const conference = (conf) => `<div class="conf-table">
    <h3 class="sub-head">${esc(conf)}</h3>
    <div class="table-scroll"><table class="slate seedtable">
      <thead><tr><th class="num">#</th><th>Team</th><th class="num">Rec</th>
        <th>How</th><th class="num" title="Chance of reaching the playoffs, from 20,000 simulated seasons">Odds</th></tr></thead>
      <tbody>${seedRows(d.conferences[conf] || [])}</tbody>
    </table></div>
  </div>`;

  /* ---------------------------------------------------------- the bracket

     A real one: four rounds left to right, the AFC above and the NFC below,
     meeting at the Super Bowl. It is drawn whether or not January has
     happened -- from the seeding before, from the games once they exist --
     because the shape is the same either way, and a bracket that only appears
     in January is a bracket you cannot use to think about December. A slot
     with no game in it yet shows who the current seeding puts there.

     This replaces a flat list of rounds and a separate "if the season ended
     today" block: the same information drawn twice, in two shapes, neither of
     them a bracket. */
  const played = {};
  for (const round of d.bracket || []) played[round.round] = round.games;

  const findGame = (round, one, two) => (played[round] || []).find(
    (g) => (g.home === one && g.away === two) || (g.home === two && g.away === one));

  const slot = (game, home, away, homeSeed, awaySeed) => {
    const g = game || { home, away, home_seed: homeSeed, away_seed: awaySeed,
                        home_score: null, away_score: null, winner: null };
    const decided = !!g.winner;
    const side = (team, seed, score, won, lost) => `<div class="br-side${
      won ? " won" : ""}${lost ? " lost" : ""}"${team
      ? ` data-team-card="${esc(team)}" tabindex="0" title="${esc(team)}"` : ""}>
      <span class="br-seed">${seed || ""}</span>
      ${team ? teamMark(team) : '<span class="tbadge ghost"></span>'}
      <span class="br-name">${esc(team || "—")}</span>
      <span class="br-score">${score === null || score === undefined ? "" : score}</span>
    </div>`;
    return `<div class="br-game${game ? "" : " projected"}${decided ? " done" : ""}"${
      game ? "" : ' title="Not played yet — this is the pairing the current seeding produces"'}>
      ${side(g.away, g.away_seed, g.away_score, decided && g.winner === g.away,
             decided && g.winner !== g.away)}
      ${side(g.home, g.home_seed, g.home_score, decided && g.winner === g.home,
             decided && g.winner !== g.home)}
    </div>`;
  };

  /* One conference's half of the tree.

     Only what has actually happened. The wild-card round is drawn from the
     seeding because that pairing *is* the seeding -- 2v7, 3v6, 4v5, with the
     top seed idle -- so it is a fact about the table rather than a guess. Every
     round after it is left blank until a game exists to fill it. Projecting
     them meant the bracket asserted a Super Bowl in October, drawn from three
     rounds of assumed results, and a picture that confident about January is
     worse than an empty one: it reads as information and is not.

     `advance` is what turns a played round into the next round's teams, and it
     returns nothing at all until every game in the round it is fed has a
     winner. A half-played round cannot seed the next one. */
  const half = (conf) => {
    const seeds = (d.conferences[conf] || []).filter((r) => r.seed <= 7);
    if (seeds.length < 7) return {};
    const at = (n) => seeds[n - 1];
    const row = (team) => seeds.find((r) => r.team === team) || null;
    const blank = () => slot(null, null, null, null, null);

    const pairs = [[2, 7], [3, 6], [4, 5]];
    const wcGames = pairs.map(([hi, lo]) =>
      findGame("Wild Card", at(hi).team, at(lo).team));
    const wc = pairs.map(([hi, lo], i) =>
      slot(wcGames[i], at(hi).team, at(lo).team, hi, lo)).join("");

    // Winners of a completed round, highest seed first. Null if any game in
    // the round has not been played.
    const advance = (games, fallbackRows) => {
      if (!games.length || games.some((g) => !g || !g.winner)) return null;
      const rows = games.map((g, i) => row(g.winner) || fallbackRows[i]);
      return rows.filter(Boolean).sort((x, y) => x.seed - y.seed);
    };

    const through = advance(wcGames, pairs.map(([hi]) => at(hi)));
    let div = blank() + blank();
    let divGames = [];
    let divPairs = [];
    if (through && through.length === 3) {
      // The top seed takes the lowest survivor; the other two meet.
      divPairs = [[at(1), through[2]], [through[0], through[1]]];
      divGames = divPairs.map(([x, y]) => findGame("Divisional", x.team, y.team));
      div = divPairs.map(([x, y], i) =>
        slot(divGames[i], x.team, y.team, x.seed, y.seed)).join("");
    }

    const divThrough = divPairs.length
      ? advance(divGames, divPairs.map(([x]) => x)) : null;
    let champ = blank();
    let champGame = null;
    if (divThrough && divThrough.length === 2) {
      champGame = findGame("Conference", divThrough[0].team, divThrough[1].team);
      champ = slot(champGame, divThrough[0].team, divThrough[1].team,
                   divThrough[0].seed, divThrough[1].seed);
    }

    const winner = champGame && champGame.winner ? champGame.winner : null;
    return { wc, div, champ, winner, seed: (row(winner) || {}).seed || null };
  };

  const afc = half("AFC");
  const nfc = half("NFC");
  const sbGame = afc.winner && nfc.winner
    ? findGame("Super Bowl", afc.winner, nfc.winner) : null;

  const tree = afc.wc && nfc.wc ? `<div class="bracket-tree">
    <div class="br-col-heads">
      <span>Wild Card</span><span>Divisional</span><span>Conference</span><span>Super Bowl</span>
    </div>
    <div class="br-body">
      <div class="br-conf">
        <span class="br-conf-tag">AFC</span>
        <div class="br-col wc">${afc.wc}</div>
        <div class="br-col">${afc.div}</div>
        <div class="br-col">${afc.champ}</div>
      </div>
      <div class="br-final">${
        afc.winner && nfc.winner
          ? slot(sbGame, afc.winner, nfc.winner, afc.seed, nfc.seed)
          : slot(null, null, null, null, null)}</div>
      <div class="br-conf">
        <span class="br-conf-tag">NFC</span>
        <div class="br-col wc">${nfc.wc}</div>
        <div class="br-col">${nfc.div}</div>
        <div class="br-col">${nfc.champ}</div>
      </div>
    </div>
    ${d.has_postseason ? "" : `<p class="note">Nothing has been played yet, so
      every pairing here is the one the current seeding produces. It fills in
      with real results as January goes on.</p>`}
  </div>` : `<div class="empty">Not enough of the season has been played to
    seed a bracket yet.</div>`;

  body.innerHTML = `<div class="panel bracket-card">
    <div class="bracket-split">
      ${tree}
      <div class="bracket-picture">
        ${conference("AFC")}
        ${conference("NFC")}
        <p class="note legend">${Object.entries(d.legend).map(([k, v]) =>
          `<span class="clinch c-${esc(k)}">${esc(k)}</span> ${esc(v)}`).join(" · ")}</p>
      </div>
    </div>
  </div>`;
  wireLogos(body);
  $$("[data-team-card]", body).forEach((node) => {
    node.addEventListener("click", (ev) => {
      ev.stopPropagation();
      openTeam(node.dataset.teamCard);
    });
  });
}

// ------------------------------------------------ moving between cards
/* Where you have been, inside the dialog.

   One card leads to another -- a game opens a team, a team opens the game it
   is playing, the bracket opens either -- and each of those used to be a dead
   end: the only way out was to close the dialog and find your way back in
   from the board. The trail is a stack, so the arrow behaves the way an arrow
   should, and it is cleared whenever the dialog is opened afresh rather than
   navigated within.

   Entries are `{kind, id}` and are replayed by `showCard`, which is the only
   thing that opens a card; every caller goes through it. */
const cardTrail = [];

function showCard(kind, id, { push = true } = {}) {
  if (push) {
    const top = cardTrail[cardTrail.length - 1];
    if (!top || top.kind !== kind || top.id !== id) cardTrail.push({ kind, id });
  }
  paintDialogNav();
  return kind === "team" ? openTeam(id, { nav: false }) : openGame(id, { nav: false });
}

function goBackCard() {
  cardTrail.pop();                       // the one being looked at
  const previous = cardTrail[cardTrail.length - 1];
  if (!previous) { $("#detail").close(); return; }
  showCard(previous.kind, previous.id, { push: false });
}

function paintDialogNav(switcher) {
  const back = $("#dialog-back");
  if (back) back.hidden = cardTrail.length < 2;
  const box = $("#dialog-switch");
  if (!box) return;
  if (!switcher || !switcher.length) { box.hidden = true; box.innerHTML = ""; return; }
  box.hidden = false;
  box.innerHTML = switcher.map((o) => `<button class="dlg-tab${
    o.on ? " on" : ""}" data-go="${esc(o.kind)}:${esc(o.id)}"${
    o.on ? " disabled" : ""}>${esc(o.label)}</button>`).join("");
}

// --------------------------------------------------- a team, in detail
/* The card behind a team's name, anywhere its name appears.

   This started as the bye card, because a team with no game this week has
   nothing else to show. It turned out to be the better answer for every
   other team as well: the game card says what happens on Sunday, and the
   question a crest actually invites is "are they any good, and where does
   this season end up". So the same card opens for anyone.

   One fetch, one renderer. The bye rows used to carry their own copy of
   these numbers in the board's payload; both now read the endpoint, which is
   local and costs a few milliseconds, and there is one place where the card
   can be wrong. */
async function openTeam(abbr, { nav = true } = {}) {
  if (!abbr) return;
  if (nav) { cardTrail.length = 0; return showCard("team", abbr); }
  const dlg = $("#detail");
  const body = $(".dialog-body", dlg);
  const meta = (state.meta?.teams || {})[abbr] || {};
  $(".dialog-title", dlg).textContent = meta.full_name || abbr;
  body.innerHTML = '<div class="empty">Loading…</div>';
  dlg.showModal();

  let t;
  try {
    t = await api(`/api/team/${encodeURIComponent(abbr)}?season=${
      state.season}&week=${state.week}`);
  } catch (err) {
    // An open dialog reading "Loading…" for ever is the worst of the two
    // failures: the error at least says which team and what went wrong.
    body.innerHTML = `<div class="empty">Could not load ${esc(abbr)} — ${
      esc(err.message)}</div>`;
    return;
  }

  const odds = [
    ["Make the playoffs", t.playoff_prob],
    ["Win the division", t.division_prob],
    ["First-round bye", t.bye_prob],
    ["Win the Super Bowl", t.sb_prob],
  ].filter(([, v]) => v !== null && v !== undefined);

  const range = t.wins_p10 === null || t.wins_p10 === undefined
    ? "" : `${num(t.wins_p10, 0)}–${num(t.wins_p90, 0)} in most seasons`;

  /* What is on, and what is next. A team can have either, both or neither:
     on a bye there is no game this week but there is one coming, and in the
     last week of the season it is the other way round. */
  const fixture = (g, label) => g ? `
    <h3 class="sub-head">${esc(label)}</h3>
    <div class="byenext">
      ${teamMark(g.opponent)}
      <span><b>${g.home ? "vs" : "at"} ${esc(g.opponent)}</b>
        <span class="muted">${g.score
          ? `${esc(g.score)} ${g.status === "final" ? "final" : ""}`
          : (g.kickoff ? esc(when(g.kickoff)) : "")}</span></span>
    </div>` : "";

  /* The switcher: this team, and the game they are in this week. Both cards
     draw the same control, so moving between them is one click either way
     rather than closing and starting again from the board. */
  paintDialogNav(t.this_week && t.this_week.game_id ? [
    { kind: "team", id: t.team, label: t.team, on: true },
    { kind: "game", id: t.this_week.game_id,
      label: `vs ${t.this_week.opponent}` },
  ] : null);

  body.innerHTML = `
    <div class="panel bye-detail">
      <div class="byetop">
        ${teamMark(t.team)}
        <div>
          <div class="byename">${esc(t.full_name || t.team)}</div>
          <div class="muted">${esc(t.division || t.conference || "")}${
            t.record ? ` · ${esc(t.record)}` : ""}${
            t.on_bye ? ' · <span class="byeflag">on bye</span>' : ""}</div>
        </div>
        <div class="byerankbig">${t.rank ? `#${t.rank}` : "–"}
          <span class="sub">power rank</span></div>
      </div>

      <div class="tiles">
        <div class="tile"><div class="label">Projected wins</div>
          <div class="value">${t.exp_wins === null || t.exp_wins === undefined
            ? "–" : num(t.exp_wins, 1)}</div>
          <div class="sub">${esc(range)}</div></div>
        <div class="tile"><div class="label">Power rating</div>
          <div class="value">${t.power === null || t.power === undefined
            ? "–" : signed(t.power, 1)}</div>
          <div class="sub">points better than an average team</div></div>
        <div class="tile" title="Win expectation implied by points scored and allowed. A team well above its record has been unlucky.">
          <div class="label">Pythagorean</div>
          <div class="value">${t.pythagorean === null || t.pythagorean === undefined
            ? "–" : pct(t.pythagorean)}</div>
          <div class="sub">by points, not results</div></div>
        <div class="tile"><div class="label">Conference</div>
          <div class="value">${esc(t.conference || "–")}</div>
          <div class="sub">${esc(t.division || "")}</div></div>
      </div>

      ${odds.length ? `<h3 class="sub-head">How the season ends</h3>
      <table class="slate"><tbody>${odds.map(([label, v]) => `<tr>
        <td class="who">${esc(label)}</td>
        <td class="num"><span class="oddsbar" style="--p:${
          Math.max(0, Math.min(1, v)) * 100}%"></span></td>
        <td class="num">${pct(v, 1)}</td>
      </tr>`).join("")}</tbody></table>` : ""}

      ${fixture(t.this_week, `This week${t.this_week && t.this_week.status === "final"
        ? "" : ""}`)}
      ${fixture(t.next, t.on_bye ? `Back in week ${t.next ? t.next.week : "?"}`
        : `Next: week ${t.next ? t.next.week : "?"}`)}
      ${!t.this_week && !t.next ? '<div class="empty">No more games scheduled.</div>' : ""}

      ${t.on_bye ? `<p class="note">A bye is the one week a team's ranking
        moves without them playing: everyone else's results shift the table
        underneath them. They also cannot be used as a survivor pick this
        week.</p>` : ""}
    </div>`;
  wireLogos(body);
}

// ------------------------------------------------- one game, in detail
/* The dialog behind every card on the board. It outlived the Games page:
   that page was a second rendering of the same week the board already
   shows, but this is the only place a single game explains itself --
   line movement, every book's current number, and the alerts raised for
   it. */
async function openGame(gameId, { nav = true } = {}) {
  if (nav) { cardTrail.length = 0; return showCard("game", gameId); }
  const dlg = $("#detail");
  const body = $(".dialog-body", dlg);
  $(".dialog-title", dlg).textContent = "Loading…";
  body.innerHTML = '<div class="empty">Loading…</div>';
  dlg.showModal();
  /* Anything that goes wrong from here has to end up on screen.

     The dialog is shown before its contents are fetched, which is right --
     a click should do something immediately -- but it means the failure mode
     of everything below is an open dialog reading "Loading…" for ever, with
     the actual error in a console nobody has open. That is precisely what
     happened, and from the outside "clicking a game does nothing" gives no
     clue where to look. An error in the dialog names the game and the
     problem, and the close button still works. */
  try {
    await fillGame(dlg, body, gameId);
  } catch (err) {
    $(".dialog-title", dlg).textContent = "Could not open this game";
    body.innerHTML = `<div class="empty">${esc(err && err.message
      ? err.message : String(err))}</div>`;
  }
}

async function fillGame(dlg, body, gameId) {

  const [d, alertsFor] = await Promise.all([
    api(`/api/game/${encodeURIComponent(gameId)}`),
    // A game with no alerts is the normal case, so a failure here must not
    // cost the dialog everything else it was going to show.
    api(`/api/alerts?game_id=${encodeURIComponent(gameId)}`).catch(() => ({ alerts: [] })),
  ]);

  /* Alerts for this game have now been seen, because they are on the screen.
     Nothing marked them, so every alert this app ever raised stayed unread for
     ever -- which makes the unread count a running total of the season rather
     than a thing to act on.

     After the fetch, and that is the whole of a bug that stopped every game
     opening. This block used to sit above the `const [d, alertsFor] = await`
     below it and read `alertsFor` to decide what to mark. A `const` is not
     hoisted the way a `var` is -- it is in scope from the top of the block but
     unreadable until its own line runs -- so touching it early does not read
     undefined, it throws. The throw landed one line after `showModal()`, which
     is why the dialog opened, said "Loading…", and then stopped: the dialog was
     already on screen and everything that would have filled it was on the far
     side of the exception. */
  api("/api/alerts/seen", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ids: (alertsFor.alerts || []).map((a) => a.id).filter(Boolean) }),
  }).catch(() => { /* a badge that stays lit is not worth an error */ });

  const g = d.game;
  $(".dialog-title", dlg).textContent = `${g.away_name} at ${g.home_name}`;
  // This game, and either team in it, one click apart.
  paintDialogNav([
    { kind: "game", id: gameId, label: "Game", on: true },
    { kind: "team", id: g.away, label: g.away },
    { kind: "team", id: g.home, label: g.home },
  ]);

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
    ${wagerNotice()}

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
          return a.missing.map((m, i) => {
            /* A zero here does not mean "no effect".

               When the replacement starter has been fed to the model as a
               feature, the downgrade is already inside the projection and
               charging it again here would double it -- so the offset is
               zero and the cost column showed a dash, which reads as the
               injury having been ignored. It has not been; it has been
               counted somewhere this table was not saying. */
            const inModel = a.qb_in_model
              && String(m.position || "").toUpperCase() === "QB";
            const cost = m.cost ? `−${num(m.cost, 2)}`
              : inModel ? `<span class="muted" title="The replacement starter is\
 a feature of the projection, so this is already priced into the number rather\
 than added on top of it.">in model</span>`
              : "–";
            return `<tr>
            <td class="team">${i === 0 ? `${esc(team)} <span class="muted">(${signed(a.adjustment)})</span>` : ""}</td>
            <td>${esc(m.player || "–")}</td><td>${esc(m.position || "–")}</td>
            <td>${esc(m.status || "–")}</td><td>${cost}</td></tr>`;
          });
        }).join("")}</tbody>
      </table></div>
      <p class="note">Injury history is not in the training data, so this is applied to the
        projection rather than learned. It mostly removes false disagreement — a model that
        has not noticed a ruled-out starter claims its biggest edge on the game it understands
        least. A quarterback's cost is the measured gap to his backup, with a floor: a
        listed starter being ruled out is never worth less than the league-average cost
        of losing one. Where a row reads "in model", the replacement has been given to
        the projection as a feature and is already priced into the number.</p>
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
    api(`/api/teams?season=${state.season}&week=${state.week}`),
    // Movement against the previous week we actually hold. Its own request
    // because a missing history must cost the ranking nothing.
    /* The same week as the table it annotates. This endpoint has always taken
       a week and the page has never sent one, so the Move column described
       the current week's movement no matter which week was on screen -- two
       halves of one row disagreeing about what they were describing. */
    api(`/api/power/history?season=${state.season}&week=${state.week}`)
      .catch(() => ({ teams: [], compared_to: null, note: "" })),
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
  /* When the week on screen has no cut of its own, the table above is the
     live ranking rather than a frozen one -- so say which it is instead of
     letting a future week borrow today's order and today's arrows. */
  const rankNote = history.note || "";
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

  /* A week with no cut of its own shows nothing, not today's order.

     It used to fall back to the live ranking with a note under it, which put
     the current standings beneath a week-three heading -- and a reader takes
     a table at its heading, not at its footnote. A week that was never ranked
     has no ranking to show, and saying so is the whole answer. */
  const ranked = history.ranked !== false;
  /* One panel now that the win distribution opens on a click, so the page
     fits the window again and the two halves of the ranking scroll inside
     it -- the same way every other page that fits is built. */
  fitsOneScreen(root);
  if (!paint(root, `
  <div class="panel rankings">
    <header><h2>Power rankings</h2>
      <span class="hint">by projected finish, the Pythagorean, the rating and
        title odds — not by record · click a team for the rest</span></header>
    ${ranked ? `<div class="rank-split">
      <div class="table-scroll">${table(data.teams.slice(0, half))}</div>
      <div class="table-scroll">${table(data.teams.slice(half))}</div>
    </div>` : `<div class="empty">${esc(rankNote
      || "This week has not been ranked yet.")}</div>`}
    ${ranked && rankNote ? `<p class="note">${esc(rankNote)}</p>` : ""}
    ${hasWinTotals ? "" : `<p class="note">No season win-total lines are
      available from the odds feed right now, so the market columns in each
      card are blank. Nothing else is affected.</p>`}
  </div>
`)) return;

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

  /* The distribution opens over the page instead of sitting under it.

     It used to be a second panel below the rankings, permanently showing
     whichever team was clicked last -- and on first load, whichever happened
     to be first. That is a chart about one team taking a third of a page
     about thirty-two, answering a question nobody had asked yet. It is the
     answer to clicking a team, so it arrives when a team is clicked. */
  const show = (abbr) => {
    const team = data.teams.find((t) => t.team === abbr);
    if (!team) return;
    const dlg = $("#detail");
    paintDialogNav(null);
    $(".dialog-title", dlg).textContent = `${team.name} — simulated season`;
    $(".dialog-body", dlg).innerHTML = `
      <div class="team-card" id="team-card">
        <div class="card-head">${teamMark(team.team)}<b>${esc(team.name)}</b>
          <span class="muted">#${team.rank} · ${num(team.exp_wins, 1)} expected wins</span>
        </div><div class="facts">${card(team)}</div>
      </div>
      <div class="panel" style="background:var(--surface-sunken);margin-top:14px">
        <header><h2>Simulated win distribution</h2>
          <span class="hint">20,000 runs of the remaining schedule</span></header>
        <div id="team-dist" style="height:220px"></div>
      </div>`;
    if (!dlg.open) dlg.showModal();
    /* The crest here is written long after the page was wired.
       `teamMark` draws the logo at opacity 0 and `wireLogos` reveals it once
       the image reports itself loaded -- so a mark inserted after that pass
       has nobody listening for it and sits invisible for ever, leaving the
       bare abbreviation underneath. Every other late write on this page
       already re-wires; this one did not. */
    wireLogos($("#team-card"));
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
}

// ------------------------------------------------------------------- picks
async function renderPicks(ticket) {
  const root = $("#view");
  /* Neither contest on this page runs in January. A survivor pool is decided
     by the end of the regular season and a pick'em board is the week's
     sixteen games, not a four-game round -- so a playoff week here is a
     question with no answer rather than an empty board. */
  if (state.postWeeks?.has(Number(state.week))) {
    paint(root, `<div class="panel"><div class="empty">
      Picks and survivor are a regular-season contest — a pool is settled by
      week 18 and a pick'em board is a full slate, not a four-game round.
      Choose a week from the regular season, or open the bracket for the
      postseason.
    </div></div>`);
    return;
  }
  const data = await api(`/api/picks?week=${state.week}&season=${state.season}`);
  if (stale(ticket)) return;
  const share = await loadSharing();
  const pickem = data.pickem || {};
  const survivor = data.survivor || {};

  /* Always the expected-points board. The other one, "leverage", deliberately
     gives up expected score to differentiate from a field that picks close to
     the market -- a real strategy, and the right one only in a large pool
     where finishing first is what pays. It was a dropdown on a page whose
     complaint was that there was too much on it, offering a choice almost
     nobody wanted to make, and the answer to "which did I leave this set to"
     was a control you had to go and look at. The API still returns both. */
  const board = pickem.ev || {};

  /* A run that has already lost. The panel used to go on recommending a team
     and quoting a path survival percentage for someone who is out of the
     pool -- numbers that are not wrong so much as no longer addressed to
     anybody. It says so instead, and says how to take it back, because the
     commonest reason to be looking at it is having pressed the wrong S. */
  const out = data.survivor_out || null;

  /* Why this page is empty, when it is.
     Picks are made for the week that is coming, so a week already played and a
     week not yet reached both have nothing on them -- and both were showing
     "— of 0 possible" above "No games to pick", which reads as a broken page
     rather than as a finished one. The week is in the selector; what it means
     should be on the page. */
  const liveWeek = state.meta?.week;
  const nothing = !(board.picks || []).length && !survivor.recommendation;
  const why = !nothing || liveWeek === undefined || state.week === liveWeek ? ""
    : state.week < liveWeek
      ? `Week ${state.week} has been played. There is nothing left to pick —
         how it went is on Home and Performance.`
      : `Week ${state.week} has not come round yet. Picks are made for the
         current week, which is week ${liveWeek}.`;
  /* Two lines to a row rather than four columns across.
     This panel is a third of the page now, and "PHI over WAS  74.2%  +1.8pp
     vs mkt" laid out in one line either wrapped in the middle of a number or
     pushed a horizontal scrollbar under the list. Stacked, the pick and its
     opponent read on the top line and the two numbers sit under them, and the
     row gets narrower instead of longer. */
  const pickRows = (board.picks || []).map((p) => `<div class="pick-row">
    <span class="conf">${p.confidence}</span>
    <span class="pick-body">
      <span class="pick-who"><strong>${esc(p.pick)}</strong>
        <span class="muted">over ${esc(p.opponent)}</span></span>
      <span class="pick-nums">
        <span class="pick-prob">${pct(p.win_prob, 1)}</span>
        ${p.edge === null ? ""
          : `<span class="muted">${signed(p.edge * 100, 1)}pp vs mkt</span>`}
      </span>
      ${p.note ? `<span class="pick-note muted">${esc(p.note)}</span>` : ""}
    </span>
  </div>`).join("");

  /* The run in two tables rather than one.

     `column-count: 2` was the obvious way and does not work: CSS columns
     fragment a block flow, and a table is one unbreakable box, so the whole
     thing sat in the first column and the back half of the season fell off
     the bottom. Splitting the rows and emitting two tables is what actually
     puts week fourteen beside week six. */
  const runRow = (s) => `<tr>
    <td class="team">Week ${s.week}</td><td>${esc(s.team)}</td>
    <td class="muted">vs ${esc(s.opponent)}</td><td>${pct(s.win_prob, 1)}</td></tr>`;
  /* Short column heads. "Opponent" and "Win prob" were each wider than every
     value beneath them, and with two of these tables side by side in half a
     panel the width they took came off the end of the row -- so the labels
     themselves were what got clipped. */
  const runTable = (rows) => rows.length ? `<table class="slate">
      <thead><tr><th>Week</th><th>Team</th><th title="Who they play">Opp</th>
        <th class="num" title="Chance that team wins that week">Win</th></tr></thead>
      <tbody>${rows.map(runRow).join("")}</tbody></table>` : "";
  const runAll = survivor.path || [];
  const runHalf = Math.ceil(runAll.length / 2);
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

  fitsOneScreen(root);
  if (!paint(root, `
  <div class="pick-board">
  <div class="panel">
    <header><h2>Picks</h2>
      <span class="hint" title="Ordered by how sure the blend is, most confident first. The number beside each pick is that confidence.">most confident first</span>
      ${shareIndicator(share)}
    </header>
    ${(board.picks || []).length ? `<div class="tiles pick-tiles">
      <div class="tile"><div class="label">Expected correct</div>
        <div class="value">${num(board.expected_correct, 1)}<span class="sub"> of ${board.n_games ?? 0}</span></div></div>
    </div>` : ""}
    <div class="pickem-list">${pickRows || `<div class="empty">${
      esc(why).replace(/\s+/g, " ") || "No games to pick."}</div>`}</div>
    ${(board.picks || []).length ? wagerNotice() : ""}
  </div>

  <!-- Survivor and its run are one panel in two halves rather than two
       panels: the choice on the left, the sixteen weeks that choice commits
       you to on the right. They were stacked, which gave the run a sliver of
       height and meant reading four weeks at a time of a thing that is only
       useful whole. Side by side the run gets the column's full height, and
       joined they read as one subject, which is what they are. -->
  <div class="panel survivor-pair${out ? " eliminated" : ""}">
  <section class="sv-half survivor-now">
    <header><h2>Survivor</h2>
      ${out ? `<span class="out-flag" title="Your run ended in week ${
        esc(String(out.week))}">Eliminated</span>`
        : `<span class="hint">${survivor.horizon
          ? `planned ${survivor.horizon} weeks ahead` : ""}</span>`}</header>
    ${out ? `<div class="sv-out">
      <p class="sv-out-line">Out in <b>week ${esc(String(out.week))}</b> —
        you had <b>${esc(out.team)}</b>${out.opponent
          ? ` against ${esc(out.opponent)}` : ""}${out.score
          ? `, and it finished ${esc(out.score)}` : ""}.
        ${out.weeks_survived
          ? `You survived ${out.weeks_survived} week${
              out.weeks_survived === 1 ? "" : "s"} before that.`
          : ""}</p>
      <p class="note">Nothing is locked. Change week ${esc(String(out.week))}'s
        S on Home to a team that won and the plan picks up again from here —
        this page is read from your picks, not from a verdict kept somewhere.</p>
    </div>` : ""}
    ${!out && survivor.recommendation ? `
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
        <h3>This week's options<span class="hint"
          title="Win chance this week, then what taking that team instead would cost across the whole remaining path."
          >win chance, then the cost of switching</span></h3>
        <div class="alt-list">${altRows
          || '<div class="empty">No alternatives.</div>'}</div>
      </div>
    ` : out ? "" : `<div class="empty">${esc(survivor.note || "").replace(/\s+/g, " ")
      || esc(why).replace(/\s+/g, " ")
      || "No survivor plan available."}</div>`}
  </section>

  ${!out && survivor.recommendation ? `
  <section class="sv-run-below survivor-run">
    <header><h2>The rest of the run</h2>
      <span class="hint" title="The recommendation is not always this week's safest team. Spending a strong team now can cost more later than it gains today, so the optimiser solves the whole remaining path — which is why the cost of switching, shown beside this week's options, is measured over this run rather than over Sunday.">every week from here</span></header>
    <div class="sv-run-cols">${runTable(runAll.slice(0, runHalf))}${
      runTable(runAll.slice(runHalf))}</div>
  </section>` : ""}
  </div>
  </div>`)) return;

  wireShareIndicator(root);

  /* No crest grid to wire any more. Which teams have been spent is recorded
     by picking them on the board, beside the game, where the week comes with
     the choice; this page reads that rather than offering a second, weekless
     way to say the same thing. */
  wireLogos(root);
}

/* A story's time as a number, so a missing or unparseable date sorts last
   under "newest" rather than jumping to the top as NaN.

   Not named `when`: format.js already exports one, it is imported at the top
   of this file, and it formats a date for display rather than measuring it. */
function storyTime(item) {
  const t = Date.parse(item && item.published_at);
  return Number.isNaN(t) ? 0 : t;
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

  /* Sortable by every field the rows already carry.

     The feed arrives ordered by estimated relevance, which is the right
     default and the wrong one for half the questions people bring to this
     page: "what has just happened", "what moves a line most", "is there
     anything on my team". Those are re-orderings of the same list, not
     different lists, so they are a control rather than three more panels.

     Sorted here rather than refetched: eighty rows are already in the
     browser, and a round trip to reorder a list you are looking at is a
     round trip you can feel. Every comparator falls back to time, so rows
     that tie on the chosen field stay in a stable, sensible order instead of
     shuffling on each render. */
  const NEWS_SORTS = {
    relevance: { label: "Estimated relevance",
                 by: (a, b) => (b.line_impact ?? -1) - (a.line_impact ?? -1)
                               || storyTime(b) - storyTime(a) },
    newest:    { label: "Newest first", by: (a, b) => storyTime(b) - storyTime(a) },
    oldest:    { label: "Oldest first", by: (a, b) => storyTime(a) - storyTime(b) },
    impact:    { label: "Biggest line impact",
                 by: (a, b) => Math.abs(b.line_impact ?? 0) - Math.abs(a.line_impact ?? 0)
                               || storyTime(b) - storyTime(a) },
    category:  { label: "Category",
                 by: (a, b) => String(a.category).localeCompare(String(b.category))
                               || storyTime(b) - storyTime(a) },
    team:      { label: "Team",
                 by: (a, b) => String((a.teams || [])[0] || "~")
                                 .localeCompare(String((b.teams || [])[0] || "~"))
                               || storyTime(b) - storyTime(a) },
    source:    { label: "Source",
                 by: (a, b) => String(a.source).localeCompare(String(b.source))
                               || storyTime(b) - storyTime(a) },
  };
  const sortKey = NEWS_SORTS[state.newsSort] ? state.newsSort : "relevance";
  const chosen = state.newsTeam === undefined ? "__all__" : state.newsTeam;
  const sorted = [...(data.items || [])]
    .filter((n) => chosen === "__all__" || (n.teams || []).includes(chosen))
    .sort(NEWS_SORTS[sortKey].by);

  const items = sorted.map((n) => `<div class="news-item">
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
  /* One picker for the whole page.

     There were two lists on this page and one way to filter them -- the crests
     chose a team for the injury table and the news feed ignored them
     entirely, which is the wrong way round for the question people actually
     bring here: "is there anything I should know about this team before I
     pick them". So the row moves to the top and filters both, and gains a
     league mark at the front that means everything, because "show me all of
     it again" needs to be one click rather than a page reload.

     Counts on each crest are injuries plus stories, since it now filters
     both and a crest that reads 0 while there is news about them would be
     lying about what a click will do. */
  const counts = data.injury_counts || {};
  const newsCounts = {};
  for (const item of data.items || []) {
    for (const t of item.teams || []) newsCounts[t] = (newsCounts[t] || 0) + 1;
  }
  const ALL = "__all__";
  const meta = state.meta?.teams || {};
  /* Alphabetical by the name on the shirt, not by the abbreviation.

     Sorting the keys put Las Vegas between Kansas City and Los Angeles --
     correct for "LV", and wrong for every reader, who is looking for the
     Raiders and finds them three crests after the Chargers. The same goes for
     the 49ers, who sit under S and were landing after Seattle. Nobody scans a
     row of crests by abbreviation; they scan it by team.

     The fallback also sorts now. It was the list of teams with someone
     injured, in whatever order the payload happened to carry, so the moment
     the metadata had not arrived the row silently lost its order entirely --
     which is the version of this a cold start shows. */
  const teamName = (t) => meta[t]?.full_name || meta[t]?.location || t;
  const known = (Object.keys(meta).length ? Object.keys(meta)
    : [...(data.injury_teams || [])])
    .sort((a, b) => teamName(a).localeCompare(teamName(b)));
  if (state.newsTeam === undefined) state.newsTeam = ALL;
  if (state.newsTeam !== ALL && !known.includes(state.newsTeam)) state.newsTeam = ALL;
  const team = state.newsTeam;

  /* No count on the crest.

     It read as an unread badge -- a queue to be cleared -- for a number that
     was the sum of two different things, injuries and stories, neither of
     which it named. A crest showing 5 could be five injuries, five stories, or
     any mix, so clicking it to see the injury table and finding two rows
     looked like rows had gone missing. The count each list actually has is on
     that list, one click away, and it is right there rather than added to
     something else. The tooltip still breaks it down for anyone who wants it
     without clicking. */
  const picker = `<button class="team-pick league${team === ALL ? " on" : ""}"
      data-team="${ALL}" title="Every team — all injuries and all news">
      ${leagueMark()}</button>`
    + known.map((t) => {
      const n = (counts[t] || 0) + (newsCounts[t] || 0);
      return `<button class="team-pick${t === team ? " on" : ""}${
        n ? "" : " empty"}" data-team="${esc(t)}"
        title="${esc(teamName(t))} — ${counts[t] || 0} injured, ${
          newsCounts[t] || 0} ${newsCounts[t] === 1 ? "story" : "stories"}">
        ${teamMark(t)}</button>`;
    }).join("");

  /* Four columns, because four things are being asked: who, what, how long,
     how serious. The position and the last-updated timestamp came out -- the
     first is on the name for anyone who follows the team, and the second is a
     fact about our polling rather than about the player. The source's prose
     comment stays too, but as a tooltip: it is the fallback when the feed did
     not break the injury out into fields, not a column of its own. */
  const shown = (data.injuries || []).filter(
    (i) => team === ALL || i.team === team);
  /* Whose player it is, but only when that is in question.

     League-wide this table was thirty-odd names with nothing saying which
     side any of them played for -- the rows were grouped by team and the
     grouping was invisible, so the list read as one undifferentiated roster
     and looked like it was showing the wrong players rather than all of
     them. Filtered to one team the column would repeat that team thirty
     times, so it appears only in the view that needs it. */
  const byTeam = team === ALL;
  const injuries = shown.map((i) => `<tr${i.detail
      ? ` title="${esc(i.detail)}"` : ""}>
    ${byTeam ? `<td class="inj-team">${teamMark(i.team)}</td>` : ""}
    <td class="team">${esc(i.player)}${i.position
      ? ` <span class="muted">${esc(i.position)}</span>` : ""}</td>
    <td>${esc(i.injury || "–")}</td>
    <td class="muted">${esc(i.how_long || "–")}</td>
    <td class="inj-status"><span class="inj ${esc(statusClass(i.status))}">${esc(i.status || "–")}</span></td>
    </tr>`).join("");

  const who = team === ALL ? "the league" : team;
  /* The crest row is fixed, the two lists take what is left.

     Both of them are long by nature -- a league-wide injury report is thirty
     rows and the feed is eighty stories -- so the page grew to fit them and
     the window scrolled, which on this page takes the team picker off the top
     of the screen. The picker is the control you came here to use. */
  fitsOneScreen(root);
  if (!paint(root, `
    <div class="panel news-filter">
      <div class="team-picker wide">${picker}</div>
    </div>
    <div class="grid-2 news-split">
    <div class="panel">
      <header><h2>Injury report</h2>
        <span class="hint">questionable, doubtful, out, IR and PUP only —
          not the whole roster</span>
        <span class="hint filter-who">${esc(who)} · ${shown.length} listed</span></header>
      ${injuries
        ? `<div class="table-scroll tall"><table class="slate roster">
            <thead><tr>${byTeam ? "<th>Team</th>" : ""}<th>Player</th>
              <th title="What is hurt, when the feed breaks it out. Hover a row for the full note.">Injury</th>
              <th title="How long they have been listed, and when they are expected back">How long</th>
              <th>Status</th></tr></thead>
            <tbody>${injuries}</tbody></table></div>`
        : `<div class="empty">${team === ALL
            ? "No injury report stored yet."
            : `Nobody listed for ${esc(team)} — everyone is available.`}</div>`}
    </div>

    <div class="panel">
      <header><h2>News &amp; changes</h2>
        <span class="hint filter-who">${esc(who)} · ${sorted.length} ${
          sorted.length === 1 ? "story" : "stories"}</span>
        <div class="controls news-sort">
          <label for="news-sort" class="hint">Sort by</label>
          <select id="news-sort">${Object.entries(NEWS_SORTS).map(([k, v]) =>
            `<option value="${k}"${k === sortKey ? " selected" : ""}>${esc(v.label)}</option>`
          ).join("")}</select>
        </div></header>
      <div class="news-feed">${items || `<div class="empty">${team === ALL
        ? "No news stored yet."
        : `Nothing about ${esc(team)} in the stored feed.`}</div>`}</div>
      <p class="note">The points estimate is a coarse prior from position and availability —
        a starting quarterback is worth two to three points, a backup almost nothing. It is a
        triage signal for what to look at, never a substitute for the market's own reaction.</p>
    </div>
  </div>`)) return;

  // The marks are drawn by the same teamMark() the board uses, and that draws
  // the logo at opacity 0 until it has actually loaded -- so without this the
  // badges rendered as bare abbreviations here while working on Home.
  wireLogos(root);

  // And the buttons had no listener at all: the picker was drawn, styled and
  // given a selected state that nothing could ever change.
  $$(".team-pick[data-team]", root).forEach((button) => {
    button.addEventListener("click", () => {
      state.newsTeam = button.dataset.team;
      render();
    });
  });

  $("#news-sort", root)?.addEventListener("change", (ev) => {
    state.newsSort = ev.target.value;
    render();
  });
}

// ------------------------------------------------------------- the desk
/* Support, what is being built, and what has changed -- three columns, one
   page.

   Named "The Desk" because that is what it is: the place you go when the app
   has not answered your question. Splitting it into a help page, a roadmap and
   a changelog would be three pages each too thin to justify a tab, and all
   three answer the same underlying question -- "is this meant to work like
   this, and if not, when will it".

   Static content. A support page that cannot load is a support page that has
   failed at the one moment it exists for. */
const DESK_HELP = [
  {
    q: "The board says “–” where a number should be",
    a: `A dash is always "we do not have this", never zero. The most common
       cause is the betting lines: without an Odds API key the book columns
       stay empty and everything the model produces on its own still works.
       Settings → Data sources takes the key.`,
  },
  {
    q: "What do the colours on a game mean",
    a: `Green is right and red is wrong, once a game is final — on the pick,
       on the spread and on the total alike. Grey is neither: a total that
       landed exactly on the number, a spread that pushed, or a game the model
       called 50/50 and therefore never called at all. The blend column is
       blue because it is a price, not a verdict.`,
  },
  {
    q: "Nothing is updating",
    a: `Fetching is manual by default. Refresh, at the bottom of the sidebar,
       does everything except the betting lines — scores, schedule, injuries,
       news — and it also runs itself once a minute while the app is open. The
       lines have their own button on Settings, because each press spends
       three requests of a monthly allowance. Settings → Model → "Fetch
       automatically" puts the rest of the timers back.`,
  },
  {
    q: "Where do I make my survivor pick",
    a: `On Home. Every team on the board carries an S beside its record —
       press it and that is your pick for the week. The plan on Picks then
       replans the rest of the season around it, because a survivor pick is
       only good if the weeks after it still have teams left.`,
  },
  {
    q: "The power rankings have not moved",
    a: `They are cut once a week and then left alone, the moment the last game
       of the previous week goes final. A ranking that changes three times on a
       Tuesday is a live readout with a week number on it, and nothing can be
       said to have moved against it. There is no week-one ranking at all, so
       the Move column starts in week three.`,
  },
  {
    q: "Where do the playoff odds and the bracket come from",
    a: `The rest of the season is simulated twenty thousand times, game by
       game, from the same projections the board shows. The odds are how often
       a team made it; the seeding follows the league's real tiebreakers. The
       bracket button beside the week shows both — the tree fills in with
       actual scores as rounds are played, and z, y, x and e are the clinch
       marks: the conference's top seed, a division, a place, and eliminated.`,
  },
  {
    q: "Closing line value is empty for some games",
    a: `It needs two numbers: where the line opened and where it closed. An
       opening line exists once, so a game the app first saw on Saturday has a
       closing number and nothing to compare it against. Games priced from
       early in the week onward are the ones that count, and the figure lives
       on Settings under Odds.`,
  },
  {
    q: "The assistant is slow, or will not start",
    a: `It runs entirely on this machine — nothing is sent anywhere — which is
       why it needs a model downloaded first. The Assistant tab sets that up
       in one press. On a laptop the first answer after a cold start is the
       slow one; the model stays warm for an hour after that. The tab can be
       hidden in Settings → Assistant.`,
  },
  {
    q: "Moving to another computer, or keeping a copy",
    a: `Picks, settings and the season's history are one database file, and
       the licence is a small file beside it. Both live in the data folder —
       Settings → Backup writes a dated copy of the database and says exactly
       which folder that is. Copying that whole folder to the same place on
       the new machine brings everything across; copying only the backup
       brings the data but not the licence.`,
  },
];

/* The update log. Written by hand: a changelog generated from commit
   messages is a list of commits, not a list of changes.

   Nothing has shipped yet, so there is exactly one entry and it is the whole
   app. Every version before this one was a build somebody was handed to look
   at, not a release, and listing those as history would invent a past the
   app does not have. It stays v1.0 until there is a v1.1 to write. */
const DESK_CHANGES = [
  {
    when: "v1.0",
    note: "The first release.",
    items: [
      "A projection for every game: margin, win probability and a total, "
        + "from a model trained on play-by-play rather than on results",
      "Book lines and totals beside each projection, with the disagreement "
        + "between them called out",
      "Power rankings cut once a week and then left alone, so movement "
        + "against them means something",
      "A full season simulated twenty thousand times for playoff odds, seeds "
        + "and a win distribution per team",
      "Survivor planned to the end of the season, not one week at a time, "
        + "and scored against the run you actually made",
      "Pick'em for the week with the model's confidence on each side",
      "A playoff bracket that fills in as rounds are played, beside the "
        + "seeding picture and who is still in the hunt",
      "Performance: record against the spread, against the total, and "
        + "closing-line value where an opening line exists",
      "Injuries and news per team, from the league's own feed",
      "An assistant that answers questions about the season and runs "
        + "entirely on this machine",
      "Scores that update while games are being played",
      "Dark and light, fullscreen, and a board that fits one screen",
    ],
  },
];

async function renderSoon(ticket) {
  const root = $("#view");
  if (stale(ticket)) return;
  const help = DESK_HELP.map((h) => `<details class="desk-q">
    <summary>${esc(h.q)}</summary>
    <p>${esc(h.a).replace(/\s+/g, " ")}</p></details>`).join("");
  const changes = DESK_CHANGES.map((c) => `<div class="desk-item">
    <div class="desk-head"><b>${esc(c.when)}</b>
      ${c.note ? `<span class="hint">${esc(c.note)}</span>` : ""}</div>
    <ul class="soon-list">${c.items.map((i) => `<li>${esc(i)}</li>`).join("")}</ul>
  </div>`).join("");

  if (!paint(root, `<div class="grid-2 desk">
    <div class="panel">
      <header><h2>Help</h2><span class="hint">the questions that come up</span></header>
      <!-- The thing Help is for that an FAQ cannot do: reach a person. One
           button rather than two, because "report a bug" and "contact
           support" were the same form with different words on it and the
           choice between them was a decision nobody should have to make
           before typing. Above the questions rather than below them, because
           anyone who has read the list and not found their problem has
           already scrolled past this once. -->
      <div class="help-actions">
        <button class="btn" type="button" data-open-report>Contact support</button>
      </div>
      ${help}
    </div>
    <div class="panel">
      <header><h2>Update logs</h2><span class="hint">newest first</span></header>
      ${changes}
    </div>
  </div>`)) return;
}


async function renderSettings(ticket) {
  const share = await loadSharing(true);
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

  /* The save bar stays put; everything under it scrolls.

     Settings is the one page with no single long list to shrink -- it is six
     stacked blocks that come to more than a window even with every section
     closed, so there is nothing to scroll *inside*. What there is instead is
     a bar with Save on it, which is the one thing that must never scroll out
     of reach while you are editing the boxes above it. */
  fitsOneScreen(root);
  if (!paint(root, `${saveBar("top")}
  <div class="settings-body">
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
        ${s.key === "NFLPICKER_SHARE_PICKS" ? shareControls(share) : ""}
      </div>`).join("")}
    </div>
  </details>`).join("")}
  </div>
  <p class="note">A secret is never sent back to this page, so an empty box
    means "leave it alone", not "clear it"; to remove a key, type a space and
    save.</p>

  <div class="grid-2 tool-row">
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

  <div class="panel tool-panel">
    <header><h2>Refresh everything</h2>
      <span class="hint">scores, schedule, injuries and news — free, and
        already automatic</span>
      <button class="why" type="button" aria-label="About refreshing"
        title="The app fetches all of this on its own once a minute, so this button is for when you do not want to wait for the next one — after fixing a connection, say, or on opening a laptop that has been shut. It does not touch the betting lines: those are metered and have their own button beside LIVE.">?</button>
      <div class="controls" style="margin-left:auto">
        <button class="btn" id="refresh">Refresh now</button>
      </div></header>
    <div class="tool-out"><span id="refresh-result" class="muted"></span></div>
  </div>
  </div>

  <!-- The lines, at the bottom and sized to what they cost.

       This was a small button in the sidebar, which is the wrong weight for
       the one control in the app that spends a metered allowance: three
       requests a press, out of a monthly budget. On the page, at the end,
       with the month's usage beside it, it is a decision you make rather
       than a button you find. -->
  <div class="panel odds-panel">
    <header><h2>Odds</h2>
      <span class="hint">the one feed with a bill attached · three requests
        of the monthly allowance per press</span></header>
    <div class="odds-row">
      <button class="btn primary odds-big" id="refresh-odds"
        title="Fetch the odds now.">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5V2L8 6l4 4V7a5 5 0 1 1-5 5H5a7 7 0 1 0 7-7z"/></svg>
        <span>Update odds</span>
      </button>
      <div class="odds-meta">
        <span id="odds-when" class="muted"></span>
        <!-- The countdown, because "once a day" was something you had to take
             on trust -- which is how it came to be polling all day without
             anyone being able to see that it was. -->
        <span id="odds-next" class="tiny muted"></span>
        <span class="tiny muted">Fetched once a day at 11:30 central. This is
          for when you want today's number before then.</span>
      </div>
    </div>
  </div>

  <!-- Closing-line value lives here rather than on Performance.

       It is the sharpest read in the app and the one that needs the least
       looking at: a season figure you check now and then, not something you
       compare against this week's board. On Performance it took a quarter of
       the page permanently and squeezed the run; here it has room for all of
       it, and Performance gets the space back. -->
  <div class="panel" id="clv-panel">
    <header><h2>Your closing-line value</h2>
      <span class="hint">did the line move toward your picks after you made
        them</span></header>
    <div class="panel-body"><div class="empty">Loading…</div></div>
  </div>
  </div>`)) return;

  // The odds line is written by paintOdds, which runs when state loads --
  // long before this page exists. Called again now that its element does.
  paintOdds();

  /* Fetched after the page is drawn: it is the last thing on it, it is a
     second request, and nothing above should wait on it. */
  api(`/api/scoreboard?season=${state.season}`)
    .then((sb) => {
      const holder = $("#clv-panel", root);
      if (!holder || stale(ticket)) return;
      holder.outerHTML = clvBlock(sb.clv) || "";
    })
    .catch(() => { $("#clv-panel", root)?.remove(); });

  /* A refresh re-renders this whole page, which used to close every section
     the reader had opened -- including the one they were halfway through
     filling in. What is open is remembered for the session instead. */
  wireShareControls(root);
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

async function renderAssistantSetup(status, root = $("#view")) {
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

  if (!paint(root, `<div class="panel">
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
  </div>`)) return;

  if (!offered) return;

  $("#setup-go")?.addEventListener("click", async () => {
    const chosen = $('input[name="setup-model"]:checked')?.value;
    await api("/api/assistant/setup", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: chosen }),
    }).catch(() => null);
    await renderAssistantSetup(status, root);
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
      await renderAssistantSetup(status, root);
    }, 1200);
  }
}

/* The assistant draws wherever it is told to.

   It was a tab, and a question about this week's board is asked while you are
   looking at the board -- leaving the page to ask it, and losing it to come
   back, is the wrong shape for the one feature that is about the rest of the
   app. It opens as a window over whatever you are on instead. The view
   function is unchanged apart from taking its root as an argument, so the
   window and the (still addressable) page are the same code. */
async function renderAssistant(ticket, root = $("#view")) {
  // The status probe starts the managed server if it is not running, which can
  // take twenty seconds. That is the longest await on any tab, and it is why
  // this is the tab that used to land on top of whichever one you switched to.
  const status = await api("/api/assistant/status").catch((e) => ({
    ready: false, message: String(e) }));
  if (stale(ticket)) return;

  if (!status.ready) {
    await renderAssistantSetup(status, root);
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
    <div class="msg-body">${m.role === "assistant"
      ? markdown(m.content) : esc(m.content)}</div></div>`).join("");

  if (!paint(root, `<div class="chat-shell">
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
  </div>`)) return;

  const scroll = () => {
    const log = $("#chat-log");
    if (log) log.scrollTop = log.scrollHeight;
  };

  /* The answer, typed into the page rather than pasted into it.

     Streaming alone did not read as typing. The model emits whole words at a
     time and a fast stretch lands several of them inside one frame, so what
     appeared on screen was a paragraph arriving in four jumps -- quicker than
     before and still not something being written. This puts a metronome
     between the stream and the page: text goes into a buffer as it arrives
     and comes out at a steady rate, a few characters every tick, so the words
     appear letter by letter at about reading speed.

     The rate is a floor, not a limit. When the buffer runs long -- the model
     surged, or the answer came back whole because streaming was not available
     and the blocking path answered instead -- each tick takes a sixth of
     what is left, so a thousand characters drain in well under a second. It
     still reads as typing; it just types faster than anybody can. */
  const TYPE_TICK_MS = 25;
  const TYPE_MIN_CHARS = 3;
  const TYPE_CATCH_UP = 6;

  const typewriter = (node) => {
    let full = "";
    let shown = 0;
    let timer = null;
    let drained = null;
    const stop = () => { if (timer) { clearInterval(timer); timer = null; } };
    const tick = () => {
      if (shown >= full.length) {
        stop();
        if (drained) { const done = drained; drained = null; done(); }
        return;
      }
      shown = Math.min(full.length,
        shown + Math.max(TYPE_MIN_CHARS,
          Math.ceil((full.length - shown) / TYPE_CATCH_UP)));
      // Plain text while it types, markdown once it is whole: a half-written
      // list or a lone backtick renders as neither.
      node.textContent = full.slice(0, shown);
      node.parentElement?.classList.remove("pending");
      scroll();
    };
    const start = () => { if (!timer) timer = setInterval(tick, TYPE_TICK_MS); };
    return {
      show(text) { full = text; start(); },
      /* Let the tail finish rather than snapping to the end. The last few
         words appearing all at once is exactly the jump this is here to
         remove, and at the catch-up rate the wait is a few hundred
         milliseconds at most. */
      finish() {
        if (shown >= full.length) { stop(); return Promise.resolve(); }
        return new Promise((resolve) => { drained = resolve; start(); });
      },
      stop,
    };
  };

  /* What the wait is made of.

     The total wait is generation on this machine and nothing here shortens
     it: three hundred tokens at fifteen a second is twenty seconds whatever
     the page does. What it changes is what those twenty seconds look like.
     The first token lands in about a second and the rest arrives at roughly
     reading speed, so the wait is spent reading rather than watching a
     spinner -- which is the whole of the difference between "slow" and
     "typing".

     Written straight into the bubble rather than through render(): a full
     re-render per token would be hundreds of them, and every one would fight
     the scroll position and rebuild the sidebar. The transcript is reloaded
     once at the end, from the database, so what stays on screen is what was
     actually stored. */
  const send = async (question) => {
    if (!question.trim() || chat.busy) return;
    // Shown immediately, and kept on screen while the model thinks: a 4B model
    // takes seconds, and a question that vanishes into a still page reads as a
    // dropped click.
    chat.messages = [...chat.messages, { role: "user", content: question }];
    chat.busy = true;
    await render();

    const log = $("#chat-log");
    const pending = log && $(".msg.assistant.pending .msg-body", log);
    const typing = pending ? typewriter(pending) : null;
    let text = "";
    let failed = "";
    try {
      const res = await fetch("/api/assistant/ask/stream", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ chat_id: chat.id, question,
                               season: state.season, week: state.week }),
      });
      if (!res.ok || !res.body) throw new Error(`stream → ${res.status}`);

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let stop = false;
      while (!stop) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        // Events are separated by a blank line; the last piece may be half of
        // the next one, so it stays in the buffer.
        const parts = buffer.split("\n\n");
        buffer = parts.pop() || "";
        for (const part of parts) {
          const line = part.split("\n").find((l) => l.startsWith("data:"));
          if (!line) continue;
          let event = {};
          try { event = JSON.parse(line.slice(5).trim()); } catch { continue; }
          if (event.chat_id) chat.id = event.chat_id;
          if (event.error) { failed = event.error; stop = true; break; }
          if (event.delta) {
            text += event.delta;
            typing?.show(text);
          }
          if (event.done) {
            // The whole answer, which is what the blocking fallback sends
            // instead of deltas. Handed to the typewriter rather than pasted
            // in, so that path reads the same as the streaming one.
            text = event.reply || text;
            typing?.show(text);
            stop = true;
            break;
          }
        }
      }
      if (failed) throw new Error(failed);
      // Let the last words finish before the transcript replaces them.
      await typing?.finish();
      // From the database rather than from what is on screen, so a reload
      // shows the same thing this does.
      if (chat.id) {
        chat.messages = (await api(`/api/assistant/chats/${chat.id}`)
          .catch(() => ({ messages: chat.messages }))).messages;
      }
    } catch (err) {
      chat.messages = [...chat.messages,
        { role: "assistant", content: `Could not answer: ${err.message || err}` }];
    } finally {
      typing?.stop();
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
/* Assistant is not in here any more: it is a window, not a page. The hash
   still works -- #assistant opens the window over Home -- so a bookmark or a
   reload of the old address lands somewhere sensible. */
const VIEWS = { home: renderHome, teams: renderTeams,
  picks: renderPicks, news: renderNews,
  performance: renderPerformance,
  settings: renderSettings, soon: renderSoon, assistant: renderAssistant };

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

/* Write a view's HTML, but only when it differs from what is already there.

   The refresh loop calls the current view once a minute. Every view builds its
   whole page as a string and assigns it to innerHTML, which destroys and
   recreates every node underneath -- so even when the fetch came back with
   byte-identical data, the page was rebuilt: text selection lost, charts torn
   down and redrawn, transitions restarted, and the DOM churned for nothing.
   Preserving scroll and focus hid the worst of it; it did not stop it
   happening.

   Comparing the string is the whole trick, and it works because these views
   are pure: the same data produces the same markup, so identical markup means
   identical data and there is nothing to draw.

   The return value matters as much as the write. Every view wires its own
   listeners immediately after assigning innerHTML, on the assumption that the
   nodes are new. If the paint is skipped the nodes are *not* new -- they still
   carry the listeners bound last time -- so wiring them again would leave two
   handlers on every button and one click would fire both. Each view returns
   early on false. That is also why this cannot be a general DOM-diffing
   morph: preserving a node and re-running the wiring beside it is precisely
   the bug, and the only safe rule is that a node either is rebuilt and
   rewired, or is left entirely alone.

   Keyed by tab, because switching pages and coming back should not be fooled
   by the previous page's markup. */
const painted = new Map();

function paint(root, html, key = state.tab) {
  if (painted.get(key) === html && root.childElementCount) return false;
  root.innerHTML = html;
  painted.set(key, html);
  return true;
}

/* Anything that edits the page outside `paint` has to say so, or the next
   identical render will believe the DOM still matches the string it stored
   and decline to put it back. */
function repaintNext(key = state.tab) {
  painted.delete(key);
}

async function render({ keepPlace = false, animate = false } = {}) {
  const ticket = ++renderTicket;
  // Connections show on Settings and the game everywhere else, so this follows
  // the tab rather than the data.
  if (state.meta) sideFoot(state.meta);
  const view = VIEWS[state.tab] || renderHome;
  // Not cleared here. Stripping it before an async view can put it back left
  // the board unconstrained for a frame, which is the flicker this used to
  // cause; it is settled below, once the new page is actually on screen.
  fitRequested = false;
  // The hero reports which season and week are on screen, so it has to follow
  // the selectors rather than only the last state load.
  if (state.meta) renderHero(state.meta);
  const place = keepPlace ? capturePlace() : null;
  try {
    await view(renderTicket);
    if (ticket !== renderTicket) return;
    // Settled once, now that the new page is on screen: whether this view
    // asked to fit, and the column that has to agree with it -- a fitted view
    // takes what is left of one viewport, and there is nothing left of a
    // container sized to its own content.
    const viewEl = $("#view");
    viewEl.classList.toggle("fit-screen", fitRequested);
    if (viewEl.parentElement) {
      viewEl.parentElement.classList.toggle("fits", fitRequested);
    }
    /* The fade belongs to the arriving page, not to the click.

       It used to start in `setTab`, the moment the tab was pressed -- and the
       new page does not exist yet at that moment. The view function is async:
       it fetches, and only then paints. So the animation ran on the *outgoing*
       content and had usually finished by the time the new page appeared,
       which is a fade of the wrong thing followed by a hard cut. Started here,
       when the new markup is in the DOM, it is the new page that moves.

       Removed and re-added with a forced reflow between, because an animation
       on an element that already carries the class does not replay -- which is
       why switching quickly between two tabs animated once and then stopped. */
    if (animate) {
      viewEl.classList.remove("swapping");
      void viewEl.offsetWidth;
      viewEl.classList.add("swapping");
    }
    measureFitSettled();
    markScrollFades($("#view"));
    restorePlace(place);
  } catch (err) {
    if (ticket !== renderTicket) return;
    $("#view").innerHTML = `<div class="panel"><div class="empty">
      Could not load this view: ${esc(err.message)}</div></div>`;
    // This wrote over the page without going through `paint`, so the string
    // `paint` is holding for this tab no longer describes what is on screen.
    // Left alone, the next render would compare the view's markup against
    // that stale string, find them equal, decline to paint -- and the error
    // would stay up for ever with the real page behind it.
    repaintNext();
  }
}

/* Putting the reader back where they were, after a refresh they did not ask
   for.

   Every minute this app fetches, recomputes and rebuilds the whole view from
   its innerHTML. That is a fine way to keep numbers current and a terrible
   way to be read over: the survivor run scrolled back to week two under the
   pointer, a half-read injury list jumped to the top, and whatever was
   focused stopped being focused. A refresh you can feel is a refresh that
   interrupts, and there is nothing on this page urgent enough to be worth
   interrupting for.

   So the automatic path records where everything was, lets the rebuild
   happen, and puts it back in the same frame -- the browser paints once, at
   the end, so none of it is visible. A refresh the reader asked for restores
   nothing: pressing the button is a request for a fresh page, and landing
   back at the top is the right answer to it.

   Keyed by id where there is one and by position among the page's own scroll
   boxes where there is not. Both are stable across a re-render of the same
   view, and that is the only case this runs in -- a tab or week change is not
   a refresh and does not keep its place. */
function scrollBoxes() {
  return $$("*", $("#view")).filter(
    (el) => el.scrollHeight - el.clientHeight > 2);
}

function placeKey(el, index) {
  return el.id ? `#${el.id}` : `${el.className || el.tagName}@${index}`;
}

function capturePlace() {
  const boxes = {};
  scrollBoxes().forEach((box, i) => {
    if (box.scrollTop > 0) boxes[placeKey(box, i)] = box.scrollTop;
  });
  const active = document.activeElement;
  return {
    page: window.scrollY,
    boxes,
    // Only by id: an element matched by position could be a different control
    // after a rebuild, and moving focus somewhere the reader did not put it is
    // worse than dropping it.
    focus: active && active.id && $("#view").contains(active) ? active.id : null,
  };
}

function restorePlace(place) {
  if (!place) return;
  scrollBoxes().forEach((box, i) => {
    const at = place.boxes[placeKey(box, i)];
    if (at) box.scrollTop = at;
  });
  if (place.page) window.scrollTo(0, place.page);
  if (place.focus) {
    const el = document.getElementById(place.focus);
    if (el && el !== document.activeElement) el.focus({ preventScroll: true });
  }
}

/* Whether an automatic refresh should redraw at all.

   Rebuilding the page under an open dialog or under someone typing is not
   something restoring a scroll position can paper over: the dialog is built
   from the row it was opened on, and a half-typed question in the assistant
   box is not in any state the server knows about. The numbers wait a minute;
   the reader does not have to. */
function busyBeingRead() {
  if ($$("dialog[open]").length) return true;
  const active = document.activeElement;
  if (!active) return false;
  const tag = active.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT"
    || active.isContentEditable;
}

/* The header's height, for the wordmark that is centred against it.

   This used to also compute how much of the window the chrome was using, so
   that a page which must fit the screen could size itself with `calc`. That
   sum is gone: `.main` is a flex column one viewport tall and the view takes
   what is left, which is the same answer with nothing to measure and nothing
   to get out of step. What remains is genuinely a measurement -- the sidebar
   cannot know the header's height from CSS alone -- and it is read with the
   floor lifted, so it reports the content's height rather than the floor it
   is about to set.

   Re-run on the next frame as well, because the first read after a render is
   of a page the browser has not finished with: a webfont still swapping lands
   it a pixel or two out. */
let fitSettleQueued = false;

function measureFit() {
  const hero = $(".hero");
  if (!hero) return;
  hero.style.minHeight = "0";
  const natural = Math.round(hero.getBoundingClientRect().height);
  hero.style.minHeight = "";
  document.documentElement.style.setProperty("--hero-h", `${natural}px`);
}

function measureFitSettled() {
  measureFit();
  if (fitSettleQueued) return;
  fitSettleQueued = true;
  requestAnimationFrame(() => {
    fitSettleQueued = false;
    measureFit();
  });
}

/* Every box on the page that fades its bottom edge while more is below.
   Kept in one place because the rule is the same everywhere and the bug was
   that it had been written three times as static CSS. */
const FADE_BOXES = ".table-scroll.tall, .news-feed, .survivor-run .table-scroll, #view.fit-screen > .panel.board > .gboard";

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
/* When the lines were last fetched, and what is left of the allowance. Its own
   line because it is its own decision: the rest of the app refreshes on a
   minute and this does not. */
/* How long until the next scheduled fetch, in the units a person would use.

   Down to the minute rather than the second: this is a wait measured in
   hours, and a seconds counter on it only draws the eye to something that is
   not going to happen for most of a day. */
function untilText(iso) {
  const at = iso ? Date.parse(iso) : NaN;
  if (!Number.isFinite(at)) return "";
  const mins = Math.max(0, Math.round((at - Date.now()) / 60000));
  if (mins < 1) return "any moment now";
  if (mins < 60) return `in ${mins} min`;
  const hours = Math.floor(mins / 60);
  const rest = mins % 60;
  return `in ${hours}h${rest ? ` ${rest}m` : ""}`;
}

function paintOdds() {
  const when = $("#odds-when");
  if (!when) return;
  const usage = state.oddsUsage || state.meta?.odds_usage;
  const at = state.oddsAt
    || (state.meta?.sources || []).find((s) => s.source === "odds")?.ts;
  const parts = [];
  if (at) parts.push(`lines ${ago(at)}`);
  if (usage && usage.budget) {
    parts.push(`${usage.remaining_budget ?? usage.budget - usage.used} left this month`);
  }
  when.textContent = parts.join(" · ") || "not fetched yet";

  const next = $("#odds-next");
  if (next) {
    const iso = state.meta?.scheduler?.next_odds_at;
    const until = untilText(iso);
    next.textContent = until ? `Next automatic fetch ${until}` : "";
    next.title = iso ? new Date(iso).toLocaleString() : "";
  }
}

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
  // The live slate for the sidebar, kept up to date by the same poll that
  // keeps everything else. Cheap: it is the local server, and it is the one
  // request that has to happen whichever page is open.
  api(`/api/games?week=${meta.week}&season=${meta.season}`)
    .then((live) => { state.liveSlate = live.games || []; sideFoot(meta); })
    .catch(() => {});
  if (state.season === null) state.season = meta.season;
  if (state.week === null) state.week = meta.week;
  state.weeks = meta.weeks && meta.weeks.length ? meta.weeks
    : Array.from({ length: 18 }, (_, i) => i + 1);
  // Which selector values are a playoff round, for the pages that have
  // nothing to say about one.
  state.postWeeks = new Set((meta.week_options || [])
    .filter((o) => o.type === "POST").map((o) => Number(o.value)));
  renderStatus(meta);

  /* Named weeks, so January reads as January.

     The selector used to build "Week ${n}" from a list of numbers, which is
     right for eighteen weeks and wrong for the four after them: nobody calls
     the divisional round week twenty. The server sends the options with their
     labels, and the postseason is numbered on from the regular season so one
     value still names one week everywhere it is read. */
  state.weekOptions = (meta.week_options && meta.week_options.length)
    ? meta.week_options
    : state.weeks.map((w) => ({ value: w, label: `Week ${w}`, type: "REG" }));
  const sel = $("#week");
  const wanted = state.weekOptions
    .map((o) => `<option value="${o.value}">${esc(o.label)}</option>`).join("");
  if (sel.dataset.built !== wanted) {
    sel.innerHTML = wanted;
    sel.dataset.built = wanted;
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
  // Which build is running, in the corner. It is the first thing worth knowing
  // when something is reported broken and the first thing nobody can see.
  const build = $("#build-line");
  if (build) {
    build.textContent = meta.build_label || "";
    build.title = meta.build?.source === "release"
      ? "The build you installed" : "Running from a source checkout";
  }
  // The version, in the corner where people look for it. Trailing zeros are
  // dropped so 1.0.0 reads "v1.0" -- the number anyone would actually say.
  const version = $("#app-version");
  if (version && meta.version) {
    const parts = String(meta.version).split(".");
    while (parts.length > 2 && parts[parts.length - 1] === "0") parts.pop();
    version.textContent = `v${parts.join(".")}`;
    version.title = meta.build_label || "";
  }
  paintOdds();
  paintAssistantButton();
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
  // Same reason as the error path above, and this one fires constantly: go to
  // Teams, come back to Home, and Home's markup is byte-identical to the last
  // time you were on it -- so without this the paint is skipped and "Loading…"
  // is the page. Every tab you revisited would have been a dead end.
  repaintNext(tab);
  render({ animate: true });
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
  paintTitlebar(next);
  render();
}

/* The one strip of the window CSS cannot reach.

   On Windows the frame with the close button is drawn by the system, and
   drawn light unless the window asks otherwise -- so a dark app had a white
   bar across the top of it. The host can ask; the page cannot, so it goes
   through the bridge. In a browser tab there is no bridge and nothing to
   paint, which is correct: the tab's chrome is the browser's business. */
function paintTitlebar(theme) {
  const api = window.pywebview?.api?.set_titlebar_theme;
  if (!api) return;
  const dark = theme === "dark"
    || (!theme && matchMedia("(prefers-color-scheme: dark)").matches);
  try { api(dark); } catch { /* an older host without it */ }
}

function initTheme() {
  // Dark unless told otherwise. A stored choice still wins in both directions,
  // so someone who picked light keeps light; only the unset case changes.
  let saved = null;
  try { saved = localStorage.getItem("theedge-theme") || localStorage.getItem("nflpicker-theme"); }
  catch { /* private window, blocked storage */ }
  document.documentElement.setAttribute("data-theme", saved || "dark");
  // The bridge is not up yet on the first frame, so this is also done once it
  // announces itself.
  paintTitlebar(saved || "dark");
  addEventListener("pywebviewready", () => paintTitlebar(
    document.documentElement.getAttribute("data-theme")), { once: true });
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
  // Before anything that can block. The activation gate carries a Report a
  // bug button, and a button wired after the gate appears is a button that
  // does nothing for exactly the people stuck behind it.
  wireReport();
  initRouting();
  // Open on the page the address names, so a reload or a saved link lands
  // where it says it will.
  const initial = location.hash.slice(1);
  if (initial && VIEWS[initial]) state.tab = initial;
  /* Pressing the tab you are already on does nothing.
     It used to tear the page down to "Loading…" and fetch the whole view
     again -- so the one gesture that means "I am staying here" was the most
     disruptive thing on the bar. Boot and the Next-up card still call setTab
     directly, because those genuinely need the render: one is the first
     paint, the other has just changed the week under it. */
  $$(".tab").forEach((b) => b.addEventListener("click", () => {
    if (b.dataset.tab === state.tab) return;
    setTab(b.dataset.tab);
  }));
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
  $("#dialog-back")?.addEventListener("click", () => goBackCard());
  $("#dialog-switch")?.addEventListener("click", (ev) => {
    const btn = ev.target.closest("[data-go]");
    if (!btn) return;
    const [kind, ...rest] = btn.dataset.go.split(":");
    showCard(kind, rest.join(":"));
  });
  // A dialog that has been closed is not somewhere you can go back to.
  $("#detail")?.addEventListener("close", () => {
    cardTrail.length = 0;
    paintDialogNav(null);
  });
  $("#bracket")?.addEventListener("click", () => openBracket());
  /* Clicking away closes it.

     A modal <dialog> already closes on Escape, and the X was the only other
     way out -- which is not where anyone's hand is. Clicking off the thing
     you opened is how every other overlay on a computer behaves, and going
     looking for a close button to dismiss a card you opened by accident is a
     small irritation that happens every single time.

     The backdrop is not an element, so there is nothing to put a listener on:
     a click on it is reported against the dialog itself. Comparing the
     pointer to the dialog's own box is what separates the two. Measured
     rather than compared by target, because the dialog's padding belongs to
     the dialog and a click there is inside it. */
  $("#detail").addEventListener("mousedown", (ev) => {
    const dlg = ev.currentTarget;
    if (ev.target !== dlg) return;          // landed on the content
    const box = dlg.getBoundingClientRect();
    const outside = ev.clientX < box.left || ev.clientX > box.right
      || ev.clientY < box.top || ev.clientY > box.bottom;
    if (outside) dlg.close();
  });

  /* The general refresh, on two buttons that mean the same thing: the one in
     the sidebar, always there, and the one on the Settings page beside the
     result line it writes to. Delegated from the document because the
     Settings one is rebuilt on every render of that page -- there is nothing
     to re-bind and nothing to leak.

     "Everything except the odds" is not a special case here: `full=1` already
     excludes the metered stages, so the free half of the app is exactly what
     this fetches. The lines have their own button, one panel away. */
  document.addEventListener("click", async (ev) => {
    const refreshBtn = ev.target.closest("#refresh, #side-refresh");
    if (!refreshBtn || state.busy) return;
    state.busy = true;
    refreshBtn.disabled = true;
    const label = $("span", refreshBtn) || refreshBtn;
    const labelText = label.textContent;
    label.textContent = "Refreshing…";
    refreshBtn.classList.add("spinning");
    try {
      // full=1: the button means "do it now", not "do whatever is due". A
      // stage inside its own polling interval is exactly the stage a person
      // pressing refresh wants fetched again.
      const result = await api("/api/refresh?full=1", { method: "POST" });
      await loadState();
      await render();
      /* What actually happened, rather than "it returned 200".

         Every stage reports its own ok, and a refresh where the schedule feed
         was down or the news feed timed out used to finish green and silent:
         the board then sat on yesterday's data with nothing anywhere saying
         why. A stage that failed is the single most useful thing this button
         can tell anyone, so it is named. */
      const stages = (result && result.stages) || {};
      const failed = Object.entries(stages)
        .filter(([, v]) => v && v.ok === false).map(([k]) => k);
      sayRefresh(failed.length
        ? `Refreshed, but ${failed.join(", ")} failed.`
        : "Up to date.", failed.length ? "warn" : "pos");
    } catch (err) {
      sayRefresh(`Refresh failed: ${err.message}`, "neg");
    } finally {
      state.busy = false;
      // render() has rebuilt the Settings page, so that one is a different
      // button by now; the sidebar's is the same element throughout.
      const live = $(refreshBtn.id === "refresh" ? "#refresh" : "#side-refresh");
      if (live) {
        live.disabled = false;
        live.classList.remove("spinning");
        const span = $("span", live) || live;
        span.textContent = labelText;
      }
    }
  });

  /* The lines, on their own button. Everything else in this app is free to
     fetch; this one spends three requests of a monthly allowance every time,
     so it is asked for rather than included. */
  document.addEventListener("click", async (ev) => {
    const oddsBtn = ev.target.closest("#refresh-odds");
    if (!oddsBtn || state.busy) return;
    state.busy = true;
    oddsBtn.disabled = true;
    oddsBtn.classList.add("spinning");
    try {
      const out = await api("/api/refresh/odds", { method: "POST" });
      state.oddsAt = Date.now();
      if (out.usage) state.oddsUsage = out.usage;
      await loadState();
      await render();
    } catch (err) {
      alert(`Could not update the odds: ${err.message}`);
    } finally {
      state.busy = false;
      // render() has rebuilt Settings, so this is a different button by now.
      const live = $("#refresh-odds");
      if (live) { live.disabled = false; live.classList.remove("spinning"); }
      paintOdds();
    }
  });
  paintOdds();

  /* The update banner: one line above the page when a newer build has been
     published, with a "Later" that remembers which build it was about. */

  // The clock is the one thing on the page that must not wait for a refresh --
  // including the one in the logo, which is why it ticks whether or not there
  // is any state to render around it.
  // A narrower window rewraps rows, which changes whether a box still has
  // anything below the fold.
  addEventListener("resize", () => { measureFitSettled(); markScrollFades(); },
                   { passive: true });
  // The webfont is the slow half of "the page has settled".
  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(() => measureFitSettled()).catch(() => {});
  }

  setBrandClock(new Date());
  setInterval(() => {
    setBrandClock(new Date());
    if (state.meta) {
      renderHero(state.meta);
      // The sidebar counts down to kickoff, so it has to be redrawn on the
      // minute rather than only when something is fetched.
      sideFoot(state.meta);
    }
  }, 30000);

  await ensureLicensed();
  /* After the gate and before the first pick can be made. On a new install
     that is the first launch after activation; on an upgrade it is the first
     launch after this build, because the version the user acknowledged is
     recorded and this one is higher. Not awaited: the app carries on behind
     it, and nothing is collected until it has been answered anyway. */
  showShareNotice();
  await loadState();
  setTab(state.tab, { fromHash: true });
  checkForUpdate();
  // Every few hours for a window left open all week; the server caches it.
  setInterval(() => { checkForUpdate(); paintLicense(); }, 3 * 3600 * 1000);

  /* Everything that is due, once a minute.
     This is a real fetch rather than a poll for someone else's work: the
     scheduler is off by default, so if this tab does not ask, nothing does.
     It can run this often precisely because the metered feed is not in it --
     scores, the schedule and the news are free, and a minute is about how long
     a score is worth being wrong for.

     Due, not everything. It used to send full=1, which is the flag that means
     "run every stage regardless of its own interval" -- and that is the whole
     reason a refresh was something you noticed. Once a minute the app was
     rebuilding the model's predictions and replaying the season twenty
     thousand times, which takes the better part of half a minute; the page
     then redrew off the back of it. A scoreboard that changes every few
     seconds and a season simulation that changes when a game ends were being
     fetched on the same clock, at the speed of the faster one.

     Each stage already carries an interval saying how often it is worth
     redoing. Left to them, the minute tick costs a scoreboard request and
     nothing else on most minutes. full=1 belongs to the button, which is the
     one place someone has actually asked for all of it.

     The scores are no longer on this tick at all. They have their own, far
     shorter one -- see startLiveTicker -- because a score is stale in
     seconds and everything this tick brings in is settled until a game ends.
     What is left here is the slow half: the lines, the injury report, the
     headlines and the pass that turns them into numbers. */
  startLiveTicker();
  setInterval(async () => {
    if (state.busy || document.hidden || busyBeingRead()) return;
    const before = state.meta?.last_recompute;
    try {
      await api("/api/refresh", { method: "POST" });
      await loadState();
      // keepPlace: this refresh is the app's idea, not the reader's, so it
      // has no business moving anything they were looking at.
      if (state.meta?.last_recompute !== before) await render({ keepPlace: true });
    } catch { /* transient: the next tick retries */ }
  }, 60000);
}

main();
