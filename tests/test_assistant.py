"""The local assistant, and the promise that nothing leaves the machine.

That promise is the reason this feature is acceptable at all, so it is the part
worth testing hardest: a guarantee that depends on the user pasting the right
address is not a guarantee.
"""

import pytest

from nflpicker import assistant


@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.delenv("NFLPICKER_LLM_URL", raising=False)
    monkeypatch.delenv("NFLPICKER_LLM_MODEL", raising=False)
    yield monkeypatch


# ------------------------------------------------------- the offline promise

@pytest.mark.parametrize("url", [
    "http://127.0.0.1:11434/v1",
    "http://localhost:11434/v1",
    "http://[::1]:8080/v1",
    "http://127.0.0.2:11434/v1",      # the whole 127/8 block is loopback
])
def test_addresses_on_this_machine_are_allowed(url):
    assert assistant._is_loopback(url) is True


@pytest.mark.parametrize("url", [
    "https://api.openai.com/v1",
    "http://192.168.1.50:11434/v1",   # the LAN is not this machine
    "http://10.0.0.5/v1",
    "http://example.com/v1",
    "http://0x7f000001/v1",           # obfuscated loopback is still not parsed
    "",
])
def test_anything_else_is_refused(url):
    assert assistant._is_loopback(url) is False


def test_a_remote_endpoint_is_refused_with_a_reason_not_dialled(clean_env):
    """The failure a user can act on: it names the address and says what to do."""
    clean_env.setenv("NFLPICKER_LLM_URL", "https://api.openai.com/v1")
    clean_env.setenv("NFLPICKER_LLM_MODEL", "gpt-4")

    state = assistant.status()
    assert state["ready"] is False
    assert state["reason"] == "not_local"
    assert "not on this machine" in state["message"]


def test_ask_refuses_before_sending_anything(clean_env, monkeypatch):
    """Not merely reported as unavailable -- no request is made at all."""
    clean_env.setenv("NFLPICKER_LLM_URL", "https://api.openai.com/v1")
    clean_env.setenv("NFLPICKER_LLM_MODEL", "gpt-4")

    import httpx

    def explode(*args, **kwargs):                       # pragma: no cover
        raise AssertionError("the assistant made a network call")

    monkeypatch.setattr(httpx, "post", explode)
    monkeypatch.setattr(httpx, "get", explode)

    with pytest.raises(assistant.AssistantError, match="not on this machine"):
        assistant.ask([{"role": "user", "content": "hi"}], 2026, 1)


# ------------------------------------------------------------ what it tells you

def test_no_endpoint_offers_to_set_one_up(clean_env):
    """This used to tell the user to go and install Ollama, run a command and
    paste an address back. The app does that itself now, so what the page needs
    from here is permission to show the button -- and a message that does not
    send a paying customer to a terminal."""
    state = assistant.status()
    assert state["reason"] == "no_endpoint"
    assert state["setup_offered"] is True
    assert "ollama pull" not in state["message"].lower()


def test_an_endpoint_without_a_model_says_which_half_is_missing(clean_env):
    clean_env.setenv("NFLPICKER_LLM_URL", "http://127.0.0.1:11434/v1")
    assert assistant.status()["reason"] == "no_model"


def test_an_unreachable_local_server_is_reported_as_unreachable(clean_env):
    # Nothing is listening on this port; the point is which reason comes back.
    clean_env.setenv("NFLPICKER_LLM_URL", "http://127.0.0.1:1/v1")
    clean_env.setenv("NFLPICKER_LLM_MODEL", "qwen3.5:4b")
    assert assistant.status()["reason"] == "unreachable"


# --------------------------------------------------------------- the prompt

def test_the_model_is_told_not_to_talk_anyone_into_a_losing_bet():
    """Still told not to sell an edge -- but no longer told what the rate is.

    A hardcoded "51% against a 52.4% break-even" went stale the first time the
    model was retrained, and an assistant reciting last season's figure at
    someone looking at this season's Performance page is worse than one that
    reads the number in front of it. So the rate comes out of the data and the
    restraint stays in the prompt.
    """
    prompt = assistant.SYSTEM_PROMPT
    assert "Do not encourage betting on an edge" in prompt
    assert "inside the noise" in prompt
    assert "never recite a rate from memory" in prompt
    assert "51%" not in prompt and "52.4%" not in prompt
    assert "Answer only from the data" in prompt


