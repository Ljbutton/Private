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


def test_a_page_of_unrelated_markets_is_not_nfl():
    """The failure this fixes was silent and looked like the venue's fault.

    When the tag was renamed, the last-resort query returned two hundred open
    markets about elections and crypto. None of them parse as a game, so the
    app reported "reachable, but quoting no NFL games right now" -- which is a
    true sentence about the wrong question.
    """
    from nflpicker.sources.polymarket import PolymarketSource, matchup_from_title

    assert matchup_from_title("Will the Chiefs beat the Broncos?") == ("KC", "DEN")
    assert matchup_from_title("Chiefs vs. Broncos") == ("KC", "DEN")
    assert matchup_from_title("Will Bitcoin hit 200k in 2026?") is None
    assert not PolymarketSource.looks_like_nfl({"question": "Who wins the election?"})
    assert PolymarketSource.looks_like_nfl({"question": "Chiefs vs. Broncos"})


def test_the_open_board_is_swept_when_every_tag_misses(monkeypatch):
    """Immune to the tag being renamed again, which it has been twice."""
    from nflpicker.sources.polymarket import PolymarketSource

    source = PolymarketSource()
    asked = []

    def fake_get(url, params=None, **kw):
        asked.append(dict(params or {}))
        tagged = any(k in (params or {}) for k in
                     ("tag_slug", "tag", "series_slug"))
        if tagged:
            return [{"question": "Will Bitcoin hit 200k in 2026?"}]
        if (params or {}).get("offset"):
            return []
        return [{"question": "Will the Chiefs beat the Broncos?"},
                {"question": "Who wins the election?"}]

    monkeypatch.setattr(source.http, "get_json", fake_get)
    markets = source.fetch_markets(limit=200)

    assert len(markets) == 1, "only the football survives the sweep"
    assert markets[0]["question"].startswith("Will the Chiefs")
    assert any("offset" in a for a in asked), "it fell through to the sweep"
