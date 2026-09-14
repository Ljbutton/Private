"""Deterministic synthetic season.

Two jobs: let the app boot into a fully populated dashboard before any live
fetch succeeds, and give the test suite a realistic fixture with known ground
truth.  Seeded, so the same season is produced every run.
"""

from __future__ import annotations

import datetime as dt
import math
import random

from ..teams import ABBRS, TEAMS, same_division
from ..util import (
    MARGIN_SD,
    margin_to_win_prob,
    prob_to_american,
    round_half,
    season_week1_thursday,
)

BOOKS = ["draftkings", "fanduel", "betmgm", "caesars", "betrivers", "pointsbetus"]

# Book-level quirks: a constant spread lean and a vig level, so the synthetic
# consensus has the same shape as a real one (books disagree by a half point).
BOOK_BIAS = {
    "draftkings": (0.0, -110), "fanduel": (0.1, -112), "betmgm": (-0.1, -110),
    "caesars": (0.05, -108), "betrivers": (-0.05, -110), "pointsbetus": (0.15, -115),
}


# Teams keep most of their quality year to year. Without this carryover the
# synthetic seasons would be independent, last year's results would carry no
# information about this year, and any model would look far worse than it is.
SEASON_CARRYOVER = 0.72
BASE_SEASON = 2000


def _true_strengths(season: int) -> dict[str, float]:
    """Latent team strength in points vs. an average opponent.

    Generated as an AR(1) chain from a fixed base year, so a team that was good
    last season is probably still good — which is what makes prior-season
    ratings informative, exactly as in the real league.
    """
    rng = random.Random(BASE_SEASON * 7919)
    strengths = {abbr: rng.gauss(0.0, 4.6) for abbr in ABBRS}
    for year in range(BASE_SEASON + 1, season + 1):
        step = random.Random(year * 7919)
        drift = math.sqrt(1.0 - SEASON_CARRYOVER**2) * 4.6
        strengths = {
            abbr: SEASON_CARRYOVER * value + step.gauss(0.0, drift)
            for abbr, value in strengths.items()
        }
    mean = sum(strengths.values()) / len(strengths)
    return {k: round(v - mean, 3) for k, v in strengths.items()}


def _pace(season: int) -> dict[str, float]:
    """Team contribution to the game total, in points above league average."""
    rng = random.Random(season * 104729)
    return {abbr: round(rng.gauss(0.0, 2.2), 3) for abbr in ABBRS}


def _week_matching(available: list[str], pair_quota: dict[tuple[str, str], int],
                   rng: random.Random, budget: int = 20000) -> list[tuple[str, str]] | None:
    """Perfect matching of one week's active teams, honouring per-pair quotas.

    Every team plays in every week it is not on bye, so each week is a *perfect*
    matching with no slack — greedy first-fit is not enough and this backtracks.
    Divisional pairs are preferred while they still owe a meeting, which is what
    makes the synthetic slate look like a real one.
    """
    teams = sorted(available)
    steps = [0]

    def key(a: str, b: str) -> tuple[str, str]:
        return (a, b) if a < b else (b, a)

    def recurse(pool: list[str], acc: list[tuple[str, str]]) -> list[tuple[str, str]] | None:
        if not pool:
            return acc
        steps[0] += 1
        if steps[0] > budget:
            return None
        head, rest = pool[0], pool[1:]
        candidates = [t for t in rest if pair_quota.get(key(head, t), 0) > 0]
        if not candidates:
            return None
        # Prefer opponents we still owe the most meetings (i.e. divisional rivals).
        rng.shuffle(candidates)
        candidates.sort(key=lambda t: -pair_quota[key(head, t)])
        for other in candidates:
            k = key(head, other)
            pair_quota[k] -= 1
            home, away = (head, other) if rng.random() < 0.5 else (other, head)
            found = recurse([t for t in rest if t != other], acc + [(home, away)])
            if found is not None:
                return found
            pair_quota[k] += 1
        return None

    rng.shuffle(teams)
    return recurse(teams, [])


