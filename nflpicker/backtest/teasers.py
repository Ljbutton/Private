"""Do 6-point teasers through the key numbers actually win?

A teaser moves the spread in your favour on every leg, and all legs must win.
The well-known claim (Stanford Wong's, and the reason "Wong teaser" is a phrase)
is that teasing a 7.5-to-8.5-point favourite down through 7 and 3, or a
1.5-to-2.5-point underdog up through 3 and 7, wins often enough to beat the
price — because NFL margins pile up on exactly those two numbers.

That is a claim about history, and history is what this file checks. Nothing
here depends on our model being right about anything: it needs only closing
spreads and final scores, both of which we already have for every game since
1999.

The break-even is unforgiving and is the whole story. A two-leg teaser at -110
needs each leg to win 72.4% of the time, because both must land:

    p² · 1 = (1 - p²) · 1.1   →   p = sqrt(1.1 / 2.1) = 0.7238

Ten cents of extra juice moves that bar by a full percentage point, which is
most of the claimed edge — so the price is reported alongside every result.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

# Standard teaser sizes offered on NFL sides.
TEASER_POINTS = 6.0

# Wong's windows: the sides whose teased line crosses both 3 and 7.
FAVOURITE_WINDOW = (-8.5, -7.5)
UNDERDOG_WINDOW = (1.5, 2.5)


def break_even(legs: int, american_price: float) -> float:
    """Per-leg win rate needed to break even on an all-or-nothing parlay."""
    from ..util import american_to_decimal

    decimal = american_to_decimal(american_price)
    if not decimal or decimal <= 1:
        return 1.0
    # p^legs * (decimal - 1) = (1 - p^legs)  →  p = (1/decimal)^(1/legs)
    return float((1.0 / decimal) ** (1.0 / legs))


def wilson_interval(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% confidence interval for a win rate.

    Reported on every result because the whole question is whether an observed
    rate is distinguishable from the break-even, and with a thousand-odd legs
    it very often is not. A point estimate above break-even with an interval
    straddling it is not an edge; it is a sample.
    """
    if n <= 0:
        return (0.0, 1.0)
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


@dataclass
class TeaserResult:
    label: str
    n: int
    wins: int
    pushes: int
    win_rate: float | None
    break_even_2leg: float
    edge: float | None          # win_rate minus the 2-leg break-even
    roi_2leg: float | None      # expected return per unit on a 2-leg ticket
    ci_low: float = 0.0
    ci_high: float = 1.0

    @property
    def significant(self) -> bool:
        """True only when the whole interval clears the break-even."""
        return self.n > 0 and self.ci_low > self.break_even_2leg

    def to_dict(self) -> dict:
        return {
            "label": self.label, "n": self.n, "wins": self.wins, "pushes": self.pushes,
            "win_rate": None if self.win_rate is None else round(self.win_rate, 4),
            "break_even_2leg": round(self.break_even_2leg, 4),
            "edge": None if self.edge is None else round(self.edge, 4),
            "roi_2leg": None if self.roi_2leg is None else round(self.roi_2leg, 4),
            "ci_low": round(self.ci_low, 4), "ci_high": round(self.ci_high, 4),
            "significant": self.significant,
        }


def _legs(games: pd.DataFrame) -> pd.DataFrame:
    """Expand each game into its two bettable sides.

    ``spread_line`` is nflverse's "points the home team is favoured by", so the
    posted home line is its negation — the sign conversion that has to happen
    exactly once, and happens here.
    """
    usable = games.dropna(subset=["spread_line", "result"]).copy()
    home = pd.DataFrame({
        "season": usable["season"],
        "spread": -usable["spread_line"],        # posted home line
        "margin": usable["result"],              # home points - away points
        "side": "home",
    })
    away = pd.DataFrame({
        "season": usable["season"],
        "spread": usable["spread_line"],
        "margin": -usable["result"],
        "side": "away",
    })
    return pd.concat([home, away], ignore_index=True)


def evaluate_window(
    legs: pd.DataFrame,
    low: float,
    high: float,
    *,
    points: float = TEASER_POINTS,
    label: str = "",
    price: float = -110,
) -> TeaserResult:
    """Win rate for every leg whose posted spread falls in [low, high]."""
    picked = legs[(legs["spread"] >= low) & (legs["spread"] <= high)].copy()
    picked["teased"] = picked["spread"] + points
    picked["result"] = picked["margin"] + picked["teased"]

    pushes = int((picked["result"] == 0).sum())
    decided = picked[picked["result"] != 0]
    wins = int((decided["result"] > 0).sum())
    n = int(len(decided))

    rate = wins / n if n else None
    be = break_even(2, price)
    roi = None
    if rate is not None:
        from ..util import american_to_decimal

        decimal = american_to_decimal(price) or 1.0
        roi = (rate**2) * (decimal - 1.0) - (1.0 - rate**2)

    ci_low, ci_high = wilson_interval(wins, n)
    return TeaserResult(
        label=label or f"{low:+g} to {high:+g}",
        n=n, wins=wins, pushes=pushes, win_rate=rate,
        break_even_2leg=be,
        edge=None if rate is None else rate - be,
        roi_2leg=roi, ci_low=ci_low, ci_high=ci_high,
    )


