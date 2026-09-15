"""The announced starter reaches the model, and is not charged twice."""

from nflpicker.availability import build_adjustments
from nflpicker.starters import apply_to_games, expected_starters

DEPTH = {"KC": ["P.MAHOMES", "J.FIELDS", "B.COOK"], "BUF": ["J.ALLEN", "M.TRUBISKY"]}


def _injury(player, position="QB", status="Out"):
    return {"player": player, "position": position, "status": status}


def test_ruled_out_starter_is_replaced_by_the_next_man_on_the_chart():
    out = expected_starters(DEPTH, {"KC": [_injury("P.MAHOMES")]})
    assert out["KC"].player == "J.FIELDS"
    assert out["KC"].nominal == "P.MAHOMES"
    assert out["KC"].changed is True
    assert "P.MAHOMES" in out["KC"].reason


def test_two_deep_absences_fall_through_to_the_third_string():
    out = expected_starters(
        DEPTH, {"KC": [_injury("P.MAHOMES"), _injury("J.FIELDS", status="Injured Reserve")]}
    )
    assert out["KC"].player == "B.COOK"
    assert out["KC"].changed is True


def test_a_questionable_starter_still_starts():
    # Questionable is roughly a coin flip and usually resolves to "plays".
    # Swapping him out entirely would overstate that as a certainty; the
    # availability adjustment prices the partial risk instead.
    out = expected_starters(DEPTH, {"KC": [_injury("P.MAHOMES", status="Questionable")]})
    assert out["KC"].player == "P.MAHOMES"
    assert out["KC"].changed is False


def test_healthy_team_reports_no_change():
    out = expected_starters(DEPTH, {})
    assert out["BUF"].player == "J.ALLEN"
    assert out["BUF"].changed is False


def test_every_quarterback_out_keeps_the_nominal_starter_and_says_why():
    out = expected_starters(
        {"KC": ["P.MAHOMES", "J.FIELDS"]},
        {"KC": [_injury("P.MAHOMES"), _injury("J.FIELDS")]},
    )
    assert out["KC"].player == "P.MAHOMES"
    assert out["KC"].changed is False
    assert "ruled out" in out["KC"].reason


def test_only_unplayed_games_are_touched():
    """Overwriting a final game's starter would rewrite history from the future."""
    games = [
        {"game_id": "done", "home": "KC", "away": "BUF", "status": "final"},
        {"game_id": "next", "home": "KC", "away": "BUF", "status": "scheduled"},
    ]
    starters = expected_starters(DEPTH, {"KC": [_injury("P.MAHOMES")]})
    filled = apply_to_games(games, starters, {"J.FIELDS": "00-0036389"})

    assert games[0].get("home_qb_id") is None
    assert games[1]["home_qb_id"] == "00-0036389"
    assert games[1]["home_qb_name"] == "J.FIELDS"
    assert filled == 2          # both sides of the scheduled game


def test_an_identity_the_feed_supplied_is_never_overwritten():
    games = [{"game_id": "g", "home": "KC", "away": "BUF", "status": "scheduled",
              "home_qb_id": "00-0033873"}]
    starters = expected_starters(DEPTH, {"KC": [_injury("P.MAHOMES")]})
    apply_to_games(games, starters, {"J.FIELDS": "00-0036389"})
    assert games[0]["home_qb_id"] == "00-0033873"


def test_a_passer_with_no_history_still_breaks_the_last_week_fallback():
    """The bug being fixed is silently reusing last week's starter. An unknown
    replacement must still read as a different passer, or qb_change stays 0."""
    games = [{"game_id": "g", "home": "KC", "away": "BUF", "status": "scheduled"}]
    starters = expected_starters(DEPTH, {"KC": [_injury("P.MAHOMES")]})
    apply_to_games(games, starters, {})          # no id known for anyone
    assert games[0]["home_qb_id"] == "name:J.FIELDS"


def test_a_replaced_starter_is_not_also_charged_as_an_injury():
    """Double-counting check: if the backup is a model feature, the points
    offset must not subtract the same downgrade a second time."""
    injuries = {"KC": [_injury("P.MAHOMES")]}
    values = {"P.MAHOMES": 0.20, "J.FIELDS": 0.02}
    depth = {"KC": ["P.MAHOMES", "J.FIELDS"]}

    offset_only = build_adjustments(injuries, qb_values=values, depth=depth)
    in_model = build_adjustments(
        injuries, qb_values=values, depth=depth, qb_priced={"KC"}
    )

    assert offset_only["KC"].adjustment < -5.0      # ~0.18 * 35 dropbacks
    assert in_model["KC"].adjustment == 0.0
    assert in_model["KC"].qb_in_model is True
    # The change is still reported, so the UI can say why the number moved.
    assert in_model["KC"].qb_change is True


def test_suppressing_the_quarterback_still_charges_everyone_else():
    injuries = {"KC": [_injury("P.MAHOMES"), _injury("T.KELCE", position="TE")]}
    built = build_adjustments(
        injuries,
        qb_values={"P.MAHOMES": 0.20, "J.FIELDS": 0.02},
        depth={"KC": ["P.MAHOMES", "J.FIELDS"]},
        qb_priced={"KC"},
    )
    assert built["KC"].adjustment < 0.0     # the tight end is still missing
    assert built["KC"].adjustment > -2.0    # but not a quarterback's worth


def test_the_accuracy_report_separates_the_cases_that_matter():
    """A headline rate over all team-games dilutes the result: the two rules
    agree on most of them, where the feature is a no-op. The disagreement rate
    is the one that says whether it is worth having."""
    from nflpicker.backtest.starters import StarterAccuracy

    acc = StarterAccuracy(
        n=1000, last_week_right=880, announced_right=889,
        disagreed=100, announced_right_when_disagreed=50,
        last_week_right_when_disagreed=41,
    )
    out = acc.to_dict()

    assert out["last_week_rate"] == 0.88
    assert out["announced_rate"] == 0.889
    assert out["announced_rate_when_disagreed"] == 0.5
    assert out["last_week_rate_when_disagreed"] == 0.41


def test_an_empty_run_reports_nothing_rather_than_dividing_by_zero():
    from nflpicker.backtest.starters import StarterAccuracy

    out = StarterAccuracy().to_dict()
    assert out["n"] == 0
    assert out["last_week_rate"] is None
    assert out["announced_rate_when_disagreed"] is None
