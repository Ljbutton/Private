// The Edge license server: a Cloudflare Worker between the desktop app and Whop.
//
// Why it exists: checking a Whop license key needs your Whop *company* API key.
// That key can read every customer's membership and email, so it can never ship
// inside the installer -- anyone could pull it out of the binary. The app talks
// to this Worker; only this Worker holds the key.
//
// Endpoints
//   POST /v1/support    {description, doing, email, details} -> {ok, ref}
//   POST /v1/validate   {license_key, machine_id}  -> {valid, status, reason, message}
//   GET  /v1/latest                                 -> {commit, built_at, notes, download_url}
//   GET  /v1/download?key=...&asset=...             -> 302 to the installer (valid keys only)
//   GET  /                                          -> "ok" (health check)
//
// Secrets / variables (Cloudflare dashboard -> Worker -> Settings -> Variables):
//   WHOP_API_KEY      secret. Whop company API key with member:basic:read,
//                     member:email:read and member:manage (the last one is needed
//                     to remember which computers a key is used on).
//   WHOP_PRODUCT_ID   optional. prod_... -- keys from any other product are refused.
//   MAX_MACHINES      optional, default 2. Computers one subscription may run on.
//   ALLOWED_STATUSES  optional, default "active,trialing,canceling,completed".
//                     `completed` is there because a one-time purchase -- the
//                     season pass -- is reported by Whop as completed once it
//                     has been paid. It means paid in full, not lapsed.
//   GITHUB_REPO       optional, e.g. Ljbutton/Private -- where releases are published.
//   GITHUB_TOKEN      secret, optional. Needed once that repository is private.
//   RELEASE_TAG       optional, default "latest".
//   DOWNLOAD_PAGE     optional. Where "Download update" sends people if the
//                     direct download is not set up (e.g. your Whop product page).
//   RESEND_API_KEY    secret. Resend API key, used to send the one email a bug
//                     report becomes. Never returned in a response.
//   SUPPORT_EMAIL_TO  secret. Where bug reports are emailed. A secret rather
//                     than a constant because this repository is public and an
//                     address in it is an address that gets scraped -- and
//                     because the app must not be able to reveal where reports
//                     go even to someone who unpacks the binary.
//   SUPPORT_RL        optional KV namespace binding, used to rate-limit reports
//                     by IP. See supportRoute for why KV and not something
//                     cleverer. Without it the endpoint still works and simply
//                     does not rate-limit.

const WHOP = "https://api.whop.com/api/v1";

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    try {
      if (request.method === "OPTIONS") return cors(new Response(null, { status: 204 }));
      if (url.pathname === "/" ) return cors(new Response("ok"));
      if (url.pathname === "/v1/support" && request.method === "POST") {
        return await supportRoute(request, env);
      }
      if (url.pathname === "/v1/validate" && request.method === "POST") {
        return json(await validate(await request.json().catch(() => ({})), env));
      }
      if (url.pathname === "/v1/latest" && request.method === "GET") {
        return json(await latest(env, url));
      }
      if (url.pathname === "/v1/download" && request.method === "GET") {
        return await download(url, env);
      }
      return json({ error: "not found" }, 404);
    } catch (err) {
      return json({ error: "server error", detail: String(err && err.message || err) }, 500);
    }
  },
};

// ------------------------------------------------------------------- support

// A bug report, turned into one email.
//
// The recipient is a secret on this Worker rather than a constant in the app
// or in this file, for three reasons: this repository is public, an address in
// public gets scraped, and the app should not be able to reveal where reports
// go even to somebody who unpacks the binary. The app posts here and does not
// know the destination.
//
// No licence key is required. "My key will not activate" is the likeliest
// report this will ever receive and the people making it cannot get past the
// gate, so requiring one would exclude exactly the reports worth having. What
// stands in for it is the size cap and the rate limit below.

// The whole payload, headers and all. A report is a description, a sentence of
// context and a hundred lines of log; thirty-two kilobytes is generous for
// that and small enough that this cannot be used to push anything through.
export const SUPPORT_MAX = 32_000;

