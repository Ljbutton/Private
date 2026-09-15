"""Collapse many books' quotes into one fair market number.

Two things matter here and they are different:

* the **consensus** line — what the market thinks, averaged across books; this
  is what the model is measured against;
* the **best available** price — the number you would actually bet, which is
  the best line on offer at any single book.  A half point and 10 cents of
  juice is most of the long-run edge in NFL betting, so both are surfaced.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from ..sources.polymarket import PREDICTION_MARKET_VENUES
from ..util import american_to_prob, devig, mean, median, prob_to_american


@dataclass
class BookQuote:
    book: str
    market: str
    captured_at: str | None = None
    home_point: float | None = None
    away_point: float | None = None
    home_price: int | None = None
    away_price: int | None = None


@dataclass
class Consensus:
    game_id: str
    captured_at: str
    spread_home: float | None = None
    spread_price_home: int | None = None
    spread_price_away: int | None = None
    total_points: float | None = None
    total_price_over: int | None = None
    total_price_under: int | None = None
    ml_home: int | None = None
    ml_away: int | None = None
    home_win_prob: float | None = None      # no-vig
    n_books: int = 0
    books: list[str] = field(default_factory=list)
    # Best number available anywhere, with the book offering it.
    best_home_spread: tuple[float, str] | None = None
    best_away_spread: tuple[float, str] | None = None
    best_over: tuple[float, str] | None = None
    best_under: tuple[float, str] | None = None
    best_ml_home: tuple[int, str] | None = None
    best_ml_away: tuple[int, str] | None = None

    def to_row(self) -> dict:
        import json

        return {
            "game_id": self.game_id,
            "captured_at": self.captured_at,
            "spread_home": self.spread_home,
            "spread_price_home": self.spread_price_home,
            "spread_price_away": self.spread_price_away,
            "total_points": self.total_points,
            "total_price_over": self.total_price_over,
            "total_price_under": self.total_price_under,
            "ml_home": self.ml_home,
            "ml_away": self.ml_away,
            "home_win_prob": self.home_win_prob,
            "n_books": self.n_books,
            "books": json.dumps(self.books),
        }


def sportsbook_quotes(quotes: list[dict]) -> list[dict]:
    """Drop prediction-market venues from a set of quotes.

    The consensus is only useful because it averages deep, efficient
    sportsbooks. Mixing a thinner prediction market into it would move the
    benchmark toward the very price we want to measure against, and the
    cross-market comparison would partly be comparing that price to itself.
    """
    return [q for q in quotes if (q.get("book") or "").lower() not in PREDICTION_MARKET_VENUES]


def latest_per_book(quotes: list[dict]) -> dict[tuple[str, str], dict]:
    """Keep only each book's most recent quote for each market."""
    newest: dict[tuple[str, str], dict] = {}
    for q in quotes:
        key = (q.get("book") or "", q.get("market") or "")
        current = newest.get(key)
        if current is None or str(q.get("captured_at") or "") >= str(current.get("captured_at") or ""):
            newest[key] = q
    return newest


def build_consensus(game_id: str, quotes: list[dict], captured_at: str) -> Consensus | None:
    """Average the latest quote from every book into a single market view."""
    quotes = sportsbook_quotes(quotes)
    if not quotes:
        return None
    newest = latest_per_book(quotes)
    by_market: dict[str, list[dict]] = defaultdict(list)
    books: set[str] = set()
    for (book, market), quote in newest.items():
        by_market[market].append(quote)
        books.add(book)

    out = Consensus(
        game_id=game_id,
        captured_at=captured_at,
        n_books=len(books),
        books=sorted(books),
    )

    # ---- spread: median line, mean price. Median resists one book's outlier.
    spreads = by_market.get("spread") or []
    if spreads:
        out.spread_home = median([q.get("home_point") for q in spreads])
        out.spread_price_home = _round_int(mean([q.get("home_price") for q in spreads]))
        out.spread_price_away = _round_int(mean([q.get("away_price") for q in spreads]))
        # Best home line = the most generous (largest) number for the home bettor.
        out.best_home_spread = _best(spreads, "home_point", maximum=True)
        out.best_away_spread = _best(spreads, "away_point", maximum=True)

    totals = by_market.get("total") or []
    if totals:
        out.total_points = median([q.get("home_point") for q in totals])
        out.total_price_over = _round_int(mean([q.get("home_price") for q in totals]))
        out.total_price_under = _round_int(mean([q.get("away_price") for q in totals]))
        # Best over = lowest total; best under = highest total.
        out.best_over = _best(totals, "home_point", maximum=False)
        out.best_under = _best(totals, "away_point", maximum=True)

    moneylines = by_market.get("moneyline") or []
    if moneylines:
        probs: list[float] = []
        for q in moneylines:
            pair = devig(american_to_prob(q.get("home_price")), american_to_prob(q.get("away_price")))
            if pair:
                probs.append(pair[0])
        if probs:
            out.home_win_prob = mean(probs)
            out.ml_home = prob_to_american(out.home_win_prob)
            out.ml_away = prob_to_american(1 - out.home_win_prob)
        out.best_ml_home = _best_price(moneylines, "home_price")
        out.best_ml_away = _best_price(moneylines, "away_price")

    # If moneylines are absent, imply a fair price from the consensus spread.
    if out.home_win_prob is None and out.spread_home is not None:
        from ..util import margin_to_win_prob

        out.home_win_prob = margin_to_win_prob(-out.spread_home)

    return out if (out.spread_home is not None or out.total_points is not None
                   or out.home_win_prob is not None) else None


def _round_int(value: float | None) -> int | None:
    return None if value is None else int(round(value))


def _best(quotes: list[dict], field_name: str, *, maximum: bool) -> tuple[float, str] | None:
    candidates = [(q.get(field_name), q.get("book")) for q in quotes if q.get(field_name) is not None]
    if not candidates:
        return None
    chosen = (max if maximum else min)(candidates, key=lambda pair: pair[0])
    return (float(chosen[0]), str(chosen[1]))


def _best_price(quotes: list[dict], field_name: str) -> tuple[int, str] | None:
    """Best American price = the one implying the lowest cost, i.e. highest payout."""
    candidates = [(q.get(field_name), q.get("book")) for q in quotes if q.get(field_name) is not None]
    if not candidates:
        return None
    chosen = max(candidates, key=lambda pair: _payout(pair[0]))
    return (int(chosen[0]), str(chosen[1]))


def _payout(american: float) -> float:
    american = float(american)
    return american / 100.0 if american > 0 else 100.0 / abs(american)


def spread_disagreement(quotes: list[dict]) -> float | None:
    """Gap between the best and worst *current* line — how split the books are.

    Must run on the latest quote per book, not the raw history: including a
    book's own superseded lines measures how far the market has moved over
    time, which is a different question entirely.
    """
    newest = latest_per_book(quotes)
    points = [
        q.get("home_point") for (_book, market), q in newest.items()
        if market == "spread" and q.get("home_point") is not None
    ]
    if len(points) < 2:
        return None
    return float(max(points) - min(points))