def build_schedule(season: int, rng: random.Random) -> list[tuple[int, str, str]]:
    """A valid 18-week, 272-game schedule: 17 games and one bye per team.

    Not the real NFL formula, but every structural constraint that matters for
    simulation holds: 17 games per team, exactly one bye each, nobody booked
    twice in a week, divisional rivals met home-and-home wherever possible.
    """
    # Weeks 5-8 rest six teams, weeks 9-10 rest four; everyone else plays.
    bye_plan = {5: 6, 6: 6, 7: 6, 8: 6, 9: 4, 10: 4}

    for _ in range(100):
        teams = list(ABBRS)
        rng.shuffle(teams)
        byes: dict[int, set[str]] = {}
        cursor = 0
        for week, count in bye_plan.items():
            byes[week] = set(teams[cursor: cursor + count])
            cursor += count

        # Divisional rivals may meet twice; everyone else at most once.
        pair_quota: dict[tuple[str, str], int] = {}
        for i, a in enumerate(ABBRS):
            for b in ABBRS[i + 1:]:
                pair_quota[(a, b)] = 2 if same_division(a, b) else 1

        schedule: list[tuple[int, str, str]] = []
        for week in range(1, 19):
            active = [t for t in ABBRS if t not in byes.get(week, set())]
            matching = _week_matching(active, pair_quota, rng)
            if matching is None:
                break
            schedule.extend((week, home, away) for home, away in matching)
        else:
            return schedule

    raise RuntimeError("could not build a synthetic schedule; this is a bug")