// Reports per IP per hour.
export const SUPPORT_RATE = 5;

// Why KV. Durable Objects would give an exact counter and are the usual answer
// to rate limiting, but the free plan is the constraint here and KV is what it
// includes. The cost of KV being eventually consistent is that somebody on two
// networks at once might get a couple of extra reports through, which is not
// the failure mode worth engineering against -- the point is to stop a script,
// not to be exact about a human. One key per IP per hour, expiring on its own,
// so nothing accumulates and nothing has to be cleaned up. With no namespace
// bound the endpoint still works and simply does not limit.
async function overRateLimit(env, ip) {
  if (!env.SUPPORT_RL || !ip) return false;
  const bucket = `rl:${ip}:${Math.floor(Date.now() / 3_600_000)}`;
  try {
    const seen = Number(await env.SUPPORT_RL.get(bucket)) || 0;
    if (seen >= SUPPORT_RATE) return true;
    // Two hours, so a bucket written at :59 is not read back as missing.
    await env.SUPPORT_RL.put(bucket, String(seen + 1), { expirationTtl: 7200 });
  } catch (err) {
    // A rate limiter that is down must not take reporting down with it.
    console.log("support: rate limit unavailable", String(err && err.message || err));
    return false;
  }
  return false;
}

async function supportRoute(request, env) {
  // Read the body with the cap applied first, so an oversize one is refused
  // rather than parsed.
  const raw = await request.text().catch(() => "");
  if (raw.length > SUPPORT_MAX) {
    return json({ ok: false, reason: "too_large",
                  message: "That report is too big to send." }, 400);
  }
  let body = {};
  try { body = JSON.parse(raw || "{}"); } catch { body = {}; }

  const description = String(body.description || "").trim();
  if (!description) {
    return json({ ok: false, reason: "empty",
                  message: "A description is required." }, 400);
  }

  const ip = request.headers.get("cf-connecting-ip") || "";
  if (await overRateLimit(env, ip)) {
    return json({ ok: false, reason: "rate_limited",
                  message: "A few reports have come from here in the last "
                           + "hour. Try again later." }, 429);
  }

  const result = await support(body, env);
  // A send that failed is a bad gateway, not a successful request reporting
  // failure in its body: the app retries and the caller's own logs should
  // show it. Nothing about why leaves here -- see support().
  const status = result.ok ? 200 : 502;
  return json(result, status);
}

export async function support(body, env) {
  const description = String(body.description || "").trim();
  const doing = String(body.doing || "").trim();
  const email = String(body.email || "").trim();
  const details = (body.details && typeof body.details === "object") ? body.details : {};

  // Six characters of base32-ish: enough to quote back in a reply and match
  // against an inbox, short enough to read out.
  const ref = Math.random().toString(36).slice(2, 8);

  if (!env.RESEND_API_KEY || !env.SUPPORT_EMAIL_TO) {
    // Never say which of the two is missing, and never name the recipient.
    console.log("support: not configured");
    return { ok: false, reason: "not_configured",
             message: "Reporting is not set up on the server yet." };
  }

  const hint = String(details.key_hint || "").trim();
  const version = details.version || {};
  const environment = details.environment || {};
  const subject = `[The Edge bug] ${description.slice(0, 60).replace(/\s+/g, " ")}`
    + ` — ${hint || "no key"}`;

  const lines = [
    description,
    "",
    doing ? `What they were doing:\n${doing}` : "What they were doing: (not given)",
    "",
    `Reference: ${ref}`,
    `Reply to: ${email || "(no address given)"}`,
    "",
    "--- included by the reporter ---",
    `Included: ${(details.included || []).join(", ") || "(nothing)"}`,
  ];
  if (version.version) {
    lines.push(`Version: ${version.version}`
      + (version.commit ? ` (${version.commit})` : "")
      + (version.built_at ? ` built ${version.built_at}` : ""));
  }
  if (environment.os) lines.push(`OS: ${environment.os}`);
  if (environment.os_detail) lines.push(`OS detail: ${environment.os_detail}`);
  if (hint) lines.push(`Licence key hint: ${hint}`);
  if (details.log) lines.push("", "--- last lines of the log ---", String(details.log));

  const message = {
    from: "The Edge Support <onboarding@resend.dev>",
    to: [env.SUPPORT_EMAIL_TO],
    subject,
    text: lines.join("\n"),
  };
  // Resend's REST field is reply_to. Only set when an address was given: an
  // empty one is rejected by the API.
  if (/^[^@\s]+@[^@\s.]+\.[^@\s]+$/.test(email)) message.reply_to = [email];

  let response;
  try {
    response = await fetch("https://api.resend.com/emails", {
      method: "POST",
      headers: {
        authorization: `Bearer ${env.RESEND_API_KEY}`,
        "content-type": "application/json",
      },
      body: JSON.stringify(message),
    });
  } catch (err) {
    // Log the reason, return none of it: an upstream error can quote back the
    // request, and the request carries the key in a header.
    console.log("support: send failed", String(err && err.message || err));
    return { ok: false, reason: "upstream",
             message: "The report could not be delivered just now." };
  }
  if (!response.ok) {
    console.log("support: resend rejected", response.status);
    return { ok: false, reason: "upstream", status: response.status,
             message: "The report could not be delivered just now." };
  }
  return { ok: true, ref };
}

