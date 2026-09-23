"""The injury report as a table of four things: who, what, how long, how serious.

It used to carry the position and a polling timestamp instead of the injury and
its duration, with the actual injury buried in a 400-character prose comment.
Two behaviours here had never been right and are pinned as a result: a player
who recovers has to leave the report, and a spell has to start when the spell
started.
"""

import pytest
from fastapi.testclient import TestClient

from nflpicker import db
from nflpicker.pipeline import RefreshResult


@pytest.fixture()
def client(temp_env):
    from nflpicker.api import create_app

    with TestClient(create_app(start_scheduler=False, bootstrap=False)) as c:
        yield c


def _listed(client, team=None):
    body = client.get("/api/news").json()
    return {i["player"]: i for i in body["injuries"]
            if team is None or i["team"] == team}


def _feed(pipeline, rows):
    """Run one news refresh with a fixed injury list."""
    pipeline.sources_for_news = None
    original = pipeline.refresh_news

    from unittest.mock import patch

    with patch.object(type(pipeline), "_injury_feed", create=True, return_value=rows):
        original(RefreshResult())


def test_a_recovered_player_leaves_the_report(pipeline, temp_env, client):
    """The feed stops mentioning a player rather than marking him healthy, and
    rows are only written on a change -- so nothing ever contradicted the last
    one and a week 2 hamstring still read "Out" in week 12."""
    db.execute("DELETE FROM injuries")
    db.execute(
        "INSERT INTO injuries(team, player, status, injury, first_seen, updated_at) "
        "VALUES('KC','Old Injury','Out','Hamstring',"
        "'2025-09-01T00:00:00Z','2025-09-01T00:00:00Z')"
    )
    assert "Old Injury" in _listed(client, "KC")

    # A refresh whose feed no longer carries him.
    pipeline.refresh_news(RefreshResult())
    assert "Old Injury" not in _listed(client, "KC"), (
        "a player the feed has stopped reporting has cleared")


def test_an_empty_feed_does_not_clear_the_report(pipeline, temp_env, client, monkeypatch):
    """Absence only means recovery in a list that arrived intact. A feed that
    returns nothing must not read as the whole league recovering at once."""
    db.execute("DELETE FROM injuries")
    db.execute(
        "INSERT INTO injuries(team, player, status, injury, first_seen, updated_at) "
        "VALUES('KC','Still Hurt','Out','Hamstring',"
        "'2025-09-01T00:00:00Z','2025-09-01T00:00:00Z')"
    )
    from nflpicker.sources import demo

    monkeypatch.setattr(demo, "generate_injuries", lambda season, per_team=3: [])
    pipeline.refresh_news(RefreshResult())
    assert "Still Hurt" in _listed(client, "KC")


def test_how_long_prefers_the_expected_return(client, temp_env):
    """Time served is always knowable; when a player is due back is what
    actually changes a decision, so it wins when the feed supplies it."""
    from datetime import timedelta

    from nflpicker.api import _how_long
    from nflpicker.util import now

    began = (now() - timedelta(days=21)).isoformat()
    back = (now() + timedelta(days=14)).isoformat()
    text = _how_long({"status": "Injured Reserve", "first_seen": began,
                      "return_date": back})
    assert text == "back in ~2 weeks", (
        "when a return is known it replaces time served rather than joining it")

    # With no return date, time served is what there is to say.
    assert _how_long({"status": "Doubtful", "first_seen": began,
                      "return_date": None}) == "3 weeks"


def test_a_long_term_designation_is_not_reported_as_this_week(temp_env):
    """IR and PUP carry a minimum absence in the rules. With no date attached,
    saying "this week" would be wrong in a way a reader would act on."""
    from nflpicker.api import _how_long
    from nflpicker.util import now

    text = _how_long({"status": "Injured Reserve", "first_seen": now().isoformat(),
                      "return_date": None})
    assert "multi-week" in text


def test_a_vague_listing_becoming_specific_is_recorded(pipeline, temp_env, monkeypatch):
    """Status is not the only field worth a row. A player whose listing goes
    from nothing to "Right Hamstring Strain" while staying Questionable is
    exactly the update the report exists to carry."""
    db.execute("DELETE FROM injuries")
    from nflpicker.sources import demo

    rows = [{"team": "KC", "player": "A Player", "position": "WR",
             "status": "Questionable", "detail": "", "injury": None,
             "return_date": None, "updated_at": "2025-09-01T00:00:00Z"}]
    monkeypatch.setattr(demo, "generate_injuries", lambda season, per_team=3: rows)
    pipeline.refresh_news(RefreshResult())

    rows[0] = dict(rows[0], injury="Right Hamstring Strain",
                   updated_at="2025-09-02T00:00:00Z")
    pipeline.refresh_news(RefreshResult())

    stored = db.query(
        "SELECT injury FROM injuries WHERE player = 'A Player' ORDER BY updated_at")
    assert [r["injury"] for r in stored] == [None, "Right Hamstring Strain"]


