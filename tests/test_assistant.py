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

def test_no_endpoint_explains_how_to_get_one(clean_env):
    state = assistant.status()
    assert state["reason"] == "no_endpoint"
    assert "ollama" in state["message"].lower()


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
    """The app has measured its own ATS below break-even. An assistant that
    enthused about its edges would contradict every other screen."""
    prompt = assistant.SYSTEM_PROMPT
    assert "51%" in prompt or "51" in prompt
    assert "52.4%" in prompt
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
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": ""},
                                 "finish_reason": "length"}]}

    import httpx
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: _Resp())
    with pytest.raises(a.AssistantError, match="ran out of room"):
        a.ask([{"role": "user", "content": "hi"}], 2026, 2)


def test_the_context_header_is_a_real_newline():
    """It was written `\\\\n` in the source, so the model received a literal
    backslash-n glued to the JSON rather than a line break."""
    import inspect

    import nflpicker.assistant as a

    assert "Current state:\\\\n" not in inspect.getsource(a.ask)
