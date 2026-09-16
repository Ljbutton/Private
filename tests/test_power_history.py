"""The power ranking, kept week by week.

team_ratings already held a history keyed by capture timestamp, which answers
"what did we think at 14:07 on Tuesday". This answers "where did this team rank
in week 6", which is a different shape and the one a ranking history exists
for. The two things most worth pinning are that a week's snapshot cannot see
its own results, and that a reconstruction never overwrites a record.
"""

import pytest
from fastapi.testclient import TestClient

from nflpicker import db


@pytest.fixture()
def client(temp_env):
    from nflpicker.api import create_app

    with TestClient(create_app(start_scheduler=False, bootstrap=False)) as c:
        yield c


def _game(game_id, season, week, home, away, hs, as_):
    db.execute(
        "INSERT OR REPLACE INTO games"
        "(game_id, season, week, season_type, kickoff, home, away,"
        " home_score, away_score, status, updated_at) "
        "VALUES(?,?,?,'REG',?,?,?,?,?,'final',?)",
        (game_id, season, week, f"2025-09-{week:02d}T17:00:00Z",
         home, away, hs, as_, "2025-09-30T00:00:00Z"),
    )


@pytest.fixture()
def four_weeks(pipeline, temp_env):
    """Two teams, four weeks, one of them winning every game."""
    db.execute("DELETE FROM games")
    db.execute("DELETE FROM power_snapshots")
    for week in range(1, 5):
        _game(f"g{week}", 2025, week, "KC", "DEN", 30, 10)
        _game(f"h{week}", 2025, week, "BUF", "NYJ", 24, 21)
    return pipeline


def test_every_week_gets_one_row_per_team(four_weeks):
    four_weeks.rebuild_power_history(2025)
    counts = db.query(
        "SELECT week, COUNT(*) n FROM power_snapshots WHERE season = 2025 "
        "GROUP BY week ORDER BY week"
    )
    assert [r["week"] for r in counts] == [1, 2, 3, 4]
    assert {r["n"] for r in counts} == {32}, "a snapshot covers the whole league"


def test_a_weeks_ranking_cannot_see_its_own_results(four_weeks):
    """The ranking for week N is the state going *into* week N.

    Including week N's own games would rank a team by a result it had not
    produced yet -- the single mistake a history like this can make, and one
    that would look entirely plausible on screen.
    """
    four_weeks.rebuild_power_history(2025)
    rows = {
        r["week"]: r for r in db.query(
            "SELECT week, wins, losses FROM power_snapshots "
            "WHERE season = 2025 AND team = 'KC' ORDER BY week"
        )
    }
    # KC won every week, so by week N it has N-1 wins, never N.
    assert (rows[1]["wins"], rows[1]["losses"]) == (0, 0)
    assert rows[2]["wins"] == 1
    assert rows[3]["wins"] == 2
    assert rows[4]["wins"] == 3


def test_a_rebuild_never_overwrites_a_recorded_week(four_weeks):
    """A reconstruction is a stand-in until the real thing exists, not a
    replacement for it: a live row is what was actually on screen that week."""
    four_weeks.rebuild_power_history(2025)
    db.execute(
        "UPDATE power_snapshots SET source = 'live', rank = 99 "
        "WHERE season = 2025 AND week = 2 AND team = 'KC'"
    )
    four_weeks.rebuild_power_history(2025, overwrite=False)
    row = db.query(
        "SELECT rank, source FROM power_snapshots "
        "WHERE season = 2025 AND week = 2 AND team = 'KC'"
    )[0]
    assert row["source"] == "live" and row["rank"] == 99


def test_no_week_is_invented_past_the_schedule(four_weeks):
    """A four-week schedule produces four rankings, not five. The week after
    the last one is real while the season is running and fiction once it has
    finished, and nothing on screen would distinguish them."""
    four_weeks.rebuild_power_history(2025)
    weeks = [r["week"] for r in db.query(
        "SELECT DISTINCT week FROM power_snapshots WHERE season = 2025")]
    assert max(weeks) == 4


def test_movement_is_measured_against_the_previous_week_we_hold(four_weeks, client):
    """Against the previous *stored* week, not literally week-1.

    A gap in the history -- the app switched off for a fortnight -- would
    otherwise be reported as one week of enormous movement that never happened.
    """
    four_weeks.rebuild_power_history(2025)
    db.execute("DELETE FROM power_snapshots WHERE season = 2025 AND week = 3")

    body = client.get("/api/power/history?season=2025&week=4").json()
    assert body["compared_to"] == 2, "week 3 is missing, so 4 is compared to 2"
    assert body["weeks"] == [1, 2, 4]


def test_the_history_says_which_weeks_were_reconstructed(four_weeks, client):
    """Rebuilt and recorded are not the same claim, and the difference is
    invisible in the numbers themselves."""
    four_weeks.rebuild_power_history(2025)
    body = client.get("/api/power/history?season=2025").json()
    assert set(body["sources"].values()) == {"rebuilt"}


def test_an_empty_history_is_reported_rather_than_faked(pipeline, temp_env, client):
    db.execute("DELETE FROM power_snapshots")
    body = client.get("/api/power/history?season=1999").json()
    assert body["weeks"] == [] and body["teams"] == [] and body["week"] is None
