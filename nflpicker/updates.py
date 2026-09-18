"""Whether a newer build exists.

An app people pay for has to be able to say when it is out of date. The model
changes during the season -- that is most of what the subscription buys -- and
a copy that silently stays on September's weights while October's ship is
worth less every week, without ever saying so.

Deliberately small. It asks the releases feed which build is newest, compares
it to the one running, and says. It does not download anything, it does not
replace itself, and it never blocks the app: a check that cannot reach the
internet is not an error, it is a Tuesday.
"""

from __future__ import annotations

import sys
import time
from urllib.parse import urlencode

from . import buildinfo, licensing

RELEASES = "https://api.github.com/repos/Ljbutton/Private/releases/tags/latest"
DOWNLOAD = "https://github.com/Ljbutton/Private/releases/tag/latest"

# Once a day is plenty for a thing that ships weekly, and it means a copy left
# open all season asks a few dozen times rather than every minute.
CHECK_EVERY = 24 * 3600

_cache: dict = {"at": 0.0, "result": None}


def _published_commit(payload: dict) -> str:
    """Which commit the newest release was built from."""
    return str(payload.get("target_commitish") or "")[:7]


def asset_name() -> str:
    """The installer a customer on this computer should download."""
    if sys.platform == "win32":
        return "TheEdge-windows-setup.exe"
    if sys.platform == "darwin":
        return "TheEdge-macos-arm.tar.gz"
    return ""


def _from_license_server(server: str, timeout: float) -> tuple[dict, str, str | None]:
    """Newest build as the license server reports it.

    Used whenever this copy is licensed. It keeps working after the repository
    goes private -- the server holds the GitHub token, the app never does --
    and its download link is only honoured for a subscription that is live.
    """
    with licensing._http(timeout) as c:
        r = c.get(f"{server}/v1/latest")
    r.raise_for_status()
    data = r.json()
    url = None
    key = licensing.saved_key()
    if data.get("download_url") and key and asset_name():
        url = f"{data['download_url']}?{urlencode({'key': key, 'asset': asset_name()})}"
    url = url or data.get("download_page") or licensing.store_url() or DOWNLOAD
    payload = {"target_commitish": data.get("commit") or "",
               "published_at": data.get("built_at")}
    return payload, url, data.get("notes") or ""


def check(*, force: bool = False, timeout: float = 6.0) -> dict:
    """What the newest release is, and whether it is newer than this build.

    `newer` is only ever True when both commits are known and they differ. An
    unknown build or an unreachable feed reports `newer: False` with a reason,
    because telling someone they are out of date when you do not know is worse
    than saying nothing.
    """
    now = time.time()
    if not force and _cache["result"] and now - _cache["at"] < CHECK_EVERY:
        return _cache["result"]

    mine = buildinfo.build_info()
    out = {"checked_at": now, "current": mine.get("commit"),
           "latest": None, "newer": False, "url": DOWNLOAD, "reason": ""}

    if mine.get("source") != "release":
        # A source checkout is not something to nag about updating.
        out["reason"] = "running from a checkout"
        _cache.update(at=now, result=out)
        return out

    try:
        server = licensing.server_url()
        if server:
            payload, out["url"], out["notes"] = _from_license_server(server, timeout)
        else:
            import httpx

            response = httpx.get(RELEASES, timeout=timeout,
                                 headers={"Accept": "application/vnd.github+json"})
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:                                  # noqa: BLE001
        out["reason"] = f"could not reach the release feed: {type(exc).__name__}"
        # Not cached for a day: a laptop that was offline when it asked should
        # be able to find out as soon as it is not.
        return out

    latest = _published_commit(payload)
    out["latest"] = latest or None
    out["published_at"] = payload.get("published_at") or payload.get("created_at")
    if not latest:
        out["reason"] = "the release does not say which commit it came from"
    elif not mine.get("commit") or mine["commit"] == "unknown":
        out["reason"] = "this build does not say which commit it came from"
    elif latest != mine["commit"]:
        out["newer"] = True
    else:
        out["reason"] = "up to date"

    _cache.update(at=now, result=out)
    return out


def reset_cache() -> None:
    _cache.update(at=0.0, result=None)
