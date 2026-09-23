"""Venue registry.

A single place that answers "is this a sportsbook or a prediction market?".

This exists because the distinction is load-bearing in two places that are easy
to miss — the sportsbook consensus, and best-available pricing — and because
keeping the set next to one adapter meant adding a second venue silently
reintroduced a bug that had already been found and fixed once: model edges
being quoted at a venue that never offered them.

Any new prediction-market adapter must register its key here. A test asserts
that every adapter's ``VENUE`` constant appears in this set, so forgetting is a
build failure rather than a subtly wrong recommendation.
"""

from __future__ import annotations

POLYMARKET = "polymarket"
KALSHI = "kalshi"

# Venues that are compared *against* the sportsbook consensus, never averaged
# into it and never used for best-available pricing.
PREDICTION_MARKET_VENUES: frozenset[str] = frozenset({POLYMARKET, KALSHI})


def is_prediction_market(book: str | None) -> bool:
    return (book or "").strip().lower() in PREDICTION_MARKET_VENUES


def is_sportsbook(book: str | None) -> bool:
    return bool(book) and not is_prediction_market(book)
