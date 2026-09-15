"""The handful of things you actually have to tell this app, and writing them down.

Everything here was already configurable through environment variables, which
is fine on a development machine and useless in a packaged app: there is no
shell to export from, and the `.env` beside the source tree does not exist once
the thing is frozen. So this reads and writes the `.env` in the *data*
directory — the one place a packaged build can be sure of — and the UI edits it.

Two rules worth keeping:

* **A secret is never sent back.** The API returns whether a key is set and its
  last four characters, never the key. A value that only travels one way cannot
  leak through a screenshot, a cached response or a browser history entry.
* **Writing a setting never loses the others.** The file is rewritten from a
  parse of itself, so a key this app has never heard of survives the edit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .config import get_config, reset_config


@dataclass(frozen=True)
class Setting:
    key: str                 # the environment variable name
    label: str
    help: str
    kind: str = "text"       # text | secret | number | bool
    placeholder: str = ""
    group: str = "General"
    # What breaks without it, in the present tense. The point of the settings
    # page is to answer "what do I need to put in here", and a description that
    # only names the field answers nothing.
    needed_for: str = ""
    link: str = ""
    # What the app does when nothing is set. Without this the page showed an
    # unticked box and "not set" for a setting that is on by default, which is
    # not a blank field -- it is the page stating the opposite of the truth.
    default: str = ""


SETTINGS: tuple[Setting, ...] = (
    Setting(
        key="ODDS_API_KEY",
        label="Odds API key",
        kind="secret",
        group="Data sources",
        placeholder="paste the key from the-odds-api.com",
        help="A free account gives 500 requests a month, which is comfortably "
             "more than this app uses at its default cadence.",
        needed_for="Sportsbook lines, the prediction-market comparison, every "
                   "edge on the Edge page, and the Sportsbook column on the "
                   "board. Without it the app still runs on schedules, scores "
                   "and its own model, but has nothing to compare them to.",
        link="https://the-odds-api.com/",
    ),
    Setting(
        key="ODDS_MONTHLY_BUDGET",
        label="Monthly request budget",
        kind="number",
        group="Data sources",
        placeholder="480",
        help="The app throttles itself to stay under this. The free tier is "
             "500, so 480 leaves headroom for a manual refresh.",
        needed_for="Stopping the app from spending the month's quota in a day.",
        default="480",
    ),
    Setting(
        key="ODDS_BOOKS",
        label="Books to include",
        group="Data sources",
        placeholder="draftkings, fanduel, betmgm  (blank = all available)",
        help="Comma separated. Leave blank unless you want the consensus built "
             "from a specific set.",
        needed_for="Nothing — a blank value uses every book the key returns.",
    ),
    Setting(
        key="PREDICTION_MARKETS_ENABLED",
        label="Prediction markets",
        kind="bool",
        group="Data sources",
        help="Kalshi and Polymarket. These need no key of their own.",
        needed_for="The Market column on the board and the prediction-market "
                   "row on the Scoreboard.",
        default="true",
    ),
    Setting(
        key="NFLPICKER_LLM_URL",
        label="Local model endpoint",
        group="Assistant",
        placeholder="http://127.0.0.1:11434/v1",
        help="Any server that speaks the OpenAI chat API. Ollama serves this "
             "at /v1 on port 11434; llama.cpp's llama-server does too.",
        needed_for="The Assistant tab. Nothing leaves your machine — the app "
                   "talks to this address and no other.",
        link="https://ollama.com/download",
    ),
    Setting(
        key="NFLPICKER_LLM_MODEL",
        label="Model name",
        group="Assistant",
        placeholder="qwen3.5:4b",
        help="Whichever model you have pulled. A 4B model is enough for "
             "questions about this data and runs on a laptop.",
        needed_for="The Assistant tab.",
    ),
    Setting(
        key="NFLPICKER_TRAIN_AUTO",
        label="Retrain automatically",
        kind="bool",
        group="Model",
        help="Refits once a week of results has landed, and keeps the new "
             "model only if it is not measurably worse.",
        needed_for="Keeping the model current as the season goes on. Safe to "
                   "leave on; a worse model is rejected rather than shipped.",
        default="true",
    ),
)

_BY_KEY = {s.key: s for s in SETTINGS}

# A key that is a secret is reported as set-or-not and never echoed.
_SECRET_KEYS = {s.key for s in SETTINGS if s.kind == "secret"}


def env_path() -> Path:
    """Where settings are written. The data directory, not the source tree —
    a frozen build's source tree lives in a temporary directory."""
    return get_config().data_dir / ".env"


