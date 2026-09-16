"""Talk to a local model about this app's own numbers.

Deliberately not a chatbot with a football opinion. The model is given the
app's current state — the board, the model's measured record, the scoreboard —
and asked about *that*. A language model does not know who is favoured this
week and cannot be told to guess; what it is good at is reading a page of
numbers you already trust and answering questions about them.

**Nothing leaves the machine.** That is enforced, not promised: the endpoint
must resolve to loopback, and a configured address that does not is refused
with an explanation rather than quietly dialled. The app speaks the OpenAI chat
API, which both Ollama and llama.cpp's `llama-server` expose, so the model is
the user's choice and no weights ship with this program.
"""

from __future__ import annotations

import ipaddress
import json
import re
from urllib.parse import urlparse

SYSTEM_PROMPT = """You are the analyst sitting beside an NFL model, talking to \
the person who built it. You are given that model's current state as JSON: this \
week's games, what the model projects, what the market says, and how the model \
has actually scored.

Rules you do not break:

- Answer only from the data you are given. If it is not in there, say so. Never \
invent a line, a score, an injury or a statistic.
- The blind projection is the model before it sees the betting line; the blend \
is after. The fitted market weight is high, so the blend is mostly the market. \
Be honest about that when asked whether the model is any good.
- This model does not beat the closing line. Its measured ATS rate is around \
51% against a 52.4% break-even. Do not encourage betting on its edges, and say \
plainly when a number is inside the noise.
- Be brief. Numbers over adjectives.
"""


class AssistantError(RuntimeError):
    """Something the user can act on, phrased for them rather than for a log."""


def _endpoint() -> str:
    import os

    return (os.environ.get("NFLPICKER_LLM_URL") or "").strip().rstrip("/")


def _model_name() -> str:
    import os

    return (os.environ.get("NFLPICKER_LLM_MODEL") or "").strip()


def _is_loopback(url: str) -> bool:
    """Is this address on this machine?

    The promise the Assistant tab makes is that the conversation never leaves
    the machine, and a promise that depends on the user pasting the right thing
    is not a promise. Anything that is not loopback is refused.
    """
    host = (urlparse(url).hostname or "").strip("[]")
    if host in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def status() -> dict:
    """Is the assistant usable, and if not, what is missing."""
    url, model = _endpoint(), _model_name()
    if not url:
        return {"ready": False, "reason": "no_endpoint",
                "message": "No local model endpoint is configured. Install Ollama, "
                           "run `ollama pull qwen3.5:4b`, then set the endpoint to "
                           "http://127.0.0.1:11434/v1 on the Settings page."}
    if not _is_loopback(url):
        return {"ready": False, "reason": "not_local",
                "message": f"{url} is not on this machine. The assistant only talks "
                           "to a local model, so this address is refused — change it "
                           "to a 127.0.0.1 address on the Settings page."}
    if not model:
        return {"ready": False, "reason": "no_model",
                "message": "No model name is set. Put the name you pulled "
                           "(for example qwen3.5:4b) on the Settings page."}

    try:
        import httpx

        response = httpx.get(f"{url}/models", timeout=5.0)
        response.raise_for_status()
        names = [m.get("id") for m in (response.json().get("data") or [])]
    except Exception as exc:                                  # noqa: BLE001
        return {"ready": False, "reason": "unreachable",
                "message": f"Nothing is answering at {url} ({exc}). Is the model "
                           "server running?"}

    if names and model not in names:
        return {"ready": False, "reason": "model_missing", "models": names,
                "message": f"{model} is not loaded there. Available: "
                           f"{', '.join(str(n) for n in names[:8])}."}
    return {"ready": True, "endpoint": url, "model": model, "models": names}


