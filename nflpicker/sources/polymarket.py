"""Polymarket adapter — a second, independent opinion on the same games.

Why this venue is treated differently from a sportsbook:

Sportsbook NFL markets are deeply liquid and highly efficient; the measured
result in this project is that a public model cannot beat their closing line.
That efficiency is useful rather than merely disappointing — it makes the
no-vig sportsbook consensus a high-quality estimate of true probability. A
prediction market with a different, smaller participant base will sometimes sit
several points away from that estimate, and when it does the likelier
explanation is that the thinner venue is lagging, not that the deepest market in
American sport is wrong.

So Polymarket is never folded into the sportsbook consensus — contaminating the
sharp benchmark with a noisy price would destroy the thing that makes the
comparison work. It is stored as its own venue and compared against, in
:mod:`nflpicker.picks.crossmarket`.

Two practical cautions are wired into the code rather than left as prose:
prices are meaningless without order-book depth behind them, and a probability
gap that cannot be traded at size is not an edge.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..teams import TEAMS, try_resolve
from ..util import iso
from ..venues import POLYMARKET
from .base import HttpClient, SourceError

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

# Venue key used in storage. The registry in :mod:`nflpicker.venues` is what
# excludes it from the sportsbook consensus and from best-available pricing.
VENUE = POLYMARKET

# "Will the Chiefs beat the Broncos?" — the subject wins if Yes resolves.
_BEAT_RE = re.compile(r"\bwill\s+the\s+(.+?)\s+(?:beat|defeat)\s+the\s+(.+?)\s*\??$", re.I)
_VS_RE = re.compile(r"^(.+?)\s+(?:vs\.?|@|at)\s+(.+?)$", re.I)


@dataclass
class PolymarketQuote:
    """One NFL game market, normalised to a home-team win probability."""

    home: str
    away: str
    home_price: float                 # probability, 0-1
    away_price: float
    kickoff: str | None = None
    market_id: str | None = None
    slug: str | None = None
    volume: float = 0.0
    liquidity: float = 0.0
    token_ids: dict[str, str] = field(default_factory=dict)   # team -> clob token
    depth: dict[str, float] = field(default_factory=dict)     # team -> tradeable size

    def to_quote_row(self, captured_at: str) -> dict:
        """Shaped like a sportsbook moneyline row so storage stays uniform."""
        from ..util import prob_to_american

        return {
            "home": self.home,
            "away": self.away,
            "kickoff": self.kickoff,
            "book": VENUE,
            "market": "moneyline",
            "captured_at": captured_at,
            "home_point": None,
            "away_point": None,
            "home_price": prob_to_american(self.home_price),
            "away_price": prob_to_american(self.away_price),
            "provider_event_id": self.market_id,
        }


def _json_list(value: Any) -> list:
    """Gamma returns several fields as JSON-encoded strings."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except (TypeError, ValueError):
            return []
    return []


def _float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return default if out != out else out


def parse_market(market: dict) -> tuple[str, str, dict[str, float], dict[str, str]] | None:
    """Extract (team_a, team_b, {team: price}, {team: token_id}) from a market.

    Handles the two shapes these markets come in: outcomes named after the
    teams, and a Yes/No question of the form "Will the X beat the Y?".
    Returns None when the market cannot be resolved to exactly two NFL teams,
    which is the right outcome for futures, props and non-NFL markets.
    """
    outcomes = [str(o) for o in _json_list(market.get("outcomes"))]
    prices = [_float(p, default=float("nan")) for p in _json_list(market.get("outcomePrices"))]
    tokens = [str(t) for t in _json_list(market.get("clobTokenIds"))]
    if len(outcomes) != 2 or len(prices) != 2:
        return None
    if any(p != p for p in prices):
        return None

    # ---- shape 1: the outcomes are the teams
    resolved = [try_resolve(o) for o in outcomes]
    if all(resolved) and resolved[0] != resolved[1]:
        a, b = resolved[0], resolved[1]
        token_map = dict(zip([a, b], tokens, strict=False)) if len(tokens) == 2 else {}
        return a, b, {a: prices[0], b: prices[1]}, token_map

    # ---- shape 2: a Yes/No question naming both teams
    lowered = [o.strip().lower() for o in outcomes]
    if set(lowered) != {"yes", "no"}:
        return None
    question = str(market.get("question") or market.get("title") or "")
    subject, opponent = _teams_from_question(question)
    if not subject or not opponent or subject == opponent:
        return None

    yes_index = lowered.index("yes")
    no_index = 1 - yes_index
    price_map = {subject: prices[yes_index], opponent: prices[no_index]}
    token_map = {}
    if len(tokens) == 2:
        token_map = {subject: tokens[yes_index], opponent: tokens[no_index]}
    return subject, opponent, price_map, token_map


