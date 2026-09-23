"""Live win probability for a game in progress.

A pregame projection stops being the right answer the moment the ball is
kicked. What matters then is the score, how much time is left, and who has the
ball — and the pregame number's influence should fade as the game resolves it.

The model here is the standard time-decay approximation, and it is deliberately
simple enough to reason about:

    expected final margin = current margin
                          + pregame margin × (fraction of game remaining)
    uncertainty           = full-game SD × sqrt(fraction remaining)
    P(home wins)          = Φ(expected margin / uncertainty)

Two properties fall out of that and are what make it behave sensibly. Early on,
the pregame projection dominates and a fluke touchdown barely moves the number.
Late, uncertainty collapses toward zero, so a three-point lead with a minute
left is close to a win. Possession, down and field position are worth a point
or two and are added on top rather than modelled from scratch.

This is not a drive-level model and does not claim to be. NFL scoring is lumpy —
it arrives in 3s and 7s — and a continuous normal cannot know that a team
trailing by 3 needs one field goal while a team trailing by 4 needs a
touchdown. Expect it to run a few points confident in the last two minutes.

It is display only: nothing here feeds a pick, a stake or the model. That is
why an approximation that needs only the scoreboard is the right trade.
"""

from __future__ import annotations

from dataclasses import dataclass

from .util import MARGIN_SD, clamp, normal_cdf

REGULATION_SECONDS = 3600.0
QUARTER_SECONDS = 900.0

# Floor on remaining time so the probability cannot divide by zero and pin to
# a certainty the scoreboard has not actually earned.
MIN_SECONDS = 20.0

# Having the ball is worth roughly this much, decaying as the game shortens —
# one possession matters far more with two minutes left than with fifty.
POSSESSION_POINTS = 1.6

# Being inside the opponent's 20 is worth more than the ball alone.
RED_ZONE_BONUS = 2.2


@dataclass
class LiveState:
    period: int | None = None
    seconds_left: float | None = None      # in the whole game
    possession: str | None = None
    down: int | None = None
    distance: int | None = None
    yard_line: int | None = None
    red_zone: bool = False
    home_score: int = 0
    away_score: int = 0

    @property
    def margin(self) -> float:
        return float(self.home_score - self.away_score)


def seconds_remaining(period: int | None, clock_seconds: float | None) -> float | None:
    """Seconds left in regulation, from a quarter number and its clock."""
    if period is None:
        return None
    if period >= 5:                      # overtime: treat as a short sudden game
        return max(MIN_SECONDS, float(clock_seconds or 0.0))
    quarters_after = max(0, 4 - int(period))
    return max(0.0, quarters_after * QUARTER_SECONDS + float(clock_seconds or 0.0))


def parse_clock(display: str | None) -> float | None:
    """'4:05' -> 245.0 seconds."""
    if not display:
        return None
    text = str(display).strip()
    if ":" not in text:
        try:
            return float(text)
        except ValueError:
            return None
    minutes, _, secs = text.partition(":")
    try:
        return float(int(minutes) * 60 + float(secs))
    except ValueError:
        return None


# Field position, as yards from the possessing team's own goal line: at or past
# this is inside the opponent's twenty.
RED_ZONE_LINE = 80


def in_red_zone(state: LiveState) -> bool:
    """Whether the offence is inside the opponent's twenty.

    Field position wins over the feed's own flag when the two disagree. A
    stale `isRedZone` is a normal feed artifact, and believing it while the
    ball sits on the offence's own 16 applies a two-point swing in the wrong
    direction — which is exactly what it did before this check existed.
    """
    if state.yard_line is not None:
        return float(state.yard_line) >= RED_ZONE_LINE
    return bool(state.red_zone)


def situation_points(state: LiveState, home: str) -> float:
    """Points of edge from possession and field position, in home terms."""
    if not state.possession:
        return 0.0
    value = POSSESSION_POINTS
    if in_red_zone(state):
        value += RED_ZONE_BONUS
    elif state.yard_line is not None:
        # Closer to the opponent's goal is worth more; scale across the field.
        value += clamp((float(state.yard_line) - 50.0) / 50.0, -0.6, 1.2)
    # Fourth and long with the ball is a liability rather than an asset.
    if state.down == 4 and (state.distance or 0) >= 5:
        value -= 1.4
    return value if state.possession == home else -value


def win_probability(
    state: LiveState,
    pregame_margin: float,
    home: str,
    *,
    margin_sd: float = MARGIN_SD,
) -> float:
    """P(home wins) given the scoreboard and the pregame projection."""
    remaining = state.seconds_left
    if remaining is None:
        return normal_cdf(pregame_margin / margin_sd)

    remaining = max(MIN_SECONDS, float(remaining))
    fraction = clamp(remaining / REGULATION_SECONDS, 0.0, 1.0)

    expected = (
        state.margin
        + pregame_margin * fraction
        + situation_points(state, home)
    )
    # Uncertainty shrinks with the square root of time left: scoring accumulates
    # like a random walk, so half a game left is about 70% of the spread, not
    # half of it.
    # Floor the uncertainty: while there is any time at all, one more scoring
    # possession is possible, and letting the spread collapse to zero would
    # report certainties the scoreboard has not earned.
    sd = max(2.0, margin_sd * (fraction**0.5))

    # A tie at the buzzer goes to overtime, which is close to a coin flip.
    if remaining <= MIN_SECONDS and abs(expected) < 0.5:
        return 0.5
    return float(clamp(normal_cdf(expected / sd), 0.001, 0.999))


def describe(state: LiveState, home: str, away: str) -> str:
    """A short human summary of the situation, for the card."""
    parts: list[str] = []
    if state.period:
        label = f"OT{state.period - 4}" if state.period > 4 else f"Q{state.period}"
        parts.append(label)
    if state.down and state.distance is not None:
        ordinal = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th"}.get(int(state.down), "")
        parts.append(f"{ordinal} & {int(state.distance)}")
    if state.possession:
        parts.append(f"{state.possession} ball")
    if state.red_zone:
        parts.append("red zone")
    return " · ".join(parts)