def wong_windows(games: pd.DataFrame, *, points: float = TEASER_POINTS,
                 price: float = -110) -> list[TeaserResult]:
    """The two classic windows, plus everything combined."""
    legs = _legs(games)
    favourites = evaluate_window(legs, *FAVOURITE_WINDOW, points=points,
                                 label="Favourites -8.5 to -7.5", price=price)
    underdogs = evaluate_window(legs, *UNDERDOG_WINDOW, points=points,
                                label="Underdogs +1.5 to +2.5", price=price)
    both = evaluate_window(
        legs[
            legs["spread"].between(*FAVOURITE_WINDOW)
            | legs["spread"].between(*UNDERDOG_WINDOW)
        ],
        -99, 99, points=points, label="Both Wong windows", price=price,
    )
    return [favourites, underdogs, both]


def sweep(games: pd.DataFrame, *, points: float = TEASER_POINTS,
          price: float = -110, width: float = 1.0) -> list[TeaserResult]:
    """Every spread window, not just the ones the folklore names.

    Checking only the windows a theory nominates is how a theory survives data
    that does not support it; sweeping the range shows whether those two
    windows are actually special or merely the ones someone named.
    """
    legs = _legs(games)
    out: list[TeaserResult] = []
    low = -14.0
    while low <= 9.0:
        result = evaluate_window(legs, low, low + width, points=points, price=price)
        if result.n >= 100:
            out.append(result)
        low += width
    return out


def by_era(games: pd.DataFrame, *, split: int = 2014, points: float = TEASER_POINTS,
           price: float = -110) -> list[TeaserResult]:
    """The edge is widely believed to have been priced away. Test that."""
    legs = _legs(games)
    windows = legs[
        legs["spread"].between(*FAVOURITE_WINDOW)
        | legs["spread"].between(*UNDERDOG_WINDOW)
    ]
    early = windows[windows["season"] < split]
    late = windows[windows["season"] >= split]
    return [
        evaluate_window(early, -99, 99, points=points,
                        label=f"Wong windows, pre-{split}", price=price),
        evaluate_window(late, -99, 99, points=points,
                        label=f"Wong windows, {split}+", price=price),
    ]


def price_sensitivity(games: pd.DataFrame, *, points: float = TEASER_POINTS,
                      prices=(-110, -120, -130, -140)) -> list[dict]:
    """The same bet at the prices books actually offer.

    Teaser pricing moved against the bettor precisely because this was known;
    a result that only survives at -110 is not a result you can place.
    """
    legs = _legs(games)
    windows = legs[
        legs["spread"].between(*FAVOURITE_WINDOW)
        | legs["spread"].between(*UNDERDOG_WINDOW)
    ]
    rows = []
    for price in prices:
        result = evaluate_window(windows, -99, 99, points=points,
                                 label=f"{price:+g}", price=price)
        rows.append(result.to_dict())
    return rows


def key_number_frequency(games: pd.DataFrame) -> list[dict]:
    """How often games land on each margin — the reason any of this works."""
    usable = games.dropna(subset=["result"])
    counts = usable["result"].abs().value_counts(normalize=True).sort_index()
    return [
        {"margin": int(margin), "share": round(float(share), 4)}
        for margin, share in counts.items() if margin <= 14
    ]


def full_report(games: pd.DataFrame, *, points: float = TEASER_POINTS) -> dict:
    return {
        "n_games": int(len(games.dropna(subset=["spread_line", "result"]))),
        "seasons": [int(games["season"].min()), int(games["season"].max())],
        "teaser_points": points,
        "break_even_2leg_110": round(break_even(2, -110), 4),
        "break_even_3leg_160": round(break_even(3, 160), 4),
        "wong": [r.to_dict() for r in wong_windows(games, points=points)],
        "by_era": [r.to_dict() for r in by_era(games, points=points)],
        "price_sensitivity": price_sensitivity(games, points=points),
        "sweep": [r.to_dict() for r in sweep(games, points=points)],
        "key_numbers": key_number_frequency(games),
    }


def describe(result: TeaserResult) -> str:
    if result.win_rate is None:
        return f"{result.label}: no qualifying legs"
    verdict = "clears" if result.significant else (
        "above, but within noise of" if (result.edge or 0) > 0 else "below"
    )
    return (
        f"{result.label}: {result.wins}/{result.n} = {result.win_rate:.1%} "
        f"[95% CI {result.ci_low:.1%}-{result.ci_high:.1%}] — {verdict} the "
        f"{result.break_even_2leg:.1%} break-even; 2-leg ROI {result.roi_2leg:+.1%}"
    )


def math_check() -> dict:
    """Sanity figures for the break-even, independent of any data."""
    return {
        "2-leg at -110": round(break_even(2, -110), 4),
        "2-leg at -120": round(break_even(2, -120), 4),
        "2-leg at -130": round(break_even(2, -130), 4),
        "3-leg at +160": round(break_even(3, 160), 4),
        "check": round(math.sqrt(1.1 / 2.1), 4),
    }
