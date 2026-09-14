"""Parsers for the live feeds, exercised against recorded-shape payloads.

Network access is not available in CI, and these are the code paths that only
run against real sources — so their shapes are pinned here instead. Each parser
must also survive the fields these feeds routinely omit.
"""

from nflpicker.sources.espn import parse_scoreboard
from nflpicker.sources.news_rss import parse_feed
from nflpicker.sources.odds_api import parse_odds_payload

SCOREBOARD = {
    "season": {"year": 2025, "type": 2},
    "week": {"number": 3},
    "events": [{
        "id": "401671789",
        "date": "2025-09-21T17:00Z",
        "season": {"year": 2025, "type": 2},
        "week": {"number": 3},
        "competitions": [{
            "neutralSite": False,
            "venue": {"fullName": "Arrowhead Stadium", "indoor": False},
            "status": {"type": {"state": "post", "completed": True}},
            "competitors": [
                {"homeAway": "home", "score": "27", "team": {"abbreviation": "KC"}},
                {"homeAway": "away", "score": "20", "team": {"abbreviation": "DEN"}},
            ],
            "odds": [{
                "provider": {"name": "ESPN BET"},
                "details": "KC -3.5",
                "overUnder": 44.5,
                "homeTeamOdds": {"moneyLine": -180},
                "awayTeamOdds": {"moneyLine": 155},
            }],
        }],
    }],
}


def test_scoreboard_parses_a_completed_game():
    games = parse_scoreboard(SCOREBOARD)
    assert len(games) == 1
    g = games[0]
    assert (g["home"], g["away"]) == ("KC", "DEN")
    assert (g["home_score"], g["away_score"]) == (27.0, 20.0)
    assert g["status"] == "final"
    assert g["season"] == 2025 and g["week"] == 3
    assert g["kickoff"].startswith("2025-09-21T17:00")


def test_espn_details_string_is_converted_to_a_home_line():
    """ESPN gives 'KC -3.5' as text. KC is home, so the home line is -3.5;
    when the favourite is the away team the sign must flip."""
    g = parse_scoreboard(SCOREBOARD)[0]
    assert g["espn_odds"]["spread_home"] == -3.5
    assert g["espn_odds"]["total"] == 44.5

    flipped = {**SCOREBOARD}
    flipped["events"][0]["competitions"][0]["odds"][0]["details"] = "DEN -3.5"
    assert parse_scoreboard(flipped)[0]["espn_odds"]["spread_home"] == 3.5


def test_scoreboard_tolerates_a_game_with_no_odds_or_score():
    payload = {"events": [{
        "id": "1", "date": "2025-09-21T17:00Z",
        "season": {"year": 2025, "type": 2}, "week": {"number": 3},
        "competitions": [{
            "status": {"type": {"state": "pre"}},
            "competitors": [
                {"homeAway": "home", "team": {"abbreviation": "SF"}},
                {"homeAway": "away", "team": {"abbreviation": "SEA"}},
            ],
        }],
    }]}
    g = parse_scoreboard(payload)[0]
    assert g["status"] == "scheduled"
    assert g["home_score"] is None
    assert "espn_odds" not in g


def test_scoreboard_skips_unparseable_events_without_failing():
    payload = {"events": [{"id": "1", "competitions": [{"competitors": []}]}]}
    assert parse_scoreboard(payload) == []


ODDS = [{
    "id": "abc123",
    "commence_time": "2025-09-21T17:00:00Z",
    "home_team": "Kansas City Chiefs",
    "away_team": "Denver Broncos",
    "bookmakers": [{
        "key": "draftkings", "title": "DraftKings",
        "last_update": "2025-09-20T12:00:00Z",
        "markets": [
            {"key": "h2h", "outcomes": [
                {"name": "Kansas City Chiefs", "price": -180},
                {"name": "Denver Broncos", "price": 155}]},
            {"key": "spreads", "outcomes": [
                {"name": "Kansas City Chiefs", "price": -110, "point": -3.5},
                {"name": "Denver Broncos", "price": -110, "point": 3.5}]},
            {"key": "totals", "outcomes": [
                {"name": "Over", "price": -105, "point": 44.5},
                {"name": "Under", "price": -115, "point": 44.5}]},
        ],
    }],
}]


def test_odds_payload_flattens_to_one_row_per_market():
    quotes = parse_odds_payload(ODDS)
    markets = {q["market"]: q for q in quotes}
    assert set(markets) == {"moneyline", "spread", "total"}
    assert all(q["home"] == "KC" and q["away"] == "DEN" for q in quotes)

    assert markets["spread"]["home_point"] == -3.5
    assert markets["spread"]["away_point"] == 3.5
    assert markets["moneyline"]["home_price"] == -180
    # For totals, home/away carry over/under rather than a team side.
    assert markets["total"]["home_point"] == 44.5
    assert markets["total"]["home_price"] == -105      # over
    assert markets["total"]["away_price"] == -115      # under


def test_odds_payload_uses_the_book_timestamp_not_fetch_time():
    """Books report their own last_update, which is what makes a single fetch
    carry usable movement history."""
    quotes = parse_odds_payload(ODDS)
    assert all(q["captured_at"].startswith("2025-09-20T12:00") for q in quotes)


def test_odds_payload_skips_unknown_teams_and_empty_books():
    assert parse_odds_payload([{"home_team": "Bogus FC", "away_team": "Nope United"}]) == []
    assert parse_odds_payload([]) == []


RSS = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Chiefs QB ruled out</title><link>https://example.com/a</link>
<pubDate>Sat, 20 Sep 2025 12:00:00 GMT</pubDate>
<description>&lt;p&gt;He is &lt;b&gt;out&lt;/b&gt;.&lt;/p&gt;</description></item>
<item><title>Bills sign kicker</title><link>https://example.com/b</link></item>
</channel></rss>"""

ATOM = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Rams trade for receiver</title>
<link href="https://example.com/c"/><published>2025-09-20T12:00:00Z</published>
<summary>Deal done.</summary></entry></feed>"""


def test_rss_and_atom_are_both_parsed():
    rss = parse_feed(RSS, "Test")
    assert [i["title"] for i in rss] == ["Chiefs QB ruled out", "Bills sign kicker"]
    assert rss[0]["url"] == "https://example.com/a"
    assert rss[0]["published_at"].startswith("2025-09-20T12:00")
    # HTML in the description is stripped rather than rendered into the UI.
    assert rss[0]["summary"] == "He is out ."

    atom = parse_feed(ATOM, "Test")
    assert atom[0]["title"] == "Rams trade for receiver"
    assert atom[0]["url"] == "https://example.com/c"


def test_feed_ids_are_stable_across_fetches():
    assert [i["id"] for i in parse_feed(RSS, "Test")] == [i["id"] for i in parse_feed(RSS, "Test")]


def test_malformed_feed_yields_nothing_rather_than_raising():
    assert parse_feed("not xml at all", "Test") == []
    assert parse_feed("", "Test") == []
