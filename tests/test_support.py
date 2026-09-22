"""Bug reports, and what must not travel in one.

A log is the most useful thing a report can carry and the most dangerous: it
is exactly where a key ends up when something goes wrong with a key. These
pin the redaction, and the rule that an unticked box means the detail is never
gathered rather than merely hidden.
"""

import pytest
from fastapi.testclient import TestClient

from nflpicker import support


@pytest.fixture()
def client(temp_env):
    from nflpicker.api import create_app

    with TestClient(create_app(start_scheduler=False, bootstrap=False)) as c:
        yield c


# ---------------------------------------------------------------- redaction

def test_a_licence_key_the_app_holds_is_matched_exactly(temp_env, monkeypatch):
    """Whatever shape it is. A key this process is holding is a known string,
    so it does not have to look like anything to be removed."""
    monkeypatch.setattr(support, "_known_secrets", lambda: ("SHORTKEY-9931",))
    out = support.redact("activation fails with SHORTKEY-9931 every time")
    assert "SHORTKEY-9931" not in out
    assert support.REDACTED in out


def test_a_key_announced_in_prose_goes(temp_env):
    """The likeliest sentence in a report about activation."""
    for text in (
        "my license key is ABCD1234EFGH5678 and it won't take",
        "License Key: ABCD1234EFGH5678",
        "apikey=ABCD1234EFGH5678",
        'token "ABCD1234EFGH5678"',
    ):
        out = support.redact(text)
        assert "ABCD1234EFGH5678" not in out, text


def test_an_odds_api_key_in_a_url_goes(temp_env):
    """The one that actually appears in this app's logs: a failed request with
    the key in the query string."""
    line = ("GET https://api.the-odds-api.com/v4/sports/?apiKey="
            "0123456789abcdef0123456789abcdef&regions=us -> 401")
    out = support.redact(line)
    assert "0123456789abcdef0123456789abcdef" not in out
    assert "apiKey=" in out, "the parameter name stays, so the line still reads"
    assert "regions=us" in out, "and the rest of the URL survives"


def test_anything_key_shaped_goes_whoever_put_it_there(temp_env):
    """The backstop. A long unbroken run of hex or base64 is a secret often
    enough that the cost of removing one that was not is the lower risk."""
    assert "deadbeef" not in support.redact("x " + "deadbeef" * 5 + " y")
    long_b64 = "aGVsbG90aGVyZWZyaWVuZHRoaXNpc2Fsb25nc3RyaW5n12345"
    assert long_b64 not in support.redact(f"Authorization Bearer {long_b64}")


def test_ordinary_prose_survives(temp_env):
    """Redaction that eats the report is not redaction. Short words, team
    abbreviations and game ids have to come through."""
    text = ("The bracket showed SEA as the 5 seed in week 12. "
            "Game id demo-2026-08-NO-TB. Score was 24-17.")
    assert support.redact(text) == text


def test_the_log_tail_is_redacted(temp_env, monkeypatch, tmp_path):
    """The log is attached verbatim apart from this, so this is the only thing
    standing between a key in a traceback and an email."""
    path = tmp_path / "logs" / "desktop.log"
    path.parent.mkdir(parents=True)
    path.write_text(
        "\n".join(f"line {i}" for i in range(200))
        + "\nERROR licence check failed for key=ABCD1234EFGH5678\n",
        encoding="utf-8")
    monkeypatch.setattr(support, "log_path", lambda: path)

    tail = support.tail_log(100)
    assert "ABCD1234EFGH5678" not in tail
    assert "line 199" in tail
    assert "line 50" not in tail, "only the last hundred lines"


def test_no_log_is_not_an_error(temp_env, monkeypatch, tmp_path):
    monkeypatch.setattr(support, "log_path", lambda: tmp_path / "nope.log")
    assert support.tail_log() == ""


# ------------------------------------------------------- only what was agreed

def test_an_unticked_detail_is_never_gathered(temp_env):
    """Not collected, rather than collected and dropped. The difference does
    not matter until the day it does."""
    out = support.details({"log": False, "key_hint": False})
    assert "log" not in out
    assert "key_hint" not in out
    assert out["included"] == ["os", "version"]
    assert "version" in out and "environment" in out


def test_everything_is_ticked_by_default(temp_env):
    out = support.details(None)
    assert out["included"] == sorted(support.DETAIL_KEYS)


def test_the_description_is_redacted_and_capped(temp_env):
    report = support.build("key=ABCD1234EFGH5678 " + "x" * 6000,
                           doing="opening the app", email=" me@example.com ")
    assert "ABCD1234EFGH5678" not in report["description"]
    assert len(report["description"]) <= support.MAX_DESCRIPTION
    assert report["email"] == "me@example.com"
    assert report["doing"] == "opening the app"


# ----------------------------------------------------------------- endpoint

def test_a_report_can_be_sent_before_activation(client, monkeypatch):
    """The whole reason this has to work at the gate: "my key won't activate"
    is the likeliest report there is, and those users cannot get past it."""
    from nflpicker import licensing

    sent = {}

    def fake_forward(report, *, timeout=10.0):
        sent.update(report)
        return {"ok": True, "ref": "ab12cd"}

    monkeypatch.setattr(licensing, "saved_key", lambda: "")
    monkeypatch.setattr("nflpicker.support.forward", fake_forward)

    response = client.post("/api/support/report",
                           json={"description": "will not activate"})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True and body["ref"] == "ab12cd"
    assert sent["description"] == "will not activate"


def test_an_empty_description_is_refused(client):
    response = client.post("/api/support/report", json={"description": "   "})
    assert response.status_code == 400


def test_no_licence_server_says_so_plainly(client, monkeypatch):
    """A source checkout has no server configured, and the form should say
    that rather than fail in a way that looks like the report was lost."""
    from nflpicker import licensing

    monkeypatch.setattr(licensing, "server_url", lambda: "")
    body = client.post("/api/support/report",
                       json={"description": "something is wrong"}).json()
    assert body["ok"] is False
    assert "isn't available" in body["message"].lower()
    assert body["reason"] == "no_server"