def test_a_second_spell_does_not_inherit_the_first_ones_start(pipeline, temp_env,
                                                              monkeypatch):
    """A player hurt in September and again in December is not hurt since
    September, which is what carrying first_seen through a recovery would say."""
    db.execute("DELETE FROM injuries")
    from nflpicker.sources import demo

    rows = [{"team": "KC", "player": "B Player", "position": "WR",
             "status": "Out", "detail": "", "injury": "Ankle",
             "return_date": None, "updated_at": "2025-09-01T00:00:00Z"}]
    monkeypatch.setattr(demo, "generate_injuries", lambda season, per_team=3: rows)
    pipeline.refresh_news(RefreshResult())

    # Recovered: the feed drops him, so the refresh marks him Active.
    rows.clear()
    rows.append({"team": "KC", "player": "Someone Else", "position": "WR",
                 "status": "Out", "detail": "", "injury": "Knee",
                 "return_date": None, "updated_at": "2025-10-01T00:00:00Z"})
    pipeline.refresh_news(RefreshResult())

    # Hurt again, months later.
    rows.clear()
    rows.append({"team": "KC", "player": "B Player", "position": "WR",
                 "status": "Out", "detail": "", "injury": "Hamstring",
                 "return_date": None, "updated_at": "2025-12-01T00:00:00Z"})
    pipeline.refresh_news(RefreshResult())

    latest = db.query(
        "SELECT first_seen FROM injuries WHERE player = 'B Player' "
        "ORDER BY id DESC LIMIT 1")[0]
    assert latest["first_seen"].startswith("2025-12"), (
        "the December spell started in December")


def _listed_row(team, player, status="Out"):
    db.execute(
        "INSERT INTO injuries(team, player, status, injury, first_seen, updated_at) "
        "VALUES(?,?,?,'Hamstring','2025-09-01T00:00:00Z','2025-09-01T00:00:00Z')",
        (team, player, status),
    )


def test_a_team_missing_from_the_response_is_left_alone(pipeline, temp_env,
                                                        monkeypatch, client):
    """The feed is grouped by team, so a truncated response drops whole teams.

    A team we did not hear about is one we learned nothing about. Clearing
    league-wide on a partial response would mark healthy every player on every
    team that happened to be missing -- which reads as good news rather than as
    the fault it is.
    """
    db.execute("DELETE FROM injuries")
    _listed_row("KC", "Chiefs Guy")
    _listed_row("BUF", "Bills Guy")

    from nflpicker.sources import demo

    # A response covering KC only, and saying nobody on KC is hurt any more.
    monkeypatch.setattr(demo, "generate_injuries", lambda season, per_team=3: [
        {"team": "KC", "player": "Someone New", "position": "WR", "status": "Out",
         "detail": "", "injury": "Knee", "return_date": None,
         "updated_at": "2025-10-01T00:00:00Z"},
    ])
    pipeline.refresh_news(RefreshResult())

    names = _listed(client)
    assert "Chiefs Guy" not in names, "KC was covered, so its absentee cleared"
    assert "Bills Guy" in names, "BUF was not in the response and must be untouched"


def test_an_implausible_sweep_is_refused(pipeline, temp_env, monkeypatch, client):
    """A covered team whose group came back empty through a fault upstream is
    the case team scoping cannot catch. Recoveries are gradual, so a response
    retiring most of the standing report at once is far likelier to be broken
    than true."""
    db.execute("DELETE FROM injuries")
    for i in range(14):
        _listed_row("KC", f"Player {i}")

    from nflpicker.sources import demo

    monkeypatch.setattr(demo, "generate_injuries", lambda season, per_team=3: [
        {"team": "KC", "player": "Player 0", "position": "WR", "status": "Out",
         "detail": "", "injury": "Knee", "return_date": None,
         "updated_at": "2025-10-01T00:00:00Z"},
    ])
    pipeline.refresh_news(RefreshResult())

    still = _listed(client, "KC")
    assert len(still) == 14, (
        f"13 of 14 is not a Tuesday; expected none cleared, got {len(still)} left")


def test_a_small_report_still_clears_normally(pipeline, temp_env, monkeypatch, client):
    """The proportional guard needs a floor. With three players listed, clearing
    two is 67% and entirely ordinary -- without a floor the guard would fire
    hardest on exactly the small early-season reports where every clearing is
    legitimate."""
    db.execute("DELETE FROM injuries")
    for i in range(3):
        _listed_row("KC", f"Player {i}")

    from nflpicker.sources import demo

    monkeypatch.setattr(demo, "generate_injuries", lambda season, per_team=3: [
        {"team": "KC", "player": "Player 0", "position": "WR", "status": "Out",
         "detail": "", "injury": "Knee", "return_date": None,
         "updated_at": "2025-10-01T00:00:00Z"},
    ])
    pipeline.refresh_news(RefreshResult())
    assert len(_listed(client, "KC")) == 1


# --------------------------------------------------- which statuses count

