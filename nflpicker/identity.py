"""Who is sitting in front of this, so the app can say hello by name.

Three things worth knowing about this file:

* **Nothing here leaves the machine.** The name is read from the local account
  and handed to the page the app serves to itself. It is never sent anywhere,
  stored in the database, or attached to a request.
* **A login name is not a name.** ``ljsto`` is an account, ``Luke`` is a
  person, and greeting someone by their account reads worse than not greeting
  them at all. So the real-name field the operating system keeps is asked for
  first, and the login name is only used when it could plausibly pass for a
  first name.
* **The user always wins.** A name set in Settings overrides everything below,
  because no amount of guessing beats being told.
"""

from __future__ import annotations

import functools
import os
import sys

# Accounts named after their role rather than their owner. Greeting one of
# these by name produces "Good morning, Administrator", which is worse than the
# plain greeting it replaced.
_GENERIC = {
    "user", "users", "admin", "administrator", "owner", "guest", "root",
    "default", "defaultuser", "localadmin", "test", "temp", "dev", "developer",
    "pc", "laptop", "desktop", "home", "office", "shared", "public", "nobody",
    "runner", "build", "ci",
}


def _windows_display_name() -> str:
    """The full name Windows shows on the sign-in screen, if it has one.

    ``GetUserNameEx(NameDisplay)`` is the only call that returns it; the usual
    ``GetUserName`` returns the login name. It fails outright on a local
    account that was never given a full name, which is not an error worth
    reporting -- it is the answer "there isn't one".
    """
    import ctypes
    from ctypes import wintypes

    NAME_DISPLAY = 3
    secur32 = ctypes.WinDLL("secur32")
    size = wintypes.ULONG(0)
    secur32.GetUserNameExW(NAME_DISPLAY, None, ctypes.byref(size))
    if not size.value:
        return ""
    buf = ctypes.create_unicode_buffer(size.value)
    if not secur32.GetUserNameExW(NAME_DISPLAY, buf, ctypes.byref(size)):
        return ""
    return buf.value.strip()


def _passwd_full_name() -> str:
    """The real-name field of the passwd entry.

    macOS fills this in with what was typed during setup, which is why the Mac
    path needs nothing more exotic. On Linux it is the historical GECOS field:
    comma-separated, with the name first and office and phone numbers after.
    """
    import pwd

    gecos = pwd.getpwuid(os.getuid()).pw_gecos or ""
    return gecos.split(",", 1)[0].strip()


def _account_name() -> str:
    import getpass

    try:
        return getpass.getuser().strip()
    except Exception:
        return ""


def _looks_like_a_name(value: str) -> bool:
    """Whether a login name can stand in for a first name.

    Deliberately strict, because the cost is asymmetric: falling back to the
    plain greeting is invisible, and "Good morning, Xy7admin" is the sort of
    detail that makes a paid app feel unfinished.
    """
    if len(value) < 2 or len(value) > 20:
        return False
    if value.lower() in _GENERIC:
        return False
    if not value.replace("-", "").replace("'", "").isalpha():
        return False           # digits, dots and underscores mean handle
    # A name has a vowel. Initials-plus-surname handles (ljsto, jsmith) often
    # do too, so this only catches the worst of them -- the Settings field is
    # the real answer for the rest.
    return any(c in "aeiouy" for c in value.lower())


def _first_name(full: str) -> str:
    """The part you would be greeted by.

    "Luke Stoub" -> "Luke". "Stoub, Luke" -> "Luke", because some directories
    store it surname-first and greeting someone by their surname is worse than
    not greeting them.
    """
    full = " ".join(full.split())
    if not full:
        return ""
    if "," in full:
        after = full.split(",", 1)[1].strip()
        if after:
            full = after
    return full.split(" ")[0]


@functools.lru_cache(maxsize=1)
def _from_system() -> tuple[str, str]:
    """(name, source) from the operating system. Cached: it cannot change
    while the app is running, and the Windows call is a DLL load."""
    full = ""
    try:
        full = _windows_display_name() if sys.platform == "win32" else _passwd_full_name()
    except Exception:
        full = ""
    name = _first_name(full)
    if name and _looks_like_a_name(name):
        return name, "system"

    account = _first_name(_account_name())
    if account and _looks_like_a_name(account):
        return account.capitalize() if account.islower() else account, "account"
    return "", ""


def greeting_name() -> dict:
    """What to greet this person as, and where it came from.

    ``source`` is what lets the page explain itself: a name we guessed from the
    computer's account should say so and point at the setting that fixes it, a
    name the user typed should not.
    """
    from .config import get_config

    chosen = (get_config().user_name or "").strip()
    if chosen:
        # Whatever they typed, used as typed -- but greeted by the first word,
        # so putting a full name in the box does not produce "Good morning,
        # Luke Stoub Jr.".
        return {"name": _first_name(chosen), "full": chosen, "source": "settings"}

    name, source = _from_system()
    return {"name": name, "full": name, "source": source}
