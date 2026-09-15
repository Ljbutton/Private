"""Published rankings, the consensus, and refusing to store half of one."""

import pytest

from nflpicker import db, rankings
from nflpicker.config import reset_config
from nflpicker.teams import ABBRS


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("NFLPICKER_DATA_DIR", str(tmp_path))
    reset_config()
    db.close_all()
    db.connect()
    yield
    db.close_all()
    reset_config()


def _full(order=None):
    """A complete 1-32 ranking, in whatever order is given."""
    teams = order or sorted(ABBRS)
    return {team: i for i, team in enumerate(teams, start=1)}


# ------------------------------------------------------------------ parsing

def test_a_numbered_list_pasted_from_a_browser_parses():
    text = "\n".join(f"{i}. {t}" for i, t in enumerate(sorted(ABBRS), start=1))
    assert rankings.parse_text(text) == _full()


def test_full_team_names_and_trailing_commentary_are_handled():
    """What actually lands on the clipboard: a name, an em dash, a paragraph."""
    names = [
        "Seattle Seahawks", "Philadelphia Eagles", "Detroit Lions", "Buffalo Bills",
        "Baltimore Ravens", "Kansas City Chiefs", "Green Bay Packers", "Denver Broncos",
        "Los Angeles Rams", "Washington Commanders", "Minnesota Vikings",
        "Tampa Bay Buccaneers", "Houston Texans", "Chicago Bears", "San Francisco 49ers",
        "Los Angeles Chargers", "Arizona Cardinals", "Cincinnati Bengals",
        "Pittsburgh Steelers", "Dallas Cowboys", "Jacksonville Jaguars", "Atlanta Falcons",
        "Indianapolis Colts", "Miami Dolphins", "New England Patriots", "Las Vegas Raiders",
        "New York Jets", "New York Giants", "Carolina Panthers", "New Orleans Saints",
        "Cleveland Browns", "Tennessee Titans",
    ]
    text = "\n".join(
        f"{i}. {n} — they did a thing this week and it was notable."
        for i, n in enumerate(names, start=1))
    parsed = rankings.parse_text(text)
    assert len(parsed) == 32
    assert parsed["SEA"] == 1
    assert parsed["TEN"] == 32


def test_a_partial_list_is_refused_rather_than_averaged_in():
    """The failure that matters. Nineteen teams produce an average that looks
    like a consensus and is not one."""
    text = "\n".join(f"{i}. {t}" for i, t in enumerate(sorted(ABBRS)[:19], start=1))
    with pytest.raises(rankings.RankingError, match="need 32"):
        rankings.parse_text(text)


def test_a_duplicated_position_is_refused():
    ranks = _full()
    ranks["KC"] = ranks["BUF"]
    with pytest.raises(rankings.RankingError, match="not 1-32"):
        rankings.validate(ranks)


def test_lines_that_are_not_teams_are_skipped_not_guessed():
    order = sorted(ABBRS)
    text = ("NFL Power Rankings, Week 2\nBy a Staff Writer\n\n"
            + "\n".join(f"{i}. {t}" for i, t in enumerate(order, start=1))
            + "\n\nMore from our NFL coverage\n1. Sign up for the newsletter\n")
    parsed = rankings.parse_text(text)
    assert len(parsed) == 32
    assert parsed[order[0]] == 1


# ---------------------------------------------------------------- consensus

def test_the_consensus_averages_the_sources_and_reports_the_spread(store):
    order = sorted(ABBRS)
    rankings.store("espn", 2026, 1, _full(order))
    # A second source that likes the last team a lot more.
    swapped = order[:]
    swapped.insert(0, swapped.pop())
    rankings.store("nfl", 2026, 1, _full(swapped))

    con = rankings.consensus(2026, (1, 2))
    assert con["n_lists"] == 2
    assert sorted(con["sources"]) == ["espn", "nfl"]
    rows = {r["team"]: r for r in con["teams"]}
    moved = rows[order[-1]]
    assert moved["best"] == 1 and moved["worst"] == 32
    assert moved["spread"] == 31
    # The consensus is itself a clean 1-32.
    assert sorted(r["consensus_rank"] for r in con["teams"]) == list(range(1, 33))


def test_weeks_one_and_two_are_pooled(store):
    order = sorted(ABBRS)
    rankings.store("espn", 2026, 1, _full(order))
    rankings.store("espn", 2026, 2, _full(order))
    assert rankings.consensus(2026, (1, 2))["n_lists"] == 2
    assert rankings.consensus(2026, (1,))["n_lists"] == 1


def test_storing_the_same_source_and_week_twice_replaces_it(store):
    order = sorted(ABBRS)
    rankings.store("espn", 2026, 1, _full(order))
    rankings.store("espn", 2026, 1, _full(list(reversed(order))))
    assert rankings.consensus(2026, (1,))["n_lists"] == 1


# --------------------------------------------------------------- comparison

def test_comparison_reports_where_we_disagree_most(store):
    order = sorted(ABBRS)
    rankings.store("espn", 2026, 1, _full(order))
    rankings.store("nfl", 2026, 1, _full(order))

    # Ours is the same, except the consensus's last team is our first.
    ours = [order[-1]] + order[:-1]
    result = rankings.compare(2026, ours, (1,))
    top = result["comparison"][0]
    assert top["team"] == order[-1]
    assert top["gap"] == 31            # they say 32, we say 1
    assert result["mean_abs_gap"] > 0


def test_an_outlier_needs_the_sources_to_agree_with_each_other(store):
    """Disagreeing with a team nobody can place is not evidence of anything."""
    order = sorted(ABBRS)
    rankings.store("espn", 2026, 1, _full(order))
    # Second source flips the last team to first, so its spread is enormous.
    contested = order[:]
    contested.insert(0, contested.pop())
    rankings.store("nfl", 2026, 1, _full(contested))

    ours = [order[-1]] + order[:-1]
    result = rankings.compare(2026, ours, (1,))
    assert all(o["team"] != order[-1] for o in result["outliers"])
