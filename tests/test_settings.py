"""Reading and writing the handful of things you have to tell this app.

The two properties worth guarding are both about not losing something: a secret
must never travel back to the browser, and saving one field must not wipe the
others.
"""

import pytest

from nflpicker import settings
from nflpicker.config import get_config, reset_config


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("NFLPICKER_DATA_DIR", str(tmp_path))
    for key in ("ODDS_API_KEY", "ODDS_BOOKS", "ODDS_MONTHLY_BUDGET",
                "NFLPICKER_LLM_URL", "NFLPICKER_LLM_MODEL"):
        monkeypatch.delenv(key, raising=False)
    reset_config()
    yield tmp_path
    reset_config()


def _find(key):
    for group in settings.current()["groups"]:
        for item in group["settings"]:
            if item["key"] == key:
                return item
    raise AssertionError(f"{key} not offered")


# ----------------------------------------------------------------- secrets

def test_a_saved_key_never_travels_back_to_the_browser(store):
    settings.save({"ODDS_API_KEY": "abcd1234efgh5678ijkl"})
    field = _find("ODDS_API_KEY")
    assert field["value"] == ""                  # not the key, not a prefix
    assert field["masked"] == "••••ijkl"         # enough to recognise your own
    assert field["is_set"] is True


def test_the_mask_does_not_leak_a_short_key(store):
    settings.save({"ODDS_API_KEY": "abc"})
    assert _find("ODDS_API_KEY")["masked"] == "••••"


# -------------------------------------------------------------- persistence

def test_saving_one_setting_leaves_the_others_alone(store):
    settings.save({"ODDS_API_KEY": "abcd1234efgh5678ijkl"})
    settings.save({"NFLPICKER_LLM_MODEL": "qwen3.5:4b"})
    assert _find("ODDS_API_KEY")["is_set"] is True
    assert _find("NFLPICKER_LLM_MODEL")["value"] == "qwen3.5:4b"


def test_a_key_this_app_does_not_define_survives_an_edit(store):
    """Hand-edited lines in the file are somebody's, not ours to drop."""
    path = settings.env_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("SOMETHING_ELSE=keepme\n", encoding="utf-8")

    settings.save({"NFLPICKER_LLM_MODEL": "qwen3.5:4b"})
    assert "SOMETHING_ELSE=keepme" in path.read_text(encoding="utf-8")


def test_only_settings_this_app_defines_are_written(store):
    """The browser posts a dict; it does not get to name arbitrary variables."""
    settings.save({"PATH": "/nope", "ODDS_BOOKS": "draftkings"})
    written = settings.env_path().read_text(encoding="utf-8")
    assert "PATH=/nope" not in written
    assert "ODDS_BOOKS=draftkings" in written


def test_an_empty_value_clears_a_setting(store):
    settings.save({"ODDS_BOOKS": "draftkings"})
    settings.save({"ODDS_BOOKS": "  "})
    assert _find("ODDS_BOOKS")["is_set"] is False
    assert "ODDS_BOOKS" not in settings.env_path().read_text(encoding="utf-8")


def test_a_saved_setting_takes_effect_without_a_restart(store):
    """The config is a frozen dataclass built from the environment, so both
    have to move or the app keeps running on the old value."""
    assert get_config().has_odds_key is False
    settings.save({"ODDS_API_KEY": "abcd1234efgh5678ijkl"})
    assert get_config().has_odds_key is True
    assert get_config().odds_api_key == "abcd1234efgh5678ijkl"


# ------------------------------------------------------------- what is shown

def test_a_default_reads_as_a_default_not_as_unset(store):
    """An unticked box beside "not set" for a setting that is on by default is
    not a blank field -- it is the page stating the opposite of the truth."""
    field = _find("PREDICTION_MARKETS_ENABLED")
    assert field["value"] == "true"
    assert field["is_set"] is True
    assert field["explicit"] is False
    assert field["source"] == "default"


def test_a_value_you_set_is_marked_as_yours(store):
    settings.save({"ODDS_MONTHLY_BUDGET": "300"})
    field = _find("ODDS_MONTHLY_BUDGET")
    assert field["explicit"] is True and field["source"] == "file"


def test_every_setting_says_what_breaks_without_it(store):
    """The page exists to answer "what do I need to fill in", which a field
    label alone does not."""
    for group in settings.current()["groups"]:
        for item in group["settings"]:
            assert item["help"], item["key"]
            assert item["needed_for"], item["key"]


def test_settings_are_written_to_the_data_directory(store):
    """Not beside the source tree: in a frozen build that is PyInstaller's temp
    extraction directory, recreated every launch."""
    settings.save({"ODDS_BOOKS": "fanduel"})
    assert settings.env_path() == store / ".env"
    assert settings.env_path().exists()


# ------------------------------------------------------------- key checking

def test_an_obviously_malformed_key_is_rejected_without_a_network_call(store):
    result = settings.validate_odds_key("not a key!!")
    assert result["ok"] is False
    assert "does not look like" in result["message"]


def test_an_empty_key_says_so(store):
    assert settings.validate_odds_key("")["ok"] is False


# ------------------------------------------------- the rename must not orphan data

def test_an_existing_install_keeps_reading_its_own_database(tmp_path, monkeypatch):
    """The app was renamed; the picks were not.

    An install that already has data under the old name must go on using it.
    Moving it on first launch would be the version of this that loses a
    season of picks to a failed copy.
    """
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr("sys.platform", "win32")
    from nflpicker import config

    legacy = tmp_path / "NFLPicker"
    legacy.mkdir()
    assert config._default_data_dir() == legacy


def test_a_fresh_install_uses_the_new_name(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr("sys.platform", "win32")
    from nflpicker import config

    assert config._default_data_dir() == tmp_path / "TheEdge"


def test_the_new_name_wins_when_both_exist(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr("sys.platform", "win32")
    from nflpicker import config

    (tmp_path / "NFLPicker").mkdir()
    (tmp_path / "TheEdge").mkdir()
    assert config._default_data_dir() == tmp_path / "TheEdge"


def test_both_entry_points_agree_on_where_data_lives(tmp_path, monkeypatch):
    """config.py and desktop_entry.py each compute this, and a packaged build
    uses one to set the environment variable the other reads."""
    import importlib.util

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr("sys.platform", "win32")
    (tmp_path / "NFLPicker").mkdir()

    from nflpicker import config

    spec = importlib.util.spec_from_file_location(
        "desktop_entry", "scripts/desktop_entry.py")
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    assert entry.default_data_dir() == config._default_data_dir()