def test_rounding_survives_the_values_a_missing_field_produces():
    assert assistant._round(None) is None
    assert assistant._round("not a number") is None
    assert assistant._round(3.14159, 2) == 3.14


# ------------------------------------------- reasoning models answer oddly

@pytest.mark.parametrize("message, expected", [
    ({"content": "Blind is 51.0% ATS."}, "Blind is 51.0% ATS."),
    ({"content": "<think>weigh it up</think>\n\nMediocre."}, "Mediocre."),
    ({"content": "<THINK>caps</THINK>Still answered."}, "Still answered."),
    ({"content": "<think>ran out mid-thought"}, "ran out mid-thought"),
    ({"content": "", "reasoning_content": "all I have"}, "all I have"),
    ({"content": "", "reasoning": "all I have"}, "all I have"),
    ({"content": None, "thinking": "all I have"}, "all I have"),
    ({"content": ""}, ""),
    ({}, ""),
])
def test_the_answer_is_found_whichever_field_carries_it(message, expected):
    """A 4B model is a reasoning model, and none of them agree on where the
    answer goes: inside <think> tags in `content`, or in a sibling field with
    `content` left empty. Reading `content` alone returned an empty string --
    and an empty string is not an error, so the UI drew an empty bubble and
    said nothing about why."""
    assert assistant._reply_from({"message": message}) == expected


def test_running_out_of_room_says_so_rather_than_going_blank():
    assert "ran out of room" in assistant._why_empty(
        {"message": {"content": ""}, "finish_reason": "length"})
    assert "content_filter" in assistant._why_empty(
        {"message": {"content": ""}, "finish_reason": "content_filter"})
    assert assistant._why_empty({"message": {}}) == "The model returned an empty answer."


def test_an_empty_answer_is_raised_not_returned(monkeypatch):
    """The blank bubble was the whole bug: the caller must get an error."""
    a = assistant

    monkeypatch.setattr(a, "status", lambda: {
        "ready": True, "endpoint": "http://127.0.0.1:11434/v1", "model": "m"})
    monkeypatch.setattr(a, "context", lambda season, week: {})

    class _Resp:
        # 404 so the native Ollama probe falls through: this test is about the
        # OpenAI-compatible path, which is what a server that is not Ollama
        # leaves us with.
        status_code = 404

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": ""},
                                 "finish_reason": "length"}]}

    import httpx

    def post(url, *args, **kwargs):
        resp = _Resp()
        if url.endswith("/chat/completions"):
            resp.status_code = 200
        return resp

    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(a.AssistantError, match="ran out of room"):
        a.ask([{"role": "user", "content": "hi"}], 2026, 2)


def test_the_context_header_is_a_real_newline():
    """It was written `\\\\n` in the source, so the model received a literal
    backslash-n glued to the JSON rather than a line break."""
    import inspect

    import nflpicker.assistant as a

    assert "Current state:\\\\n" not in inspect.getsource(a.ask)


# ------------------------------------------------------ many conversations

def test_chats_are_created_listed_renamed_and_deleted(temp_env):
    from nflpicker import assistant, db

    db.connect()
    assert assistant.list_chats() == []

    first = assistant.create_chat("Week 2")
    second = assistant.create_chat("Survivor")
    assert {c["id"] for c in assistant.list_chats()} == {first["id"], second["id"]}

    assistant.rename_chat(first["id"], "Week 2 board")
    titles = {c["id"]: c["title"] for c in assistant.list_chats()}
    assert titles[first["id"]] == "Week 2 board"

    assistant.delete_chat(second["id"])
    assert [c["id"] for c in assistant.list_chats()] == [first["id"]]


def test_deleting_a_chat_takes_its_messages_with_it(temp_env):
    """The cascade is declared, but SQLite only honours it with foreign keys
    switched on -- a per-connection pragma, not a schema property. Orphaned
    rows would accumulate invisibly."""
    from nflpicker import assistant, db

    db.connect()
    chat = assistant.create_chat("scratch")
    assistant.append_message(chat["id"], "user", "hello")
    assistant.append_message(chat["id"], "assistant", "hi")
    assert len(assistant.chat_messages(chat["id"])) == 2

    assistant.delete_chat(chat["id"])
    left = db.query_one("SELECT COUNT(*) AS n FROM chat_messages WHERE chat_id = ?",
                        (chat["id"],))
    assert left["n"] == 0


