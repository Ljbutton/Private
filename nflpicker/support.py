"""Bug reports: the log, the redaction, and the payload that leaves the machine.

Three jobs, in the order they matter.

**A log worth attaching.** A packaged Windows build is windowed, so every
traceback on the way to the window went nowhere; the desktop entry point sends
them to a file. That file is now rotating and installed from anywhere the app
starts, not just the desktop host, because a report from someone running
``nflpicker serve`` is worth as much as one from the app.

**Redaction before anything leaves.** A log is the most useful thing a report
can carry and the most dangerous: it is exactly where a key ends up when
something goes wrong with a key. So the text is scrubbed on the way out --
the licence key, the Odds API key, and anything key-shaped whether or not this
app put it there. It runs over what the user typed too: "my key ABCD-1234
doesn't work" is the single likeliest sentence in a report about activation.

**Only what was agreed.** Every attachment is a checkbox, ticked by default
and readable in full before sending. Unticking one means it is not collected,
not merely hidden -- see :func:`details`.
"""

from __future__ import annotations

import contextlib
import logging
import logging.handlers
import platform
import re
from pathlib import Path

from .config import get_config

# Small on purpose. This exists to be read by a person and attached to a
# report, not to be an audit trail: half a megabyte is several thousand lines,
# which is far more than the hundred a report carries.
LOG_MAX_BYTES = 512_000
LOG_BACKUPS = 2
LOG_TAIL_LINES = 100

# What the form will send, and what each line of the box says.
DETAIL_KEYS = ("version", "os", "key_hint", "log")

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def log_path() -> Path:
    """Where the app writes its log.

    Kept at the name the desktop host has always used, so an existing
    installation's log is not orphaned by this becoming the shared path.
    """
    return Path(get_config().data_dir) / "logs" / "desktop.log"


def install_logging(level: int = logging.INFO) -> Path | None:
    """Attach a rotating file handler to the root logger, once.

    Idempotent: called from both entry points, and calling it twice would
    otherwise write every line twice and hold two handles on one file.
    """
    path = log_path()
    root = logging.getLogger()
    for handler in root.handlers:
        if isinstance(handler, logging.handlers.RotatingFileHandler) and (
                Path(getattr(handler, "baseFilename", "")) == path):
            return path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS,
            encoding="utf-8")
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        root.setLevel(level)
        root.addHandler(handler)
    except Exception:                                         # noqa: BLE001
        # A log that cannot be opened is not a reason the app cannot start.
        return None
    return path


# --------------------------------------------------------------- redaction

# A licence key, as this app's own server defines one: four to a hundred
# characters of the URL-safe alphabet. Matched only when it is announced as a
# key, because that pattern on its own is most English words.
_LABELLED = re.compile(
    r"""(?ix)
    \b(
      licen[cs]e[\s_-]*key | licen[cs]e | api[\s_-]*key | apikey | api[\s_-]*token
      | access[\s_-]*token | secret | bearer | auth(?:orization)? | token | key
    )
    (\s*[:=]\s*|\s+is\s+|\s+)
    ["']?([A-Za-z0-9_\-]{8,100})["']?
    """)

# A query parameter carrying one. `key=` and `apiKey=` are what the two feeds
# this app talks to actually use.
_QUERY = re.compile(
    r"(?i)\b((?:api[_-]?key|apikey|key|token|access_token|secret)=)"
    r"([^&\s\"'>]+)")

# Something that is a key whatever it is called: a long unbroken run of hex or
# of base64. Thirty-two is the shortest thing worth worrying about (an MD5, a
# Whop key) and short enough to catch them; below it the false positives start
# being real words and identifiers.
_HEXISH = re.compile(r"\b[0-9a-fA-F]{32,}\b")
_BASE64ISH = re.compile(r"\b[A-Za-z0-9+/_-]{40,}={0,2}\b")

REDACTED = "[redacted]"


def redact(text: str, *, extra: tuple[str, ...] = ()) -> str:
    """Take the secrets out of a piece of text.

    Order matters. The exact known values go first, so a key this app holds is
    removed whether or not it is shaped like the patterns below; then the
    labelled and query-parameter forms, which know what they are looking at;
    then the shape-only rules, which are the blunt backstop.

    Deliberately blunt at the end. A report with a hash needlessly starred out
    is a small loss; a report carrying somebody's live key is not.
    """
    if not text:
        return ""
    out = str(text)

    for value in (*extra, *_known_secrets()):
        value = (value or "").strip()
        if len(value) >= 6:
            out = out.replace(value, REDACTED)

    out = _LABELLED.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", out)
    out = _QUERY.sub(lambda m: f"{m.group(1)}{REDACTED}", out)
    out = _HEXISH.sub(REDACTED, out)
    out = _BASE64ISH.sub(REDACTED, out)
    return out


