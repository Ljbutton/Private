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
        self.weekly_budget = cfg.odds_weekly_budget
        self.daily_budget = cfg.odds_daily_budget
        self.http = client or HttpClient(cache_ttl=0.0)
        self.remaining: int | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    # ------------------------------------------------------------- budgeting
    #
    # Three windows, all enforced, because they answer different questions.
    #
    # The month is the bill: spend it and the account is done until it resets.
    # The day and the week are burst ceilings -- they exist so a bug, a retry
    # loop or a stuck scheduler cannot spend a month's credits in an afternoon,
    # which is exactly the failure the month-only guard could not see coming.
    #
    # The daily ceiling is deliberately well above the sustainable rate. At 480
    # a month the even pace is about 16 credits a day; a cap of 50 lets a busy
    # Sunday poll harder than a quiet Tuesday without ever letting a runaway
    # loop off the leash. The month still governs the total -- if the daily cap
    # were the binding constraint, 50 a day would be 1,500 a month.
    @staticmethod
    def _period() -> str:
        return now().strftime("%Y-%m")

    @staticmethod
    def _day() -> str:
        return now().strftime("%Y-%m-%d")

    @staticmethod
    def _week() -> str:
        year, week, _ = now().isocalendar()
        return f"{year}-W{week:02d}"

    def _spent(self, key: str, bucket: str) -> int:
        from .. import db

        return int((db.get_meta(key, {}) or {}).get(bucket, 0))

    def usage(self) -> dict:
        from .. import db

        counters = db.get_meta("odds_api_usage", {}) or {}
        period = self._period()
        used = int(counters.get(period, 0))
        day_used = self._spent("odds_api_usage_daily", self._day())
        week_used = self._spent("odds_api_usage_weekly", self._week())
        return {
            "day": self._day(),
            "day_used": day_used,
            "day_budget": self.daily_budget,
            "day_remaining": max(0, self.daily_budget - day_used),
            "week": self._week(),
            "week_used": week_used,
            "week_budget": self.weekly_budget,
            "week_remaining": max(0, self.weekly_budget - week_used),
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

        cost = max(1, int(cost))
        for key, bucket, keep in (
            ("odds_api_usage", self._period(), 6),
            ("odds_api_usage_weekly", self._week(), 10),
            ("odds_api_usage_daily", self._day(), 21),
        ):
            counters = db.get_meta(key, {}) or {}
            counters[bucket] = int(counters.get(bucket, 0)) + cost
            # Keep a short history so meta cannot grow forever.
            for stale in sorted(counters)[:-keep]:
                counters.pop(stale, None)
            db.set_meta(key, counters)

    def budget_available(self) -> bool:
        return self.budget_block() is None

    def budget_block(self) -> str | None:
        """Which window is spent, if any. None means the call may proceed."""
        u = self.usage()
        if u["day_remaining"] <= 0:
            return (f"the daily Odds API cap of {self.daily_budget} credits is spent "
                    f"({u['day_used']} used today); it resets at midnight")
        if u["week_remaining"] <= 0:
            return (f"the weekly Odds API cap of {self.weekly_budget} credits is spent "
                    f"({u['week_used']} used this week)")
        if u["remaining_budget"] <= 0:
            return (f"the monthly Odds API budget of {self.budget} credits is spent "
                    f"({u['used']} used in {u['period']})")
        return None

    # ---------------------------------------------------------------- fetch
    def fetch_odds(self, *, force: bool = False) -> list[dict]:
        """Current lines for every upcoming NFL game, one row per book/market."""
        if not self.enabled:
            raise SourceError("no ODDS_API_KEY configured")
        blocked = None if force else self.budget_block()
        if blocked:
            raise SourceError(
                f"{blocked}. Raise the limit on the Settings page, or wait."
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
