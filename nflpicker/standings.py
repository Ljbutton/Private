"""Where the season actually stands: records, seeds, and what is clinched.

The season simulator already seeds a playoff field twenty thousand times over
(see :mod:`nflpicker.sim.season`), but every one of those is a hypothetical.
This answers the other question -- what is true right now, from games that have
been played -- which is what a bracket has to be drawn from.

**On tiebreakers.** The NFL's are long: head to head, then division record,
then common games, then conference record, then strength of victory, and four
more after that. The first four are implemented here and they settle the
overwhelming majority of real ties; past them this falls back to win
percentage and says so rather than inventing an order. A seeding shown to one
decimal place that is wrong in the eighth tiebreaker is worse than one that
admits where it stops.

**On clinching.** The official process asks whether a team holds its place
under every remaining combination of results. That is what is computed here,
with one deliberate simplification: a rival who could *tie* a team is treated
as a rival who could pass them. Ties are then settled by tiebreakers that
depend on games not yet played, so claiming the tie in advance would be a
guess. The effect is that a clinch is announced a little late rather than a
little early, which is the right direction for a mark that says "this is
settled".
"""

from __future__ import annotations

from .teams import DIVISIONS, TEAMS

CONFERENCES = ("AFC", "NFC")
GAMES_IN_SEASON = 17
# Seeds per conference: four division winners and three wild cards.
PLAYOFF_SPOTS = 7


class Record:
    """One team's season so far, and who it has played."""

    __slots__ = ("team", "wins", "losses", "ties", "beaten", "lost_to",
                 "div_w", "div_l", "div_t", "conf_w", "conf_l", "conf_t",
                 "played", "points_for", "points_against")

    def __init__(self, team: str) -> None:
        self.team = team
        self.wins = self.losses = self.ties = 0
        self.div_w = self.div_l = self.div_t = 0
        self.conf_w = self.conf_l = self.conf_t = 0
        self.played = 0
        self.points_for = self.points_against = 0
        self.beaten: list[str] = []
        self.lost_to: list[str] = []

    @property
    def win_pct(self) -> float:
        if not self.played:
            return 0.0
        return (self.wins + 0.5 * self.ties) / self.played

    @property
    def div_pct(self) -> float:
        n = self.div_w + self.div_l + self.div_t
        return (self.div_w + 0.5 * self.div_t) / n if n else 0.0

    @property
    def conf_pct(self) -> float:
        n = self.conf_w + self.conf_l + self.conf_t
        return (self.conf_w + 0.5 * self.conf_t) / n if n else 0.0

    @property
    def games_left(self) -> int:
        return max(0, GAMES_IN_SEASON - self.played)

    @property
    def max_wins(self) -> float:
        """Their win total if they win out. Ties already banked count a half."""
        return self.wins + 0.5 * self.ties + self.games_left

    @property
    def banked_wins(self) -> float:
        """Their win total if they lose out."""
        return self.wins + 0.5 * self.ties

    def label(self) -> str:
        return (f"{self.wins}-{self.losses}"
                + (f"-{self.ties}" if self.ties else ""))

    def to_dict(self) -> dict:
        return {
            "team": self.team,
            "wins": self.wins, "losses": self.losses, "ties": self.ties,
            "record": self.label(),
            "win_pct": round(self.win_pct, 4),
            "division_record": f"{self.div_w}-{self.div_l}"
                               + (f"-{self.div_t}" if self.div_t else ""),
            "conference_record": f"{self.conf_w}-{self.conf_l}"
                                 + (f"-{self.conf_t}" if self.conf_t else ""),
            "points_for": self.points_for,
            "points_against": self.points_against,
            "games_left": self.games_left,
        }


