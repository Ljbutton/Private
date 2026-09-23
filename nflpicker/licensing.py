"""Subscription check: is this copy licensed to run?

The key comes from the customer's Whop purchase. The app never talks to Whop
directly -- that needs the company API key, which must not ship in an
installer -- it asks the license server in ``license-server/`` instead.

When the check applies
    Only a packaged build that was stamped with a license server address
    (``LICENSE_SERVER`` in ``_build.py``), or any copy run with
    ``NFLPICKER_LICENSE_SERVER`` set. A source checkout, the test suite and the
    demo season never ask for a key, so development and the build's selftest
    work exactly as before.

Offline
    A key that checked out within the last ``GRACE_DAYS`` keeps working when
    the server cannot be reached -- a flaky connection on a Sunday must not lock
    a paying customer out. A server that *answers* "no" ends access at once.

This is a deterrent, not DRM: a determined person can patch a Python app. It
exists so that sharing a download is not the same as sharing a subscription.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import platform
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

GRACE_DAYS = 7
RECHECK_HOURS = 12
TIMEOUT = 10.0

_lock = threading.Lock()
_checking = threading.Event()


# ------------------------------------------------------------------ settings

def server_url() -> str:
    """The license server, or "" when licensing is off for this copy."""
    env = os.environ.get("NFLPICKER_LICENSE_SERVER", "").strip()
    if env:
        return env.rstrip("/")
    with contextlib.suppress(Exception):
        from . import _build  # type: ignore

        return str(getattr(_build, "LICENSE_SERVER", "") or "").strip().rstrip("/")
    return ""


def store_url() -> str:
    """Where to buy or manage a subscription (the Whop product page)."""
    env = os.environ.get("NFLPICKER_STORE_URL", "").strip()
    if env:
        return env
    with contextlib.suppress(Exception):
        from . import _build  # type: ignore

        return str(getattr(_build, "STORE_URL", "") or "").strip()
    return ""


def _demo() -> bool:
    return os.environ.get("NFLPICKER_DEMO", "0").strip() not in ("", "0", "false", "no")


def required() -> bool:
    return bool(server_url()) and not _demo()


def _path() -> Path:
    from .config import get_config

    return Path(get_config().data_dir) / "license.json"


# ---------------------------------------------------------------- machine id

def _raw_machine_id() -> str:
    """Something stable for this computer. Only ever sent hashed."""
    with contextlib.suppress(Exception):
        if sys.platform == "win32":
            import winreg  # type: ignore

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"SOFTWARE\Microsoft\Cryptography") as k:
                return str(winreg.QueryValueEx(k, "MachineGuid")[0])
        if sys.platform == "darwin":
            out = subprocess.run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                                 capture_output=True, text=True, timeout=5).stdout
            for line in out.splitlines():
                if "IOPlatformUUID" in line:
                    return line.split("=")[-1].strip().strip('"')
        for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            if Path(p).exists():
                return Path(p).read_text().strip()
    return f"{platform.node()}-{uuid.getnode()}"


def machine_id() -> str:
    raw = _raw_machine_id()
    return hashlib.sha256(f"the-edge:{raw}".encode()).hexdigest()[:24]


# --------------------------------------------------------------------- state

def _load() -> dict:
    try:
        return json.loads(_path().read_text())
    except Exception:                                         # noqa: BLE001
        return {}


def _save(data: dict) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(p)


def _now() -> float:
    return time.time()


def _iso(ts: float | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def saved_key() -> str:
    return str(_load().get("key") or "")


def _hint(key: str) -> str:
    return f"…{key[-4:]}" if len(key) >= 4 else ""


# --------------------------------------------------------------------- check

def _http(timeout: float = TIMEOUT) -> httpx.Client:
    """One place to build the client, so tests can swap it out."""
    return httpx.Client(timeout=timeout)


def _ask_server(key: str) -> dict:
    """The server's verdict. ``valid`` is True, False, or None (no answer)."""
    try:
        with _http() as c:
            r = c.post(f"{server_url()}/v1/validate",
                       json={"license_key": key, "machine_id": machine_id()})
        if r.status_code >= 500:
            return {"valid": None, "reason": "offline",
                    "message": f"License server error ({r.status_code})."}
        data = r.json()
        return {"valid": data.get("valid"), "reason": data.get("reason") or "",
                "message": data.get("message") or "", "status": data.get("status")}
    except Exception as exc:                                  # noqa: BLE001
        return {"valid": None, "reason": "offline",
                "message": f"Couldn't reach the license server ({type(exc).__name__})."}