def context(season: int, week: int) -> dict:
    """The slice of the app the model is allowed to see.

    Trimmed hard on purpose: a small model given sixteen games of full JSON
    spends its whole context window on field names. This is the board reduced
    to what a question is likely to be about.
    """
    from . import scoreboard
    from .api import game_cards
    from .ml.train import load_report

    games = []
    for card in game_cards(season, week):
        prediction = card.get("prediction") or {}
        market = card.get("market") or {}
        games.append({
            "game": f"{card['away']} @ {card['home']}",
            "status": card.get("status"),
            "score": (None if card.get("home_score") is None
                      else f"{card['away']} {card['away_score']} – "
                           f"{card['home']} {card['home_score']}"),
            "blind_margin_home": _round(prediction.get("margin_home")),
            "blend_margin_home": _round(prediction.get("fair_margin")),
            "our_home_win_prob": _round(prediction.get("home_win_prob"), 3),
            "book_spread_home": _round(market.get("spread_home")),
            "book_home_win_prob": _round(market.get("home_win_prob"), 3),
            "book_total": _round(market.get("total_points")),
            "line_moved_toward_us": _round((card.get("movement") or {}).get("toward_us")),
        })

    report = load_report() or {}
    return {
        "season": season,
        "week": week,
        "games": games,
        "model_record": {
            "walk_forward_margin_mae": _round((report.get("blind") or {}).get("margin_mae"), 2),
            "closing_line_margin_mae": _round(
                (report.get("blind") or {}).get("market_margin_mae"), 2),
            "walk_forward_ats_rate": _round((report.get("blind") or {}).get("ats_rate"), 3),
            "straight_up_rate": _round((report.get("blind") or {}).get("su_rate"), 3),
            "fitted_market_weight": report.get("market_weight"),
            "note": "An ATS rate below 0.524 loses money at standard juice.",
        },
        "season_scoreboard": scoreboard.report(season)["totals"]["all"],
    }


def _round(value, places: int = 1):
    try:
        return None if value is None else round(float(value), places)
    except (TypeError, ValueError):
        return None




# Reasoning models are the normal case at this size, and they do not put their
# answer where a plain chat model does. Depending on the server, the thinking
# arrives wrapped in <think> tags inside `content`, or in a sibling field with
# `content` left empty. Reading `content` alone got an empty string, and an
# empty string is not an error -- so the app rendered an empty bubble and said
# nothing at all about why.
_THINK = re.compile(r"<(think|thinking|reasoning)>.*?</\1>", re.S | re.I)
_STRAY_TAG = re.compile(r"</?(think|thinking|reasoning)>", re.I)

# Ollama, llama.cpp and the OpenAI-compatible servers have each picked a
# different name for the same field.
_REASONING_KEYS = ("reasoning_content", "reasoning", "thinking")


def _reply_from(choice: dict) -> str:
    """The answer, with any thinking removed -- or the thinking if that is all
    there is, because a visible ramble beats a blank bubble."""
    message = choice.get("message") or {}
    content = str(message.get("content") or "")
    # Complete blocks go first, then any stray tag left by a model that ran out
    # mid-thought -- keeping what it did say beats showing nothing, but showing
    # it still wearing a `<think>` tag is just the bug with extra steps.
    answer = _STRAY_TAG.sub("", _THINK.sub("", content)).strip()
    if answer:
        return answer
    for key in _REASONING_KEYS:
        value = str(message.get(key) or "").strip()
        if value:
            return value
    return ""


def _why_empty(choice: dict) -> str:
    """Say what the server actually reported, rather than 'no answer'."""
    reason = choice.get("finish_reason") or choice.get("native_finish_reason")
    if reason == "length":
        return ("The model ran out of room before it finished answering. This "
                "usually means a reasoning model spent its whole budget "
                "thinking -- try a shorter question, or a non-reasoning model.")
    if reason:
        return f"The model stopped without answering (finish_reason: {reason})."
    return "The model returned an empty answer."


