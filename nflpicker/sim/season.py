"""Monte Carlo simulation of the rest of the season.

Expected wins is not "current pace extrapolated" — it is the mean of tens of
thousands of simulated seasons where every remaining game is replayed from its
projected margin.  That is also what makes playoff odds and season win-total
over/under edges fall out for free, since they are just different questions
asked of the same simulated distribution.

Tiebreakers are simplified: the NFL's real procedure runs to a dozen steps, and
implementing it fully would add a lot of code to move playoff odds by well under
a percentage point.  Ties in simulated records are broken at random, which is
unbiased across many simulations.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..ratings.power import PowerRatings
from ..teams import ABBRS, DIVISIONS, TEAMS
from ..util import MARGIN_SD

CONFERENCES = {"AFC": [t for t in ABBRS if TEAMS[t].conference == "AFC"],
               "NFC": [t for t in ABBRS if TEAMS[t].conference == "NFC"]}


@dataclass
class TeamSeason:
    team: str
    wins_actual: float = 0.0
    losses_actual: float = 0.0
    ties_actual: float = 0.0
    exp_wins: float = 0.0
    wins_p10: float = 0.0
    wins_p50: float = 0.0
    wins_p90: float = 0.0
    playoff_prob: float = 0.0
    division_prob: float = 0.0
    bye_prob: float = 0.0
    conference_prob: float = 0.0
    sb_prob: float = 0.0
    distribution: dict[int, float] = field(default_factory=dict)
    games_remaining: int = 0

    def to_row(self) -> dict:
        import json

        return {
            "team": self.team,
            "wins_actual": self.wins_actual,
            "losses_actual": self.losses_actual,
            "ties_actual": self.ties_actual,
            "exp_wins": self.exp_wins,
            "wins_p10": self.wins_p10,
            "wins_p90": self.wins_p90,
            "playoff_prob": self.playoff_prob,
            "division_prob": self.division_prob,
            "bye_prob": self.bye_prob,
            "sb_prob": self.sb_prob,
            "distribution": json.dumps({str(k): v for k, v in self.distribution.items()}),
        }


@dataclass
class SeasonSimulation:
    season: int
    n_sims: int
    teams: dict[str, TeamSeason] = field(default_factory=dict)

    def ranked(self) -> list[TeamSeason]:
        return sorted(self.teams.values(), key=lambda t: t.exp_wins, reverse=True)


def simulate_season(
    season: int,
    games: list[dict],
    power: PowerRatings,
    *,
    n_sims: int = 20000,
    margin_sd: float = MARGIN_SD,
    game_margins: dict[str, float] | None = None,
    seed: int = 12345,
) -> SeasonSimulation:
    """Replay every unplayed game ``n_sims`` times.

    ``game_margins`` lets the caller override the projected margin per game —
    the pipeline passes in model projections so the simulation uses the same
    numbers shown on the game cards rather than power ratings alone.
    """
    rng = np.random.default_rng(seed)
    index = {abbr: i for i, abbr in enumerate(ABBRS)}
    n_teams = len(ABBRS)

    wins_base = np.zeros(n_teams)
    losses_base = np.zeros(n_teams)
    ties_base = np.zeros(n_teams)
    remaining: list[tuple[int, int, float]] = []
    games_remaining = np.zeros(n_teams, dtype=int)

    for g in games:
        home, away = g.get("home"), g.get("away")
        if home not in index or away not in index:
            continue
        if str(g.get("season_type", "REG")).upper() != "REG":
            continue
        h, a = index[home], index[away]
        hs, as_ = g.get("home_score"), g.get("away_score")
        if g.get("status") == "final" and hs is not None and as_ is not None:
            if hs > as_:
                wins_base[h] += 1
                losses_base[a] += 1
            elif as_ > hs:
                wins_base[a] += 1
                losses_base[h] += 1
            else:
                ties_base[h] += 1
                ties_base[a] += 1
        else:
            margin = (game_margins or {}).get(str(g.get("game_id")))
            if margin is None:
                margin = power.margin(home, away, neutral_site=bool(g.get("neutral_site")))
            remaining.append((h, a, float(margin)))
            games_remaining[h] += 1
            games_remaining[a] += 1

    wins = np.tile(wins_base + 0.5 * ties_base, (n_sims, 1))

    if remaining:
        home_idx = np.array([r[0] for r in remaining])
        away_idx = np.array([r[1] for r in remaining])
        margins = np.array([r[2] for r in remaining])
        draws = rng.normal(margins, margin_sd, size=(n_sims, len(remaining)))
        home_won = (draws > 0).astype(np.float64)
        np.add.at(wins.T, home_idx, home_won.T)
        np.add.at(wins.T, away_idx, (1.0 - home_won).T)

    # Random tiebreak: a tiny jitter makes argsort order equal records at random.
    jitter = rng.random((n_sims, n_teams)) * 1e-3
    standings = wins + jitter

    division_winner, playoff, bye, seeds_by_conf = _seed_playoffs(standings, index)
    conference, champion = _simulate_bracket(seeds_by_conf, power, rng, margin_sd)

    sim = SeasonSimulation(season=season, n_sims=n_sims)
    for team, i in index.items():
        column = wins[:, i]
        counts = np.bincount(np.rint(column).astype(int), minlength=19)[:19]
        sim.teams[team] = TeamSeason(
            team=team,
            wins_actual=float(wins_base[i]),
            losses_actual=float(losses_base[i]),
            ties_actual=float(ties_base[i]),
            exp_wins=float(column.mean()),
            wins_p10=float(np.percentile(column, 10)),
            wins_p50=float(np.percentile(column, 50)),
            wins_p90=float(np.percentile(column, 90)),
            playoff_prob=float(playoff[:, i].mean()),
            division_prob=float(division_winner[:, i].mean()),
            bye_prob=float(bye[:, i].mean()),
            conference_prob=float(conference[:, i].mean()),
            sb_prob=float(champion[:, i].mean()),
            distribution={w: float(c) / n_sims for w, c in enumerate(counts) if c},
            games_remaining=int(games_remaining[i]),
        )
    return sim


def _seed_playoffs(standings: np.ndarray, index: dict[str, int]):
    """Division winners, the seven seeds per conference, and first-round byes."""
    n_sims, n_teams = standings.shape
    division_winner = np.zeros((n_sims, n_teams), dtype=bool)
    playoff = np.zeros((n_sims, n_teams), dtype=bool)
    bye = np.zeros((n_sims, n_teams), dtype=bool)
    seeds_by_conf: dict[str, np.ndarray] = {}

    winners_by_conf: dict[str, list[np.ndarray]] = {"AFC": [], "NFC": []}
    for division, members in DIVISIONS.items():
        cols = np.array([index[m] for m in members])
        best = cols[np.argmax(standings[:, cols], axis=1)]
        division_winner[np.arange(n_sims), best] = True
        winners_by_conf[division.split()[0]].append(best)

    for conf, members in CONFERENCES.items():
        cols = np.array([index[m] for m in members])
        conf_standings = standings[:, cols]
        winners = np.stack(winners_by_conf[conf], axis=1)          # (n_sims, 4)

        # Seeds 1-4: division winners ordered by record.
        winner_records = np.take_along_axis(
            standings, winners, axis=1
        )
        order = np.argsort(-winner_records, axis=1)
        top4 = np.take_along_axis(winners, order, axis=1)

        # Seeds 5-7: best remaining records in the conference.
        is_winner = np.zeros_like(conf_standings, dtype=bool)
        for k in range(winners.shape[1]):
            match = conf_standings == np.take_along_axis(standings, winners[:, [k]], axis=1)
            is_winner |= match
        masked = np.where(is_winner, -np.inf, conf_standings)
        wildcard_local = np.argsort(-masked, axis=1)[:, :3]
        wildcards = cols[wildcard_local]

        seeds = np.concatenate([top4, wildcards], axis=1)          # (n_sims, 7)
        seeds_by_conf[conf] = seeds
        rows = np.arange(n_sims)[:, None]
        playoff[rows, seeds] = True
        bye[np.arange(n_sims), seeds[:, 0]] = True

    return division_winner, playoff, bye, seeds_by_conf


def _simulate_bracket(seeds_by_conf: dict[str, np.ndarray], power: PowerRatings,
                      rng: np.random.Generator, margin_sd: float):
    """Play out the 14-team bracket: wild card, divisional, championship, Super Bowl.

    The higher seed hosts every round, so home-field advantage is applied there;
    the Super Bowl is neutral.
    """
    n_sims = next(iter(seeds_by_conf.values())).shape[0]
    n_teams = len(ABBRS)
    power_points = np.array([power.get(t).power for t in ABBRS])
    home_edge = np.array([1.8 for _ in ABBRS])

    def play(higher: np.ndarray, lower: np.ndarray, neutral: bool = False) -> np.ndarray:
        """Return the winners; the first argument hosts unless neutral."""
        margin = power_points[higher] - power_points[lower]
        if not neutral:
            margin = margin + home_edge[higher]
        draw = rng.normal(margin, margin_sd)
        return np.where(draw > 0, higher, lower)

    conference = np.zeros((n_sims, n_teams), dtype=bool)
    champion = np.zeros((n_sims, n_teams), dtype=bool)
    finalists: list[np.ndarray] = []

    for _conf, seeds in seeds_by_conf.items():
        s1, s2, s3, s4, s5, s6, s7 = (seeds[:, i] for i in range(7))
        w27 = play(s2, s7)
        w36 = play(s3, s6)
        w45 = play(s4, s5)

        # The top remaining seed plays the lowest remaining seed.
        survivors = np.stack([w27, w36, w45], axis=1)
        seed_rank = np.zeros((n_sims, 3), dtype=int)
        for col, (hi, hi_seed, lo_seed) in enumerate(
            [(s2, 2, 7), (s3, 3, 6), (s4, 4, 5)]
        ):
            seed_rank[:, col] = np.where(survivors[:, col] == hi, hi_seed, lo_seed)
        order = np.argsort(seed_rank, axis=1)
        ordered = np.take_along_axis(survivors, order, axis=1)
        best, middle, worst = ordered[:, 0], ordered[:, 1], ordered[:, 2]

        d1 = play(s1, worst)
        d2 = play(best, middle)
        conf_winner = play(
            np.where(_seed_of(seeds, d1) <= _seed_of(seeds, d2), d1, d2),
            np.where(_seed_of(seeds, d1) <= _seed_of(seeds, d2), d2, d1),
        )
        conference[np.arange(n_sims), conf_winner] = True
        finalists.append(conf_winner)

    winner = play(finalists[0], finalists[1], neutral=True)
    champion[np.arange(n_sims), winner] = True
    return conference, champion


def _seed_of(seeds: np.ndarray, team: np.ndarray) -> np.ndarray:
    """Seed number (1-7) of ``team`` within each simulation's bracket."""
    match = seeds == team[:, None]
    return np.argmax(match, axis=1) + 1
