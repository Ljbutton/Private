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
             "content": "Current state:\\n" + json.dumps(context(season, week))},
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
    return {
        "reply": (choices[0].get("message") or {}).get("content", ""),
        "model": body.get("model", state["model"]),
    }
