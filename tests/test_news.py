from nflpicker.news.impact import affected_games, classify, tag_items


def test_a_starting_quarterback_ruling_dominates_the_feed():
    c = classify("Chiefs starting quarterback ruled out with a shoulder injury")
    assert c["category"] == "qb"
    assert c["teams"] == ["KC"]
    assert c["position"] == "QB"
    assert c["line_impact"] < -2      # negative: the team is worse off
    assert c["impact"] > 0.8


def test_a_backup_barely_registers():
    starter = classify("Jets starting running back ruled out")
    backup = classify("Jets backup running back ruled out")
    assert abs(backup["line_impact"]) < abs(starter["line_impact"])


def test_returning_players_are_good_news_not_bad():
    """'Activated off injured reserve' names an injury but is a positive."""
    c = classify("Bills activate star wide receiver off injured reserve")
    assert c["line_impact"] > 0
    assert classify("Packers running back cleared from concussion protocol")["line_impact"] > 0


def test_substring_traps_do_not_create_phantom_statuses():
    # "fired" contains "ir"; "running back" contains "back".
    assert classify("Eagles head coach fired after loss")["status"] is None
    questionable = classify("Jets backup running back questionable")
    assert questionable["status"] == "questionable"
    assert questionable["line_impact"] < 0


def test_weather_is_categorised_and_not_given_a_points_estimate():
    c = classify("High winds expected for Bears game at Soldier Field")
    assert c["category"] == "weather"
    assert c["line_impact"] == 0.0


def test_suspensions_are_treated_like_absences():
    c = classify("Ravens quarterback suspended two games")
    assert c["category"] == "suspension"
    assert c["line_impact"] < -2


def test_tagging_sorts_by_impact_and_keeps_existing_teams():
    items = [
        {"id": "1", "title": "Cowboys sign veteran linebacker", "teams": []},
        {"id": "2", "title": "Chiefs starting quarterback ruled out", "teams": ["KC"]},
    ]
    tagged = tag_items(items)
    assert tagged[0]["id"] == "2"
    assert tagged[0]["teams"] == ["KC"]


def test_news_is_attached_to_the_games_it_affects():
    items = tag_items([
        {"id": "1", "title": "Chiefs starting quarterback ruled out", "teams": ["KC"]},
        {"id": "2", "title": "Rams sign a kicker", "teams": ["LAR"]},
    ])
    games = [{"game_id": "g1", "home": "KC", "away": "DEN"},
             {"game_id": "g2", "home": "SF", "away": "SEA"}]
    hits = affected_games(items, games)
    assert [i["id"] for i in hits["g1"]] == ["1"]
    assert "g2" not in hits


def test_short_position_names_are_recognised():
    """Headlines say 'receiver' and 'corner', not 'wide receiver' and
    'cornerback'. Missing these silently zeroed the impact estimate."""
    cases = {
        "Jaguars place No. 1 receiver on injured reserve": "WR",
        "Rams wideout ruled out for Sunday": "WR",
        "Bears corner doubtful with a hamstring": "CB",
        "Saints left tackle ruled out": "LT",
        "Titans pass rusher suspended": "EDGE",
        "Colts signal-caller questionable": "QB",
    }
    for headline, position in cases.items():
        result = classify(headline)
        assert result["position"] == position, headline
        assert result["line_impact"] < 0, headline


def test_injured_reserve_is_not_read_as_a_backup():
    """'reserve' appears inside 'injured reserve'; treating that as the backup
    hint scored a starter going on IR at a third of its real impact."""
    ir = classify("Jaguars place No. 1 receiver on injured reserve")
    backup = classify("Jaguars place backup receiver on injured reserve")
    assert ir["line_impact"] < -0.5
    assert abs(backup["line_impact"]) < abs(ir["line_impact"])
