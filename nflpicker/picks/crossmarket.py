"""Cross-venue edges: the sharp market's price, aimed at a softer venue.

This is a different claim from the rest of the app, and a much weaker one to
have to defend. Everywhere else the question is "can our model beat the closing
line", and the measured answer is no. Here the question is only "is the thinner
venue lagging the deepest market in American sport", and the fair value is the
sportsbook consensus itself rather than anything we modelled.

That makes the sportsbook market's efficiency an asset. It does not make this
free money, and three things are enforced rather than assumed:

* **Depth.** A five-point probability gap on two hundred dollars of book is not
  an opportunity. Every edge is sized against the order book, not the last
  price, and edges with no depth behind them are dropped.
* **Consensus quality.** A "sharp" number from one book is not sharp. A minimum
  number of books must agree before their average is treated as truth.
* **Gap plausibility.** A very large disagreement is more often news the thin
  venue has priced and the books have not yet, a resolution-rule difference, or
  a market about to be voided — than a gift. Those are surfaced and flagged, not
  recommended.
"""

from __future__ import annotations

from dataclasses import dataclass

# The consensus is only treated as fair value when enough books agree.
MIN_BOOKS = 3

# Minimum probability disagreement worth acting on, in percentage points.
MIN_GAP = 0.03

# Minimum tradeable depth, in dollars, behind the side being bought.
MIN_DEPTH = 250.0

# Past this, the likelier explanations are news, a resolution-rule difference,
# or a market that will be voided — not a mispricing.
IMPLAUSIBLE_GAP = 0.20

# Polymarket has historically charged no per-trade fee, but settlement and
# gas are real and fee policy changes. Assumed cost per unit staked.
DEFAULT_FEE = 0.01


@dataclass
class CrossMarketEdge:
    game_id: str
    venue: str
    team: str                 # side to buy
    opponent: str
    venue_price: float        # probability you pay, 0-1
    fair_prob: float          # sportsbook no-vig consensus for that side
    gap: float                # fair_prob - venue_price
    expected_value: float     # per unit staked, after fees
    kelly: float
    depth: float              # tradeable dollars behind the side
    max_stake: float          # what the book actually supports
    n_books: int
    confidence: str           # lean | solid | strong | suspect
    rationale: str = ""

    def to_dict(self) -> dict:
        return {
            "game_id": self.game_id,
            "venue": self.venue,
            "team": self.team,
            "opponent": self.opponent,
            "venue_price": round(self.venue_price, 4),
            "fair_prob": round(self.fair_prob, 4),
            "gap": round(self.gap, 4),
            "expected_value": round(self.expected_value, 4),
            "kelly": round(self.kelly, 4),
            "depth": round(self.depth, 2),
            "max_stake": round(self.max_stake, 2),
            "n_books": self.n_books,
            "confidence": self.confidence,
            "rationale": self.rationale,
        }


def _confidence(gap: float, depth: float) -> str:
    if abs(gap) >= IMPLAUSIBLE_GAP:
        return "suspect"
    if gap >= 0.08 and depth >= 1000:
        return "strong"
    if gap >= 0.05:
        return "solid"
    return "lean"


def evaluate_game(
    game: dict,
    consensus,
    venue_home_prob: float | None,
    *,
    venue: str = "polymarket",
    depth: dict[str, float] | None = None,
    fee: float = DEFAULT_FEE,
    bankroll: float = 1000.0,
) -> list[CrossMarketEdge]:
    """Compare one venue's price against the sportsbook consensus for a game."""
    if consensus is None or venue_home_prob is None:
        return []
    fair_home = getattr(consensus, "home_win_prob", None)
    n_books = int(getattr(consensus, "n_books", 0) or 0)
    if fair_home is None or n_books < MIN_BOOKS:
        return []

    depth = depth or {}
    home, away = game["home"], game["away"]
    edges: list[CrossMarketEdge] = []

    for team, opponent, fair, price in (
        (home, away, float(fair_home), float(venue_home_prob)),
        (away, home, 1.0 - float(fair_home), 1.0 - float(venue_home_prob)),
    ):
        gap = fair - price
        if gap < MIN_GAP or not (0.0 < price < 1.0):
            continue

        # Buying at `price` to win 1 unit: profit (1 - price) with probability
        # `fair`, loss `price` otherwise. Fees are charged on the stake.
        expected_value = (fair * (1.0 - price) - (1.0 - fair) * price) / price - fee
        if expected_value <= 0:
            continue

        available = float(depth.get(team, 0.0))
        if available < MIN_DEPTH:
            continue

        # Binary-payout Kelly, quarter-sized and capped like everywhere else.
        b = (1.0 - price) / price
        kelly = max(0.0, (fair * b - (1.0 - fair)) / b) * 0.25
        kelly = min(kelly, 0.05)

        confidence = _confidence(gap, available)
        edges.append(
            CrossMarketEdge(
                game_id=str(game.get("game_id")),
                venue=venue, team=team, opponent=opponent,
                venue_price=price, fair_prob=fair, gap=gap,
                expected_value=expected_value, kelly=kelly,
                depth=available,
                max_stake=min(kelly * bankroll, available),
                n_books=n_books,
                confidence=confidence,
                rationale=(
                    f"{n_books} sportsbooks imply {fair:.0%} for {team}; "
                    f"{venue} is offering {price:.0%}"
                    + (
                        " — a gap this large is more likely news, a resolution-rule "
                        "difference or a market about to be voided than a mispricing."
                        if confidence == "suspect" else "."
                    )
                ),
            )
        )
    return edges


def find_cross_market_edges(
    games: list[dict],
    consensus_by_game: dict,
    venue_probs: dict[str, float],
    *,
    depths: dict[str, dict[str, float]] | None = None,
    venue: str = "polymarket",
    bankroll: float = 1000.0,
) -> list[CrossMarketEdge]:
    """Evaluate every game for which we have both a consensus and a venue price."""
    depths = depths or {}
    out: list[CrossMarketEdge] = []
    for game in games:
        game_id = str(game.get("game_id"))
        prob = venue_probs.get(game_id)
        if prob is None:
            continue
        out.extend(
            evaluate_game(
                game, consensus_by_game.get(game_id), prob,
                venue=venue, depth=depths.get(game_id), bankroll=bankroll,
            )
        )
    return sorted(out, key=lambda e: e.expected_value, reverse=True)