// ------------------------------------------------------------------ validate

export async function validate(body, env) {
  const key = String(body.license_key || "").trim();
  const machine = String(body.machine_id || "").trim().slice(0, 64);
  if (!key) return deny("missing_key", "Enter the license key from your Whop purchase.");
  if (!/^[A-Za-z0-9_-]{4,100}$/.test(key)) {
    return deny("not_found", "That doesn't look like a license key. Copy it from your Whop purchase.");
  }

  const res = await whop(env, `/memberships/${encodeURIComponent(key)}`);
  if (res.status === 404) {
    return deny("not_found", "That license key wasn't found. Check it matches the one on Whop.");
  }
  if (!res.ok) {
    // Our problem, not the customer's: the app treats this like being offline
    // and keeps working on its last good check.
    return { valid: null, reason: "upstream", message: `Whop answered ${res.status}.` };
  }
  const m = await res.json();

  if (env.WHOP_PRODUCT_ID && m.product && m.product.id && m.product.id !== env.WHOP_PRODUCT_ID) {
    return deny("wrong_product", "That key is for a different product.");
  }

  // `completed` is an entitlement, not an ending: Whop reports a one-time
  // purchase that way once it is paid, which is what the season pass is. A
  // subscription that genuinely lapsed comes back as canceled or expired.
  const allowed = String(env.ALLOWED_STATUSES || "active,trialing,canceling,completed")
    .split(",").map((s) => s.trim()).filter(Boolean);
  if (!allowed.includes(m.status)) {
    return deny("inactive", statusMessage(m.status), m.status);
  }

  // Remember which computers this key runs on. The first MAX_MACHINES are
  // accepted; after that the customer is told to free one up.
  if (machine) {
    const max = Math.max(1, Number(env.MAX_MACHINES || 2));
    const meta = m.metadata && typeof m.metadata === "object" ? { ...m.metadata } : {};
    const machines = String(meta.edge_machines || "").split(",").filter(Boolean);
    if (!machines.includes(machine)) {
      if (machines.length >= max) {
        return deny("too_many_machines",
          `This key is already in use on ${max} computer${max > 1 ? "s" : ""}. ` +
          "Contact support to move it to this one.", m.status);
      }
      machines.push(machine);
      meta.edge_machines = machines.join(",");
      const upd = await whop(env, `/memberships/${encodeURIComponent(m.id)}`, {
        method: "PATCH", body: JSON.stringify({ metadata: meta }),
      });
      if (!upd.ok) {
        return { valid: null, reason: "upstream", message: `Could not register this computer (${upd.status}).` };
      }
    }
  }

  return {
    valid: true,
    status: m.status,
    reason: "ok",
    message: m.status === "canceling"
      ? "Your subscription is set to end at the close of this billing period."
      : "",
    // Never the email or anything else personal: the app only needs a yes.
    renews_at: m.renewal_period_end || null,
  };
}

