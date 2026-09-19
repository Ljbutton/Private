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
the user's choice.

No weights ship inside this program -- it would be a gigabyte of download for
a tab a buyer may never open -- but the app can fetch and run them itself; see
``localmodel``. A user who already runs their own model server keeps it: this
only ever installs into its own directory, on its own port.
"""

from __future__ import annotations

import ipaddress
import json
import re
from urllib.parse import urlparse

SYSTEM_PROMPT = """You are the analyst sitting beside an NFL model, talking to \
the person who built it. You are given that model's current state as JSON: the \
season so far, every team's rating and projection, this week's games, what the \
model projects, what the market says, and how the model has actually scored.

Rules you do not break:

- Answer only from the data you are given. If it is not in there, say so. Never \
invent a line, a score, an injury or a statistic.
- "This week" means the week in `current_week`, and its games are the ones in \
`this_week`. `results_so_far` is earlier weeks and is history. Never add the \
two together or describe a finished game as part of this week.
- Most of this week has usually not been played. `this_week.state` says how \
much of it has; read it before writing a word about how the week is going, and \
never imply a game has a result when it has not.
- Every record has a period attached, and you always name it. \
`record_for_the_whole_season_so_far` is the season, not this week -- quoting \
it as "so far" when asked about this week is the one mistake to avoid here.
- The blind projection is the model before it sees the betting line; the blend \
is after. The fitted market weight is high, so the blend is mostly the market. \
Be honest about that when asked whether the model is any good.
- How the model measures up against the closing line is whatever the \
performance data you were given says it is. Read it off there if you are asked; \
never recite a rate from memory. Do not encourage betting on an edge, and say \
plainly when a number is inside the noise.

How to answer:

