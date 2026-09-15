"""Who is actually picking these games best: you, the model, or the market.

Straight-up winners only, deliberately. Every source here names a favourite, so
that is the one question all of them can be asked, and the comparison stays
honest without a spread or a price to argue about.

The one rule that makes the table mean anything: **every picker is scored on
the same games**. A source with no opinion on a game is not counted as wrong
there, and it does not get to sit out the hard ones either — the "common"
figures below score only games where every picker had a view, which is the only
comparison where a higher number actually means better.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import db

# Picker keys, fixed so the UI and the totals cannot drift apart.
YOU = "you"
BLIND = "blind"          # the model before it is shown the line
MODEL = "model"          # that same model blended with the line
BOOK = "book"
MARKET = "market"
PICKERS = (YOU, BLIND, MODEL, BOOK, MARKET)

# Not a picker: a marker stored alongside them recording which picks were not
# that picker's own. See picks_for().
INHERITED = "_inherited"

LABELS = {
    YOU: "You",
    BLIND: "Blind model",
    MODEL: "Our blend",
    BOOK: "Sportsbook",
    MARKET: "Prediction markets",
}

# What each source is, in one line, for the places that have room to say it.
DESCRIPTIONS = {
    YOU: "Your own picks, recorded on the board.",
    BLIND: "The model's own view, fitted without ever seeing the line.",
    MODEL: "That same model blended with the market — what the app actually claims.",
    BOOK: "The sportsbook consensus, de-vigged.",
    MARKET: "Kalshi and Polymarket contract prices.",
}


@dataclass
class Tally:
    """One picker's record."""

    correct: int = 0
    wrong: int = 0
    push: int = 0            # a tie: nobody was right, nobody was wrong

    @property
    def n(self) -> int:
        return self.correct + self.wrong

    @property
    def rate(self) -> float | None:
        return self.correct / self.n if self.n else None

    def to_dict(self) -> dict:
        return {
            "correct": self.correct, "wrong": self.wrong, "push": self.push,
            "n": self.n,
            "rate": round(self.rate, 4) if self.rate is not None else None,
        }


@dataclass
class WeekRow:
    season: int
    week: int
    games: int = 0
    tallies: dict[str, Tally] = field(default_factory=lambda: {k: Tally() for k in PICKERS})
    common: dict[str, Tally] = field(default_factory=lambda: {k: Tally() for k in PICKERS})
    # Picks that were not the picker's own, by picker. Today only the model can
    # inherit, but counting it generally keeps the shape honest if that changes.
    inherited: dict[str, int] = field(default_factory=lambda: dict.fromkeys(PICKERS, 0))

    def to_dict(self) -> dict:
        return {
            "season": self.season, "week": self.week, "games": self.games,
            "tallies": {k: v.to_dict() for k, v in self.tallies.items()},
            "common": {k: v.to_dict() for k, v in self.common.items()},
            "inherited": dict(self.inherited),
        }


def _winner(game: dict) -> str | None:
    """Who won, or None for a tie or a game that has not finished."""
    if str(game.get("status") or "").lower() != "final":
        return None
    home, away = game.get("home_score"), game.get("away_score")
    if home is None or away is None or home == away:
        return None
    return game["home"] if home > away else game["away"]


def _favourite(prob: float | None, home: str, away: str) -> str | None:
    """The side a home-win probability points at. Exactly 50% is no opinion."""
    if prob is None:
        return None
    if abs(float(prob) - 0.5) < 1e-9:
        return None
    return home if float(prob) > 0.5 else away