def build_records(games: list[dict]) -> dict[str, Record]:
    """Walk the finished games once and total everything the tiebreaks need."""
    records = {team: Record(team) for team in TEAMS}
    for game in games:
        if game.get("status") != "final":
            continue
        home, away = game.get("home"), game.get("away")
        hs, as_ = game.get("home_score"), game.get("away_score")
        if home not in records or away not in records:
            continue
        if hs is None or as_ is None:
            continue
        hs, as_ = int(hs), int(as_)
        h, a = records[home], records[away]
        same_division = TEAMS[home].division == TEAMS[away].division
        same_conference = TEAMS[home].conference == TEAMS[away].conference
        for rec, pf, pa in ((h, hs, as_), (a, as_, hs)):
            rec.played += 1
            rec.points_for += pf
            rec.points_against += pa
        if hs == as_:
            h.ties += 1
            a.ties += 1
            if same_division:
                h.div_t += 1
                a.div_t += 1
            if same_conference:
                h.conf_t += 1
                a.conf_t += 1
            continue
        winner, loser = (h, a) if hs > as_ else (a, h)
        winner.wins += 1
        loser.losses += 1
        winner.beaten.append(loser.team)
        loser.lost_to.append(winner.team)
        if same_division:
            winner.div_w += 1
            loser.div_l += 1
        if same_conference:
            winner.conf_w += 1
            loser.conf_l += 1
    return records


def _head_to_head(a: Record, b: Record) -> float:
    """a's win share in the games between these two, or 0.5 if they are even
    or have not met. Only meaningful between exactly two teams; the league's
    rules for three-way ties are different and are not claimed here."""
    wins = a.beaten.count(b.team)
    losses = b.beaten.count(a.team)
    if wins == losses:
        return 0.5
    return 1.0 if wins > losses else 0.0


def _rank_key(rec: Record, rivals: list[Record]) -> tuple:
    """Sort key, best first. Negated so a plain ascending sort works."""
    # Head to head only enters as a pairwise comparison, which a sort key
    # cannot express; it is applied afterwards, between adjacent pairs.
    del rivals
    return (-rec.win_pct, -rec.div_pct, -rec.conf_pct,
            -(rec.points_for - rec.points_against), rec.team)


def order_teams(records: list[Record]) -> list[Record]:
    """Best first, with head-to-head applied to adjacent ties."""
    ordered = sorted(records, key=lambda r: _rank_key(r, records))
    # One pass of adjacent swaps for pairs that are level on everything the
    # key covers: at that point the two teams' own result against each other
    # is the rule, and it is the only tiebreak a sort key cannot carry.
    for i in range(len(ordered) - 1):
        a, b = ordered[i], ordered[i + 1]
        if _rank_key(a, ordered)[:-1] == _rank_key(b, ordered)[:-1]:
            if _head_to_head(b, a) > 0.5:
                ordered[i], ordered[i + 1] = b, a
    return ordered


def seed_conference(conf: str, records: dict[str, Record]) -> list[dict]:
    """The conference in seed order: four division winners, then the rest."""
    divisions = [d for d in DIVISIONS if d.startswith(conf)]
    winners = []
    for division in divisions:
        members = [records[t] for t in DIVISIONS[division] if t in records]
        if members:
            winners.append(order_teams(members)[0])
    winner_names = {w.team for w in winners}
    others = [r for t, r in records.items()
              if TEAMS[t].conference == conf and t not in winner_names]

    seeded = order_teams(winners) + order_teams(others)
    out = []
    for i, rec in enumerate(seeded, start=1):
        row = rec.to_dict()
        row["seed"] = i
        row["division"] = TEAMS[rec.team].division
        row["division_winner"] = rec.team in winner_names
        out.append(row)
    return out


def clinch_marks(conf_rows: list[dict],
                 records: dict[str, Record]) -> dict[str, str]:
    """What each team has settled, in the league's own letters.

    ``z`` the conference's top seed, ``y`` the division, ``x`` a place in the
    field, ``e`` eliminated from it. One letter per team, the strongest that
    applies, which is how a standings table prints it.
    """
    marks: dict[str, str] = {}
    conf_recs = [records[r["team"]] for r in conf_rows]

    for row in conf_rows:
        rec = records[row["team"]]
        rivals = [r for r in conf_recs if r.team != rec.team]

        # Could still finish level with or above them, if everything falls the
        # rival's way and nothing falls theirs.
        could_pass = [r for r in rivals if r.max_wins >= rec.banked_wins]
        div_rivals = [r for r in could_pass
                      if TEAMS[r.team].division == TEAMS[rec.team].division]

        if not could_pass:
            marks[rec.team] = "z"
        elif not div_rivals:
            marks[rec.team] = "y"
        elif len(could_pass) < PLAYOFF_SPOTS:
            marks[rec.team] = "x"
        else:
            # Eliminated: even winning out leaves seven rivals they cannot
            # catch. The same conservatism applies in reverse -- a rival they
            # could only draw level with does not count against them.
            above = sum(1 for r in rivals if r.banked_wins > rec.max_wins)
            if above >= PLAYOFF_SPOTS:
                marks[rec.team] = "e"
    return marks


