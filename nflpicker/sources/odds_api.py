"""the-odds-api.com adapter — real book-by-book lines.

The free tier allows 500 requests a month, so the adapter tracks its own usage
against a configurable monthly budget and simply refuses to call once spent.
Running out of quota on a Wednesday is worse than polling a little less often.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import httpx

from ..config import get_config
from ..teams import try_resolve
from ..util import iso, now
from .base import HttpClient, SourceError

BASE = "https://api.the-odds-api.com/v4"
SPORT = "americanfootball_nfl"
MARKETS = "h2h,spreads,totals"


class OddsApiSource:
    def __init__(self, client: HttpClient | None = None, api_key: str | None = None) -> None:
        cfg = get_config()
        self.api_key = (api_key if api_key is not None else cfg.odds_api_key).strip()
        self.books = [b.lower() for b in cfg.odds_books]
        self.budget = cfg.odds_monthly_budget
        self.http = client or HttpClient(cache_ttl=0.0)
        self.remaining: int | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    # ------------------------------------------------------------- budgeting
    @staticmethod
    def _period() -> str:
        return now().strftime("%Y-%m")

    def usage(self) -> dict:
        from .. import db

        counters = db.get_meta("odds_api_usage", {}) or {}
        period = self._period()
        used = int(counters.get(period, 0))
        return {
            "period": period,
            "used": used,
            "budget": self.budget,
            "remaining_budget": max(0, self.budget - used),
            "provider_remaining": self.remaining,
        }

    @staticmethod
    def _cost_of(response: Any, params: dict[str, Any]) -> int:
        """How many credits that request actually spent.

        The Odds API bills one credit per market *per region*, not one per
        HTTP request. Counting requests made the budget guard undercount by
        exactly the number of markets -- three, here -- so a 500-request
        budget was spent after 167 polls while the app still believed it had
        two thirds left.

        `x-requests-last` is the provider's own figure for the call that just
        happened, so it is preferred; the multiplication is the fallback.
        """
        header = getattr(response, "headers", {}) or {}
        reported = header.get("x-requests-last")
        if reported is not None:
            try:
                return max(1, int(float(reported)))
            except (TypeError, ValueError):
                pass
        markets = len([m for m in str(params.get("markets", "")).split(",") if m])
        regions = len([r for r in str(params.get("regions", "")).split(",") if r])
        return max(1, markets * regions)

    def _record_call(self, cost: int = 1) -> None:
        from .. import db

        counters = db.get_meta("odds_api_usage", {}) or {}
        period = self._period()
        counters[period] = int(counters.get(period, 0)) + max(1, int(cost))
        # Keep only the last few months so meta does not grow forever.
        for key in sorted(counters)[:-6]:
            counters.pop(key, None)
        db.set_meta("odds_api_usage", counters)

    def budget_available(self) -> bool:
        return self.usage()["remaining_budget"] > 0

    # ---------------------------------------------------------------- fetch
    def fetch_odds(self, *, force: bool = False) -> list[dict]:
        """Current lines for every upcoming NFL game, one row per book/market."""
        if not self.enabled:
            raise SourceError("no ODDS_API_KEY configured")
        if not force and not self.budget_available():
            raise SourceError(
                f"monthly Odds API budget of {self.budget} requests is spent; "
                "raise ODDS_MONTHLY_BUDGET or wait for the next period"
            )

        params: dict[str, Any] = {
            "apiKey": self.api_key,
            "regions": "us",
            "markets": MARKETS,
            "oddsFormat": "american",
            "dateFormat": "iso",
        }
        if self.books:
            params["bookmakers"] = ",".join(self.books)

        cfg = get_config()
        try:
            with httpx.Client(timeout=cfg.http_timeout, headers={"User-Agent": cfg.user_agent}) as c:
                resp = c.get(f"{BASE}/sports/{SPORT}/odds", params=params)
            self._record_call(self._cost_of(resp, params))
            remaining = resp.headers.get("x-requests-remaining")
            if remaining is not None:
                try:
                    self.remaining = int(float(remaining))
                except ValueError:
                    pass
            if resp.status_code == 401:
                raise SourceError("Odds API rejected the key (401)")
            if resp.status_code == 429:
                raise SourceError("Odds API quota exhausted (429)")
            resp.raise_for_status()
            payload = resp.json()
        except SourceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise SourceError(f"Odds API request failed: {exc}") from exc

        return parse_odds_payload(payload)


def _american(value: Any) -> int | None:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _point(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_odds_payload(payload: list[dict]) -> list[dict]:
    """Normalise the API's event/bookmaker/market nesting into flat quotes.

    One output row = one book's take on one market for one game.
    """
    quotes: list[dict] = []
    for event in payload or []:
        home = try_resolve(event.get("home_team"))
        away = try_resolve(event.get("away_team"))
        if not home or not away:
            continue
        kickoff = iso(event.get("commence_time"))
        base = {
            "provider_event_id": event.get("id"),
            "home": home,
            "away": away,
            "kickoff": kickoff,
        }
        for book in event.get("bookmakers") or []:
            book_key = book.get("key") or book.get("title")
            if not book_key:
                continue
            last_update = iso(book.get("last_update"))
            for market in book.get("markets") or []:
                kind = market.get("key")
                outcomes = market.get("outcomes") or []
                by_team: dict[str, dict] = {}
                for o in outcomes:
                    name = (o.get("name") or "").strip()
                    key = try_resolve(name) or name.lower()
                    by_team[key] = o
                if kind == "h2h" and home in by_team and away in by_team:
                    quotes.append({
                        **base, "book": book_key, "market": "moneyline",
                        "captured_at": last_update,
                        "home_price": _american(by_team[home].get("price")),
                        "away_price": _american(by_team[away].get("price")),
                        "home_point": None, "away_point": None,
                    })
                elif kind == "spreads" and home in by_team and away in by_team:
                    quotes.append({
                        **base, "book": book_key, "market": "spread",
                        "captured_at": last_update,
                        "home_point": _point(by_team[home].get("point")),
                        "away_point": _point(by_team[away].get("point")),
                        "home_price": _american(by_team[home].get("price")),
                        "away_price": _american(by_team[away].get("price")),
                    })
                elif kind == "totals":
                    over, under = by_team.get("over"), by_team.get("under")
                    if over and under:
                        quotes.append({
                            **base, "book": book_key, "market": "total",
                            "captured_at": last_update,
                            "home_point": _point(over.get("point")),
                            "away_point": _point(under.get("point")),
                            "home_price": _american(over.get("price")),
                            "away_price": _american(under.get("price")),
                        })
    return quotes


def suggested_interval(budget_remaining: int, days_left_in_month: float) -> int:
    """Seconds between polls that spends the remaining budget evenly.

    Called by the scheduler so a user with a free key still gets fresh lines on
    Sunday morning instead of a quota wall on the 12th.
    """
    days_left = max(0.5, days_left_in_month)
    if budget_remaining <= 0:
        return 24 * 3600
    per_day = budget_remaining / days_left
    if per_day <= 0:
        return 24 * 3600
    return int(max(600, min(24 * 3600, 86400 / per_day)))


def days_left_in_month(when: dt.datetime | None = None) -> float:
    when = when or now()
    if when.month == 12:
        nxt = when.replace(year=when.year + 1, month=1, day=1, hour=0, minute=0,
                           second=0, microsecond=0)
    else:
        nxt = when.replace(month=when.month + 1, day=1, hour=0, minute=0,
                           second=0, microsecond=0)
    return (nxt - when).total_seconds() / 86400.0