def test_a_status_the_filter_has_never_seen_keeps_the_player(temp_env):
    """The rule is an exclusion, and it used to be an inclusion.

    A list of statuses that counted meant every label not on it removed the
    player from the page without a word. "Day-To-Day" is one of the feed's
    commonest statuses and was one of those; so were "Suspension" (the list
    had "suspended"), "Game Time Decision" and "IR-R". Teams that plainly had
    players hurt showed a short list and nothing said why.
    """
    from nflpicker.availability import is_notable_injury

    for status in ("Day-To-Day", "Day To Day", "Suspension", "IR-R",
                   "Game Time Decision", "Reserve/Retired",
                   "Something ESPN Renames It To Next Season"):
        assert is_notable_injury(status), f"{status} belongs on the report"


def test_the_ways_a_feed_says_fine_are_still_excluded(temp_env):
    """An injury report that lists everybody is not an injury report."""
    from nflpicker.availability import is_notable_injury

    for status in ("Active", "active", " Healthy ", "Probable",
                   "Full Participation", "Active/Healthy", "Cleared",
                   "Available", "-", "", "   ", None):
        assert not is_notable_injury(status), f"{status!r} is not an injury"


def test_limited_participation_is_not_read_as_participation(temp_env):
    """The exclusion matches whole words, so the presence of "participation"
    in a status does not make a limited practice a clean bill of health."""
    from nflpicker.availability import is_notable_injury

    assert is_notable_injury("Limited Participation")
    assert is_notable_injury("Did Not Participate")
    assert not is_notable_injury("Full Participation")


def test_a_day_to_day_player_reaches_the_page(pipeline, temp_env, client):
    """End to end: the status that was being dropped now arrives on screen."""
    db.execute("DELETE FROM injuries")
    db.execute(
        "INSERT INTO injuries(team, player, status, injury, first_seen, updated_at) "
        "VALUES('CHI','Day To Day Guy','Day-To-Day','Ankle',"
        "'2025-09-01T00:00:00Z','2025-09-01T00:00:00Z')"
    )
    assert "Day To Day Guy" in _listed(client, "CHI")


# ------------------------------------------- what the feed is allowed to lose

def _injury_payload(group: dict) -> dict:
    return {"injuries": [group]}


def test_an_unrecognised_team_name_does_not_delete_the_team(temp_env):
    """`try_resolve(displayName or abbreviation)` is not a fallback.

    A display name that is present but unrecognised short-circuits the `or`,
    so the abbreviation is never tried and every injured player on that team
    is dropped without a word.
    """
    from nflpicker.sources.espn import _resolve_team

    assert _resolve_team({"abbreviation": "KC"}) == "KC"
    assert _resolve_team({"displayName": "Washington Football Club",
                          "abbreviation": "WAS"}) == "WAS"
    assert _resolve_team({"team": {"abbreviation": "SF"}}) == "SF"
    assert _resolve_team({"displayName": "Not A Team"}) is None


def test_a_player_whose_status_will_not_parse_is_still_listed(temp_env):
    """Being on the injury report is itself the information.

    The status was read from two fields and the player dropped when neither
    produced one -- so anyone whose label arrived in a shape the parser did
    not know about vanished, which is the opposite of what their presence in
    an injury feed means.
    """
    from nflpicker.sources.espn import INJURY_STATUS_UNKNOWN, _injury_status

    assert _injury_status({"status": "Questionable"}) == "Questionable"
    assert _injury_status({"status": {"name": "Out"}}) == "Out"
    assert _injury_status({"type": {"description": "Injured Reserve"}}) == "Injured Reserve"
    assert _injury_status({"type": {"name": "Doubtful"}}) == "Doubtful"
    assert _injury_status({}) == INJURY_STATUS_UNKNOWN

    # And that placeholder has to survive the filter, or the fix does nothing.
    from nflpicker.availability import is_notable_injury

    assert is_notable_injury(INJURY_STATUS_UNKNOWN)


def test_the_whole_group_survives_a_broken_name(temp_env):
    """End to end through the parser: three players, none of them lost."""
    from nflpicker.sources.espn import EspnSource

    source = EspnSource.__new__(EspnSource)
    source.covered_teams = set()
    payload = _injury_payload({
        "displayName": "A Name Nobody Knows",
        "abbreviation": "CHI",
        "injuries": [
            {"athlete": {"displayName": "A Player"}, "status": "Day-To-Day"},
            {"athlete": {"fullName": "B Player"}, "status": {"name": "Out"}},
            {"athlete": {"displayName": "C Player"}},          # no status at all
        ],
    })
    rows = []
    for group in payload["injuries"]:
        from nflpicker.sources.espn import _injury_status, _resolve_team

        team = _resolve_team(group)
        for item in group["injuries"]:
            athlete = item.get("athlete") or {}
            name = athlete.get("displayName") or athlete.get("fullName")
            rows.append({"team": team, "player": name,
                         "status": _injury_status(item)})
    assert [r["team"] for r in rows] == ["CHI", "CHI", "CHI"]
    assert [r["player"] for r in rows] == ["A Player", "B Player", "C Player"]

    from nflpicker.availability import is_notable_injury

    assert all(is_notable_injury(r["status"]) for r in rows), (
        "every one of them belongs on the report")
