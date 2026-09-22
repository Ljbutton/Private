"""Pick sharing: what leaves this machine, when, and how to stop it.

The app already knows every pick its user makes -- the "You" column on the
board, the survivor team they spent. This sends a copy to the licence server,
where picks are graded and ranked, so the model can learn from people who are
consistently right.

Three things decide whether that is acceptable, and all three live here.

**It is a pseudonym, not an anonymiser.** The id is a SHA-256 of the licence
key and a fixed salt, cut to sixteen hex characters. The salt is in the source
and the owner holds the keys, so the owner can work out which customer an id
belongs to whenever they want to. That is the point of the feature and it is
what the notice says. What it does *not* say, and must not, is "anonymous".

**The key travels, and is not stored.** It did not, at first, and the notice
said so -- but an endpoint that takes picks from anybody who finds its URL is
an endpoint whose table is whatever a stranger decides it is, and checking a
subscription means sending the thing that identifies one. So the key goes with
each batch, the server checks it against Whop and throws it away: what is
written down there is the id, as before. The notice says this now, because the
sentence it used to carry stopped being true.

**Nothing is sent before the notice has been seen.** Sharing is on by default,
which is only defensible because this is true: :func:`may_send` is the one
gate, checked on the way into the queue as well as on the way out, so a pick
made before the notice was acknowledged is not sitting in a queue waiting to be
flushed the moment it is. Default on plus a notice nobody has read would be
collecting quietly, which is a different feature.

**Off means off, and deleted means deleted.** Turning the switch off stops the
sender and leaves the queue to be dropped; the delete button asks the server to
remove everything it holds for this id, and proves it is entitled to by sending
the licence key alongside -- the one request in this module that carries it.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import threading

from . import db
from .util import now_iso

log = logging.getLogger(__name__)

# Bump when the wording changes materially. A notice nobody has read is not a
# notice, and a version that never moves turns a changed bargain into a silent
# one. See NOTICE in this module for the text this number describes.
NOTICE_VERSION = 1

# Fixed, and in the source on purpose: the app and the Worker have to derive
# the same id from the same key, and a per-install salt would give one customer
# a different id on every machine. It is not a secret and is not doing the work
# a secret would -- see the module docstring on what this id is and is not.
SALT = "the-edge:picks:v1"
ID_CHARS = 16

SETTING = "NFLPICKER_SHARE_PICKS"
NOTICE_KEY = "share_notice_seen"
NAME_KEY = "share_display_name"

# What a pick can be about. "winner" is the straight-up pick on the board,
# "spread" the same board's against-the-spread contest, "survivor" the team
# spent this week. "total" is here because the shape of a shared pick has to
# carry one and adding a field later is worse than leaving one empty now.
KINDS = ("winner", "spread", "total", "survivor")

# How many queued picks go in one request, and how often the sender wakes.
BATCH = 50
FLUSH_SECONDS = 60.0
MAX_TRIES = 8


# ------------------------------------------------------------------ identity

def picker_id(key: str | None = None) -> str:
    """The id this installation is known by, or "" with no licence key.

    Sixteen hex characters: enough that two customers will not collide, short
    enough to read out over the phone when somebody asks which picker they are.
    """
    if key is None:
        from . import licensing

        key = licensing.saved_key()
    key = (key or "").strip()
    if not key:
        return ""
    digest = hashlib.sha256(f"{SALT}:{key}".encode()).hexdigest()
    return digest[:ID_CHARS]


def default_name(pid: str = "") -> str:
    """What the leaderboard calls somebody who has not named themselves."""
    pid = pid or picker_id()
    return f"Picker #{pid[:4]}" if pid else "Picker"


def display_name() -> str:
    stored = str(db.get_meta(NAME_KEY, "") or "").strip()
    return stored[:40] or default_name()


def set_display_name(name: str) -> str:
    db.set_meta(NAME_KEY, str(name or "").strip()[:40])
    return display_name()


# -------------------------------------------------------------------- consent

def chosen() -> bool:
    """Whether the user has ever made a choice about this.

    The difference between "off" and "has not said" is the whole of the
    upgrade path: somebody who turned this off once must not be opted back in
    by a later build deciding the default has changed.
    """
    from . import settings

    return settings.raw_value(SETTING) is not None


def enabled() -> bool:
    """Whether sharing is switched on. Off unless the user said otherwise."""
    from . import settings

    raw = settings.raw_value(SETTING)
    if raw is None:
        return DEFAULT_ON
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def set_enabled(on: bool) -> bool:
    from . import settings

    settings.save({SETTING: "1" if on else "0"})
    if not on:
        # Stop meaning stop. What is already queued is not a promise to send
        # it later -- it is a pile of picks the user has just said they did
        # not want sent.
        with contextlib.suppress(Exception):
            db.execute("DELETE FROM share_queue")
    return enabled()


def notice_seen() -> int:
    with contextlib.suppress(Exception):
        return int(db.get_meta(NOTICE_KEY, 0) or 0)
    return 0


def mark_notice_seen(version: int = NOTICE_VERSION) -> int:
    db.set_meta(NOTICE_KEY, int(version))
    return notice_seen()


def needs_notice() -> bool:
    """Whether the one-time notice still has to be shown."""
    return notice_seen() < NOTICE_VERSION


def may_send() -> bool:
    """The single gate. Nothing is queued or sent unless this is true."""
    return enabled() and not needs_notice() and bool(picker_id())


# Whether an installation that has never chosen is sharing.
#
# On. That is a decision with a cost, and the notice is what pays it: nothing
# is queued or sent until the user has seen the modal and pressed one of its
# two buttons, so "default on" means "on once they have been told", never "on
# quietly". :func:`may_send` is where that is enforced.
#
# It applies to somebody who has never made a choice. Anybody who has -- and
# turning it off is a choice -- keeps theirs, which is why :func:`chosen`
# exists and why this is not simply a default on the Setting.
DEFAULT_ON = True


# ---------------------------------------------------------------- the queue

def enqueue(kind: str, *, game_id: str, season: int, week: int, side: str,
            line: float | None = None, price: int | None = None,
            total_line: float | None = None, book_prob: float | None = None,
            picked_at: str | None = None) -> bool:
    """Put one pick in the outbox, or don't.

    Returns whether it was queued, which is what the tests assert on: the
    promise is that a pick made before the notice was acknowledged leaves no
    trace at all, not that it is filtered out later.
    """
    if kind not in KINDS or not may_send():
        return False
    try:
        db.execute(
            "INSERT OR REPLACE INTO share_queue"
            "(kind, game_id, season, week, side, line, price, total_line,"
            " book_prob, picked_at, queued_at, tries) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,0)",
            (kind, game_id, int(season), int(week), str(side or ""),
             line, price, total_line, book_prob,
             picked_at or now_iso(), now_iso()))
    except Exception:                                         # noqa: BLE001
        log.debug("could not queue a shared pick", exc_info=True)
        return False
    return True


def clear(kind: str, game_id: str) -> None:
    """A pick taken back before it was sent is not a pick."""
    with contextlib.suppress(Exception):
        db.execute("DELETE FROM share_queue WHERE kind = ? AND game_id = ?",
                   (kind, game_id))


def pending(limit: int = BATCH) -> list[dict]:
    try:
        return db.query(
            "SELECT * FROM share_queue WHERE tries < ? "
            "ORDER BY id LIMIT ?", (MAX_TRIES, int(limit)))
    except Exception:                                         # noqa: BLE001
        return []


def _payload(rows: list[dict]) -> dict:
    """The batch, as the server wants it.

    The licence key is in here and nowhere else in this module's normal
    traffic: it is what proves the subscription is live, and it is the reason
    the id below cannot simply be invented. It is not logged here and is not
    stored there -- see the module docstring.
    """
    from . import licensing

    return {
        "picker": picker_id(),
        "license_key": licensing.saved_key(),
        "name": display_name(),
        "picks": [{
            "game_id": r["game_id"],
            "kind": r["kind"],
            "side": r["side"],
            "season": r["season"],
            "week": r["week"],
            "line": r["line"],
            "price": r["price"],
            "total_line": r["total_line"],
            "book_prob": r["book_prob"],
            "picked_at": r["picked_at"],
        } for r in rows],
    }


def flush(limit: int = BATCH, *, timeout: float = 10.0) -> dict:
    """Send what is queued. Never raises, never blocks a request.

    A failure leaves the rows where they are with their try count up, so the
    next wake tries again; past MAX_TRIES a row is left alone rather than
    retried for ever, because a pick nobody could deliver for eight hours is
    not going to start working and the game has kicked off by then anyway.
    """
    from . import licensing

    if not may_send():
        return {"sent": 0, "reason": "off"}
    server = licensing.server_url()
    if not server:
        return {"sent": 0, "reason": "no_server"}
    rows = pending(limit)
    if not rows:
        return {"sent": 0, "reason": "empty"}

    ids = ",".join(str(r["id"]) for r in rows)
    try:
        import httpx

        with httpx.Client(timeout=timeout) as client:
            response = client.post(f"{server}/v1/picks", json=_payload(rows))
        # A refusal is an answer, not a hiccup. Sharing needs a live
        # subscription, and a lapsed one will not start working because the
        # app asked sixty more times -- so the batch is dropped rather than
        # queued against a day that is not coming. A rate limit is the other
        # way round: that one is temporary and worth waiting out.
        if response.status_code in (400, 403):
            with contextlib.suppress(Exception):
                db.execute(f"DELETE FROM share_queue WHERE id IN ({ids})")  # noqa: S608
            log.debug("shared picks refused: %s", response.status_code)
            return {"sent": 0, "reason": "refused"}
        response.raise_for_status()
    except Exception as exc:                                  # noqa: BLE001
        with contextlib.suppress(Exception):
            db.execute(
                f"UPDATE share_queue SET tries = tries + 1 WHERE id IN ({ids})")  # noqa: S608
        log.debug("shared picks did not go: %s", type(exc).__name__)
        return {"sent": 0, "reason": "unreachable"}

    with contextlib.suppress(Exception):
        db.execute(f"DELETE FROM share_queue WHERE id IN ({ids})")  # noqa: S608
    return {"sent": len(rows), "reason": "ok"}


def forget(*, timeout: float = 15.0) -> dict:
    """Ask the server to delete everything it holds for this installation.

    The licence key goes with this request, as it does with a batch of picks.
    Without it anybody who learned an id could delete somebody else's record,
    and an id is not a secret -- it is on the leaderboard.
    """
    from . import licensing

    pid = picker_id()
    key = licensing.saved_key()
    server = licensing.server_url()
    with contextlib.suppress(Exception):
        db.execute("DELETE FROM share_queue")
    if not server:
        return {"ok": False, "reason": "no_server",
                "message": "This build has no server to delete from — nothing "
                           "of yours has ever been sent."}
    if not pid or not key:
        return {"ok": False, "reason": "no_key",
                "message": "Without a licence key there is nothing to delete: "
                           "picks are only ever shared under a key."}
    try:
        import httpx

        with httpx.Client(timeout=timeout) as client:
            response = client.request(
                "DELETE", f"{server}/v1/picks",
                json={"picker": pid, "license_key": key})
        if response.status_code == 403:
            return {"ok": False, "reason": "refused",
                    "message": "The server would not accept that as proof of "
                               "who you are. Check the licence key is the one "
                               "this app activated with."}
        response.raise_for_status()
        body = response.json()
    except Exception as exc:                                  # noqa: BLE001
        return {"ok": False, "reason": "unreachable",
                "message": f"Could not reach the server ({type(exc).__name__}). "
                           f"Nothing has been deleted — try again later."}
    return {"ok": True, "deleted": int((body or {}).get("deleted") or 0)}


def state() -> dict:
    """Everything the Settings page and the notice need, in one call."""
    queued = 0
    with contextlib.suppress(Exception):
        queued = int(db.query_one(
            "SELECT COUNT(*) AS n FROM share_queue")["n"])
    return {
        "enabled": enabled(),
        "chosen": chosen(),
        "default_on": DEFAULT_ON,
        "needs_notice": needs_notice(),
        "notice_version": NOTICE_VERSION,
        "notice_seen": notice_seen(),
        "picker_id": picker_id(),
        "display_name": display_name(),
        "default_name": default_name(),
        "queued": queued,
        "can_send": bool(picker_id()),
    }


# ------------------------------------------------------------- the sender

class Sender:
    """A thread that empties the queue and otherwise does nothing.

    Its own thread because the one thing sharing must never do is make the app
    slower: a pick is written to the database and the request returns, and
    whether the network is up is somebody else's problem sixty seconds later.
    """

    def __init__(self, interval: float = FLUSH_SECONDS) -> None:
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="share-sender",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            with contextlib.suppress(Exception):
                flush()


def _flush_quietly() -> None:
    with contextlib.suppress(Exception):
        flush()


def nudge() -> None:
    """Try the queue now, off the request thread.

    Called after a pick is saved, so a pick made with the network up is shared
    in the second it was made rather than at the next wake. It is a thread
    because the alternative is a pick button that waits on a round trip.
    """
    threading.Thread(target=_flush_quietly, daemon=True).start()
