"""ESPN pick'em recommendations, including confidence-point assignment.

Straight picks are the easy part.  The confidence assignment has a clean
answer: to maximise expected points, give the most points to the most likely
winner.  (Rearrangement inequality — pairing the largest weights with the
largest probabilities maximises the sum.)

The harder, more useful question is what to do in a *big* pool, where matching
the field's expected score is not enough to win.  That is what leverage mode is
for: it deliberately deviates where our probability disagrees most with the
market, because the field picks close to the market.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..teams import TEAMS


@dataclass
class PickemPick:
    game_id: str
    pick: str
    opponent: str
    home_away: str
    win_prob: float
    market_prob: float | None
    confidence: int
    edge: float | None           # model probability minus market probability
    kickoff: str | None = None
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "game_id": self.game_id,
            "pick": self.pick,
            "pick_name": TEAMS[self.pick].full_name if self.pick in TEAMS else self.pick,
            "opponent": self.opponent,
            "home_away": self.home_away,
            "win_prob": round(self.win_prob, 4),
            "market_prob": None if self.market_prob is None else round(self.market_prob, 4),
            "confidence": self.confidence,
            "edge": None if self.edge is None else round(self.edge, 4),
            "kickoff": self.kickoff,
            "note": self.note,
        }


@dataclass
class PickemBoard:
    season: int
    week: int
    mode: str
    picks: list[PickemPick] = field(default_factory=list)
    expected_correct: float = 0.0
    expected_points: float = 0.0
    max_points: int = 0
    field_expected_points: float = 0.0

    def to_dict(self) -> dict:
        return {
            "season": self.season,
            "week": self.week,
            "mode": self.mode,
            "picks": [p.to_dict() for p in self.picks],
            "expected_correct": round(self.expected_correct, 2),
            "expected_points": round(self.expected_points, 2),
            "max_points": self.max_points,
            "field_expected_points": round(self.field_expected_points, 2),
            "n_games": len(self.picks),
        }


def build_pickem(
    season: int,
    week: int,
    entries: list[dict],
    *,
    mode: str = "ev",
    leverage_weight: float = 1.5,
) -> PickemBoard:
    """Build the week's board.

    ``entries`` items need: game_id, home, away, home_win_prob, and optionally
    market_home_prob and kickoff.

    ``mode`` is ``"ev"`` (maximise expected points — right for small pools) or
    ``"leverage"`` (tilt toward disagreement with the market — right when you
    need to finish first in a large pool rather than merely score well).
    """
    picks: list[PickemPick] = []
    for entry in entries:
        home_prob = float(entry["home_win_prob"])
        market_prob = entry.get("market_home_prob")
        if home_prob >= 0.5:
            team, opponent, side, prob = entry["home"], entry["away"], "home", home_prob
            market_side = market_prob
        else:
            team, opponent, side, prob = entry["away"], entry["home"], "away", 1 - home_prob
            market_side = None if market_prob is None else 1 - market_prob

        picks.append(
            PickemPick(
                game_id=str(entry.get("game_id")),
                pick=team, opponent=opponent, home_away=side,
                win_prob=prob, market_prob=market_side, confidence=0,
                edge=None if market_side is None else prob - market_side,
                kickoff=entry.get("kickoff"),
            )
        )

    n = len(picks)
    if mode == "leverage":
        def rank_key(p: PickemPick) -> float:
            return p.win_prob + leverage_weight * (p.edge or 0.0)
    else:
        def rank_key(p: PickemPick) -> float:
            return p.win_prob

    ordered = sorted(picks, key=rank_key, reverse=True)
    for i, pick in enumerate(ordered):
        pick.confidence = n - i
        if mode == "leverage" and pick.edge is not None and abs(pick.edge) >= 0.05:
            direction = "above" if pick.edge > 0 else "below"
            pick.note = (
                f"Model is {abs(pick.edge):.0%} {direction} the market here — "
                "this is where the pool can be gained or lost."
            )

    board = PickemBoard(season=season, week=week, mode=mode, picks=ordered)
    board.expected_correct = sum(p.win_prob for p in ordered)
    board.expected_points = sum(p.win_prob * p.confidence for p in ordered)
    board.max_points = n * (n + 1) // 2

    # What would the field score? The field is assumed to pick market favourites
    # and rank them by market confidence. Crucially, that assignment is scored
    # under *our* probabilities, the same ones used to score our own board —
    # scoring each side under its own beliefs compares nothing at all.
    with_market = [p for p in ordered if p.market_prob is not None]
    if with_market:
        field_order = sorted(with_market, key=lambda p: p.market_prob or 0.0, reverse=True)
        n_field = len(field_order)
        board.field_expected_points = sum(
            _our_prob_of_field_pick(p) * (n_field - i) for i, p in enumerate(field_order)
        )
    return board


def _our_prob_of_field_pick(pick: PickemPick) -> float:
    """Our probability that the field's side of this game wins.

    The field takes the market favourite. When that is the same team we took,
    it is simply our probability; when we disagree, it is one minus ours.
    """
    field_takes_our_side = (pick.market_prob or 0.0) >= 0.5
    return pick.win_prob if field_takes_our_side else 1.0 - pick.win_prob
