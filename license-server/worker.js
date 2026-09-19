// The Edge license server: a Cloudflare Worker between the desktop app and Whop.
//
// Why it exists: checking a Whop license key needs your Whop *company* API key.
// That key can read every customer's membership and email, so it can never ship
// inside the installer -- anyone could pull it out of the binary. The app talks
// to this Worker; only this Worker holds the key.
//
// Endpoints
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
//   ALLOWED_STATUSES  optional, default "active,trialing,canceling".
//   GITHUB_REPO       optional, e.g. Ljbutton/Private -- where releases are published.
//   GITHUB_TOKEN      secret, optional. Needed once that repository is private.
//   RELEASE_TAG       optional, default "latest".
//   DOWNLOAD_PAGE     optional. Where "Download update" sends people if the
//                     direct download is not set up (e.g. your Whop product page).

const WHOP = "https://api.whop.com/api/v1";

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    try {
      if (request.method === "OPTIONS") return cors(new Response(null, { status: 204 }));
      if (url.pathname === "/" ) return cors(new Response("ok"));
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

  const allowed = String(env.ALLOWED_STATUSES || "active,trialing,canceling")
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
    case "expired":
    case "completed": return "This subscription has ended. Resubscribe on Whop to keep using The Edge.";
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
