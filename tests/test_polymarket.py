"""Polymarket market parsing.

The live API is not reachable from the test environment, so these pin the
documented response shapes. Both market forms are covered: outcomes named after
the teams, and a Yes/No question.
"""

from nflpicker.sources.polymarket import normalise, parse_market

TEAM_OUTCOME_MARKET = {
    "id": "512",
    "question": "Chiefs vs. Broncos",
    "slug": "nfl-kc-den-2025-09-21",
    "outcomes": '["Kansas City Chiefs", "Denver Broncos"]',
    "outcomePrices": '["0.72", "0.28"]',
    "clobTokenIds": '["tok-kc", "tok-den"]',
    "volume": "48000",
    "liquidity": "9000",
    "gameStartTime": "2025-09-21T17:00:00Z",
    "closed": False,
}

YES_NO_MARKET = {
    "id": "513",
    "question": "Will the Buffalo Bills beat the New York Jets?",
    "outcomes": '["Yes", "No"]',
    "outcomePrices": '["0.64", "0.36"]',
    "clobTokenIds": '["tok-yes", "tok-no"]',
    "volume": "21000",
    "closed": False,
}


def test_team_named_outcomes_are_parsed():
    a, b, prices, tokens = parse_market(TEAM_OUTCOME_MARKET)
    assert {a, b} == {"KC", "DEN"}
    assert abs(prices["KC"] - 0.72) < 1e-9
    assert tokens["KC"] == "tok-kc"


def test_a_yes_no_question_resolves_yes_to_the_subject():
    """'Will the Bills beat the Jets?' — Yes means Buffalo, and mapping that
    backwards would invert every probability from this market shape."""
    a, b, prices, tokens = parse_market(YES_NO_MARKET)
    assert {a, b} == {"BUF", "NYJ"}
    assert abs(prices["BUF"] - 0.64) < 1e-9
    assert abs(prices["NYJ"] - 0.36) < 1e-9
    assert tokens["BUF"] == "tok-yes"


def test_non_nfl_and_non_game_markets_are_ignored():
    assert parse_market({"outcomes": '["Yes","No"]', "outcomePrices": '["0.5","0.5"]',
                         "question": "Will it rain in Paris?"}) is None
    assert parse_market({"outcomes": '["Yes","No"]', "outcomePrices": '["0.5","0.5"]',
                         "question": "Will the Chiefs win the Super Bowl?"}) is None
    assert parse_market({"outcomes": '["A","B","C"]',
                         "outcomePrices": '["0.3","0.3","0.4"]'}) is None
    assert parse_market({}) is None


def test_prices_are_normalised_to_sum_to_one():
    market = {**TEAM_OUTCOME_MARKET, "outcomePrices": '["0.74", "0.30"]'}
    quotes = normalise([market])
    assert len(quotes) == 1
    assert abs(quotes[0].home_price + quotes[0].away_price - 1.0) < 1e-9


def test_closed_and_low_volume_markets_are_dropped():
    assert normalise([{**TEAM_OUTCOME_MARKET, "closed": True}]) == []
    assert normalise([TEAM_OUTCOME_MARKET], min_volume=100000) == []
    assert len(normalise([TEAM_OUTCOME_MARKET], min_volume=1000)) == 1


def test_quotes_convert_to_a_storable_moneyline_row():
    quote = normalise([TEAM_OUTCOME_MARKET])[0]
    row = quote.to_quote_row("2025-09-20T12:00:00+00:00")
    assert row["book"] == "polymarket"
    assert row["market"] == "moneyline"
    # A 72% favourite must price as a favourite.
    assert row["home_price"] < 0 if quote.home == "KC" else row["away_price"] < 0


def test_malformed_payloads_do_not_raise():
    assert normalise([]) == []
    assert normalise([{"outcomes": "not json", "outcomePrices": None}]) == []
    assert normalise([{**TEAM_OUTCOME_MARKET, "outcomePrices": '["nan","nan"]'}]) == []
