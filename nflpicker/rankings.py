"""Published power rankings from other people, and what they agree on.

Every source here is somebody's editorial top-32. Individually they are opinion;
averaged they are a reasonable read on how the league is perceived, which is a
different and useful thing from what this app's own rating says. Where the two
disagree sharply is the interesting part — either the consensus is anchored on
reputation, or our rating has found an outlier and is wrong.

**A ranking is stored only if it is complete.** Every fetcher and the paste
importer run through :func:`validate`, which demands exactly 32 distinct teams
ranked 1 to 32 with nothing missing. Editorial pages are hand-built HTML that
changes without notice; a parser that half-works would otherwise store eleven
teams and quietly drag the consensus toward whoever it managed to read. A loud
failure is recoverable, a plausible-looking wrong average is not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import db
from .teams import ABBRS, TEAMS, try_resolve
from .util import now_iso


@dataclass(frozen=True)
class Source:
    key: str
    name: str
    note: str = ""


# These are labels for where a pasted list came from, not fetchers. Nothing here
# goes and gets a ranking: they are articles rather than APIs, the markup changes
# without notice, and a scraper that half-works would store eleven teams and drag
# the consensus toward whichever ones it managed to read. If fetchers are ever
# added they go through validate() like everything else.
SOURCES: tuple[Source, ...] = (
    Source("espn", "ESPN", note="Panel of writers, voted."),
    Source("nfl", "NFL.com", note="Single writer."),
    Source("cbs", "CBS Sports", note="Single writer."),
    Source("fox", "FOX Sports", note="Single writer."),
    Source("br", "Bleacher Report", note="Panel."),
    Source("user", "Imported", note="Pasted in by hand."),
)

SOURCE_NAMES = {s.key: s.name for s in SOURCES}


class RankingError(ValueError):
    """A ranking that cannot be trusted, with the reason a person can act on."""


def validate(ranks: dict[str, int]) -> dict[str, int]:
    """Exactly 32 teams, ranked 1..32, or refuse the lot.

    Partial credit is the failure mode to avoid. A parser that reads nineteen
    teams produces an average that looks like a consensus and is not one.
    """
    if len(ranks) != 32:
        missing = sorted(set(ABBRS) - set(ranks))
        raise RankingError(
            f"got {len(ranks)} teams, need 32"
            + (f" — missing {', '.join(missing[:6])}" if missing else "")
        )
    unknown = sorted(set(ranks) - set(ABBRS))
    if unknown:
        raise RankingError(f"not NFL teams: {', '.join(unknown)}")
    positions = sorted(ranks.values())
    if positions != list(range(1, 33)):
        duplicated = sorted({p for p in positions if positions.count(p) > 1})
        raise RankingError(
            "ranks are not 1-32"
            + (f" — {duplicated} appear more than once" if duplicated else "")
        )
    return dict(ranks)


# "1. Seattle Seahawks", "1 SEA", "1|SEA", "1) Seahawks — ...", "SEA 1"
_LINE = re.compile(
    r"^\s*(?:(?P<rank1>\d{1,2})\s*[.)|:\-—]?\s+(?P<team1>[A-Za-z0-9 .'&/-]+?)"
    r"|(?P<team2>[A-Za-z0-9 .'&/-]+?)\s*[|,]\s*(?P<rank2>\d{1,2}))\s*$"
)


def parse_text(text: str) -> dict[str, int]:
    """Read a pasted list. Forgiving about shape, strict about the result.

    People paste these out of a browser, so the input is whatever the page put
    on the clipboard: numbered lists, tables, trailing commentary. Anything that
    does not resolve to a team is skipped, and validate() then decides whether
    what survived is a ranking or a mess.
    """
    ranks: dict[str, int] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        # Commentary after an em dash or a tab is the usual shape; drop it.
        line = re.split(r"\s+[—–]\s+|\t", line, maxsplit=1)[0].strip()
        match = _LINE.match(line)
        if not match:
            continue
        rank = match.group("rank1") or match.group("rank2")
        name = match.group("team1") or match.group("team2")
        if not rank or not name:
            continue
        team = try_resolve(name.strip())
        if team and team not in ranks:
            ranks[team] = int(rank)
    return validate(ranks)


def store(source: str, season: int, week: int, ranks: dict[str, int]) -> int:
    """Save a validated ranking, replacing any earlier one for that week."""
    ranks = validate(ranks)
    stamp = now_iso()
    db.execute(
        "DELETE FROM external_rankings WHERE source = ? AND season = ? AND week = ?",
        (source, season, week),
    )
    db.executemany(
        "INSERT INTO external_rankings(source, season, week, team, rank, captured_at) "
        "VALUES(?,?,?,?,?,?)",
        [[source, season, week, team, rank, stamp] for team, rank in ranks.items()],
    )
    return len(ranks)


def stored(season: int, weeks: tuple[int, ...] | None = None) -> dict[str, dict]:
    """source -> {week -> {team -> rank}} for what has been collected."""
    sql = "SELECT * FROM external_rankings WHERE season = ?"
    params: list = [season]
    if weeks:
        sql += f" AND week IN ({','.join('?' for _ in weeks)})"
        params += list(weeks)
    out: dict[str, dict] = {}
    for row in db.query(sql, params):
        out.setdefault(row["source"], {}).setdefault(row["week"], {})[row["team"]] = row["rank"]
    return out


def consensus(season: int, weeks: tuple[int, ...] = (1, 2)) -> dict:
    """Average the sources into one ranking, and say how much they disagree.

    Averaging ranks rather than any underlying rating is the only option — these
    sources publish an order and nothing else — and it is also the right one:
    the thing being combined is opinion about ordering.

    Spread matters as much as the average. Every source agreeing on a team at 7
    is a different statement from half of them saying 2 and half saying 14, and
    only the second is worth arguing with.
    """
    by_source = stored(season, weeks)
    per_team: dict[str, list[int]] = {}
    contributing: list[str] = []
    for source, by_week in by_source.items():
        for _week, ranks in by_week.items():
            if len(ranks) != 32:
                continue                         # never let a partial in
            contributing.append(source)
            for team, rank in ranks.items():
                per_team.setdefault(team, []).append(int(rank))

    rows = []
    for team, values in per_team.items():
        mean = sum(values) / len(values)
        spread = max(values) - min(values)
        rows.append({
            "team": team,
            "name": TEAMS[team].full_name if team in TEAMS else team,
            "mean_rank": round(mean, 2),
            "best": min(values), "worst": max(values),
            "spread": spread, "n": len(values),
        })
    # Re-rank the averages so the consensus is itself a 1-32 ordering.
    rows.sort(key=lambda r: (r["mean_rank"], r["team"]))
    for i, row in enumerate(rows, start=1):
        row["consensus_rank"] = i

    return {
        "season": season,
        "weeks": list(weeks),
        "sources": sorted(set(contributing)),
        "source_names": {s: SOURCE_NAMES.get(s, s) for s in sorted(set(contributing))},
        "n_lists": len(contributing),
        "teams": rows,
    }


def compare(season: int, our_order: list[str], weeks: tuple[int, ...] = (1, 2)) -> dict:
    """Our ranking against the consensus, worst disagreements first.

    `our_order` is our teams best-first. The gap is consensus rank minus ours,
    so a positive number means we like a team more than everyone else does.
    """
    con = consensus(season, weeks)
    ours = {team: i for i, team in enumerate(our_order, start=1)}
    if not con["teams"] or not ours:
        return {**con, "comparison": [], "mean_abs_gap": None, "outliers": []}

    rows = []
    for row in con["teams"]:
        mine = ours.get(row["team"])
        if mine is None:
            continue
        gap = row["consensus_rank"] - mine
        rows.append({**row, "our_rank": mine, "gap": gap})

    gaps = [abs(r["gap"]) for r in rows]
    mean_abs = sum(gaps) / len(gaps) if gaps else None
    # An outlier is a disagreement the sources themselves do not have: they are
    # tight on this team and we are a long way off. Disagreeing with a team the
    # sources cannot agree on is not evidence of anything.
    outliers = sorted(
        (r for r in rows if abs(r["gap"]) >= 8 and r["spread"] <= 8),
        key=lambda r: -abs(r["gap"]),
    )
    rows.sort(key=lambda r: -abs(r["gap"]))
    return {
        **con,
        "comparison": rows,
        "mean_abs_gap": round(mean_abs, 2) if mean_abs is not None else None,
        "outliers": outliers,
    }