function deny(reason, message, status = null) {
  return { valid: false, reason, message, status };
}

function statusMessage(status) {
  switch (status) {
    case "past_due": return "Your last payment didn't go through. Update your card on Whop to keep using The Edge.";
    case "canceled":
    case "expired": return "This subscription has ended. Resubscribe on Whop to keep using The Edge.";
    default: return `This subscription isn't active (${status}).`;
  }
}

function whop(env, path, init = {}) {
  return fetch(WHOP + path, {
    ...init,
    headers: {
      Authorization: `Bearer ${env.WHOP_API_KEY}`,
      "Content-Type": "application/json",
      Accept: "application/json",
    },
  });
}

// ------------------------------------------------------------ latest version

async function releaseInfo(env) {
  if (!env.GITHUB_REPO) return null;
  const tag = env.RELEASE_TAG || "latest";
  const res = await fetch(
    `https://api.github.com/repos/${env.GITHUB_REPO}/releases/tags/${tag}`,
    { headers: gh(env), cf: { cacheTtl: 300 } });
  if (!res.ok) return null;
  return res.json();
}

export async function latest(env, url) {
  const release = await releaseInfo(env);
  if (!release) return { commit: null, built_at: null, notes: "", download_url: env.DOWNLOAD_PAGE || null };
  // The build workflow uploads version.json beside the installers; it names
  // the exact commit and build time the installers were made from.
  let version = {};
  const asset = (release.assets || []).find((a) => a.name === "version.json");
  if (asset) {
    const res = await fetch(asset.url, { headers: { ...gh(env), Accept: "application/octet-stream" } });
    if (res.ok) version = await res.json().catch(() => ({}));
  }
  return {
    commit: version.commit || null,
    built_at: version.built_at || null,
    notes: version.notes || "",
    // The app appends ?key=... and the file name for its platform.
    download_url: `${url.origin}/v1/download`,
    download_page: env.DOWNLOAD_PAGE || null,
  };
}

async function download(url, env) {
  const key = url.searchParams.get("key") || "";
  const check = await validate({ license_key: key }, env);
  if (check.valid !== true) {
    return new Response(check.message || "License not valid.", { status: 403 });
  }
  const want = url.searchParams.get("asset") || "TheEdge-windows-setup.exe";
  const release = await releaseInfo(env);
  const asset = release && (release.assets || []).find((a) => a.name === want);
  if (!asset) {
    if (env.DOWNLOAD_PAGE) return Response.redirect(env.DOWNLOAD_PAGE, 302);
    return new Response("No download available.", { status: 404 });
  }
  // GitHub answers an asset request with a short-lived signed link; hand that
  // to the customer so the file never passes through the Worker.
  const res = await fetch(asset.url, {
    headers: { ...gh(env), Accept: "application/octet-stream" }, redirect: "manual",
  });
  const loc = res.headers.get("Location");
  if (loc) return Response.redirect(loc, 302);
  return new Response(res.body, {
    status: res.status,
    headers: { "Content-Disposition": `attachment; filename="${want}"` },
  });
}

function gh(env) {
  const h = { "User-Agent": "the-edge-license-server", Accept: "application/vnd.github+json" };
  if (env.GITHUB_TOKEN) h.Authorization = `Bearer ${env.GITHUB_TOKEN}`;
  return h;
}

// ------------------------------------------------------------------- helpers

function json(data, status = 200) {
  return cors(new Response(JSON.stringify(data), {
    status, headers: { "Content-Type": "application/json" },
  }));
}

function cors(res) {
  res.headers.set("Access-Control-Allow-Origin", "*");
  res.headers.set("Access-Control-Allow-Headers", "Content-Type");
  return res;
}
