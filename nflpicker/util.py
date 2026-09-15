"""Time handling, NFL calendar helpers, and betting-market math."""

from __future__ import annotations

import datetime as dt
import hashlib
import math
from typing import Any

import numpy as np

UTC = dt.timezone.utc

# Standard deviation of NFL final margins around the expected margin.  ~13.2 is
# the long-run figure from historical scores; it is the single most important
# constant for turning a projected margin into a win probability.
MARGIN_SD = 13.2
TOTAL_SD = 10.5


# ------------------------------------------------------------------ time utils

def now() -> dt.datetime:
    return dt.datetime.now(UTC)


def now_iso() -> str:
    return now().replace(microsecond=0).isoformat()


def to_utc(value: Any) -> dt.datetime | None:
    """Parse the various timestamp spellings our sources emit."""
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(float(value), tz=UTC)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
                    "%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z"):
            try:
                parsed = dt.datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def iso(value: Any) -> str | None:
    parsed = to_utc(value)
    return parsed.replace(microsecond=0).isoformat() if parsed else None


def hours_between(a: Any, b: Any) -> float | None:
    da, db_ = to_utc(a), to_utc(b)
    if not da or not db_:
        return None
    return (db_ - da).total_seconds() / 3600.0


# -------------------------------------------------------------- nfl calendar

def labor_day(year: int) -> dt.date:
    """First Monday in September."""
    d = dt.date(year, 9, 1)
    return d + dt.timedelta(days=(7 - d.weekday()) % 7)


def season_week1_thursday(season: int) -> dt.date:
    """Week 1 opens the Thursday after Labor Day."""
    return labor_day(season) + dt.timedelta(days=3)


def current_season(today: dt.date | None = None) -> int:
    """The NFL season label for a date. A January game belongs to the prior season."""
    today = today or now().date()
    # Roll over to the new season once the prior one's playoffs are done (~mid Feb).
    return today.year if today.month >= 3 else today.year - 1


def estimate_week(when: dt.date | None = None, season: int | None = None) -> int:
    """Best-effort week number, used only when the schedule table is empty."""
    when = when or now().date()
    season = season or current_season(when)
    start = season_week1_thursday(season)
    if when < start:
        return 1
    return max(1, min(18, (when - start).days // 7 + 1))


# ------------------------------------------------------------- market math

def american_to_prob(odds: float | None) -> float | None:
    """Implied probability of American odds, vig included."""
    if odds is None:
        return None
    odds = float(odds)
    if odds == 0:
        return None
    if odds > 0:
        return 100.0 / (odds + 100.0)
    return -odds / (-odds + 100.0)


def prob_to_american(prob: float | None) -> int | None:
    if prob is None or not (0.0 < prob < 1.0):
        return None
    if prob >= 0.5:
        return int(round(-100.0 * prob / (1.0 - prob)))
    return int(round(100.0 * (1.0 - prob) / prob))


def american_to_decimal(odds: float | None) -> float | None:
    """Decimal payout for American odds. Zero is not a price, so it is None —
    the same guard ``american_to_prob`` already applies, without which a
    malformed feed sending 0 crashes rather than being ignored."""
    if odds is None:
        return None
    odds = float(odds)
    if odds == 0:
        return None
    return 1.0 + (odds / 100.0 if odds > 0 else 100.0 / -odds)


def devig(prob_a: float | None, prob_b: float | None, method: str = "power") -> tuple[float, float] | None:
    """Remove the bookmaker's margin from a two-way market.

    ``multiplicative`` just normalises; it systematically over-prices favourites.
    ``power`` solves for k with p_a^k + p_b^k = 1, which tracks observed
    closing-line fairness better, so it is the default.
    """
    if prob_a is None or prob_b is None:
        return None
    if prob_a <= 0 or prob_b <= 0:
        return None
    total = prob_a + prob_b
    if total <= 0:
        return None
    if method == "multiplicative" or abs(total - 1.0) < 1e-9:
        return prob_a / total, prob_b / total
    lo, hi = 0.2, 5.0
    for _ in range(60):
        k = (lo + hi) / 2
        s = prob_a**k + prob_b**k
        if s > 1.0:
            lo = k
        else:
            hi = k
    k = (lo + hi) / 2
    a, b = prob_a**k, prob_b**k
    s = a + b
    return a / s, b / s


def normal_cdf(x):
    """Standard normal CDF for a scalar or a numpy array.

    ``math.erf`` is scalar-only, and the prediction path evaluates whole slates
    at once, so arrays take the vectorised branch.
    """
    if isinstance(x, np.ndarray):
        from scipy.special import ndtr

        return ndtr(x)
    return 0.5 * (1.0 + math.erf(float(x) / math.sqrt(2.0)))


def normal_ppf(p: float) -> float:
    """Inverse normal CDF (Acklam's rational approximation, ~1e-9 accurate)."""
    if p <= 0.0:
        return -8.0
    if p >= 1.0:
        return 8.0
    a = [-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
         1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00]
    b = [-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
         6.680131188771972e01, -1.328068155288572e01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
         -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00,
         3.754408661907416e00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


def margin_to_win_prob(margin, sd: float = MARGIN_SD):
    """P(team wins) given its projected scoring margin. Accepts arrays."""
    if isinstance(margin, np.ndarray):
        return normal_cdf(margin / sd)
    return normal_cdf(float(margin) / sd)


def win_prob_to_margin(prob: float, sd: float = MARGIN_SD) -> float:
    return normal_ppf(min(max(prob, 1e-6), 1 - 1e-6)) * sd


def cover_prob(margin, spread_home, sd: float = MARGIN_SD):
    """P(home covers) where ``spread_home`` is the home line (-3.5 = 3.5-pt fav)."""
    return normal_cdf((margin + spread_home) / sd)


def over_prob(projected_total, market_total, sd: float = TOTAL_SD):
    return normal_cdf((projected_total - market_total) / sd)


def kelly_fraction(win_prob: float, american_odds: float | None, cap: float = 0.05) -> float:
    """Fractional-Kelly stake (quarter Kelly), capped. Returns 0 when no edge."""
    dec = american_to_decimal(american_odds)
    if dec is None or dec <= 1.0:
        return 0.0
    b = dec - 1.0
    edge = win_prob * b - (1.0 - win_prob)
    if edge <= 0:
        return 0.0
    return min(cap, max(0.0, (edge / b) * 0.25))


def expected_value(win_prob: float, american_odds: float | None) -> float | None:
    """EV per 1 unit staked."""
    dec = american_to_decimal(american_odds)
    if dec is None:
        return None
    return win_prob * (dec - 1.0) - (1.0 - win_prob)


# -------------------------------------------------------------------- misc

def stable_id(*parts: Any) -> str:
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def mean(values) -> float | None:
    vals = [float(v) for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def median(values) -> float | None:
    vals = sorted(float(v) for v in values if v is not None)
    if not vals:
        return None
    n = len(vals)
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def round_half(value: float | None) -> float | None:
    """Snap to the nearest half point, the way a book posts a line."""
    return None if value is None else round(value * 2.0) / 2.0
