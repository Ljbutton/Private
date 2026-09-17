"""Kalshi adapter — a second prediction-market venue.

Kalshi is a CFTC-regulated US exchange, which for most readers of this project
matters more than Polymarket's availability does. Mechanically the two are the
same shape once normalised: a binary contract per side, priced 0-1, with an
order book behind it.

Kalshi quotes in **cents** and exposes both sides of the book, so a mid price
between bid and ask is available and is what gets used — a last-traded price on
a thin market can be hours stale and several points away from anything you
could actually transact at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..teams import try_resolve
from ..util import iso
from ..venues import KALSHI
from .base import HttpClient, SourceError

API = "https://api.elections.kalshi.com/trade-api/v2"
FALLBACK_API = "https://trading-api.kalshi.com/trade-api/v2"

VENUE = KALSHI

# NFL single-game winner series. Kalshi has renamed series before, so several
# are tried rather than hard-coding one.
NFL_SERIES = ["KXNFLGAME", "NFLGAME", "KXNFL"]

# Market tickers look like KXNFLGAME-25SEP21DENKC-KC: the trailing segment is
# the team whose Yes side wins.
_TICKER_TEAM = re.compile(r"-([A-Z]{2,4})$")


@dataclass
class KalshiQuote:
    home: str
    away: str
    home_price: float
    away_price: float
    kickoff: str | None = None
    event_ticker: str | None = None
    volume: float = 0.0
    depth: dict[str, float] = field(default_factory=dict)

    def to_quote_row(self, captured_at: str) -> dict:
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
            "provider_event_id": self.event_ticker,
        }


def _cents(value: Any) -> float | None:
    """Kalshi prices are integer cents; convert to a probability."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out <= 0 or out >= 100:
        return None
    return out / 100.0


def mid_price(market: dict) -> float | None:
    """Mid of the Yes book, falling back to last trade.

    A thin market's last trade can be hours old and several points from
    anything transactable, so the book is preferred whenever both sides exist.
    """
    bid = _cents(market.get("yes_bid"))
    ask = _cents(market.get("yes_ask"))
    if bid is not None and ask is not None:
        return (bid + ask) / 2.0
    return _cents(market.get("last_price")) or bid or ask


def team_from_market(market: dict) -> str | None:
    """Which team this contract pays out on."""
    for key in ("yes_sub_title", "subtitle", "title"):
        team = try_resolve(market.get(key))
        if team:
            return team
    ticker = str(market.get("ticker") or "")
    match = _TICKER_TEAM.search(ticker)
    return try_resolve(match.group(1)) if match else None


def normalise(events: list[dict]) -> list[KalshiQuote]:
    """Pair each event's two contracts into one quote.

    Home/away cannot be read reliably from Kalshi's ticker, so both teams are
    returned and the pipeline orients them against our own schedule.
    """
    out: list[KalshiQuote] = []
    for event in events or []:
        markets = event.get("markets") or []
        priced: dict[str, float] = {}
        volume = 0.0
        for market in markets:
            if str(market.get("status", "open")).lower() not in {"open", "active"}:
                continue
            team = team_from_market(market)
            price = mid_price(market)
            if team and price is not None:
                priced[team] = price
                volume += float(market.get("volume") or 0)
        if len(priced) != 2:
            continue

        (a, pa), (b, pb) = priced.items()
        total = pa + pb
        if total <= 0:
            continue
        out.append(
            KalshiQuote(
                home=a, away=b,
                home_price=pa / total, away_price=pb / total,
                kickoff=iso(event.get("close_time") or event.get("expected_expiration_time")),
                event_ticker=event.get("event_ticker") or event.get("ticker"),
                volume=volume,
            )
        )
    return out


class KalshiSource:
    """Read-only public market data. No key required for quotes."""

    def __init__(self, client: HttpClient | None = None) -> None:
        self.http = client or HttpClient(cache_ttl=120.0)

    def _get(self, path: str, params: dict) -> Any:
        last: Exception | None = None
        for base in (API, FALLBACK_API):
            try:
                return self.http.get_json(f"{base}{path}", params, cache_ttl=120.0, retries=2)
            except SourceError as exc:
                last = exc
        raise SourceError(f"Kalshi unavailable: {last}")

    def probe(self, *, limit: int = 200) -> list[dict]:
        """What each series ticker actually returned, attempt by attempt.

        Exists because "reachable, quoting no NFL games" and "we asked the
        wrong question" are the same answer from outside, need opposite fixes,
        and the second is the likelier one: Kalshi has renamed this series
        before, and a rename is indistinguishable from a quiet Tuesday unless
        someone records which ticker was asked and what came back.
        """
        attempts: list[dict] = []
        for series in NFL_SERIES:
            # Without the status filter as well, because a series that is
            # present but whose events are not yet "open" returns an empty list
            # to the filtered question and a full one to the unfiltered.
            for status in ("open", None):
                params = {"series_ticker": series, "with_nested_markets": "true",
                          "limit": limit}
                if status:
                    params["status"] = status
                label = f"{series}[{status or 'any'}]"
                try:
                    payload = self._get("/events", params)
                except SourceError as exc:
                    attempts.append({"series": label, "error": str(exc)[:160],
                                     "events": 0, "markets": 0})
                    continue
                events = payload.get("events") if isinstance(payload, dict) else payload
                events = events or []
                attempts.append({
                    "series": label,
                    "error": None,
                    "events": len(events),
                    "markets": sum(len(e.get("markets") or []) for e in events),
                    "sample": next(
                        (m.get("ticker") for e in events
                         for m in (e.get("markets") or []) if m.get("ticker")),
                        None),
                    "rows": events,
                })
                if events:
                    break   # this series answered; no need for the other status
        return attempts

    def fetch_events(self, *, limit: int = 200) -> list[dict]:
        """Open NFL game events, with their markets nested.

        Every series ticker is tried, and the results merged, because Kalshi
        has renamed them before and one series being gone must not cost us the
        others. Merged rather than first-wins: a stale series answering with
        three leftovers used to stop the search before the live one was asked.

        If *every* attempt errored, that is a failure and has to say so.
        Returning an empty list made an unreachable host look exactly like an
        empty board, which are opposite problems -- one is "your network", the
        other is "it is Tuesday".
        """
        attempts = self.probe(limit=limit)
        merged: dict[str, dict] = {}
        for attempt in attempts:
            for event in attempt.get("rows") or []:
                key = str(event.get("event_ticker") or event.get("ticker") or id(event))
                merged.setdefault(key, event)
        if merged:
            return list(merged.values())
        if attempts and all(a["error"] for a in attempts):
            raise SourceError(
                "; ".join(f"{a['series']}: {a['error']}" for a in attempts)[:300])
        return []

    def fetch(self) -> list[KalshiQuote]:
        return normalise(self.fetch_events())
