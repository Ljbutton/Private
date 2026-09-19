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

# Market tickers look like KXNFLGAME-26SEP17DETBUF-BUF. Two things are in
# there and both are worth having: the trailing segment is the team whose Yes
# side pays, and the middle segment carries the date and *both* teams.
_TICKER_TEAM = re.compile(r"-([A-Z]{2,4})$")
_TICKER_MATCHUP = re.compile(r"-\d{2}[A-Z]{3}\d{2}([A-Z]{4,8})(?:-|$)")


def split_matchup(code: str) -> tuple[str, str] | None:
    """The two teams in a ticker's middle segment, e.g. DETBUF -> (DET, BUF).

    Ambiguous in general -- LACLV is LAC+LV or LA+CLV -- so every split is
    tried and one is only accepted when both halves are teams we know and the
    split is unique. Guessing between two readings of the same string would
    quietly quote the wrong game.
    """
    found = []
    for cut in (2, 3, 4):
        left, right = code[:cut], code[cut:]
        if not (2 <= len(right) <= 4):
            continue
        a, b = try_resolve(left), try_resolve(right)
        if a and b and a != b:
            found.append((a, b))
    return found[0] if len(found) == 1 else None


def matchup_from_market(market: dict) -> tuple[str, str] | None:
    match = _TICKER_MATCHUP.search(str(market.get("ticker") or ""))
    return split_matchup(match.group(1)) if match else None


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
    """Mid of the Yes book, falling back to the No book and then to last trade.

    A thin market's last trade can be hours old and several points from
    anything transactable, so a book is preferred whenever one exists.

    The No side is read because it is the same book seen from the other end --
    a No ask of 38c is a Yes bid of 62c -- and on a contract where only one
    side has been quoted it is the difference between a price and nothing.
    """
    bid = _cents(market.get("yes_bid"))
    ask = _cents(market.get("yes_ask"))
    if bid is not None and ask is not None:
        return (bid + ask) / 2.0
    no_bid = _cents(market.get("no_bid"))
    no_ask = _cents(market.get("no_ask"))
    if no_bid is not None and no_ask is not None:
        return 1.0 - (no_bid + no_ask) / 2.0
    for implied in (_cents(market.get("last_price")), bid, ask,
                    None if no_ask is None else 1.0 - no_ask,
                    None if no_bid is None else 1.0 - no_bid):
        if implied is not None:
            return implied
    return None


def team_from_market(market: dict) -> str | None:
    """Which team this contract pays out on.

    The ticker is asked first now, and that is the fix for a real failure: the
    titles are per-*event* on some series, so both contracts in a game carried
    the same words, both resolved to the same team, and the pair collapsed to
    one. Thirty-two games came back and none of them survived. The ticker's
    trailing segment is per-contract and cannot do that.
    """
    match = _TICKER_TEAM.search(str(market.get("ticker") or ""))
    if match:
        team = try_resolve(match.group(1))
        if team:
            return team
    for key in ("yes_sub_title", "subtitle", "title"):
        team = try_resolve(market.get(key))
        if team:
            return team
    return None


