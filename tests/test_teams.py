from nflpicker import teams


def test_all_32_teams_in_8_divisions():
    assert len(teams.TEAMS) == 32
    assert len(teams.DIVISIONS) == 8
    assert all(len(members) == 4 for members in teams.DIVISIONS.values())


def test_resolves_the_spellings_sources_actually_use():
    cases = {
        "Kansas City Chiefs": "KC", "WSH": "WAS", "LA Rams": "LAR",
        "49ers": "SF", "jax": "JAX", "Las Vegas Raiders": "LV",
        "Washington Football Team": "WAS", "Oakland Raiders": "LV",
        "San Diego Chargers": "LAC", "St. Louis Rams": "LAR", "GNB": "GB",
    }
    for value, expected in cases.items():
        assert teams.resolve(value) == expected, value


def test_ambiguous_city_names_are_rejected_not_guessed():
    # "New York" alone cannot distinguish Giants from Jets; guessing would
    # silently attribute news and odds to the wrong team.
    assert teams.try_resolve("New York") is None
    assert teams.try_resolve("Los Angeles") is None
    assert teams.try_resolve("not a team at all") is None


def test_distance_is_symmetric_and_sane():
    d = teams.distance_miles("KC", "SEA")
    assert abs(d - teams.distance_miles("SEA", "KC")) < 1e-6
    assert 1400 < d < 1700
    assert teams.distance_miles("NYG", "NYJ") < 1        # same stadium


def test_the_ui_reference_covers_every_team_with_a_logo_key():
    ref = teams.reference()
    assert set(ref) == set(teams.TEAMS)
    for row in ref.values():
        assert row["name"] and row["location"]
        assert row["color"].startswith("#")
        assert row["espn"] and row["espn"] == row["espn"].lower()


def test_espn_logo_keys_use_espn_s_own_spelling():
    # The board addresses team marks by ESPN's abbreviation, which is not ours
    # everywhere. Getting this wrong shows a blank badge rather than an error.
    assert teams.espn_abbr("WAS") == "wsh"
    assert teams.espn_abbr("KC") == "kc"
    assert teams.espn_abbr("LAR") == "lar"
