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


_load_dotenv(_REPO_ROOT / ".env")


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

    refresh_odds: int = field(default_factory=lambda: _int("REFRESH_ODDS_SECONDS", 900))
    refresh_scores: int = field(default_factory=lambda: _int("REFRESH_SCORES_SECONDS", 300))
    refresh_news: int = field(default_factory=lambda: _int("REFRESH_NEWS_SECONDS", 900))
    refresh_stats: int = field(default_factory=lambda: _int("REFRESH_STATS_SECONDS", 21600))

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
        return self.data_dir / "models"

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