def _apply(key: str, verdict: dict) -> dict:
    """Fold a server verdict into what is stored."""
    data = _load() if saved_key() == key else {}
    data["key"] = key
    data["last_attempt"] = _now()
    if verdict["valid"] is True:
        data.update(valid=True, last_ok=_now(), reason="ok",
                    message=verdict.get("message", ""), status=verdict.get("status"))
    elif verdict["valid"] is False:
        data.update(valid=False, reason=verdict.get("reason", ""),
                    message=verdict.get("message", ""), status=verdict.get("status"))
        data.pop("last_ok", None)
    else:
        # No answer: keep the last verdict, remember why this one failed.
        # The *why* matters as much as the fact: "no answer" covers a dead
        # connection and a license server that answered perfectly well but
        # could not get a word out of Whop, and those want opposite advice.
        data["offline_message"] = verdict.get("message", "")
        data["offline_reason"] = verdict.get("reason", "") or "offline"
    _save(data)
    return data


def _no_answer(verdict: dict) -> str:
    """What to tell someone when the check produced no verdict at all."""
    detail = verdict.get("message", "")
    if verdict.get("reason") == "upstream":
        # The server is up and talking to us; it is Whop that would not
        # answer it. Nothing the customer can do, and nothing they broke.
        return (f"The license server couldn't confirm your subscription with "
                f"Whop just now ({detail.rstrip('.')}). That's on our side, "
                f"not yours -- try again in a few minutes, and use Contact "
                f"support below if it keeps happening.")
    return f"{detail} Check your internet connection and try again."


def activate(key: str) -> dict:
    """Check a key typed by the customer and remember it if it is good."""
    key = (key or "").strip()
    if not key:
        return status() | {"message": "Enter your license key."}
    verdict = _ask_server(key)
    if verdict["valid"] is None:
        # A first activation cannot fall back on a grace period it never had.
        # Only send someone to their router when the server really did not
        # answer: "check your connection" is worse than useless advice to a
        # customer whose connection is fine and whose key is fine.
        return status() | {"message": _no_answer(verdict)}
    if verdict["valid"] is False:
        # A mistyped *new* key must not knock out one that is working; a "no"
        # about the key already in use is final.
        if key == saved_key() or not allowed():
            _apply(key, verdict)
        return status() | {"valid": False, "message": verdict["message"],
                           "reason": verdict.get("reason")}
    _apply(key, verdict)
    return status()


def recheck(force: bool = False) -> dict:
    key = saved_key()
    if not key:
        return status()
    data = _load()
    stale = _now() - float(data.get("last_attempt") or 0) > RECHECK_HOURS * 3600
    if force or stale:
        _apply(key, _ask_server(key))
    return status()


def recheck_in_background() -> None:
    """Recheck if it is due, without holding up a request."""
    if not required() or not saved_key() or _checking.is_set():
        return
    data = _load()
    if _now() - float(data.get("last_attempt") or 0) <= RECHECK_HOURS * 3600:
        return

    def run() -> None:
        _checking.set()
        try:
            with _lock:
                recheck()
        finally:
            _checking.clear()

    threading.Thread(target=run, daemon=True, name="license-recheck").start()


def deactivate() -> dict:
    with contextlib.suppress(FileNotFoundError):
        _path().unlink()
    return status()


def allowed() -> bool:
    """May the app be used right now? Cheap: reads the local file only."""
    if not required():
        return True
    data = _load()
    if not data.get("key") or not data.get("valid"):
        return False
    last_ok = float(data.get("last_ok") or 0)
    return _now() - last_ok <= GRACE_DAYS * 86400


def status() -> dict:
    data = _load()
    ok = allowed()
    last_ok = float(data.get("last_ok") or 0) or None
    grace_left = None
    offline = False
    if required() and ok and last_ok:
        grace_left = max(0.0, GRACE_DAYS - (_now() - last_ok) / 86400)
        # Offline means the last attempt did not produce a fresh answer.
        offline = float(data.get("last_attempt") or 0) - last_ok > 60
    message = data.get("message") or ""
    if required() and data.get("valid") and not ok:
        message = ("It's been more than a week since The Edge could confirm your "
                   "subscription. Connect to the internet and check again.")
    return {
        "required": required(),
        "valid": ok,
        "has_key": bool(data.get("key")),
        "key_hint": _hint(str(data.get("key") or "")),
        "status": data.get("status"),
        "reason": data.get("reason") or ("" if ok else "missing_key"),
        "message": message,
        "offline": offline,
        "offline_message": data.get("offline_message") if offline else "",
        "offline_reason": (data.get("offline_reason") or "offline") if offline else "",
        "grace_days_left": round(grace_left, 1) if grace_left is not None else None,
        "last_ok": _iso(last_ok),
        "store_url": store_url(),
    }