def test_a_conversation_keeps_its_order(temp_env):
    from nflpicker import assistant, db

    db.connect()
    chat = assistant.create_chat("ordered")
    for i in range(5):
        assistant.append_message(chat["id"], "user", f"q{i}")
        assistant.append_message(chat["id"], "assistant", f"a{i}")
    messages = assistant.chat_messages(chat["id"])
    assert [m["content"] for m in messages] == [
        x for i in range(5) for x in (f"q{i}", f"a{i}")]


def test_chats_do_not_share_a_transcript(temp_env):
    """The reason for having more than one: a question about week 3 should not
    arrive with twenty lines about week 2 attached."""
    from nflpicker import assistant, db

    db.connect()
    a = assistant.create_chat("A")
    b = assistant.create_chat("B")
    assistant.append_message(a["id"], "user", "about week 2")
    assistant.append_message(b["id"], "user", "about week 3")

    assert [m["content"] for m in assistant.chat_messages(a["id"])] == ["about week 2"]
    assert [m["content"] for m in assistant.chat_messages(b["id"])] == ["about week 3"]


def test_a_chat_is_named_after_the_first_question(temp_env):
    from nflpicker import assistant

    assert assistant.title_from("  Is this model any good?  ") == "Is this model any good?"
    assert assistant.title_from("") == "New chat"
    long = assistant.title_from("x" * 200)
    assert len(long) <= 49 and long.endswith("…")


def test_an_empty_rename_is_refused(temp_env):
    from nflpicker import assistant, db

    db.connect()
    chat = assistant.create_chat("keep me")
    with pytest.raises(assistant.AssistantError):
        assistant.rename_chat(chat["id"], "   ")
    assert assistant.list_chats()[0]["title"] == "keep me"


# ------------------------------------------------- thinking out loud, untagged

def test_a_reasoning_preamble_without_tags_is_dropped():
    """The screenshot that prompted this: "Thinking Process:" followed by a
    numbered plan four times longer than the answer. There is no <think> tag
    to strip, so the shape has to be recognised instead."""
    text = (
        "Thinking Process:\n\n"
        "1. **Analyze the Request:** User asks what about this week.\n"
        "2. **Analyze the Data:** Season 2026, Week 2, 16 games.\n\n"
        "### Answer\n\n"
        "Week 2 is 16 games. The model likes TEN over IND at 92.2%."
    )
    out = assistant._strip_untagged_thinking(text)
    assert out.startswith("Week 2 is 16 games")
    assert "Thinking Process" not in out


def test_an_answer_that_never_rambled_is_left_alone():
    """Cutting a real answer in half is far worse than leaving a tidy preamble
    in place, so the stripper only fires on text that opens like working-out."""
    for text in [
        "TEN over IND is the strongest pick at 92.2%.",
        "The model's ATS rate is 50.7%, below the 52.4% break-even.",
        "Analysis of the board shows nothing unusual.",   # a real first word
    ]:
        assert assistant._strip_untagged_thinking(text) == text


def test_thinking_with_no_labelled_answer_keeps_the_conclusion():
    text = ("Let me think about this.\n\n"
            "The Chiefs are 1-0 but their Pythagorean is poor.\n\n"
            "So: Kansas City is overrated at rank 9.")
    assert assistant._strip_untagged_thinking(text).startswith("So: Kansas City")


def test_the_prompt_forbids_showing_the_working():
    prompt = assistant.SYSTEM_PROMPT
    assert "Thinking Process" in prompt      # named, so the model cannot miss it
    assert "Give the answer first" in prompt


def test_thinking_is_switched_off_and_the_answer_is_capped():
    """The biggest single lever on how long an answer takes: a 4B reasoning
    model will spend a thousand tokens deciding how to approach a question
    that needs thirty to answer."""
    assert assistant.NO_THINKING["chat_template_kwargs"]["enable_thinking"] is False
    assert assistant.NO_THINKING["think"] is False
    assert 200 <= assistant.MAX_TOKENS <= 1200


