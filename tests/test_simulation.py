"""The season simulation must satisfy hard structural identities: exactly 14
playoff teams, 8 division winners, 2 byes, 1 champion, in every run."""

from nflpicker.ratings.elo import run_elo
from nflpicker.ratings.power import build_power_ratings
from nflpicker.sim.season import simulate_season
from nflpicker.sources import demo


def _sim(through_week=10, n_sims=4000):
    season = demo.generate_season(2025, through_week=through_week)
    finals = sorted((g for g in season["games"] if g["status"] == "final"),
                    key=lambda g: g["kickoff"])
    elo = run_elo(finals)
    power = build_power_ratings(elo.as_points(), week=through_week, elo_raw=elo.snapshot())
    return simulate_season(2025, season["games"], power, n_sims=n_sims)


def test_probabilities_sum_to_the_number_of_slots():
    sim = _sim()
    total = lambda attr: sum(getattr(t, attr) for t in sim.teams.values())  # noqa: E731
    assert abs(total("playoff_prob") - 14) < 0.02
    assert abs(total("division_prob") - 8) < 0.02
    assert abs(total("bye_prob") - 2) < 0.02
    assert abs(total("sb_prob") - 1) < 0.02
    assert abs(total("conference_prob") - 2) < 0.02


def test_expected_wins_sum_to_games_played():
    sim = _sim()
    # 272 regular season games, one win awarded per game.
    assert abs(sum(t.exp_wins for t in sim.teams.values()) - 272) < 0.5


def test_every_team_appears_with_a_plausible_win_total():
    sim = _sim()
    assert len(sim.teams) == 32
    for t in sim.teams.values():
        assert 0 <= t.exp_wins <= 17
        assert t.wins_p10 <= t.exp_wins + 1e-6
        assert t.wins_p90 >= t.exp_wins - 1e-6
        assert abs(sum(t.distribution.values()) - 1.0) < 0.01


def test_a_finished_season_has_no_uncertainty_left():
    sim = _sim(through_week=18)
    for t in sim.teams.values():
        assert t.games_remaining == 0
        assert abs(t.exp_wins - (t.wins_actual + 0.5 * t.ties_actual)) < 1e-6
    # Playoff slots are fully determined, so probabilities are 0 or 1.
    assert all(p in (0.0, 1.0) for p in
               (t.playoff_prob for t in sim.teams.values()))