def picks_for(season: int, week: int | None = None) -> dict[str, dict[str, str]]:
    """game_id -> picker -> selection, for every picker that has one."""
    where = "season = ?" + (" AND week = ?" if week else "")
    params = (season, week) if week else (season,)

    out: dict[str, dict[str, str]] = {}

    for row in db.query(
        f"SELECT game_id, selection FROM user_picks WHERE {where} AND contest = 'straight'",
        params,
    ):
        out.setdefault(row["game_id"], {})[YOU] = row["selection"]

    games = {g["game_id"]: g for g in db.query(
        f"SELECT * FROM games WHERE {where}", params)}
    if not games:
        return out

    placeholders = ",".join("?" for _ in games)
    ids = tuple(games)

    # Latest prediction and latest consensus per game.
    #
    # Two picks come out of one prediction row. `margin_home` is the model
    # before it is shown the line and `home_win_prob` is built from the blend
    # afterwards, so scoring them separately is the only way to see whether the
    # blend is adding anything or whether the market is carrying it.
    for row in db.query(
        f"SELECT p.game_id, p.margin_home FROM predictions p JOIN "
        f"(SELECT game_id, MAX(captured_at) m FROM predictions "
        f" WHERE game_id IN ({placeholders}) GROUP BY game_id) x "
        f"ON x.game_id = p.game_id AND x.m = p.captured_at", ids,
    ):
        margin = row["margin_home"]
        if margin is None or abs(float(margin)) < 1e-9:
            continue
        game = games[row["game_id"]]
        out.setdefault(row["game_id"], {})[BLIND] = (
            game["home"] if float(margin) > 0 else game["away"])

    for row in db.query(
        f"SELECT p.game_id, p.home_win_prob FROM predictions p JOIN "
        f"(SELECT game_id, MAX(captured_at) m FROM predictions "
        f" WHERE game_id IN ({placeholders}) GROUP BY game_id) x "
        f"ON x.game_id = p.game_id AND x.m = p.captured_at", ids,
    ):
        game = games[row["game_id"]]
        pick = _favourite(row["home_win_prob"], game["home"], game["away"])
        if pick:
            out.setdefault(row["game_id"], {})[MODEL] = pick

    for row in db.query(
        f"SELECT c.game_id, c.home_win_prob FROM consensus c JOIN "
        f"(SELECT game_id, MAX(captured_at) m FROM consensus "
        f" WHERE game_id IN ({placeholders}) GROUP BY game_id) x "
        f"ON x.game_id = c.game_id AND x.m = c.captured_at", ids,
    ):
        game = games[row["game_id"]]
        pick = _favourite(row["home_win_prob"], game["home"], game["away"])
        if pick:
            out.setdefault(row["game_id"], {})[BOOK] = pick

    from .venues import is_prediction_market

    venue_probs: dict[str, list[float]] = {}
    for row in db.query(
        f"SELECT game_id, book, home_price, away_price FROM odds_snapshots o JOIN "
        f"(SELECT game_id AS g, book AS b, MAX(captured_at) m FROM odds_snapshots "
        f" WHERE game_id IN ({placeholders}) GROUP BY game_id, book) x "
        f"ON x.g = o.game_id AND x.b = o.book AND x.m = o.captured_at "
        f"WHERE o.market = 'moneyline'", ids,
    ):
        if not is_prediction_market(row["book"]):
            continue
        from .market.prediction_markets import venue_probability

        prob = venue_probability(row["home_price"], row["away_price"])
        if prob is not None:
            venue_probs.setdefault(row["game_id"], []).append(prob)

    for game_id, probs in venue_probs.items():
        game = games[game_id]
        pick = _favourite(sum(probs) / len(probs), game["home"], game["away"])
        if pick:
            out.setdefault(game_id, {})[MARKET] = pick

    # A game already played that the model never saw -- anything from before
    # the app was running -- shows the sportsbook's pick rather than sitting
    # blank, so an old week still reads as a full row.
    #
    # Only for finished games, deliberately. This is a failsafe for history,
    # not a policy: from here on the model has its own view of every upcoming
    # game, and lending it the book's pick for one it simply has not made yet
    # would be inventing an opinion rather than filling in a missing one.
    #
    # It is also marked rather than silent. On an inherited game the model and
    # the book agree by construction, so a season of them would show the two
    # tied and mean nothing by it; INHERITED records which, so the table can say
    # how much of the model's record is really its own.
    for game_id, picks in out.items():
        game = games.get(game_id)
        if not game or str(game.get("status") or "").lower() != "final":
            continue
        if MODEL not in picks and BOOK in picks:
            picks[MODEL] = picks[BOOK]
            picks.setdefault(INHERITED, set()).add(MODEL)

    return out


def weekly(season: int) -> list[WeekRow]:
    """One row per week, scored against finished games."""
    games = db.query(
        "SELECT * FROM games WHERE season = ? AND status = 'final' ORDER BY week", (season,))
    if not games:
        return []

    by_pick = picks_for(season)
    rows: dict[int, WeekRow] = {}

    for game in games:
        winner = _winner(game)
        week = int(game["week"])
        row = rows.setdefault(week, WeekRow(season=season, week=week))
        picks = by_pick.get(game["game_id"], {})
        if not picks:
            continue
        row.games += 1

        everyone = all(p in picks for p in PICKERS)
        borrowed = picks.get(INHERITED) or set()
        for picker in PICKERS:
            pick = picks.get(picker)
            if not pick:
                continue
            if picker in borrowed:
                row.inherited[picker] += 1
            if winner is None:
                row.tallies[picker].push += 1
                if everyone:
                    row.common[picker].push += 1
                continue
            hit = pick == winner
            tally = row.tallies[picker]
            tally.correct += int(hit)
            tally.wrong += int(not hit)
            if everyone:
                shared = row.common[picker]
                shared.correct += int(hit)
                shared.wrong += int(not hit)

    return [rows[w] for w in sorted(rows)]


