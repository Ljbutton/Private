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
    """Against the previous week we *cut*, not literally the week before.

    A gap in the history -- the app switched off for a fortnight -- would
    otherwise be reported as one week of enormous movement that never happened.
    """
    four_weeks.rebuild_power_history(2025)
    db.execute("DELETE FROM power_snapshots WHERE season = 2025 AND week = 3")
    # These weeks were cut while the app was running for them; see the test
    # below for why a reconstruction is not something a team can move against.
    db.execute("UPDATE power_snapshots SET source = 'live' WHERE season = 2025")

    body = client.get("/api/power/history?season=2025&week=4").json()
    assert body["compared_to"] == 2, "week 3 is missing, so 4 is compared to 2"
    assert body["weeks"] == [1, 2, 4]


def test_nothing_moves_against_a_week_that_was_reconstructed(four_weeks, client):
    """A backfilled week is not a previous position.

    An app first opened in week two showed every team up or down against a
    week one that had been computed after the fact, from games that had
    already been played, and had never been on screen for anyone to have
    moved away from. Those arrows described the backfill, not the season. The
    reconstruction stays -- it is what draws the trend line -- it just is not
    something a rank can be measured against.
    """
    four_weeks.rebuild_power_history(2025)
    db.execute(
        "UPDATE power_snapshots SET source = 'live' WHERE season = 2025 AND week = 4")

    body = client.get("/api/power/history?season=2025&week=4").json()
    assert body["compared_to"] is None
    assert {row["move"] for row in body["teams"]} == {None}
    assert body["weeks"] == [1, 2, 3, 4], "the rebuilt weeks are still held"


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


def test_a_past_week_does_not_change_once_it_is_written(four_weeks):
    """The whole value of a history is that it says what was thought *then*.

    A week's snapshot is a function of the games completed before it, so a
    later recompute would reconstruct the same numbers -- but only while the
    rating code stays put. Pinning the row rather than the arithmetic is what
    survives the next change to the rating, which is exactly when a history
    quietly rewriting itself would matter and be hardest to notice.
    """
    four_weeks.rebuild_power_history(2025)
    before = {
        (r["week"], r["team"]): (r["rank"], r["power"], r["captured_at"])
        for r in db.query(
            "SELECT week, team, rank, power, captured_at FROM power_snapshots "
            "WHERE season = 2025 AND week <= 3")
    }

    # New results arrive, and the rating itself moves under them.
    for week in (5, 6):
        _game(f"late{week}", 2025, week, "KC", "DEN", 45, 3)
        _game(f"lateb{week}", 2025, week, "BUF", "NYJ", 40, 6)
    import nflpicker.ratings.power as power_mod

    original = power_mod.PYTHAGOREAN_POINTS
    try:
        power_mod.PYTHAGOREAN_POINTS = original * 3
        four_weeks.rebuild_power_history(2025)
    finally:
        power_mod.PYTHAGOREAN_POINTS = original

    after = {
        (r["week"], r["team"]): (r["rank"], r["power"], r["captured_at"])
        for r in db.query(
            "SELECT week, team, rank, power, captured_at FROM power_snapshots "
            "WHERE season = 2025 AND week <= 3")
    }
    assert after == before, "weeks 1-3 are history and must not move"


def test_recompute_fills_the_weeks_before_it_on_its_own(pipeline, temp_env):
    """Installed in week 10, you should still be able to look at week 3.

    The first recompute reconstructs every earlier week rather than starting
    the history at whatever week the app happened to be installed in.
    """
    db.execute("DELETE FROM games")
    db.execute("DELETE FROM power_snapshots")
    for week in range(1, 6):
        _game(f"g{week}", 2025, week, "KC", "DEN", 30, 10)
    assert not db.query("SELECT 1 FROM power_snapshots LIMIT 1")

    pipeline.recompute()
    weeks = sorted(r["week"] for r in db.query(
        "SELECT DISTINCT week FROM power_snapshots WHERE season = 2025"))
    assert weeks[:5] == [1, 2, 3, 4, 5], f"got {weeks}"


def test_a_live_cut_is_never_rewritten(four_weeks):
    """Once taken, a week's ranking says the same thing in December.

    It used to be rewritten on every recompute while its week was current,
    which made "the power rankings" something that quietly reordered itself
    several times a day -- and made the Move column a comparison between two
    numbers that had both moved since anyone last looked.
    """
    db.execute(
        "INSERT INTO power_snapshots"
        "(season, week, team, rank, power, elo, pythagorean,"
        " wins, losses, ties, source, captured_at) "
        "VALUES(2025, 2, 'KC', 99, 0, 1500, 0.5, 0, 0, 0, 'live', 'then')")

    from nflpicker.ratings.elo import run_elo

    power = four_weeks.power_from([], run_elo([]), 2025, week=2)
    written = four_weeks.store_power_snapshot(2025, 2, power, [])

    assert written == 0, "the cut stands"
    row = db.query("SELECT rank, captured_at FROM power_snapshots "
                   "WHERE season = 2025 AND week = 2 AND team = 'KC'")[0]
    assert row["rank"] == 99 and row["captured_at"] == "then"


def test_the_cut_waits_for_its_day(four_weeks):
    """Wednesday, and not before.

    Monday and Tuesday are deliberately quiet even though the week number has
    already turned over: last week's cut is the most recent claim anyone has
    made, and inventing a new one from a weekend still being counted is a
    worse answer than waiting a day.
    """
    from datetime import datetime

    db.execute(
        "INSERT INTO power_snapshots"
        "(season, week, team, rank, power, elo, pythagorean,"
        " wins, losses, ties, source, captured_at) "
        "VALUES(2025, 1, 'KC', 1, 0, 1500, 0.5, 0, 0, 0, 'live', 'then')")

    monday = datetime(2025, 9, 15)
    wednesday = datetime(2025, 9, 17)
    assert monday.weekday() == 0 and wednesday.weekday() == 2
    assert not four_weeks.ranking_cut_due(2025, 2, now=monday)
    assert four_weeks.ranking_cut_due(2025, 2, now=wednesday)


def test_the_first_cut_of_a_season_does_not_wait(four_weeks):
    """An app first opened on a Sunday should not sit with an empty history
    until the following Wednesday. The weekly rhythm starts from the first
    cut, whenever that happens to be."""
    from datetime import datetime

    monday = datetime(2025, 9, 15)
    assert monday.weekday() == 0, "a day the weekly rule would say no to"
    assert four_weeks.ranking_cut_due(2025, 2, now=monday)