def _known_secrets() -> tuple[str, ...]:
    """Every secret this process actually holds, so it can be matched exactly."""
    values: list[str] = []
    with contextlib.suppress(Exception):
        from . import licensing

        values.append(licensing.saved_key())
    with contextlib.suppress(Exception):
        config = get_config()
        values.append(getattr(config, "odds_api_key", "") or "")
    return tuple(v for v in values if v)


# ------------------------------------------------------------ what we attach

def tail_log(lines: int = LOG_TAIL_LINES) -> str:
    """The end of the log, redacted. Empty string when there is no log yet."""
    path = log_path()
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            kept = handle.readlines()[-max(1, int(lines)):]
    except Exception:                                         # noqa: BLE001
        return ""
    return redact("".join(kept)).strip()


def environment() -> dict:
    """The machine, in the two lines that ever matter for a bug report."""
    return {
        "os": f"{platform.system()} {platform.release()}".strip(),
        "os_detail": platform.platform(),
        "python": platform.python_version(),
    }


def version_line() -> dict:
    """Which build this is, from whatever the build stamped in."""
    from . import __version__, buildinfo

    info = {}
    with contextlib.suppress(Exception):
        info = buildinfo.build_info() or {}
    return {
        "version": __version__,
        "commit": info.get("commit") or "",
        "built_at": info.get("built_at") or "",
    }


def details(include: dict | None = None) -> dict:
    """Assemble only the attachments that were left ticked.

    Unticked means *not gathered*. The form could have collected everything
    and sent a subset, and that would be the same thing right up until the
    moment it was not -- so the decision is made here, where the collecting
    happens, and an untouched box leaves no trace in the payload at all.
    """
    want = {key: True for key in DETAIL_KEYS}
    for key, value in (include or {}).items():
        if key in want:
            want[key] = bool(value)

    out: dict = {"included": sorted(k for k, v in want.items() if v)}
    if want["version"]:
        out["version"] = version_line()
    if want["os"]:
        out["environment"] = environment()
    if want["key_hint"]:
        with contextlib.suppress(Exception):
            from . import licensing

            out["key_hint"] = licensing.status().get("key_hint") or ""
    if want["log"]:
        out["log"] = tail_log()
    return out


def build(description: str, *, doing: str = "", email: str = "",
          include: dict | None = None) -> dict:
    """The report, redacted and ready to go.

    The description is scrubbed as thoroughly as the log is. "My key
    ABCD-1234 won't activate" is the likeliest sentence in a report about
    activation, and it would otherwise be the one place a live key travelled
    in clear.
    """
    return {
        "description": redact(str(description or "").strip())[:MAX_DESCRIPTION],
        "doing": redact(str(doing or "").strip())[:MAX_DESCRIPTION],
        "email": str(email or "").strip()[:200],
        "details": details(include),
    }


MAX_DESCRIPTION = 4000


# ------------------------------------------------------------------ sending

def forward(report: dict, *, timeout: float = 10.0) -> dict:
    """Hand the report to the licence server, which emails it on.

    The app does not know where reports end up and cannot be made to say: the
    destination is a secret on that server, so it is not in a binary anyone
    can unpack and it can be changed without shipping a new build.

    No licence key is required. The commonest report is "my key will not
    activate", and requiring a working key to say so would lock out exactly
    the people with something to report. The key *hint* travels when it is
    ticked and there is one, which is enough to find a subscription.
    """
    from . import licensing

    server = licensing.server_url()
    if not server:
        return {"ok": False, "reason": "no_server",
                "message": "Bug reporting isn't available in this build — "
                           "it needs the support server, which only the "
                           "packaged app is set up for."}
    try:
        import httpx

        with httpx.Client(timeout=timeout) as client:
            response = client.post(f"{server}/v1/support", json=report)
        if response.status_code >= 500:
            return {"ok": False, "reason": "server_error",
                    "message": "The support server could not take the report "
                               "just now. Copy it and try again later."}
        response.raise_for_status()
        body = response.json()
    except Exception as exc:                                  # noqa: BLE001
        return {"ok": False, "reason": "unreachable",
                "message": f"Could not reach the support server "
                           f"({type(exc).__name__}). Copy the report so "
                           f"nothing you wrote is lost."}
    if not isinstance(body, dict) or not body.get("ok"):
        return {"ok": False, "reason": "refused",
                "message": "The support server refused the report."}
    return {"ok": True, "ref": str(body.get("ref") or "")}