def pairing_report(events: list[dict]) -> dict:
    """Why events did not become quotes, counted by cause.

    "Thirty-two events came back and none survived pairing" is a true sentence
    that names four different bugs. Unpriced contracts, contracts whose team we
    cannot read, and both contracts resolving to the same team need completely
    different fixes, and from outside they are the same silence.
    """
    counts = {"events": 0, "markets": 0, "paired": 0,
              "no_price": 0, "no_team": 0, "one_sided": 0, "same_team": 0}
    for event in events or []:
        counts["events"] += 1
        markets = event.get("markets") or []
        counts["markets"] += len(markets)
        teams, priced = set(), {}
        for market in markets:
            if str(market.get("status", "open")).lower() not in {"open", "active"}:
                continue
            team = team_from_market(market)
            price = mid_price(market)
            if team is None:
                counts["no_team"] += 1
                continue
            teams.add(team)
            if price is None:
                counts["no_price"] += 1
                continue
            priced[team] = price
        if len(priced) >= 2:
            counts["paired"] += 1
        elif len(teams) == 1 and len(markets) > 1:
            counts["same_team"] += 1
        elif len(priced) == 1:
            counts["one_sided"] += 1
    return counts


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
        matchup = None
        for market in markets:
            if str(market.get("status", "open")).lower() not in {"open", "active"}:
                continue
            matchup = matchup or matchup_from_market(market)
            team = team_from_market(market)
            price = mid_price(market)
            if team and price is not None:
                priced[team] = price
                volume += float(market.get("volume") or 0)

        # One side priced and the other not is not a broken event, it is a
        # binary contract: the other side is what is left of the dollar. This
        # is the common shape early in a week, when one contract has a book and
        # its opposite has not traded, and dropping it lost the whole game.
        if len(priced) == 1 and matchup:
            (known,) = priced
            other = next((t for t in matchup if t != known), None)
            if other:
                priced[other] = 1.0 - priced[known]

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

    def fetch_markets(self, series: str, *, limit: int = 1000) -> list[dict]:
        """A series' contracts from /markets, which is the endpoint that owns
        their books.

        The events endpoint nests markets, and what it nests is a summary: on
        this series it came back with sixty-four contracts across thirty-two
        games and not one price between them, which from outside looked exactly
        like a venue quoting nothing. /markets returns the same contracts with
        their bids, asks and last trade, and each one names its event, which is
        everything the pairing needs.
        """
        out: list[dict] = []
        cursor = None
        for _ in range(5):
            params: dict = {"series_ticker": series, "limit": min(limit, 1000),
                            "status": "open"}
            if cursor:
                params["cursor"] = cursor
            payload = self._get("/markets", params)
            rows = (payload.get("markets") if isinstance(payload, dict) else payload) or []
            out.extend(rows)
            cursor = payload.get("cursor") if isinstance(payload, dict) else None
            if not cursor or not rows:
                break
        return out

    @staticmethod
    def _has_any_price(events: list[dict]) -> bool:
        return any(mid_price(m) is not None
                   for e in events for m in (e.get("markets") or []))

    def fill_prices(self, events: list[dict]) -> list[dict]:
        """Re-nest markets from /markets when the events' own carry no book.

        Only when *nothing* has a price -- one unpriced contract is a market
        nobody has quoted yet, which is ordinary, while sixty-four of them is
        the wrong endpoint. That keeps the extra request off the common path.
        """
        if not events or self._has_any_price(events):
            return events
        by_event: dict[str, list[dict]] = {}
        for series in NFL_SERIES:
            try:
                rows = self.fetch_markets(series)
            except SourceError:
                continue
            for market in rows:
                key = str(market.get("event_ticker") or "")
                if key:
                    by_event.setdefault(key, []).append(market)
            if by_event:
                break
        if not by_event:
            return events
        for event in events:
            key = str(event.get("event_ticker") or event.get("ticker") or "")
            found = by_event.get(key)
            if found:
                event["markets"] = found
        return events

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

    def events_from_markets(self, *, limit: int = 1000) -> list[dict]:
        """Build the events from /markets, which is where the books live.

        The other way round from how this started. /events with nested markets
        is one request and reads well, and on this series it returns contracts
        without a bid or an ask -- so the pairing had nothing to price and the
        venue looked like it was quoting nothing. /markets is the endpoint that
        owns market data; every contract names its event, so the events can be
        assembled from it rather than fetched and then patched.
        """
        by_event: dict[str, dict] = {}
        for series in NFL_SERIES:
            try:
                rows = self.fetch_markets(series, limit=limit)
            except SourceError:
                continue
            for market in rows:
                key = str(market.get("event_ticker") or "")
                if not key:
                    continue
                event = by_event.setdefault(
                    key, {"event_ticker": key, "markets": [],
                          "close_time": market.get("close_time")})
                event["markets"].append(market)
            if by_event:
                break
        return list(by_event.values())

    def fetch(self) -> list[KalshiQuote]:
        """Quotes, from whichever endpoint has the prices.

        /markets first because it is the one that carries a book. The events
        endpoint is kept as the fallback: it is a single request, it is what
        works when the series is filed somewhere the markets query does not
        reach, and having both means a change at either end is survivable.
        """
        quotes = normalise(self.events_from_markets())
        if quotes:
            return quotes
        return normalise(self.fill_prices(self.fetch_events()))
