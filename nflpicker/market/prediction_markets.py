"""Prediction-market prices, shown beside the sportsbook consensus.

This is a **display**, not a recommendation engine. It answers one question —
"where do the prediction markets have this game, compared with the books and
with us?" — and stops there. No expected value, no stake sizing, no ratings.

Deliberate, because the two kinds of number should not be mixed. Sportsbook
lines are what the picks are priced against; prediction-market prices are a
second opinion from a different crowd. Presenting them side by side lets you see
a disagreement and judge it yourself, without the app implying a bet follows
from it.

Prediction venues are still excluded from the sportsbook consensus and from
best-available pricing (see :mod:`nflpicker.market.consensus`). Nothing here
feeds a model or a suggestion.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..util import american_to_prob, devig, mean
from ..venues import KALSHI, POLYMARKET

# Venue key -> display name, in the order they should appear.
VENUES: dict[str, str] = {
    POLYMARKET: "Polymarket",
    KALSHI: "Kalshi",
}

# Below this the venues and the books effectively agree; not worth flagging.
NOTABLE_GAP = 0.05


@dataclass
class VenuePrice:
    venue: str
    label: str
    home_prob: float
    captured_at: str | None = None

    def to_dict(self) -> dict:
        return {
            "venue": self.venue,
            "label": self.label,
            "home_prob": round(self.home_prob, 4),
            "captured_at": self.captured_at,
        }


@dataclass
class MarketComparison:
    """One game, priced by everyone who has an opinion on it."""

    game_id: str
    home: str
    away: str
    kickoff: str | None = None
    book_prob: float | None = None        # no-vig sportsbook consensus, home side
    n_books: int = 0
    model_prob: float | None = None       # our blended projection, home side
    venues: list[VenuePrice] = field(default_factory=list)

    @property
    def venue_prob(self) -> float | None:
        """Average across the prediction venues that priced this game."""
        return mean([v.home_prob for v in self.venues])

    @property
    def gap(self) -> float | None:
        """Prediction-market average minus the sportsbook consensus."""
        venue = self.venue_prob
        if venue is None or self.book_prob is None:
            return None
        return venue - self.book_prob

    @property
    def notable(self) -> bool:
        gap = self.gap
        return gap is not None and abs(gap) >= NOTABLE_GAP

    @property
    def leans(self) -> str | None:
        """Which side the prediction markets are higher on, in plain terms."""
        gap = self.gap
        if gap is None or abs(gap) < NOTABLE_GAP:
            return None
        return self.home if gap > 0 else self.away

    def to_dict(self) -> dict:
        return {
            "game_id": self.game_id,
            "home": self.home,
            "away": self.away,
            "kickoff": self.kickoff,
            "book_prob": None if self.book_prob is None else round(self.book_prob, 4),
            "n_books": self.n_books,
            "model_prob": None if self.model_prob is None else round(self.model_prob, 4),
            "venues": [v.to_dict() for v in self.venues],
            "venue_prob": None if self.venue_prob is None else round(self.venue_prob, 4),
            "gap": None if self.gap is None else round(self.gap, 4),
            "notable": self.notable,
            "leans": self.leans,
        }


def venue_probability(home_price, away_price) -> float | None:
    """No-vig home win probability from a venue's two-sided prices."""
    pair = devig(american_to_prob(home_price), american_to_prob(away_price))
    return pair[0] if pair else None


def build_comparisons(
    games: list[dict],
    consensus_by_game: dict,
    venue_quotes: dict[str, dict[str, dict]],
    predictions_by_game: dict | None = None,
) -> list[MarketComparison]:
    """Assemble one comparison row per game that any venue has priced.

    ``venue_quotes`` maps venue key -> game_id -> {home_price, away_price,
    captured_at}.
    """
    predictions_by_game = predictions_by_game or {}
    rows: list[MarketComparison] = []

    for game in games:
        game_id = str(game.get("game_id"))
        prices: list[VenuePrice] = []
        for venue, label in VENUES.items():
            quote = (venue_quotes.get(venue) or {}).get(game_id)
            if not quote:
                continue
            prob = venue_probability(quote.get("home_price"), quote.get("away_price"))
            if prob is None:
                continue
            prices.append(
                VenuePrice(venue=venue, label=label, home_prob=prob,
                           captured_at=quote.get("captured_at"))
            )
        if not prices:
            continue

        consensus = consensus_by_game.get(game_id) or {}
        prediction = predictions_by_game.get(game_id) or {}
        rows.append(
            MarketComparison(
                game_id=game_id,
                home=game["home"], away=game["away"], kickoff=game.get("kickoff"),
                book_prob=consensus.get("home_win_prob"),
                n_books=int(consensus.get("n_books") or 0),
                model_prob=prediction.get("home_win_prob"),
                venues=prices,
            )
        )

    # Biggest disagreements first: those are the rows worth a second look.
    rows.sort(key=lambda r: abs(r.gap or 0), reverse=True)
    return rows
