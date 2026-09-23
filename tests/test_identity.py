"""The name in the greeting.

Worth testing rather than eyeballing, because every interesting case is one
this machine cannot produce: the CI runner's account is called `runner`, a
developer's is called whatever they called it, and the Windows path does not
exist here at all.
"""

from __future__ import annotations

import pytest

from nflpicker import identity


@pytest.fixture(autouse=True)
def _forget_the_system():
    """The system lookup is cached for the life of the process, which is right
    in the app and wrong in a test that changes what the system says."""
    identity._from_system.cache_clear()
    yield
    identity._from_system.cache_clear()


@pytest.mark.parametrize(
    ("full", "expected"),
    [
        ("Luke Stoub", "Luke"),
        ("  Luke   Stoub  ", "Luke"),
        ("Luke", "Luke"),
        # Surname-first, as some directories store it. Greeting someone by
        # their surname is worse than not greeting them.
        ("Stoub, Luke", "Luke"),
        ("Stoub, Luke J", "Luke"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_first_name(full, expected):
    assert identity._first_name(full) == expected


@pytest.mark.parametrize("value", ["Luke", "sarah", "o'brien", "mary-jane"])
def test_names_that_can_be_greeted(value):
    assert identity._looks_like_a_name(value)


@pytest.mark.parametrize(
    "value",
    [
        "admin", "Administrator", "user", "runner", "root",   # roles, not people
        "x", "",                                              # too short
        "ljsto7", "user_2", "j.smith",                        # handles
        "qwrtz",                                              # no vowel
        "a" * 21,                                             # a path, not a name
    ],
)
def test_names_that_should_not_be_greeted(value):
    assert not identity._looks_like_a_name(value)


def test_the_setting_wins(monkeypatch):
    monkeypatch.setenv("NFLPICKER_USER_NAME", "Luke Stoub")
    from nflpicker.config import reset_config

    reset_config()
    try:
        assert identity.greeting_name() == {
            "name": "Luke", "full": "Luke Stoub", "source": "settings"}
    finally:
        monkeypatch.delenv("NFLPICKER_USER_NAME", raising=False)
        reset_config()


def test_the_system_name_is_used_when_nothing_is_set(monkeypatch):
    monkeypatch.setattr(identity, "_windows_display_name", lambda: "Luke Stoub")
    monkeypatch.setattr(identity, "_passwd_full_name", lambda: "Luke Stoub")
    assert identity.greeting_name() == {
        "name": "Luke", "full": "Luke", "source": "system"}


def test_the_login_name_is_the_fallback(monkeypatch):
    monkeypatch.setattr(identity, "_windows_display_name", lambda: "")
    monkeypatch.setattr(identity, "_passwd_full_name", lambda: "")
    monkeypatch.setattr(identity, "_account_name", lambda: "sarah")
    assert identity.greeting_name() == {
        "name": "Sarah", "full": "Sarah", "source": "account"}


def test_a_role_account_is_greeted_without_a_name(monkeypatch):
    """The case this is really for: a machine whose account is `Administrator`
    gets the plain greeting rather than a wrong one."""
    monkeypatch.setattr(identity, "_windows_display_name", lambda: "")
    monkeypatch.setattr(identity, "_passwd_full_name", lambda: "")
    monkeypatch.setattr(identity, "_account_name", lambda: "Administrator")
    assert identity.greeting_name() == {"name": "", "full": "", "source": ""}


def test_a_failing_system_lookup_is_not_an_error(monkeypatch):
    """Windows raises from the ctypes path on an account with no full name.
    That is an answer, not a crash -- and a crash here would take the whole
    /api/state response with it."""
    def boom():
        raise OSError("no mapping")

    monkeypatch.setattr(identity, "_windows_display_name", boom)
    monkeypatch.setattr(identity, "_passwd_full_name", boom)
    monkeypatch.setattr(identity, "_account_name", lambda: "sarah")
    assert identity.greeting_name()["name"] == "Sarah"