def _parse(path: Path) -> dict[str, str]:
    """Read a .env into a dict, keeping only assignments."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        out[name.strip()] = value.strip().strip('"').strip("'")
    return out


def _mask(value: str) -> str:
    """Enough to recognise a key you already pasted, useless to anyone else."""
    if not value:
        return ""
    return f"••••{value[-4:]}" if len(value) > 4 else "••••"


def current() -> dict:
    """Every setting, its present value, and whether the app can see it.

    A secret's value is replaced by a mask. `source` says where the value came
    from, because "I set that" and "that is the default" look identical in a
    text box and only one of them survives a restart.
    """
    import os

    stored = _parse(env_path())
    groups: dict[str, list[dict]] = {}
    for s in SETTINGS:
        live = os.environ.get(s.key, "")
        value = stored.get(s.key) or live or s.default
        secret = s.key in _SECRET_KEYS
        explicit = s.key in stored or bool(live)
        groups.setdefault(s.group, []).append({
            "key": s.key,
            "label": s.label,
            "help": s.help,
            "needed_for": s.needed_for,
            "kind": s.kind,
            "placeholder": s.placeholder,
            "link": s.link,
            "is_set": bool(value),
            # Whether *you* set it, as opposed to it having a default. In a
            # text box those look identical and only one survives a reinstall.
            "explicit": explicit,
            "default": s.default,
            "value": "" if secret else value,
            "masked": _mask(value) if secret else "",
            "source": ("file" if s.key in stored else ("env" if live else "default")),
        })
    return {
        "path": str(env_path()),
        "groups": [{"name": name, "settings": items} for name, items in groups.items()],
    }


def save(values: dict[str, str]) -> dict:
    """Write settings, keeping anything already in the file that we did not set.

    An empty string clears a setting rather than storing a blank, so the box can
    be used to remove a key as well as to add one. A secret left untouched by
    the UI arrives as absent, not as empty, so it is not cleared by accident.
    """
    path = env_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    stored = _parse(path)

    changed = []
    for key, raw in values.items():
        if key not in _BY_KEY:
            continue                       # never write a name we do not define
        value = str(raw).strip()
        if value:
            stored[key] = value
        else:
            stored.pop(key, None)
        changed.append(key)

    lines = [
        "# NFL Picker settings. Written by the Settings page; safe to edit by hand.",
        "# Anything here overrides the environment.",
    ]
    lines += [f"{k}={v}" for k, v in sorted(stored.items())]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Make the new values live without a restart. The config is a frozen
    # dataclass built from the environment, so both have to move.
    import os

    for key in changed:
        value = stored.get(key)
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    reset_config()
    return {"saved": sorted(changed), "path": str(path)}


def validate_odds_key(key: str) -> dict:
    """Ask the Odds API whether a key works, and say so in one line.

    Pasting a key and being told nothing is the worst version of this page: a
    typo looks exactly like a working key until the next refresh silently
    fetches nothing.
    """
    key = (key or "").strip()
    if not key:
        return {"ok": False, "message": "No key entered."}
    if not re.fullmatch(r"[A-Za-z0-9]{16,64}", key):
        return {"ok": False, "message": "That does not look like an Odds API key —"
                                        " they are 20-40 letters and digits."}
    try:
        import httpx

        response = httpx.get(
            "https://api.the-odds-api.com/v4/sports",
            params={"apiKey": key}, timeout=15.0,
        )
    except Exception as exc:                                  # noqa: BLE001
        return {"ok": False, "message": f"Could not reach the Odds API: {exc}"}

    if response.status_code == 401:
        return {"ok": False, "message": "The Odds API rejected that key."}
    if response.status_code >= 400:
        return {"ok": False, "message": f"The Odds API answered {response.status_code}."}

    remaining = response.headers.get("x-requests-remaining")
    used = response.headers.get("x-requests-used")
    detail = f" {remaining} of {int(remaining or 0) + int(used or 0)} requests left this month." \
        if remaining else ""
    return {"ok": True, "message": f"Key works.{detail}"}
