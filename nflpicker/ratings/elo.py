"""NFL-tuned Elo with margin-of-victory scaling and season regression.

Elo's job here is to be the leak-free power rating the ML model trains on: it
is computed strictly forward through time, so a game's features never contain
information from that game or any later one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..teams import ABBRS

# 25 Elo points ≈ 1 point of point spread, the long-standing NFL convention.
ELO_PER_POINT = 25.0


@dataclass
class EloConfig:
    k: float = 20.0
    mean: float = 1505.0
    home_field: float = 48.0       # ≈ 1.9 points
    regression: float = 0.33       # fraction reverted to the mean each offseason
    rest_bonus: float = 12.0       # Elo points for a team coming off a bye
    playoff_multiplier: float = 1.2
    qb_adjust: bool = True


@dataclass
class EloRatings:
    config: EloConfig = field(default_factory=EloConfig)
    ratings: dict[str, float] = field(default_factory=dict)
    history: list[dict] = field(default_factory=list)
    _season: int | None = None

    def __post_init__(self) -> None:
        if not self.ratings:
            self.ratings = {abbr: self.config.mean for abbr in ABBRS}

    # ------------------------------------------------------------------ core
    def get(self, team: str) -> float:
        return self.ratings.get(team, self.config.mean)

    def expected(self, rating_a: float, rating_b: float) -> float:
        """Win probability for A given the (already adjusted) rating gap."""
        return 1.0 / (1.0 + 10.0 ** ((rating_b - rating_a) / 400.0))

    def start_season(self, season: int) -> None:
        """Revert toward the mean between seasons: last year matters, but less."""
        if self._season is not None and season != self._season:
            r = self.config.regression
            for team in list(self.ratings):
                self.ratings[team] = self.ratings[team] * (1 - r) + self.config.mean * r
        self._season = season

    def pregame(
        self,
        home: str,
        away: str,
        *,
        neutral_site: bool = False,
        home_rest: float | None = None,
        away_rest: float | None = None,
        home_qb_delta: float = 0.0,
        away_qb_delta: float = 0.0,
    ) -> dict:
        """Adjusted ratings and the resulting projection, before the game."""
        cfg = self.config
        home_adj = self.get(home) + home_qb_delta
        away_adj = self.get(away) + away_qb_delta
        if not neutral_site:
            home_adj += cfg.home_field
        if home_rest is not None and home_rest >= 10:
            home_adj += cfg.rest_bonus
        if away_rest is not None and away_rest >= 10:
            away_adj += cfg.rest_bonus

        diff = home_adj - away_adj
        return {
            "home_elo": self.get(home),
            "away_elo": self.get(away),
            "home_adj": home_adj,
            "away_adj": away_adj,
            "elo_diff": diff,
            "home_win_prob": self.expected(home_adj, away_adj),
            "margin": diff / ELO_PER_POINT,
        }

    def update(
        self,
        home: str,
        away: str,
        home_score: float,
        away_score: float,
        *,
        neutral_site: bool = False,
        home_rest: float | None = None,
        away_rest: float | None = None,
        playoff: bool = False,
        game_id: str | None = None,
        kickoff: str | None = None,
    ) -> dict:
        """Apply one completed game and return the pregame view of it."""
        pre = self.pregame(
            home, away, neutral_site=neutral_site,
            home_rest=home_rest, away_rest=away_rest,
        )
        margin = home_score - away_score
        actual = 1.0 if margin > 0 else (0.0 if margin < 0 else 0.5)
        shift = self.config.k * mov_multiplier(margin, pre["elo_diff"]) * (
            actual - pre["home_win_prob"]
        )
        if playoff:
            shift *= self.config.playoff_multiplier

        self.ratings[home] = self.get(home) + shift
        self.ratings[away] = self.get(away) - shift
        record = {
            **pre,
            "game_id": game_id,
            "kickoff": kickoff,
            "home": home,
            "away": away,
            "margin": margin,
            "shift": shift,
            "home_elo_post": self.ratings[home],
            "away_elo_post": self.ratings[away],
        }
        self.history.append(record)
        return record

    def spread(self, home: str, away: str, *, neutral_site: bool = False) -> float:
        """Projected home margin in points."""
        return self.pregame(home, away, neutral_site=neutral_site)["margin"]

    def as_points(self) -> dict[str, float]:
        """Ratings expressed as points better than an average team."""
        return {t: (r - self.config.mean) / ELO_PER_POINT for t, r in self.ratings.items()}

    def snapshot(self) -> dict[str, float]:
        return dict(self.ratings)


def mov_multiplier(margin: float, elo_diff: float) -> float:
    """Dampen blowouts and correct Elo's autocorrelation bias.

    Without the denominator, a favourite that wins big gains too much and
    ratings run away; this is the standard FiveThirtyEight correction.
    """
    winner_diff = elo_diff if margin > 0 else -elo_diff
    return math.log(abs(margin) + 1.0) * (2.2 / (winner_diff * 0.001 + 2.2))


def run_elo(games, config: EloConfig | None = None) -> EloRatings:
    """Walk a chronologically ordered iterable of completed games.

    Each item needs season/home/away/home_score/away_score; extras are optional.
    """
    elo = EloRatings(config=config or EloConfig())
    for g in games:
        season = int(g.get("season") or 0)
        if season:
            elo.start_season(season)
        home_score, away_score = g.get("home_score"), g.get("away_score")
        if home_score is None or away_score is None:
            continue
        elo.update(
            g["home"], g["away"], float(home_score), float(away_score),
            neutral_site=bool(g.get("neutral_site")),
            home_rest=g.get("home_rest"), away_rest=g.get("away_rest"),
            playoff=str(g.get("season_type", "REG")).upper() == "POST",
            game_id=g.get("game_id"), kickoff=g.get("kickoff"),
        )
    return elo