def test_a_truncated_ramble_is_reported_rather_than_shown():
    """The model spending its whole budget thinking has no answer in it.

    What was on screen when this was reported: "Thinking Process:", a numbered
    plan, and then a sentence stopping mid-word -- the 700-token answer had
    gone entirely on working-out. The last paragraph of an unfinished plan is
    not a conclusion, it is a fragment of somebody's notes.
    """
    from nflpicker import assistant

    reply = assistant._reply_from({
        "finish_reason": "length",
        "message": {"content": "Thinking Process:\n\n1. Analyse the request\n"
                               "2. Look at the JSON\n\nSeveral games have"},
    })
    assert "ran out of room" in reply
    assert "Thinking Process" not in reply


def test_a_finished_answer_is_left_alone():
    """The guard above must not fire on a model that simply answered."""
    from nflpicker import assistant

    text = "TEN over IND at 85.7% is the strongest edge on the board."
    assert assistant._reply_from(
        {"finish_reason": "stop", "message": {"content": text}}) == text


def test_thinking_is_switched_off_in_the_prompt_not_asked_for():
    """`/no_think` is a Qwen chat-template switch, not an instruction.

    Asking a reasoning model not to reason does not work -- the prompt asks
    twice in words and the model wrote "Thinking Process:" anyway. The switch
    closes the thinking block before generation starts, which is what stops
    the tokens being spent rather than merely hiding them afterwards.
    """
    from nflpicker import assistant

    assert assistant.SYSTEM_PROMPT.rstrip().endswith("/no_think")
    assert assistant.NO_THINKING["think"] is False


def test_thinking_goes_in_its_own_field_and_is_never_read(monkeypatch):
    """Ollama's native endpoint separates reasoning from the answer.

    This is the fix that actually holds. `/no_think` is a Qwen 3 template
    convention the model ignored, the OpenAI-compatible endpoint drops `think`
    because it is not part of that API, and asking in words does not work --
    it was asked twice and still opened with "Thinking Process:". Here the
    reasoning arrives in `message.thinking` and simply is not read.
    """
    import httpx

    from nflpicker import assistant

    sent = {}

    class Reply:
        status_code = 200

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {"model": "qwen", "done_reason": "stop", "message": {
                "thinking": "Thinking Process:\n1. Analyse the request\n2. ...",
                "content": "TEN over IND at 85.7% is the strongest edge.",
            }}

    def fake_post(url, json=None, timeout=None):
        sent["url"] = url
        sent["body"] = json
        return Reply()

    monkeypatch.setattr(httpx, "post", fake_post)
    out = assistant._ask_ollama(
        "http://127.0.0.1:11435", {"model": "qwen", "messages": []}, 30.0)

    assert out["reply"] == "TEN over IND at 85.7% is the strongest edge."
    assert "Thinking" not in out["reply"]
    assert sent["url"].endswith("/api/chat")
    assert sent["body"]["think"] is False


def test_a_server_that_is_not_ollama_falls_through(monkeypatch):
    """A 404 on /api/chat means the compatible endpoint is all there is."""
    import httpx

    from nflpicker import assistant

    class NotFound:
        status_code = 404

    monkeypatch.setattr(httpx, "post", lambda *a, **k: NotFound())
    assert assistant._ask_ollama("http://x", {"model": "m", "messages": []}, 5.0) is None


def test_the_context_says_how_much_of_the_week_has_been_played(pipeline, temp_env):
    """The bug this closes: asked to summarise week two, the model answered
    with the season-to-date record -- seventeen graded games, sixteen of them
    from week one -- and never said which period it meant. The figure was
    right and the answer was wrong.
    """
    from nflpicker import db
    from nflpicker.assistant import context

    season = pipeline.season()
    for i in range(3):
        db.execute(
            "INSERT OR REPLACE INTO games(game_id, season, week, season_type,"
            " kickoff, home, away, home_score, away_score, status, updated_at) "
            "VALUES(?, ?, 2, 'REG', '2026-09-17T00:00:00+00:00', ?, ?, ?, ?, ?, 'now')",
            (f"wk2-{i}", season, ["KC", "BUF", "PHI"][i], ["DEN", "MIA", "DAL"][i],
             24 if i == 0 else None, 17 if i == 0 else None,
             "final" if i == 0 else "scheduled"))

    ctx = context(season, 2)
    week = ctx["this_week"]
    assert week["n_games"] == 3
    assert week["n_played"] == 1
    assert week["n_still_to_play"] == 2
    assert "1 of 3" in week["state"]