CLINCH_MEANING = {
    "z": "clinched the conference's top seed",
    "y": "clinched the division",
    "x": "clinched a playoff place",
    "e": "eliminated from playoff contention",
}


def picture(games: list[dict]) -> dict:
    """The whole playoff picture: both conferences, seeded and marked."""
    records = build_records(games)
    out: dict = {"conferences": {}, "legend": CLINCH_MEANING}
    for conf in CONFERENCES:
        rows = seed_conference(conf, records)
        marks = clinch_marks(rows, records)
        for row in rows:
            row["clinch"] = marks.get(row["team"], "")
        out["conferences"][conf] = rows
    return out


# Which round is which, by how many games it holds rather than by its number.
#
# The feed numbers postseason weeks 1..5 with the Pro Bowl in the middle, and
# that numbering has changed before. How many games a round has is a fact
# about the format, not about anyone's numbering: six wild-card games, four
# divisional, two conference, one final. Read that way the rounds label
# themselves, and a feed that renumbers them is not a bug to chase.
ROUND_BY_SIZE = {6: "Wild Card", 4: "Divisional", 2: "Conference",
                 1: "Super Bowl"}
ROUND_ORDER = ["Wild Card", "Divisional", "Conference", "Super Bowl"]


def label_rounds(post_games: list[dict]) -> dict[int, str]:
    """Postseason week number -> round name."""
    by_week: dict[int, int] = {}
    for game in post_games:
        by_week[int(game["week"])] = by_week.get(int(game["week"]), 0) + 1
    named: dict[int, str] = {}
    for week in sorted(by_week):
        name = ROUND_BY_SIZE.get(by_week[week])
        if name and name not in named.values():
            named[week] = name
    # Anything left over -- an odd count, a round split across two feed weeks
    # -- takes the next unused name in order rather than going unlabelled.
    spare = [n for n in ROUND_ORDER if n not in named.values()]
    for week in sorted(by_week):
        if week not in named and spare:
            named[week] = spare.pop(0)
    return named


def bracket(post_games: list[dict], seeds: dict[str, list[dict]]) -> list[dict]:
    """The postseason as rounds of games, with each side's seed attached."""
    seed_of = {row["team"]: row["seed"]
               for rows in seeds.values() for row in rows}
    rounds = label_rounds(post_games)
    out: dict[str, list[dict]] = {}
    for game in post_games:
        name = rounds.get(int(game["week"]))
        if not name:
            continue
        home, away = game.get("home"), game.get("away")
        hs, as_ = game.get("home_score"), game.get("away_score")
        final = game.get("status") == "final" and hs is not None
        out.setdefault(name, []).append({
            "game_id": game.get("game_id"),
            "week": game["week"],
            "kickoff": game.get("kickoff"),
            "status": game.get("status"),
            "home": home, "away": away,
            "home_seed": seed_of.get(home), "away_seed": seed_of.get(away),
            "home_score": hs, "away_score": as_,
            "conference": (TEAMS[home].conference
                           if home in TEAMS and away in TEAMS
                           and TEAMS[home].conference == TEAMS[away].conference
                           else None),
            "winner": (None if not final else
                       (home if int(hs) > int(as_) else
                        away if int(as_) > int(hs) else None)),
        })
    return [{"round": name, "games": sorted(out[name],
                                            key=lambda g: (g["kickoff"] or ""))}
            for name in ROUND_ORDER if name in out]