- Give the answer first. No preamble, no restating the question, no describing \
what you are about to do.
- Do not show your working. Never write "Thinking Process", "Let me analyse", \
"Step 1", "First, I will" or anything of that shape. The reader wants the \
conclusion and the numbers behind it, not the route you took.
- Asked for a list, give the list. A short line each, with the number that \
justifies it. No summary paragraph afterwards.
- Be brief. Numbers over adjectives. Under 150 words unless asked for more.
/no_think
"""

# That last line is not an instruction the model is asked to follow. Asking a
# reasoning model not to reason does not work -- the prompt above already asks
# twice, and the reported symptom was it writing "Thinking Process:" and a
# numbered plan anyway. `/no_think` is a switch the Qwen chat template reads:
# with it present the template closes the thinking block before generation
# starts, so there is no ramble to leak and none of the tokens it would have
# cost.
#
# The token part is the real fix. The answer was stopping mid-sentence because
# the whole budget had gone on working-out and the answer itself never got
# written, and no amount of stripping afterwards recovers an answer that was
# never generated.

# Sent as parameters as well, for servers that read the switch there instead.
# Ollama and llama.cpp both accept these; a server that knows neither ignores
# both, which is why they can be sent unconditionally.
NO_THINKING = {"chat_template_kwargs": {"enable_thinking": False}, "think": False}

# Enough for a ranked list with a line of reasoning each, and not enough for an
# essay. A cap is also a latency ceiling, which is the point.
MAX_TOKENS = 700

# How many completed games to hand over. The season's results are what lets it
# answer anything beyond this Sunday; all of them by January is ten kilobytes
# of JSON, which on a 4B model is context better spent on the question.
RESULT_LIMIT = 120


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
    from . import localmodel

    url, model = _endpoint(), _model_name()
    # A server this app installed does not need to be running all the time --
    # it is started when the tab is opened and left to Ollama's own idle
    # unload after that. Doing it here rather than at launch keeps a feature
    # nobody is using out of the machine's memory.
    if url.startswith(localmodel.ENDPOINT.rstrip("/")) and not localmodel.serving(1.0):
        localmodel.start(wait=25.0)
    if not url:
        return {"ready": False, "reason": "no_endpoint", "setup_offered": True,
                "message": "The assistant needs a model on this machine, and "
                           "there is not one yet. Setting it up is one button."}
    if not _is_loopback(url):
        return {"ready": False, "reason": "not_local",
                "message": f"{url} is not on this machine. The assistant only talks "
                           "to a local model, so this address is refused — change it "
                           "to a 127.0.0.1 address on the Settings page."}
    if not model:
        return {"ready": False, "reason": "no_model", "setup_offered": True,
                "message": "A model server is configured but no model is "
                           "chosen. Pick one below, or name one on Settings."}

    try:
        import httpx

        response = httpx.get(f"{url}/models", timeout=5.0)
        response.raise_for_status()
        names = [m.get("id") for m in (response.json().get("data") or [])]
    except Exception as exc:                                  # noqa: BLE001
        return {"ready": False, "reason": "unreachable",
                "setup_offered": localmodel.binary() is None,
                "message": f"Nothing is answering at {url} ({exc}). Is the model "
                           "server running?"}

    if names and model not in names:
        return {"ready": False, "reason": "model_missing", "models": names,
                "setup_offered": True,
                "message": f"{model} is not loaded there. Available: "
                           f"{', '.join(str(n) for n in names[:8])}."}
    return {"ready": True, "endpoint": url, "model": model, "models": names}


def context(season: int, week: int) -> dict:
    """The slice of the app the model is allowed to see.

    It used to be this week's sixteen games and nothing else, which made every
    question about the season unanswerable -- "who are the best five teams" got
    a reading of one Sunday. It now carries the whole table, the results so far
    and what the model is built out of, and each part is trimmed hard: a small
    model given full JSON spends its context window on field names.
    """
    from . import db, scoreboard
    from .api import game_cards, latest_team_rows, team_rank_order
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

    _played = sum(1 for g in games if str(g.get("status")) == "final")

    # Every team, in our order, as one line each.
    #
    # This was thirty-two twelve-key objects and seven and a half thousand
    # characters -- over half the whole prompt, and on a four-billion-parameter
    # model running on somebody's laptop the prompt is most of the wait. The
    # same numbers as a labelled line read just as well and cost a third as
    # much, which is the difference between an answer in ten seconds and one in
    # forty. The key order is fixed and documented in `teams_columns` so the
    # model never has to infer what a number is.
    ratings = latest_team_rows(season, "team_ratings")
    projections = latest_team_rows(season, "season_projections")
    teams = []
    for rank, abbr in enumerate(team_rank_order(season, ratings, projections), start=1):
        rating = ratings.get(abbr) or {}
        projection = projections.get(abbr) or {}
        span = ("?" if projection.get("wins_p10") is None
                else f"{int(projection['wins_p10'])}-{int(projection['wins_p90'])}")
        teams.append(
            f"{rank} {abbr} "
            f"{int(projection.get('wins_actual') or 0)}-"
            f"{int(projection.get('losses_actual') or 0)} "
            f"pow {_round(rating.get('power'), 1)} "
            f"pyth {_round(rating.get('pythagorean'), 2)} "
            f"proj {_round(projection.get('exp_wins'), 1)} ({span}) "
            f"playoff {_round(projection.get('playoff_prob'), 2)} "
            f"div {_round(projection.get('division_prob'), 2)} "
            f"title {_round(projection.get('sb_prob'), 3)}"
        )

    # The season's results, as short strings. A game is "W3 KC 27-20 DEN",
    # which a model reads as well as a nested object and at a fifth the tokens.
    results = [
        f"W{r['week']} {r['away']} {int(r['away_score'])}-"
        f"{int(r['home_score'])} {r['home']}"
        for r in db.query(
            "SELECT week, home, away, home_score, away_score FROM games "
            "WHERE season = ? AND status = 'final' AND season_type = 'REG' "
            "AND home_score IS NOT NULL ORDER BY week DESC, game_id LIMIT ?",
            (season, RESULT_LIMIT))
    ]

    report = load_report() or {}
    return {
        "season": season,
        "current_week": week,
        # Spelled out, because the model got this wrong in exactly the way an
        # unlabelled structure invites: asked to summarise "this week" it
        # counted seventeen games, which is this week's sixteen plus the whole
        # of last week's results sitting in the same payload. The two lists are
        # different questions and the field names alone did not say so.
        "how_to_read_this": (
            f"'this_week' is week {week} of the {season} season and is the only "
            f"thing 'this week' ever means. Those games have not been played "
            f"unless a score is shown. 'results_so_far' is every earlier week, "
            f"already finished, and is history -- never count it as part of this "
            f"week. Each line there starts with its week number."),
        "how_the_model_works": {
            "inputs": [
                "Elo rating, shrunk toward the mean between seasons",
                "Pythagorean expectation from points scored and allowed",
                "EPA efficiency per play, offence and defence, where "
                "play-by-play exists",
                "rest days, travel distance, and neutral or home site",
                "weather at kickoff for outdoor venues",
                "the quarterback who has been starting, valued in points",
                "the sportsbook consensus line, which the blend leans on",
            ],
            "blind_vs_blend": "blind is the model alone; blend is after the "
                              "market is weighed in, at the fitted weight below",
            "fitted_market_weight": report.get("market_weight"),
            "trained_on": report.get("n_games"),
            "ranking_inputs": "projected wins, Pythagorean, rating, title odds "
                              "and the floor of the 80% range — not record",
        },
        "teams_columns": "rank abbr record pow pyth proj (80% range) "
                         "playoff div title",
        "teams": teams,
        # How much of this week has actually happened, counted rather than left
        # to be inferred from which lines carry a score. Asked to summarise
        # "this week" in week two, the model answered with the season-to-date
        # record -- seventeen graded games, sixteen of them from week one --
        # and never said which period it meant. The number was right and the
        # answer was wrong, which is the failure a labelled field prevents and
        # a well-named one does not.
        "this_week": {
            "week": week,
            "n_games": len(games),
            "n_played": _played,
            "n_still_to_play": len(games) - _played,
            "state": (f"{_played} of {len(games)} games in week {week} have "
                      f"finished" if _played else
                      f"none of week {week}'s {len(games)} games have been "
                      f"played yet"),
            "games": games,
        },
        "results_so_far": {"weeks_finished": sorted(
            {int(r.split()[0][1:]) for r in results if r[:1] == "W"}),
            "n_games": len(results), "games": results},
        "model_record": {
            "walk_forward_margin_mae": _round((report.get("blind") or {}).get("margin_mae"), 2),
            "closing_line_margin_mae": _round(
                (report.get("blind") or {}).get("market_margin_mae"), 2),
            "walk_forward_ats_rate": _round((report.get("blind") or {}).get("ats_rate"), 3),
            "straight_up_rate": _round((report.get("blind") or {}).get("su_rate"), 3),
            "note": "An ATS rate below 0.524 loses money at standard juice.",
        },
        # Named for what it covers. It was "season_scoreboard", which is
        # accurate and was read as "this week" anyway.
        "record_for_the_whole_season_so_far": {
            "covers": (f"every game graded so far this season -- weeks 1 to "
                       f"{week} -- and NOT week {week} on its own"),
            **scoreboard.report(season)["totals"]["all"],
        },
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


# Thinking that is not in a tag at all. Some models simply write the essay:
# "Thinking Process:" then a numbered plan, then -- eventually -- the answer.
# There is no marker to strip, so the two ends are found instead: where the
# ramble starts, and where the answer does.
_PREAMBLE = re.compile(
    r"^\s*(?:#{1,6}\s*)?\**\s*(thinking(?:\s+process)?|thought process|"
    r"reasoning|analysis|my approach|let me (?:think|analy[sz]e|work)|"
    r"step\s*1|first,? (?:i|let)|i (?:need|should|will) to?)\b",
    re.I)
_ANSWER_MARK = re.compile(
    r"^\s*(?:#{1,6}\s*)?\**\s*(?:final\s+)?(?:answer|response|"
    r"here(?:'s| is)\b|summary|conclusion|recommendation)s?\b[:*]*\s*$",
    re.I | re.M)


def _strip_untagged_thinking(text: str) -> str:
    """Drop a reasoning preamble a model wrote as ordinary prose.

    Deliberately conservative: it only fires when the text *opens* like
    working-out, and it only keeps a later section when that section is clearly
    labelled as the answer. A model that simply answered is never touched,
    because cutting a real answer in half is far worse than leaving a tidy
    preamble in place.
    """
    if not _PREAMBLE.match(text):
        return text
    marks = list(_ANSWER_MARK.finditer(text))
    if marks:
        answer = text[marks[-1].end():].strip()
        if answer:
            return answer
    # No labelled answer: keep the last paragraph block, which is where a
    # model that thinks out loud puts the conclusion.
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    return blocks[-1] if len(blocks) > 1 else text


def _is_thinking(text: str) -> bool:
    """Whether this text is working-out rather than an answer."""
    return bool(_PREAMBLE.match(text)) and not _ANSWER_MARK.search(text)


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
        # A model that ran out of room mid-thought has no answer in it, and the
        # last paragraph of an unfinished plan is not one -- it is a fragment
        # of somebody's notes, which is exactly what was on screen when this
        # was reported. Say what happened instead.
        if (choice.get("finish_reason") or "") == "length" and _is_thinking(answer):
            return ("The model spent its whole answer thinking and ran out of room "
                    "before writing one. Ask again, or ask something narrower.")
        return _strip_untagged_thinking(answer)
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


def _ollama_base(url: str) -> str | None:
    """The native Ollama root behind an OpenAI-compatible URL, if it is one.

    Ollama serves both: `/v1/chat/completions` for compatibility and
    `/api/chat` for everything the compatibility layer cannot express. The
    switch that turns a reasoning model's thinking off is one of those things.
    """
    url = (url or "").rstrip("/")
    return url[: -len("/v1")] if url.endswith("/v1") else None


def _ask_ollama(base: str, payload: dict, timeout: float) -> dict | None:
    """Ask Ollama directly. Returns None if this is not Ollama after all.

    Worth the extra code path because it is the only one that actually works.
    `/no_think` is a Qwen *3* template convention and this model ignored it;
    the OpenAI-compatible endpoint drops `think` because it is not part of that
    API; and no amount of asking in the prompt stops a reasoning model
    reasoning -- it was asked twice, in words, and still opened with "Thinking
    Process:".

    Here `think: false` is a real parameter, and Ollama returns any reasoning in
    `message.thinking`, separate from `message.content`. Separate is the whole
    point: there is nothing to strip, and a model that thinks anyway cannot
    spend the answer's tokens doing it.
    """
    import httpx

    native = {
        "model": payload["model"],
        "messages": payload["messages"],
        "stream": False,
        "think": False,
        "options": {"temperature": 0.2, "num_predict": MAX_TOKENS},
    }
    try:
        response = httpx.post(f"{base}/api/chat", json=native, timeout=timeout)
    except Exception:                                         # noqa: BLE001
        return None
    if response.status_code == 404:
        return None                     # not Ollama; the caller falls back
    response.raise_for_status()
    body = response.json()
    message = body.get("message") or {}
    # `thinking` is deliberately never read. It is the model's working-out and
    # the reader asked for an answer.
    answer = _STRAY_TAG.sub("", _THINK.sub("", str(message.get("content") or ""))).strip()
    if answer and body.get("done_reason") == "length" and _is_thinking(answer):
        answer = ("The model spent its whole answer thinking and ran out of room "
                  "before writing one. Ask again, or ask something narrower.")
    elif answer:
        answer = _strip_untagged_thinking(answer)
    if not answer:
        raise AssistantError(
            f"The model returned an empty answer (done_reason: "
            f"{body.get('done_reason') or 'unknown'}).")
    return {"reply": answer, "model": body.get("model", payload["model"])}


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
        # A ceiling on the answer is also a ceiling on how long it takes.
        "max_tokens": MAX_TOKENS,
        # Servers that do not know these ignore them, which is the whole reason
        # they can be sent unconditionally.
        **NO_THINKING,
    }

    import httpx

    # Ollama's own endpoint first, because it is the one that can be told not
    # to think. Anything else falls through to the compatible API below.
    base = _ollama_base(state["endpoint"])
    if base:
        answered = _ask_ollama(base, payload, timeout)
        if answered is not None:
            return answered

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
