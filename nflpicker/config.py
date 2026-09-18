"""Runtime configuration, read once from the environment (and optional .env)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader; real environment variables always win."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _default_data_dir() -> Path:
    """Where a packaged build keeps its data, mirroring scripts/desktop_entry.py.

    "TheEdge" for a new install; an existing "NFLPicker" directory is used as
    it stands. The app was renamed, the picks were not -- and the safe way to
    carry a database across a rename is to keep reading it where it already
    is, not to move it on first launch and hope the copy survived. Nothing is
    ever written to the old location that a new install would look for.
    """
    import sys

    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    for name in ("TheEdge", "NFLPicker"):
        candidate = base / name
        if candidate.exists():
            return candidate
    return base / "TheEdge"


# Source checkout first, then the data directory. The second one is what makes a
# packaged build configurable at all: in a frozen app ``__file__`` lives inside
# PyInstaller's temp extraction directory, which is recreated on every launch,
# so a .env beside the "repo root" is both unreachable and unwritable. The data
# directory is the one place that survives, so it is where a settings file has
# to live -- otherwise the only way to give the app an API key is to launch it
# from a terminal with the variable exported.
_load_dotenv(_REPO_ROOT / ".env")
_load_dotenv(Path(os.environ.get("NFLPICKER_DATA_DIR") or _default_data_dir()) / ".env")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _bool(name: str, default: bool = False) -> bool:
    return (os.environ.get(name, "") or str(int(default))).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _csv(name: str) -> list[str]:
    return [p.strip() for p in (os.environ.get(name, "") or "").split(",") if p.strip()]


@dataclass(frozen=True)
class Config:
    data_dir: Path = field(
        default_factory=lambda: Path(
            os.environ.get("NFLPICKER_DATA_DIR") or (_REPO_ROOT / "data")
        ).resolve()
    )
    odds_api_key: str = field(default_factory=lambda: os.environ.get("ODDS_API_KEY", "").strip())
    odds_books: list[str] = field(default_factory=lambda: _csv("ODDS_BOOKS"))
    odds_monthly_budget: int = field(default_factory=lambda: _int("ODDS_MONTHLY_BUDGET", 480))
    # Burst ceilings, not the bill. 480 a month is about 16 credits a day
    # sustained; these sit well above that so a busy Sunday can poll harder
    # than a quiet Tuesday, while a stuck scheduler still cannot spend the
    # month in an afternoon. The monthly figure governs the total.
    odds_weekly_budget: int = field(default_factory=lambda: _int("ODDS_WEEKLY_BUDGET", 120))
    odds_daily_budget: int = field(default_factory=lambda: _int("ODDS_DAILY_BUDGET", 50))

    refresh_odds: int = field(default_factory=lambda: _int("REFRESH_ODDS_SECONDS", 900))
    refresh_scores: int = field(default_factory=lambda: _int("REFRESH_SCORES_SECONDS", 300))
    refresh_news: int = field(default_factory=lambda: _int("REFRESH_NEWS_SECONDS", 900))
    refresh_stats: int = field(default_factory=lambda: _int("REFRESH_STATS_SECONDS", 21600))
    # Retraining is checked daily but only acts once a week of results has
    # landed, so the cadence follows the season rather than the clock.
    refresh_train: int = field(default_factory=lambda: _int("REFRESH_TRAIN_SECONDS", 86400))
    train_auto: bool = field(default_factory=lambda: _bool("NFLPICKER_TRAIN_AUTO", True))
    # Roughly a week of games. Retraining on two or three new results spends
    # minutes of CPU to move the weights by nothing.
    train_min_new_games: int = field(
        default_factory=lambda: _int("NFLPICKER_TRAIN_MIN_NEW_GAMES", 12))
    # How stale a fit may get before one new result is reason enough to redo
    # it. Three days puts the refit on a Wednesday or Thursday, after Monday
    # night has been played and before the next slate is projected.
    train_max_age_hours: int = field(
        default_factory=lambda: _int("NFLPICKER_TRAIN_MAX_AGE_HOURS", 72))

    # The last week the survivor plan covers: the end of the season.
    #
    # It was seventeen, on the reasoning that most pools settle there and that
    # week eighteen rests starters -- which is true, and is the week a
    # projection is worth least. It is still the wrong default: a plan that
    # stops a week early cannot be extended by its reader, while one that runs
    # a week long can simply be read from the row above.
    survivor_last_week: int = field(
        default_factory=lambda: _int("NFLPICKER_SURVIVOR_LAST_WEEK", 18))

    # Which day the week's power ranking is cut on. Monday is 0, so 2 is
    # Wednesday: Monday night has been played, the injury reports have started,
    # and nothing about the coming Sunday is known yet that will not still be
    # true on Saturday. The ranking is taken once on that day and then left
    # alone for the week -- a ranking that keeps being revised is a live
    # readout with a week number on it, and cannot be moved against.
    ranking_cut_weekday: int = field(
        default_factory=lambda: _int("NFLPICKER_RANKING_CUT_WEEKDAY", 2))
    # How much worse a fresh model may be before it is rejected. Walk-forward
    # MAE is computed over all history each time, and one week changes the
    # sample by about a third of a percent, so runs are comparable in practice;
    # this tolerance is sized to catch a genuine break -- an upstream schema
    # change, a feature that went empty -- not to arbitrate noise.
    train_max_regression: float = field(
        default_factory=lambda: float(
            os.environ.get("NFLPICKER_TRAIN_MAX_REGRESSION", "") or 0.15))
    refresh_weather: int = field(
        default_factory=lambda: _int("REFRESH_WEATHER_SECONDS", 10800)
    )
    refresh_prediction_markets: int = field(
        default_factory=lambda: _int("REFRESH_PREDICTION_MARKETS_SECONDS", 600)
    )
    prediction_markets_enabled: bool = field(
        default_factory=lambda: _bool("PREDICTION_MARKETS_ENABLED", True)
    )
    # Ignore thinly traded markets outright; their prices are not informative
    # and their books cannot absorb a stake.
    polymarket_min_volume: int = field(
        default_factory=lambda: _int("POLYMARKET_MIN_VOLUME", 1000)
    )

    # What to call you. Blank means "read it off the computer's account", which
    # is what happens on a machine nobody has told this app anything about.
    user_name: str = field(
        default_factory=lambda: os.environ.get("NFLPICKER_USER_NAME", "").strip()
    )

    host: str = field(default_factory=lambda: os.environ.get("HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _int("PORT", 8000))

    demo: bool = field(default_factory=lambda: _bool("NFLPICKER_DEMO", False))
    season_override: int = field(default_factory=lambda: _int("NFLPICKER_SEASON", 0))
    # Pins which week the synthetic season has reached. Without it the demo
    # follows today's date, which makes tests depend on when they are run and
    # makes a mid-season state impossible to reproduce.
    demo_week: int = field(default_factory=lambda: _int("NFLPICKER_DEMO_WEEK", 0))

    http_timeout: float = 20.0
    user_agent: str = "nflpicker/0.1 (+https://github.com/Ljbutton/ESPNpicem)"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "nflpicker.db"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def model_dir(self) -> Path:
        """Where a model *you* trained is written."""
        return self.data_dir / "models"

    @property
    def bundled_model_dir(self) -> Path | None:
        """The model shipped inside a packaged build, if there is one.

        PyInstaller unpacks the bundle to a temporary directory and the entry
        point records it as NFLPICKER_BUNDLE. Nothing read it until now, which
        is why a packaged install ran on power ratings alone however many
        seasons the shipped model had been trained on -- it was in the repo and
        never in the app.
        """
        base = os.environ.get("NFLPICKER_BUNDLE")
        if not base:
            return None
        path = Path(base) / "data" / "models"
        return path if path.exists() else None

    @property
    def model_dirs(self) -> list[Path]:
        """Where to look for a model, best first.

        A model you trained wins over the shipped one: it is newer, it is
        fitted in this environment, and retraining is the documented fix when
        the shipped artifact will not unpickle here.
        """
        return [d for d in (self.model_dir, self.bundled_model_dir) if d]

    @property
    def has_odds_key(self) -> bool:
        return bool(self.odds_api_key)

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.cache_dir, self.model_dir):
            d.mkdir(parents=True, exist_ok=True)


_CONFIG: Config | None = None


def get_config() -> Config:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = Config()
        _CONFIG.ensure_dirs()
    return _CONFIG


def reset_config() -> None:
    """Drop the cached config (tests re-read the environment)."""
    global _CONFIG
    _CONFIG = None