def season_totals(rows: list[WeekRow]) -> dict:
    """Add the weeks up, keeping the all-games and common-games splits apart."""
    totals = {k: Tally() for k in PICKERS}
    common = {k: Tally() for k in PICKERS}
    inherited = dict.fromkeys(PICKERS, 0)
    for row in rows:
        for picker in PICKERS:
            for source, target in ((row.tallies, totals), (row.common, common)):
                target[picker].correct += source[picker].correct
                target[picker].wrong += source[picker].wrong
                target[picker].push += source[picker].push
            inherited[picker] += row.inherited.get(picker, 0)
    return {
        "all": {k: v.to_dict() for k, v in totals.items()},
        "common": {k: v.to_dict() for k, v in common.items()},
        "inherited": inherited,
    }


def by_team(season: int) -> list[dict]:
    """Per team, how often each picker called that team's games correctly.

    "That team's games" means any game it played, not games where the picker
    took that side. The question this answers is which teams are being read
    wrongly — a team every picker keeps missing is either genuinely volatile or
    priced on a reputation nobody has updated.
    """
    games = db.query(
        "SELECT * FROM games WHERE season = ? AND status = 'final' ORDER BY week", (season,))
    if not games:
        return []

    by_pick = picks_for(season)
    tallies: dict[str, dict[str, Tally]] = {}

    for game in games:
        winner = _winner(game)
        picks = by_pick.get(game["game_id"], {})
        if not picks:
            continue
        for team in (game["home"], game["away"]):
            row = tallies.setdefault(team, {k: Tally() for k in PICKERS})
            for picker in PICKERS:
                pick = picks.get(picker)
                if not pick:
                    continue
                if winner is None:
                    row[picker].push += 1
                    continue
                row[picker].correct += int(pick == winner)
                row[picker].wrong += int(pick != winner)

    out = []
    for team in sorted(tallies):
        row = tallies[team]
        played = max((t.n + t.push) for t in row.values())
        out.append({
            "team": team,
            "games": played,
            "tallies": {k: v.to_dict() for k, v in row.items()},
        })
    # Ordered by how well the model reads each team, best first. Teams it has
    # no opinion on sort last rather than to one end of the scale, where a
    # missing rate would otherwise read as a score of zero.
    out.sort(key=lambda r: (r["tallies"][MODEL]["rate"] is None,
                            -(r["tallies"][MODEL]["rate"] or 0.0),
                            r["team"]))
    return out


# Why a source has no record at all. A bare dash is indistinguishable from a
# broken fetch, and every one of these has a different answer -- one is a
# missing key, one is a limit of what can ever be bought, one is just that you
# have not picked anything yet.
def coverage(season: int) -> dict[str, dict]:
    """For each source, how many of the season's finished games it had a view
    on, and — when that is none — why not."""
    finals = db.query(
        "SELECT game_id FROM games WHERE season = ? AND status = 'final'", (season,))
    total = len(finals)
    picks = picks_for(season)
    counts = dict.fromkeys(PICKERS, 0)
    for game_id in (g["game_id"] for g in finals):
        for picker in PICKERS:
            if picks.get(game_id, {}).get(picker):
                counts[picker] += 1

    reasons = {
        YOU: "You have not recorded a pick on a finished game yet — click the "
             "circle beside a team on the board.",
        BLIND: "No stored projection covers these games. The model only writes "
               "a prediction for games it saw before kickoff.",
        MODEL: "No stored projection covers these games, and no sportsbook "
               "number to stand in for one.",
        BOOK: "No sportsbook odds are stored for these games. Odds are a "
              "snapshot of what was on offer at a moment and cannot be "
              "backfilled — a week that finished before this app was running "
              "(or before an Odds API key was configured) has none and never "
              "will. Backfilling a season brings in schedules and scores only.",
        MARKET: "No prediction-market prices are stored for these games. Same "
                "limit as the sportsbook odds: a contract price exists while "
                "the contract is open, and nobody sells the past.",
    }
    return {
        picker: {
            "picked": counts[picker],
            "games": total,
            "note": reasons[picker] if total and not counts[picker] else None,
        }
        for picker in PICKERS
    }


def report(season: int) -> dict:
    rows = weekly(season)
    return {
        "season": season,
        "labels": LABELS,
        "descriptions": DESCRIPTIONS,
        "pickers": list(PICKERS),
        "weeks": [r.to_dict() for r in rows],
        "totals": season_totals(rows),
        "teams": by_team(season),
        "coverage": coverage(season),
    }