def test_an_unplayed_week_says_so_rather_than_counting_zero(pipeline, temp_env):
    from nflpicker import db
    from nflpicker.assistant import context

    season = pipeline.season()
    db.execute(
        "INSERT OR REPLACE INTO games(game_id, season, week, season_type,"
        " kickoff, home, away, status, updated_at) "
        "VALUES('wk3-0', ?, 3, 'REG', '2026-09-24T00:00:00+00:00',"
        " 'KC', 'DEN', 'scheduled', 'now')", (season,))
    state = context(season, 3)["this_week"]["state"]
    assert "none of week 3" in state


def test_the_season_record_is_named_for_the_period_it_covers(pipeline, temp_env):
    """It was called `season_scoreboard`, which is accurate and was read as
    "this week" anyway. The field now says so in its own value."""
    from nflpicker.assistant import context

    ctx = context(pipeline.season(), 2)
    assert "season_scoreboard" not in ctx
    covers = ctx["record_for_the_whole_season_so_far"]["covers"]
    assert "weeks 1 to 2" in covers
    assert "NOT week 2 on its own" in covers


def test_the_prompt_forbids_quoting_a_record_without_its_period():
    from nflpicker import assistant

    prompt = assistant.SYSTEM_PROMPT
    assert "Every record has a period attached" in prompt
    assert "this_week.state" in prompt


# ------------------------------------------------- the window the model reads

def test_the_context_window_is_set_and_big_enough(monkeypatch):
    """The reason a chat had no memory.

    Ollama defaults to 2048 tokens and silently drops whatever does not fit,
    oldest first. The instructions and the attached board come to roughly 2,800
    before anything is asked, so every request was already over -- the history
    was sent in full each time and discarded on arrival. Nothing in the app
    could see it happen: the transcript was correct, the request was correct,
    and the model answered as though it had just met you.
    """
    import httpx

    from nflpicker import assistant

    sent = {}

    class _Reply:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"message": {"content": "ok"}, "done_reason": "stop"}

    def _post(url, json=None, timeout=None, **kw):
        sent.update(json or {})
        return _Reply()

    monkeypatch.setattr(httpx, "post", _post)
    assistant._ask_ollama("http://x", {"model": "m", "messages": [
        {"role": "user", "content": "hi"}]}, 5.0)

    options = sent.get("options") or {}
    assert "num_ctx" in options, "unset means 2048, which is the bug"
    assert options["num_ctx"] == assistant.CONTEXT_TOKENS
    # Room for the instructions, the board, a conversation and an answer.
    assert options["num_ctx"] >= 4096
    assert options["num_ctx"] > options["num_predict"] * 4


def test_the_whole_conversation_is_sent(monkeypatch, temp_env):
    """Turn two carries turn one, so a follow-up can be a follow-up."""
    import httpx

    from nflpicker import assistant

    seen = []

    class _Reply:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"message": {"content": "ok"}, "done_reason": "stop"}

    monkeypatch.setattr(assistant, "status", lambda: {
        "ready": True, "model": "m", "endpoint": "http://x/v1", "message": ""})
    monkeypatch.setattr(httpx, "post",
                        lambda url, json=None, timeout=None, **kw: (
                            seen.append(json), _Reply())[1])

    chat = assistant.create_chat("t")["id"]
    for question in ("who plays thursday?", "and the total?"):
        assistant.append_message(chat, "user", question)
        assistant.ask(assistant.chat_messages(chat), 2026, 6)
        assistant.append_message(chat, "assistant", "ok")

    second = [m["content"] for m in seen[-1]["messages"] if m["role"] != "system"]
    assert second == ["who plays thursday?", "ok", "and the total?"]


# --------------------------------------------------------------- latency caps

def test_the_answer_ceiling_matches_the_length_asked_for():
    """A ceiling nothing reaches is not a ceiling.

    Generation is the slow half of the wait on a model running on the user's
    own machine, so the cap on the answer is also the cap on how long it can
    take. It was seven hundred tokens against a prompt asking for "under 150
    words" -- about two hundred tokens -- so five hundred of it were pure
    worst case that only ever showed up as the answer arriving late.
    """
    from nflpicker import assistant

    assert "under 150 words" in assistant.SYSTEM_PROMPT.lower()
    assert assistant.MAX_TOKENS <= 400, "the cap tracks the length asked for"
    assert assistant.MAX_TOKENS >= 250, "and still fits a ranked list"


