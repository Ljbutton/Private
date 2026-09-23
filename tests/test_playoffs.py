"""Seeding, clinching and the bracket.

The simulator seeds a playoff field twenty thousand times over, and every one
of those is a hypothetical. This is the other question -- what is true now,
from games that have been played -- which is what a bracket has to be drawn
from, and it had no implementation at all until there was a bracket to draw.
"""

import pytest
from fastapi.testclient import TestClient

from nflpicker import db
from nflpicker.standings import (
    bracket,
    build_records,
    clinch_marks,
    label_rounds,
    picture,
    seed_conference,
)
from nflpicker.teams import DIVISIONS, TEAMS


@pytest.fixture()
def client(temp_env):
    from nflpicker.api import create_app

    with TestClient(create_app(start_scheduler=False, bootstrap=False)) as c:
        yield c


def _g(home, away, hs, as_, week=1, status="final"):
    return {"game_id": f"{week}-{home}-{away}", "week": week, "home": home,
            "away": away, "home_score": hs, "away_score": as_, "status": status}


def test_a_win_a_loss_and_a_tie_are_all_counted(temp_env):
    records = build_records([
        _g("KC", "DEN", 24, 10),
        _g("KC", "LV", 17, 17, week=2),
        _g("LAC", "KC", 20, 13, week=3),
    ])
    kc = records["KC"]
    assert (kc.wins, kc.losses, kc.ties) == (1, 1, 1)
    assert kc.label() == "1-1-1"
    # A tie is half a win in every rate the league computes.
    assert kc.win_pct == pytest.approx(1.5 / 3)
    # All three were division games, so the division record matches.
    assert kc.div_w == 1 and kc.div_l == 1 and kc.div_t == 1


def test_an_unplayed_game_counts_for_nobody(temp_env):
    records = build_records([_g("KC", "DEN", None, None, status="scheduled")])
    assert records["KC"].played == 0


def test_head_to_head_settles_a_dead_heat(temp_env):
    """Two teams level on everything a sort key can hold: the rule is the game
    between them, and a sort key cannot express a pairwise comparison."""
    games = []
    # Both go 1-1 against the same two outside opponents, and split nothing:
    # BUF beat MIA head to head.
    games.append(_g("BUF", "MIA", 20, 17, week=1))
    games.append(_g("NYJ", "BUF", 20, 17, week=2))
    games.append(_g("MIA", "NE", 20, 17, week=3))
    records = build_records(games)
    rows = seed_conference("AFC", records)
    order = [r["team"] for r in rows]
    assert order.index("BUF") < order.index("MIA"), (
        "BUF won the game between them")


def test_the_four_division_winners_take_the_top_four_seeds(temp_env):
    """A 1-0 division leader outranks a 3-0 team that finished second."""
    games = [
        # AFC East: BUF 1-0, everyone else idle.
        _g("BUF", "NYJ", 20, 10),
        # AFC North: BAL 3-0, PIT 2-1 -- PIT is the best non-winner.
        _g("BAL", "PIT", 20, 10, week=1),
        _g("BAL", "CLE", 20, 10, week=2),
        _g("BAL", "CIN", 20, 10, week=3),
        _g("PIT", "CLE", 20, 10, week=4),
        _g("PIT", "CIN", 20, 10, week=5),
    ]
    rows = seed_conference("AFC", build_records(games))
    top4 = [r for r in rows if r["seed"] <= 4]
    assert all(r["division_winner"] for r in top4)
    assert {r["division"].split()[1] for r in top4} == {"East", "North",
                                                        "South", "West"}
    pit = next(r for r in rows if r["team"] == "PIT")
    assert pit["seed"] == 5, "the best non-winner is the first wild card"
    assert not pit["division_winner"]


def _finished_season(wins_by_team: dict[str, int]) -> list[dict]:
    """A completed 17-game season for every team, to the given win totals.

    Cross-conference throughout, so a team's conference and division records
    stay empty and the only thing separating anyone is the win column. That
    is what these tests are about; the tiebreak tests above cover the rest.
    """
    games: list[dict] = []
    week = 0
    for team, wins in wins_by_team.items():
        others = [t for t in TEAMS
                  if TEAMS[t].conference != TEAMS[team].conference]
        for i in range(17):
            week += 1
            opponent = others[i % len(others)]
            if i < wins:
                games.append(_g(team, opponent, 30, 3, week=week))
            else:
                games.append(_g(team, opponent, 3, 30, week=week))
    return games


def _afc_season(kc_wins: int, others: int) -> list[dict]:
    afc = [t for t in TEAMS if TEAMS[t].conference == "AFC"]
    plan = {t: (kc_wins if t == "KC" else others) for t in afc}
    return _finished_season(plan)