def ask(messages: list[dict], season: int, week: int, *, timeout: float = 120.0) -> dict:
    """Send a conversation to the local model with the board attached."""
    state = status()
    if not state.get("ready"):
        raise AssistantError(state["message"])

    payload = {
        "model": state["model"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "system",
             "content": "Current state:\n" + json.dumps(context(season, week))},
            *[{"role": m.get("role", "user"), "content": str(m.get("content", ""))}
              for m in messages[-12:]],
        ],
        "temperature": 0.2,
        "stream": False,
    }

    import httpx

    try:
        response = httpx.post(
            f"{state['endpoint']}/chat/completions", json=payload, timeout=timeout)
        response.raise_for_status()
        body = response.json()
    except Exception as exc:                                  # noqa: BLE001
        raise AssistantError(f"The model did not answer: {exc}") from exc

    choices = body.get("choices") or []
    if not choices:
        raise AssistantError("The model returned nothing.")
    reply = _reply_from(choices[0])
    if not reply:
        raise AssistantError(_why_empty(choices[0]))
    return {"reply": reply, "model": body.get("model", state["model"])}


# ------------------------------------------------------------------ chats
# Conversations, kept like any other app keeps them: many of them, named, and
# still there tomorrow. A single running log meant every question shared one
# context -- asking about week 3 after twenty lines about week 2 fed the model
# all twenty -- and there was no way to put a thread aside and come back.

def _new_id() -> str:
    import uuid

    return uuid.uuid4().hex[:12]


def list_chats() -> list[dict]:
    """Newest activity first, with a count and a preview."""
    from . import db

    return db.query(
        "SELECT c.id, c.title, c.created_at, c.updated_at,"
        "  (SELECT COUNT(*) FROM chat_messages m WHERE m.chat_id = c.id) AS n,"
        "  (SELECT m.content FROM chat_messages m WHERE m.chat_id = c.id"
        "     ORDER BY m.id LIMIT 1) AS opener "
        "FROM chats c ORDER BY c.updated_at DESC"
    )


def create_chat(title: str = "New chat") -> dict:
    from . import db
    from .util import now_iso

    stamp = now_iso()
    chat_id = _new_id()
    db.execute(
        "INSERT INTO chats(id, title, created_at, updated_at) VALUES(?,?,?,?)",
        (chat_id, (title or "New chat").strip()[:80] or "New chat", stamp, stamp),
    )
    return {"id": chat_id, "title": title, "created_at": stamp,
            "updated_at": stamp, "messages": []}


def chat_messages(chat_id: str) -> list[dict]:
    from . import db

    return db.query(
        "SELECT role, content, created_at FROM chat_messages "
        "WHERE chat_id = ? ORDER BY id", (chat_id,)
    )


def rename_chat(chat_id: str, title: str) -> dict:
    from . import db
    from .util import now_iso

    clean = (title or "").strip()[:80]
    if not clean:
        raise AssistantError("A chat needs a name.")
    db.execute("UPDATE chats SET title = ?, updated_at = ? WHERE id = ?",
               (clean, now_iso(), chat_id))
    return {"id": chat_id, "title": clean}


def delete_chat(chat_id: str) -> dict:
    from . import db

    # The cascade is declared, but SQLite only honours it with foreign keys
    # switched on -- which is a per-connection pragma, not a schema property.
    # Deleting the messages explicitly is one line and cannot silently leak.
    db.execute("DELETE FROM chat_messages WHERE chat_id = ?", (chat_id,))
    db.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
    return {"deleted": chat_id}


def append_message(chat_id: str, role: str, content: str) -> None:
    from . import db
    from .util import now_iso

    stamp = now_iso()
    db.execute(
        "INSERT INTO chat_messages(chat_id, role, content, created_at) VALUES(?,?,?,?)",
        (chat_id, role, content, stamp),
    )
    db.execute("UPDATE chats SET updated_at = ? WHERE id = ?", (stamp, chat_id))


def title_from(question: str) -> str:
    """A name from the first thing asked, so the list is readable without
    anyone having to name anything."""
    clean = " ".join((question or "").split())
    if not clean:
        return "New chat"
    return clean[:48] + ("…" if len(clean) > 48 else "")
