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

import contextlib
import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path

from .config import get_config, reset_config


class SettingsError(RuntimeError):
    """Something the user can act on, phrased for the Settings page."""



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
        key="NFLPICKER_USER_NAME",
        label="Your name",
        group="General",
        placeholder="read from this computer's account",
        help="Only ever used to say hello. It is not sent anywhere, and the "
             "app works identically if you leave it blank.",
        needed_for="Nothing. The app reads the name on this computer's account "
                   "when the box is empty, and greets you without a name when "
                   "that account is called something like `admin`.",
    ),
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
        key="ODDS_WEEKLY_BUDGET",
        label="Weekly credit ceiling",
        group="Data sources",
        placeholder="120",
        help="A burst ceiling, not the bill. 480 a month is 120 a week, so this "
             "is the week's share — it stops one busy week spending the month.",
        needed_for="Nothing on its own — it caps how fast the monthly budget "
                   "can be spent.",
        default="120",
    ),
    Setting(
        key="ODDS_DAILY_BUDGET",
        label="Daily credit ceiling",
        group="Data sources",
        placeholder="50",
        help="Each poll costs 3 credits (spread, total, moneyline), and 480 a "
             "month works out at about 16 credits — five polls — a day. This is "
             "deliberately above that so a Sunday can poll harder than a "
             "Tuesday; it is a ceiling on a bad day, not a daily allowance. "
             "Setting it near 16 would cap every day at the average and leave "
             "the month unspent.",
        needed_for="Nothing on its own — it is the tightest of the three caps.",
        default="50",
    ),
    Setting(
        key="NFLPICKER_LLM_URL",
        label="Local model endpoint",
        group="Assistant",
        placeholder="set for you by the Assistant tab",
        help="Any server that speaks the OpenAI chat API. The Assistant tab "
             "can install and run one for you and fill this in; these two "
             "boxes are for pointing at a server you already run instead.",
        needed_for="The Assistant tab. Nothing leaves your machine — the app "
                   "talks to this address and no other, and refuses any "
                   "address that is not on this computer.",
    ),
    Setting(
        key="NFLPICKER_LLM_MODEL",
        label="Model name",
        group="Assistant",
        placeholder="qwen3.5:4b",
        help="Whichever model that server has. A 4B model is enough for "
             "questions about this data and runs on a laptop.",
        needed_for="The Assistant tab.",
    ),
    Setting(
        key="NFLPICKER_SURVIVOR_LAST_WEEK",
        label="Survivor runs through week",
        kind="number",
        group="General",
        placeholder="18",
        help="The end of the season. Set it to 17 if your pool settles there "
             "— week 18 rests starters, so it is the week a projection is "
             "worth least, and planning for it spends a team you could keep.",
        needed_for="How far ahead the survivor path is planned, and how many "
                   "weeks show in the rest of the run.",
        default="18",
    ),
    Setting(
        key="NFLPICKER_TRAIN_MAX_AGE_HOURS",
        label="Refit at the latest after",
        kind="number",
        group="Model",
        placeholder="72",
        help="Hours. Once a fit is this old, a single new result is enough to "
             "redo it — which is what puts a refit midweek, after Monday "
             "night, rather than waiting for the next Sunday.",
        needed_for="Nothing on its own. It is the second of two ways to earn "
                   "a refit; the first is a week's worth of new results.",
        default="72",
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
        "# The Edge settings. Written by the Settings page; safe to edit by hand.",
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


# --------------------------------------------------------------- backups
# A copy you asked for, kept beside the database it came from.
#
# The migration path already writes one before it changes the schema, but that
# is one file per source version and a second migration from the same version
# overwrites it. It is a safety net for the app's own upgrades, not a backup
# you can rely on -- and the thing worth protecting here is a season of picks
# that exists in exactly one place.
BACKUP_DIR = "backups"
BACKUP_KEEP = 10


def backup_dir() -> Path:
    return Path(get_config().data_dir) / BACKUP_DIR


def list_backups() -> list[dict]:
    """Newest first."""
    directory = backup_dir()
    if not directory.exists():
        return []
    rows = []
    for path in directory.glob("*.db"):
        try:
            stat = path.stat()
        except OSError:
            continue
        rows.append({
            "name": path.name,
            "path": str(path),
            "bytes": stat.st_size,
            "made_at": dt.datetime.fromtimestamp(
                stat.st_mtime, tz=dt.timezone.utc).isoformat(),
        })
    return sorted(rows, key=lambda r: r["made_at"], reverse=True)


def make_backup() -> dict:
    """Copy the database, with its settings beside it.

    Uses sqlite's own backup API rather than copying the file. A live database
    has a write-ahead log, and copying the .db on its own can capture a moment
    that never existed -- committed pages without the log that explains them.
    The API takes a consistent snapshot of a database that is still being
    written to, which is exactly the situation here.
    """
    import shutil
    import sqlite3

    source = Path(get_config().db_path)
    if not source.exists():
        raise SettingsError("There is no database to back up yet.")

    directory = backup_dir()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(tz=dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = directory / f"nflpicker-{stamp}.db"

    with sqlite3.connect(str(source)) as src, sqlite3.connect(str(target)) as dst:
        src.backup(dst)

    # The settings ride along: an API key and an assistant endpoint are part of
    # "restore this machine", and they are not in the database.
    env = env_path()
    if env.exists():
        with contextlib.suppress(OSError):
            shutil.copy2(env, target.with_suffix(".env"))

    pruned = _prune_backups()
    made = target.stat().st_size
    return {
        "ok": True,
        "name": target.name,
        "path": str(target),
        "bytes": made,
        "pruned": pruned,
        "backups": list_backups(),
    }


def _prune_backups() -> int:
    """Keep the newest few. Unbounded backups of a growing database fill a disk
    quietly, and the oldest copy of a season is the least useful one."""
    existing = list_backups()
    removed = 0
    for row in existing[BACKUP_KEEP:]:
        path = Path(row["path"])
        with contextlib.suppress(OSError):
            path.unlink()
            removed += 1
        with contextlib.suppress(OSError):
            path.with_suffix(".env").unlink()
    return removed


def _pairing_message(seen: int, found: list[dict]) -> str:
    """Why the events that came back produced no quotes.

    "None survived pairing" is a true sentence that names four different bugs,
    and from outside they are the same silence. The counts say which one: no
    price on any contract is the wrong endpoint or a board nobody has quoted
    yet; both contracts resolving to the same team is a ticker whose shape has
    changed under us; no team at all is an abbreviation we do not know.
    """
    from .sources.kalshi import KalshiSource, pairing_report

    rows = [e for a in found for e in (a.get("rows") or [])]
    # Through the same refetch the real fetch uses, so this reports on what the
    # app would actually have got rather than on the first response. Otherwise
    # the panel says "no prices" about a board the pipeline goes on to price.
    with contextlib.suppress(Exception):
        rows = KalshiSource().fill_prices(rows)
    counts = pairing_report(rows)
    if counts["paired"]:
        return (f"{counts['paired']} of {seen} events do pair once the books "
                f"are fetched separately — this reads as a stale cache rather "
                f"than a broken feed; try again in a minute")
    sample = found[0].get("sample") or "none"
    if counts["markets"] == 0:
        return f"{seen} events came back carrying no contracts at all"
    if counts["no_price"] and not counts["paired"]:
        return (f"{seen} events, {counts['markets']} contracts, and not one of "
                f"them has a price — either nobody has quoted this board yet or "
                f"the book is not in this response (sample: {sample})")
    if counts["same_team"]:
        return (f"{counts['same_team']} of {seen} events have both contracts "
                f"resolving to the same team — the ticker shape has changed "
                f"(sample: {sample})")
    if counts["no_team"]:
        return (f"{counts['no_team']} contracts name a team we do not "
                f"recognise (sample: {sample})")
    return (f"{seen} events came back but none survived pairing "
            f"(sample: {sample})")


def test_prediction_markets() -> dict:
    """Ask each venue directly and report what happened, per venue.

    The status dot says a feed failed; it cannot say why without a hover, and
    the reason is the whole diagnosis -- "no NFL markets right now" and "your
    network blocks this host" look identical from the outside and need
    opposite responses.
    """
    from .sources.kalshi import KalshiSource
    from .sources.polymarket import PolymarketSource

    results = []
    for name, fetch in (
        ("Polymarket", lambda: PolymarketSource().fetch(with_depth=False)),
        ("Kalshi", lambda: KalshiSource().fetch()),
    ):
        try:
            quotes = fetch()
        except Exception as exc:                          # noqa: BLE001
            results.append({
                "venue": name, "ok": False, "n": 0,
                "message": f"{type(exc).__name__}: {exc}"[:300],
            })
            continue
        results.append({
            "venue": name, "ok": True, "n": len(quotes),
            "message": (f"{len(quotes)} NFL contracts" if quotes
                        else "reachable, but it is quoting no NFL games right now"),
        })

    # When a venue answers with nothing, say where the games were lost rather
    # than only that there were none. The venue quoting nothing and us asking
    # the wrong question look identical from here and need opposite fixes --
    # and a series ticker Kalshi has renamed is the likelier of the two, so
    # the diagnosis is worth the extra request.
    kalshi = next((r for r in results if r["venue"] == "Kalshi"), None)
    if kalshi and kalshi["ok"] and not kalshi["n"]:
        with contextlib.suppress(Exception):
            # Probed once and read twice: the message needs the events
            # themselves to say *why* they did not pair, and the response
            # carries only the counts, because thirty-one events with their
            # markets nested is most of a megabyte of JSON going to a panel
            # that displays five numbers.
            #
            # These used to be the same stripped list, which made the
            # diagnostic wrong in the most misleading way available: it read
            # the rows it had just removed, found none, and reported "31
            # events carrying no contracts at all" on the same screen as a row
            # saying "31 events · 62 markets".
            probed = KalshiSource().probe()
            kalshi["attempts"] = [
                {k: v for k, v in a.items() if k != "rows"} for a in probed
            ]
            found = [a for a in probed if a["events"]]
            if found:
                seen = sum(a["events"] for a in found)
                kalshi["message"] = _pairing_message(seen, found)
            else:
                asked = ", ".join(a["series"] for a in kalshi["attempts"])
                kalshi["message"] = (
                    f"no events under any known series ticker ({asked}) — "
                    "Kalshi has likely renamed the NFL series again")

    quoting = [r for r in results if r["ok"] and r["n"]]
    answered = [r for r in results if r["ok"]]
    failed = [r for r in results if not r["ok"]]

    if quoting:
        summary = "Working — " + ", ".join(f"{r['venue']}: {r['n']}" for r in quoting)
        if failed:
            summary += f" ({', '.join(r['venue'] for r in failed)} unreachable)"
    elif answered and not failed:
        summary = ("Both venues answered and neither is quoting NFL games. That is "
                   "normal outside the season and in the hours after a slate.")
    elif answered:
        summary = (f"{answered[0]['venue']} answered with no NFL games; "
                   f"{', '.join(r['venue'] for r in failed)} could not be reached.")
    else:
        summary = ("Neither venue could be reached. If both errors mention a "
                   "connection, proxy or certificate, it is this machine's network "
                   "rather than the app.")
    return {"ok": bool(quoting), "summary": summary, "venues": results}