def test_a_team_that_cannot_be_caught_has_clinched(temp_env):
    """Seventeen wins, and no rival who could reach even ten."""
    games = _afc_season(kc_wins=17, others=5)
    records = build_records(games)
    rows = seed_conference("AFC", records)
    marks = clinch_marks(rows, records)
    assert marks["KC"] == "z", "nobody in the conference can reach them"


def test_nothing_is_clinched_in_september(temp_env):
    """One week in, every team can still catch every other."""
    records = build_records([_g("KC", "DEN", 24, 10)])
    rows = seed_conference("AFC", records)
    marks = clinch_marks(rows, records)
    assert not any(marks.values()), "a clinch in week one would be a bug"


def test_a_rival_who_could_only_draw_level_still_blocks_the_clinch(temp_env):
    """The deliberate conservatism, pinned.

    A tie is settled by tiebreakers that depend on games not yet played, so
    claiming one in advance is a guess. The mark arrives a little late rather
    than a little early, which is the right direction for something that says
    "this is settled".
    """
    afc = [t for t in TEAMS if TEAMS[t].conference == "AFC"]
    # KC finishes on 12; one rival has 12 still reachable, the rest are done.
    plan = {t: 3 for t in afc}
    plan["KC"] = 12
    games = _finished_season(plan)
    # Take back one rival's season so they have games in hand.
    rival = afc[1] if afc[0] == "KC" else afc[0]
    games = [g for g in games if rival not in (g["home"], g["away"])]
    games.extend(_g(rival, "PHI", 30, 3, week=900 + i) for i in range(12))
    records = build_records(games)
    rows = seed_conference("AFC", records)
    marks = clinch_marks(rows, records)
    assert marks.get("KC") != "z", (
        f"{rival} can still reach 12, so the top seed is not settled")


def test_a_team_that_cannot_reach_the_field_is_out(temp_env):
    """Seven rivals already past what they could finish on."""
    afc = [t for t in TEAMS if TEAMS[t].conference == "AFC"]
    plan = {t: (12 if i < 7 else 1) for i, t in enumerate(afc)}
    records = build_records(_finished_season(plan))
    rows = seed_conference("AFC", records)
    marks = clinch_marks(rows, records)
    assert marks[afc[-1]] == "e", "one win with the season over is not a berth"
    # Seven on twelve and nine on one: the seven are in, however they are
    # ordered among themselves.
    assert marks[afc[0]] in ("z", "y", "x")


def test_eight_teams_level_for_seven_places_have_not_clinched(temp_env):
    """One of them misses out and only a tiebreaker says which."""
    afc = [t for t in TEAMS if TEAMS[t].conference == "AFC"]
    plan = {t: (12 if i < 8 else 1) for i, t in enumerate(afc)}
    records = build_records(_finished_season(plan))
    rows = seed_conference("AFC", records)
    marks = clinch_marks(rows, records)
    assert all(marks.get(t, "") == "" for t in afc[:8]), (
        "eight into seven does not go, so nobody in the eight is safe")


# ------------------------------------------------------------ the bracket

def _post(week, home, away, hs=None, as_=None):
    return {"game_id": f"p{week}-{home}", "week": week, "home": home,
            "away": away, "home_score": hs, "away_score": as_,
            "kickoff": f"2027-01-{10 + week:02d}T18:00:00Z",
            "status": "final" if hs is not None else "scheduled"}


def test_rounds_are_named_by_how_many_games_they_hold(temp_env):
    """Not by their number.

    The feed numbers postseason weeks 1..5 with the Pro Bowl in the middle,
    and that numbering has changed before. Six games is a wild-card round in
    any year and under any numbering.
    """
    games = ([_post(1, "KC", f"D{i}") for i in range(6)]
             + [_post(2, "KC", f"E{i}") for i in range(4)]
             + [_post(3, "KC", f"F{i}") for i in range(2)]
             + [_post(5, "KC", "G")])
    assert label_rounds(games) == {1: "Wild Card", 2: "Divisional",
                                   3: "Conference", 5: "Super Bowl"}


def test_the_bracket_carries_seeds_and_a_winner(temp_env):
    afc = [t for t in TEAMS if TEAMS[t].conference == "AFC"]
    plan = {t: (14 - i) for i, t in enumerate(afc)}
    pic = picture(_finished_season(plan))
    # Six games, so the round names itself Wild Card.
    post = [_post(1, afc[0], afc[1], 31, 17)]
    post += [_post(1, afc[2 * i], afc[2 * i + 1]) for i in range(1, 6)]
    rounds = bracket(post, pic["conferences"])
    assert [r["round"] for r in rounds] == ["Wild Card"]
    game = next(g for g in rounds[0]["games"] if g["home_score"] is not None)
    assert game["winner"] == afc[0]
    assert game["home_seed"] and game["away_seed"]
    assert game["conference"] == "AFC", "both sides are AFC teams"


