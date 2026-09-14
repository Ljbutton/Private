"""Shared fixtures. Every test runs against a throwaway database and never
touches the network, so the suite is deterministic and offline."""

from __future__ import annotations

import warnings

import pytest

warnings.filterwarnings("ignore")


@pytest.fixture()
def temp_env(tmp_path, monkeypatch):
    """Point the app at an isolated data directory in demo mode."""
    monkeypatch.setenv("NFLPICKER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NFLPICKER_DEMO", "1")
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    monkeypatch.setenv("NFLPICKER_SEASON", "2025")
    # Pin the synthetic season mid-flight so the suite is independent of the
    # date it runs on: weeks 1-7 complete, 8-18 still to play.
    monkeypatch.setenv("NFLPICKER_DEMO_WEEK", "8")

    from nflpicker import config, db

    config.reset_config()
    db.close_all()
    yield tmp_path
    db.close_all()
    config.reset_config()


@pytest.fixture()
def pipeline(temp_env):
    from nflpicker.pipeline import Pipeline

    return Pipeline()