def _kickoff(season: int, week: int, index: int) -> dt.datetime:
    """Thursday night, Sunday windows, Monday night — roughly the real slate."""
    thursday = season_week1_thursday(season) + dt.timedelta(days=7 * (week - 1))
    if index == 0:
        return dt.datetime.combine(thursday, dt.time(0, 15), tzinfo=dt.timezone.utc)
    if index == 1:
        monday = thursday + dt.timedelta(days=4)
        return dt.datetime.combine(monday, dt.time(0, 15), tzinfo=dt.timezone.utc)
    sunday = thursday + dt.timedelta(days=3)
    slot = [17, 20, 21, 0][min(3, (index - 2) // 5)]
    day = sunday + dt.timedelta(days=1 if slot == 0 else 0)
    return dt.datetime.combine(day, dt.time(slot, 0), tzinfo=dt.timezone.utc)


def generate_season(season: int, through_week: int = 0) -> dict:
    """Build a whole synthetic season.

    ``through_week`` games are played out; later weeks stay scheduled and get
    market lines with a movement history.
    """
    rng = random.Random(season * 31 + 17)
    strengths = _true_strengths(season)
    paces = _pace(season)
    schedule = build_schedule(season, rng)

    games: list[dict] = []
    per_week_index: dict[int, int] = {}
    for week, home, away in schedule:
        idx = per_week_index.get(week, 0)
        per_week_index[week] = idx + 1
        kickoff = _kickoff(season, week, idx)
        hfa = 1.9 if TEAMS[home].roof == "outdoor" else 1.6
        true_margin = strengths[home] - strengths[away] + hfa
        true_total = 43.5 + paces[home] + paces[away]

        game = {
            "game_id": f"demo-{season}-{week:02d}-{away}-{home}",
            "season": season,
            "week": week,
            "season_type": "REG",
            "kickoff": kickoff.isoformat(),
            "home": home,
            "away": away,
            "status": "scheduled",
            "home_score": None,
            "away_score": None,
            "neutral_site": False,
            "roof": TEAMS[home].roof,
            "venue": f"{TEAMS[home].location} Stadium",
            "_true_margin": round(true_margin, 3),
            "_true_total": round(true_total, 3),
        }

        if week <= through_week:
            margin = rng.gauss(true_margin, MARGIN_SD)
            total = max(20.0, rng.gauss(true_total, 10.0))
            home_score = int(round((total + margin) / 2))
            away_score = int(round((total - margin) / 2))
            if home_score == away_score:  # no synthetic ties; nudge the winner
                home_score += 1 if margin >= 0 else -1
            game.update(
                home_score=max(0, home_score),
                away_score=max(0, away_score),
                status="final",
            )
        games.append(game)

    quotes = _generate_quotes(games, rng, through_week)
    return {"season": season, "games": games, "quotes": quotes, "strengths": strengths}


def _generate_quotes(games: list[dict], rng: random.Random, through_week: int) -> list[dict]:
    """Book-by-book lines with a plausible movement history per game."""
    quotes: list[dict] = []
    for game in games:
        if game["week"] <= through_week:
            snapshots = 1  # settled: keep one closing record
        else:
            snapshots = 6
        # Market opinion = truth plus a small persistent error, drifting toward truth.
        market_bias = rng.gauss(0.0, 2.1)
        total_bias = rng.gauss(0.0, 2.4)
        kickoff = dt.datetime.fromisoformat(game["kickoff"])
        for step in range(snapshots):
            ago = dt.timedelta(hours=(snapshots - step) * 18)
            captured = (kickoff - ago).replace(microsecond=0)
            decay = (step + 1) / snapshots
            margin = game["_true_margin"] + market_bias * (1 - 0.6 * decay)
            total = game["_true_total"] + total_bias * (1 - 0.6 * decay)
            for book in BOOKS:
                lean, price = BOOK_BIAS[book]
                spread_home = round_half(-(margin + lean + rng.gauss(0, 0.18)))
                total_pts = round_half(total + lean * 0.5 + rng.gauss(0, 0.25))
                wp = margin_to_win_prob(margin)
                vig = 0.022
                base = {
                    "home": game["home"], "away": game["away"],
                    "kickoff": game["kickoff"], "book": book,
                    "captured_at": captured.isoformat(),
                    "provider_event_id": game["game_id"],
                }
                quotes.append({**base, "market": "spread", "home_point": spread_home,
                               "away_point": -spread_home if spread_home is not None else None,
                               "home_price": price, "away_price": price})
                quotes.append({**base, "market": "total", "home_point": total_pts,
                               "away_point": total_pts,
                               "home_price": price, "away_price": price})
                quotes.append({
                    **base, "market": "moneyline", "home_point": None, "away_point": None,
                    "home_price": prob_to_american(min(0.97, wp + vig)),
                    "away_price": prob_to_american(min(0.97, 1 - wp + vig)),
                })
    return quotes


DEMO_HEADLINES = [
    ("{team} QB listed as questionable with a shoulder strain", "injury", 0.85),
    ("{team} activate starting left tackle off injured reserve", "injury", 0.45),
    ("{team} sign veteran edge rusher to bolster pass rush", "transaction", 0.30),
    ("{team} head coach says starting running back is week to week", "injury", 0.55),
    ("{team} place No. 1 receiver on injured reserve", "injury", 0.80),
    ("{team} name backup quarterback the starter for Sunday", "qb", 0.95),
    ("{team} offensive coordinator to call plays from the booth", "coaching", 0.20),
    ("{team} expect starting cornerback back from concussion protocol", "injury", 0.40),
]


def generate_news(season: int, count: int = 24) -> list[dict]:
    from ..util import now, stable_id

    rng = random.Random(season * 601 + count)
    items: list[dict] = []
    base = now()
    for i in range(count):
        template, category, impact = rng.choice(DEMO_HEADLINES)
        team = rng.choice(ABBRS)
        title = template.format(team=TEAMS[team].full_name)
        published = base - dt.timedelta(hours=rng.random() * 72)
        items.append(
            {
                "id": stable_id("demo-news", season, i, title),
                "source": rng.choice(["ESPN", "ProFootballTalk", "CBS Sports"]),
                "title": title,
                "url": "",
                "summary": "Synthetic demo item — enable live sources for real news.",
                "published_at": published.replace(microsecond=0).isoformat(),
                "teams": [team],
                "category": category,
                "impact": impact,
            }
        )
    return sorted(items, key=lambda x: x["published_at"], reverse=True)