def test_the_endpoint_answers_with_both_conferences(temp_env, client):
    db.execute("DELETE FROM games")
    for i, team in enumerate([t for t in TEAMS][:8]):
        db.execute(
            "INSERT INTO games(game_id, season, week, season_type, kickoff,"
            " home, away, home_score, away_score, status, updated_at) "
            "VALUES(?,2026,?,'REG','2026-09-10T17:00:00Z',?,?,?,?,'final',?)",
            (f"g{i}", i + 1, team, [t for t in TEAMS][20 + i], 30, 3,
             "2026-09-11T00:00:00Z"))
    out = client.get("/api/playoffs?season=2026").json()
    assert set(out["conferences"]) == {"AFC", "NFC"}
    assert len(out["conferences"]["AFC"]) == 16
    assert len(out["conferences"]["NFC"]) == 16
    assert out["has_postseason"] is False
    assert set(out["legend"]) == {"z", "y", "x", "e"}


def test_every_division_is_represented_in_the_top_four(temp_env):
    """A conference has four divisions and four division winners, always."""
    pic = picture(_finished_season({t: 8 for t in TEAMS}))
    for conf, rows in pic["conferences"].items():
        divisions = {r["division"] for r in rows if r["division_winner"]}
        assert len(divisions) == 4, f"{conf} must seed four division winners"
        assert divisions == {d for d in DIVISIONS if d.startswith(conf)}


# ------------------------------------------ where January sits in the year

def test_the_postseason_is_numbered_on_from_the_regular_season(temp_env):
    """The feed restarts its count in January and the database must not.

    A wild-card game arrives as "week 1". Stored that way it sorts alongside
    the opening Sunday of September in everything that orders a season by
    week -- the rolling-form window in the feature builder, the power history,
    and every query that asks for a week by number. The offset is applied once,
    where the feed is read.
    """
    from nflpicker.sources.espn import REGULAR_SEASON_WEEKS, parse_scoreboard

    def _payload(season_type: int, week: int) -> dict:
        return {
            "season": {"year": 2026, "type": season_type},
            "week": {"number": week},
            "events": [{
                "id": f"{season_type}-{week}",
                "date": "2027-01-10T18:00:00Z",
                "season": {"year": 2026, "type": season_type},
                "week": {"number": week},
                "competitions": [{"competitors": [
                    {"homeAway": "home", "team": {"abbreviation": "KC"}, "score": "31"},
                    {"homeAway": "away", "team": {"abbreviation": "BUF"}, "score": "17"},
                ]}],
            }],
        }

    regular = parse_scoreboard(_payload(2, 1))[0]
    assert regular["season_type"] == "REG"
    assert regular["week"] == 1

    wild_card = parse_scoreboard(_payload(3, 1))[0]
    assert wild_card["season_type"] == "POST"
    assert wild_card["week"] == REGULAR_SEASON_WEEKS + 1 == 19, (
        "a January game that sorts as September corrupts every rolling window")

    # And the final, which the feed numbers 5 because week 4 is the Pro Bowl.
    assert parse_scoreboard(_payload(3, 5))[0]["week"] == 23


def test_the_demo_season_numbers_its_postseason_the_same_way(temp_env):
    """The synthetic season exists so January is testable in September; it is
    only useful if it has the shape the real thing does."""
    from nflpicker.sources import demo

    games = demo.generate_season(2026, through_week=19)["games"]
    post = [g for g in games if g["season_type"] == "POST"]
    assert post, "a finished regular season produces a bracket"
    assert min(g["week"] for g in post) > 18
    # Six, four, two, one -- which is what lets the rounds name themselves.
    counts = sorted(
        (w, sum(1 for g in post if g["week"] == w))
        for w in {g["week"] for g in post})
    assert [n for _, n in counts] == [6, 4, 2, 1]


def test_a_playoff_round_is_its_own_week_on_the_board(temp_env, client):
    """Week 19 is the wild-card round, not the opening Sunday of September."""
    db.execute("DELETE FROM games")
    for week, season_type, gid in ((1, "REG", "sept"), (19, "POST", "january")):
        db.execute(
            "INSERT INTO games(game_id, season, week, season_type, kickoff,"
            " home, away, status, updated_at) "
            "VALUES(?,2026,?,?,'2026-09-10T17:00:00Z','KC','BUF','scheduled',?)",
            (gid, week, season_type, "2026-09-01T00:00:00Z"))

    september = client.get("/api/games?season=2026&week=1").json()
    january = client.get("/api/games?season=2026&week=19").json()
    assert [g["game_id"] for g in september["games"]] == ["sept"]
    assert [g["game_id"] for g in january["games"]] == ["january"]
    assert january["season_type"] == "POST"
    assert january["byes"] == [], "nobody is on a bye in January"
    assert january["survivor_pick"] is None