def test_the_model_is_asked_to_stay_loaded(monkeypatch):
    """Ollama unloads after five minutes by default, which is almost exactly
    the wrong number: you ask, read, think, ask again -- and the second
    question pays a full model load for no reason anyone watching could
    infer."""
    from nflpicker import assistant

    sent = {}

    class _Response:
        status_code = 200

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {"message": {"content": "KC by 3."}, "model": "m"}

    def fake_post(url, json=None, timeout=None):
        sent.update(json or {})
        return _Response()

    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)
    out = assistant._ask_ollama(
        "http://127.0.0.1:11434",
        {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        10.0)

    assert out["reply"] == "KC by 3."
    assert sent.get("keep_alive") == assistant.KEEP_ALIVE
    assert sent["options"]["num_predict"] == assistant.MAX_TOKENS
    assert sent["options"]["num_ctx"] == assistant.CONTEXT_TOKENS


# --------------------------------------------------------------- streaming

def _stream_response(chunks, status_code=200):
    """A stand-in for httpx's streaming response context manager."""
    import json as _json

    class _Response:
        def __init__(self):
            self.status_code = status_code

        def raise_for_status(self):
            if status_code >= 400:
                raise RuntimeError(f"HTTP {status_code}")

        @staticmethod
        def iter_lines():
            for chunk in chunks:
                yield _json.dumps(chunk)

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def stream(self, *a, **k):
            import contextlib as _ctx

            @_ctx.contextmanager
            def _open():
                yield _Response()
            return _open()

    return _Client


def test_the_answer_arrives_in_pieces_and_then_whole(monkeypatch, temp_env):
    """What streaming is for. The wait is generation on somebody's own CPU and
    nothing here shortens it; what changes is that it is spent reading."""
    import httpx

    from nflpicker import assistant

    monkeypatch.setattr(assistant, "status", lambda: {
        "ready": True, "model": "m", "endpoint": "http://127.0.0.1:11434/v1"})
    monkeypatch.setattr(assistant, "context", lambda s, w: {"season": s})
    monkeypatch.setattr(httpx, "Client", _stream_response([
        {"message": {"content": "KC "}, "done": False},
        {"message": {"content": "by 3."}, "done": False},
        {"message": {"content": ""}, "done": True, "model": "m"},
    ]))

    events = list(assistant.stream([{"role": "user", "content": "who wins"}],
                                   2026, 8))
    assert [e["delta"] for e in events if "delta" in e] == ["KC ", "by 3."]
    final = events[-1]
    assert final["done"] is True
    assert final["reply"] == "KC by 3.", "the whole answer, for storing"


def test_thinking_is_stripped_across_chunk_boundaries(monkeypatch, temp_env):
    """A tag can be split between two chunks, which is exactly why the strip
    happens once at the end rather than per piece -- doing it per piece lets
    half a tag through and eats the other half."""
    import httpx

    from nflpicker import assistant

    monkeypatch.setattr(assistant, "status", lambda: {
        "ready": True, "model": "m", "endpoint": "http://127.0.0.1:11434/v1"})
    monkeypatch.setattr(assistant, "context", lambda s, w: {})
    monkeypatch.setattr(httpx, "Client", _stream_response([
        {"message": {"content": "<thi"}, "done": False},
        {"message": {"content": "nk>plan</think>KC by 3."}, "done": False},
        {"message": {"content": ""}, "done": True},
    ]))

    final = list(assistant.stream([{"role": "user", "content": "q"}], 2026, 8))[-1]
    assert final["reply"] == "KC by 3."
    assert "think" not in final["reply"]


def test_streaming_needs_ollama_and_says_so(monkeypatch, temp_env):
    """Anything that is not Ollama cannot be told not to think, so it falls
    back to the blocking path rather than streaming a model's working-out.

    A llama.cpp server is the case: loopback, ready, and serving a plain
    `/chat/completions` rather than the `/v1` Ollama puts its compatibility
    layer behind. It must refuse before opening a connection, not after.
    """
    from nflpicker import assistant

    monkeypatch.setattr(assistant, "status", lambda: {
        "ready": True, "model": "m", "endpoint": "http://127.0.0.1:8080"})
    with pytest.raises(assistant.AssistantError, match="Ollama"):
        list(assistant.stream([{"role": "user", "content": "q"}], 2026, 8))
