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
