"""Betting edges: where our estimate beats the number you can actually bet.

Two deliberate choices:

*Priced against the best available line*, not the consensus, because that is the
bet you would really place — half a point and ten cents of juice is most of the
long-run edge in NFL betting.

*Priced from the blended estimate*, not the model's raw disagreement with the
market.  See ``ml/predict.py``: a model that says +7 into a line of 0 has not
found seven points of value, and treating it that way inflates every expected
value on the board.  ``prediction.spread_edge`` is already the shrunk, actionable
number, and that is what is used here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..util import (
    MARGIN_SD,
    TOTAL_SD,
    expected_value,
    kelly_fraction,
    margin_to_win_prob,
    over_prob,
)

# Minimum disagreement before a side is even considered. Below roughly a point
# and a half the model is not more precise than the market.
MIN_SPREAD_EDGE = 1.5
MIN_TOTAL_EDGE = 2.0
MIN_EV = 0.01

# Below this, the projection rests on too few games to be worth a stake.  This
# is what stops a cold Week 1 model — whose ratings are still near their priors
# — from reporting enormous phantom edges against a market that knows more.
MIN_INFORMATION = 0.6

# A disagreement this large with a liquid market is far more likely to be our
# bug or our blind spot than the market's mistake, so it is reported but never
# graded "strong".
IMPLAUSIBLE_EDGE = 10.0


@dataclass
class BetEdge:
    game_id: str
    market: str            # spread|total|moneyline
    selection: str         # e.g. "KC -3.5" or "Over 47.5"
    team: str | None
    line: float | None
    price: int | None
    book: str | None
    win_prob: float
    fair_price: int | None
    edge_points: float | None
    expected_value: float
    kelly: float
    confidence: str        # lean|solid|strong
    rationale: str = ""

    def to_dict(self) -> dict:
        return {
            "game_id": self.game_id,
            "market": self.market,
            "selection": self.selection,
            "team": self.team,
            "line": self.line,
            "price": self.price,
            "book": self.book,
            "win_prob": round(self.win_prob, 4),
            "fair_price": self.fair_price,
            "edge_points": None if self.edge_points is None else round(self.edge_points, 2),
            "expected_value": round(self.expected_value, 4),
            "kelly": round(self.kelly, 4),
            "confidence": self.confidence,
            "rationale": self.rationale,
        }


@dataclass
class EdgeReport:
    edges: list[BetEdge] = field(default_factory=list)

    def sorted_by_value(self) -> list[BetEdge]:
        return sorted(self.edges, key=lambda e: e.expected_value, reverse=True)


def _confidence(edge_points: float | None, ev: float) -> str:
    size = abs(edge_points or 0.0)
    if size >= IMPLAUSIBLE_EDGE:
        return "suspect"
    if size >= 3.0 and ev >= 0.05:
        return "strong"
    if size >= 2.0 and ev >= 0.025:
        return "solid"
    return "lean"


def find_edges(
    prediction,
    consensus,
    game: dict,
    *,
    residual_sd: float = MARGIN_SD,
) -> list[BetEdge]:
    """Evaluate spread, total and moneyline for one game."""
    from ..teams import TEAMS

    edges: list[BetEdge] = []
    home, away = game["home"], game["away"]
    game_id = str(game.get("game_id"))
    if consensus is None:
        return edges
    if getattr(prediction, "information", 1.0) < MIN_INFORMATION:
        return edges

    # ---------------------------------------------------------------- spread
    if consensus.spread_home is not None and prediction.spread_edge is not None:
        # Positive spread_edge means our blended estimate likes the home side.
        if prediction.spread_edge > 0:
            best = consensus.best_home_spread or (consensus.spread_home, "consensus")
            team, line, book = home, best[0], best[1]
            price = consensus.spread_price_home or -110
            margin_vs_line = prediction.fair_margin + line
        else:
            away_line = (
                consensus.best_away_spread[0] if consensus.best_away_spread
                else -consensus.spread_home
            )
            book = consensus.best_away_spread[1] if consensus.best_away_spread else "consensus"
            team, line = away, away_line
            price = consensus.spread_price_away or -110
            margin_vs_line = -prediction.fair_margin + line

        win_prob = margin_to_win_prob(margin_vs_line, sd=residual_sd)
        ev = expected_value(win_prob, price) or 0.0
        if abs(prediction.spread_edge) >= MIN_SPREAD_EDGE and ev >= MIN_EV:
            edges.append(
                BetEdge(
                    game_id=game_id, market="spread",
                    selection=f"{team} {line:+g}", team=team, line=line,
                    price=price, book=book, win_prob=win_prob,
                    fair_price=_fair(win_prob), edge_points=margin_vs_line,
                    expected_value=ev, kelly=kelly_fraction(win_prob, price),
                    confidence=_confidence(margin_vs_line, ev),
                    rationale=(
                        f"Model projects {TEAMS[team].name} by "
                        f"{abs(prediction.model_margin):.1f} where the line implies "
                        f"{abs(consensus.spread_home):.1f}; blended estimate keeps "
                        f"{abs(prediction.spread_edge):.1f} points of that."
                    ),
                )
            )

    # ----------------------------------------------------------------- total
    if consensus.total_points is not None and prediction.total_edge is not None:
        if prediction.total_edge > 0:
            best = consensus.best_over or (consensus.total_points, "consensus")
            side, line, book = "Over", best[0], best[1]
            price = consensus.total_price_over or -110
            win_prob = over_prob(prediction.fair_total, line, sd=TOTAL_SD)
        else:
            best = consensus.best_under or (consensus.total_points, "consensus")
            side, line, book = "Under", best[0], best[1]
            price = consensus.total_price_under or -110
            win_prob = 1.0 - over_prob(prediction.fair_total, line, sd=TOTAL_SD)

        ev = expected_value(win_prob, price) or 0.0
        if abs(prediction.total_edge) >= MIN_TOTAL_EDGE and ev >= MIN_EV:
            edges.append(
                BetEdge(
                    game_id=game_id, market="total",
                    selection=f"{side} {line:g}", team=None, line=line,
                    price=price, book=book, win_prob=win_prob,
                    fair_price=_fair(win_prob),
                    edge_points=prediction.fair_total - line,
                    expected_value=ev, kelly=kelly_fraction(win_prob, price),
                    confidence=_confidence(prediction.total_edge, ev),
                    rationale=(
                        f"Model projects {prediction.model_total:.1f} combined points; "
                        f"blended with the market gives {prediction.fair_total:.1f} "
                        f"against a {line:g} line."
                    ),
                )
            )

    # ------------------------------------------------------------- moneyline
    if consensus.home_win_prob is not None:
        for team, model_prob, best in (
            (home, prediction.home_win_prob, consensus.best_ml_home),
            (away, 1 - prediction.home_win_prob, consensus.best_ml_away),
        ):
            if best is None:
                continue
            price, book = best
            ev = expected_value(model_prob, price) or 0.0
            # Moneyline dogs are where stale prices survive longest, but the
            # variance is brutal, so demand a clearly larger edge than a spread.
            if ev >= 0.04 and model_prob >= 0.12:
                edges.append(
                    BetEdge(
                        game_id=game_id, market="moneyline",
                        selection=f"{team} ML {price:+d}", team=team, line=None,
                        price=price, book=book, win_prob=model_prob,
                        fair_price=_fair(model_prob), edge_points=None,
                        expected_value=ev, kelly=kelly_fraction(model_prob, price),
                        confidence="solid" if ev >= 0.08 else "lean",
                        rationale=(
                            f"Model win probability {model_prob:.0%} versus "
                            f"{_implied(price):.0%} implied by the price."
                        ),
                    )
                )

    return edges


def _fair(prob: float) -> int | None:
    from ..util import prob_to_american

    return prob_to_american(prob)


def _implied(price: int) -> float:
    from ..util import american_to_prob

    return american_to_prob(price) or 0.0