def _teams_from_question(question: str) -> tuple[str | None, str | None]:
    """Team that Yes refers to, and its opponent."""
    match = _BEAT_RE.search(question.strip())
    if match:
        return try_resolve(match.group(1)), try_resolve(match.group(2))

    match = _VS_RE.search(question.strip().rstrip("?"))
    if match:
        return try_resolve(match.group(1)), try_resolve(match.group(2))

    # Fall back to any two distinct nicknames mentioned, in order.
    found: list[str] = []
    lowered = f" {question.lower()} "
    for abbr, team in TEAMS.items():
        if re.search(rf"\b{re.escape(team.name.lower())}\b", lowered) and abbr not in found:
            found.append(abbr)
    return (found[0], found[1]) if len(found) == 2 else (None, None)


def normalise(markets: list[dict], *, min_volume: float = 0.0) -> list[PolymarketQuote]:
    """Turn raw Gamma markets into home-oriented quotes.

    Home/away cannot be read from the market itself, so both teams are returned
    and the pipeline orients them against our own schedule.
    """
    out: list[PolymarketQuote] = []
    for market in markets or []:
        if market.get("closed") or market.get("archived"):
            continue
        parsed = parse_market(market)
        if not parsed:
            continue
        a, b, prices, tokens = parsed
        volume = _float(market.get("volume") or market.get("volumeNum"))
        if volume < min_volume:
            continue
        total = prices[a] + prices[b]
        if total <= 0:
            continue
        # Prices should already sum to ~1; normalise the small spread away.
        out.append(
            PolymarketQuote(
                home=a, away=b,
                home_price=prices[a] / total, away_price=prices[b] / total,
                kickoff=iso(market.get("gameStartTime") or market.get("endDate")),
                market_id=str(market.get("id") or market.get("conditionId") or ""),
                slug=market.get("slug"),
                volume=volume,
                liquidity=_float(market.get("liquidity") or market.get("liquidityNum")),
                token_ids=tokens,
            )
        )
    return out


class PolymarketSource:
    """Read-only client. No key required for public market data."""

    def __init__(self, client: HttpClient | None = None) -> None:
        self.http = client or HttpClient(cache_ttl=120.0)

    def fetch_markets(self, *, limit: int = 200) -> list[dict]:
        """Open NFL markets. Tag filtering is best-effort across API versions."""
        attempts = [
            {"closed": "false", "limit": limit, "tag_slug": "nfl"},
            {"closed": "false", "limit": limit, "tag": "nfl"},
            {"closed": "false", "limit": limit},
        ]
        last_error: Exception | None = None
        for params in attempts:
            try:
                payload = self.http.get_json(
                    f"{GAMMA}/markets", params, cache_ttl=120.0, retries=2
                )
            except SourceError as exc:
                last_error = exc
                continue
            markets = payload if isinstance(payload, list) else payload.get("data") or []
            if markets:
                return markets
        if last_error:
            raise SourceError(f"Polymarket markets unavailable: {last_error}")
        return []

    def order_book_depth(self, token_id: str, *, side: str = "buy",
                         max_price: float = 0.98) -> float:
        """Dollars available to trade on one side of a token's book.

        A probability gap with no depth behind it is not an opportunity, so
        every cross-market edge is sized against this rather than the last
        traded price.
        """
        try:
            payload = self.http.get_json(
                f"{CLOB}/book", {"token_id": token_id}, cache_ttl=60.0, retries=2
            )
        except SourceError:
            return 0.0
        levels = payload.get("asks" if side == "buy" else "bids") or []
        total = 0.0
        for level in levels:
            price = _float(level.get("price"))
            size = _float(level.get("size"))
            if 0 < price <= max_price:
                total += price * size
        return round(total, 2)

    def fetch(self, *, with_depth: bool = True, min_volume: float = 0.0) -> list[PolymarketQuote]:
        quotes = normalise(self.fetch_markets(), min_volume=min_volume)
        if with_depth:
            for quote in quotes:
                for team, token in quote.token_ids.items():
                    if token:
                        quote.depth[team] = self.order_book_depth(token)
        return quotes
