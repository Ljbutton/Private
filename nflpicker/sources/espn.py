"""ESPN public endpoints: schedule, live scores, standings, injuries, news.

These are undocumented but stable and key-free.  Every parser is defensive:
ESPN omits fields freely (no odds posted yet, no score before kickoff), and a
missing field must degrade one game rather than kill a refresh.
"""

from __future__ import annotations

from typing import Any

from ..teams import try_resolve
from ..util import iso, stable_id
from .base import HttpClient, SourceError

SITE = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
SITE_V2 = "https://site.api.espn.com/apis/v2/sports/football/nfl"
WEB = "https://site.web.api.espn.com/apis/site/v2/sports/football/nfl"

SEASON_TYPES = {1: "PRE", 2: "REG", 3: "POST"}


def _dig(obj: Any, *path, default=None):
    """Walk nested dict/list access without a pile of try/except."""
    cur = obj
    for key in path:
        if cur is None:
            return default
        try:
            cur = cur[key]
        except (KeyError, IndexError, TypeError):
            return default
    return cur if cur is not None else default


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _status(competition: dict, event: dict) -> str:
    state = _dig(competition, "status", "type", "state") or _dig(event, "status", "type", "state")
    completed = _dig(competition, "status", "type", "completed", default=False)
    if completed or state == "post":
        return "final"
    if state == "in":
        return "in_progress"
    return "scheduled"


def parse_scoreboard(payload: dict) -> list[dict]:
    """Turn one scoreboard response into normalised game rows."""
    games: list[dict] = []
    for event in payload.get("events") or []:
        comp = _dig(event, "competitions", 0, default={}) or {}
        competitors = comp.get("competitors") or []
        home = away = None
        for c in competitors:
            side = (c.get("homeAway") or "").lower()
            abbr = try_resolve(
                _dig(c, "team", "abbreviation")
                or _dig(c, "team", "displayName")
                or _dig(c, "team", "name")
            )
            if not abbr:
                continue
            record = {"abbr": abbr, "score": _num(c.get("score"))}
            if side == "home":
                home = record
            elif side == "away":
                away = record
        if not home or not away:
            continue

        season = _dig(event, "season", "year") or _dig(payload, "season", "year")
        season_type = SEASON_TYPES.get(
            _dig(event, "season", "type") or _dig(payload, "season", "type"), "REG"
        )
        week = _dig(event, "week", "number") or _dig(payload, "week", "number") or 0
        roof = _dig(comp, "venue", "indoor")

        game = {
            "game_id": f"espn-{event.get('id')}",
            "espn_id": str(event.get("id") or ""),
            "season": int(season) if season else None,
            "week": int(week),
            "season_type": season_type,
            "kickoff": iso(event.get("date") or comp.get("date")),
            "home": home["abbr"],
            "away": away["abbr"],
            "home_score": home["score"],
            "away_score": away["score"],
            "status": _status(comp, event),
            "neutral_site": bool(comp.get("neutralSite")),
            "roof": "dome" if roof else "outdoor",
            "venue": _dig(comp, "venue", "fullName"),
            "broadcast": _dig(comp, "broadcasts", 0, "names", 0),
        }
        odds = _parse_event_odds(comp, game)
        if odds:
            game["espn_odds"] = odds
        if game["status"] == "in_progress":
            live = _parse_live_state(comp, home["abbr"], away["abbr"])
            if live:
                game["live"] = live
        games.append(game)
    return games


def _parse_live_state(comp: dict, home: str, away: str) -> dict | None:
    """Down, distance, possession and clock for a game in progress.

    Possession comes back as a team *id*, so it is resolved against this
    event's own competitors rather than a global table — ESPN's ids are stable
    but there is no reason to carry a second mapping when the answer is here.
    """
    from ..live import parse_clock, seconds_remaining

    status = comp.get("status") or {}
    period = status.get("period")
    clock = parse_clock(status.get("displayClock")) or _num(status.get("clock"))

    by_id: dict[str, str] = {}
    for competitor in comp.get("competitors") or []:
        team_id = str(_dig(competitor, "team", "id") or "")
        abbr = try_resolve(_dig(competitor, "team", "abbreviation"))
        if team_id and abbr:
            by_id[team_id] = abbr

    situation = comp.get("situation") or {}
    possession = by_id.get(str(situation.get("possession") or ""))

    return {
        "period": int(period) if period else None,
        "clock": status.get("displayClock"),
        "seconds_left": seconds_remaining(int(period) if period else None, clock),
        "possession": possession,
        "down": _int_or_none(situation.get("down")),
        "distance": _int_or_none(situation.get("distance")),
        "yard_line": _int_or_none(situation.get("yardLine")),
        "red_zone": bool(situation.get("isRedZone")),
        "home_timeouts": _int_or_none(situation.get("homeTimeouts")),
        "away_timeouts": _int_or_none(situation.get("awayTimeouts")),
        "last_play": (_dig(situation, "lastPlay", "text") or "")[:300] or None,
        "detail": _dig(status, "type", "detail"),
        "home": home,
        "away": away,
    }


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_event_odds(comp: dict, game: dict) -> dict | None:
    """ESPN posts a single consensus line; useful as a fallback with no API key."""
    entries = comp.get("odds") or []
    if not entries:
        return None
    entry = entries[0]
    spread_home = _num(entry.get("spread"))
    # ESPN's `spread` is already home-relative, but `details` ("KC -3.5") is the
    # field that is always present, so fall back to parsing it.
    if spread_home is None:
        details = (entry.get("details") or "").strip()
        if details and details.upper() not in {"EVEN", "PK"}:
            parts = details.split()
            if len(parts) >= 2:
                line = _num(parts[-1])
                favourite = try_resolve(" ".join(parts[:-1]))
                if line is not None and favourite:
                    spread_home = line if favourite == game["home"] else -line
        elif details:
            spread_home = 0.0
    return {
        "book": (entry.get("provider") or {}).get("name", "espn-consensus"),
        "spread_home": spread_home,
        "total": _num(entry.get("overUnder")),
        "ml_home": _num(_dig(entry, "homeTeamOdds", "moneyLine")),
        "ml_away": _num(_dig(entry, "awayTeamOdds", "moneyLine")),
    }


class EspnSource:
    def __init__(self, client: HttpClient | None = None) -> None:
        self.http = client or HttpClient(cache_ttl=60.0)

    def scoreboard(self, season: int, week: int, season_type: int = 2) -> list[dict]:
        payload = self.http.get_json(
            f"{SITE}/scoreboard",
            {"dates": season, "seasontype": season_type, "week": week},
            cache_ttl=60.0,
        )
        return parse_scoreboard(payload)

    def season_schedule(self, season: int, weeks: int = 18) -> list[dict]:
        """Full regular season.

        Weeks are fetched independently so one bad response costs a single week
        rather than the whole schedule.  But if the first few all fail the
        problem is the connection, not the weeks — retrying the remaining
        fifteen with backoff would burn well over a minute to learn the same
        thing, so give up early and let the caller report it.
        """
        games: list[dict] = []
        consecutive_failures = 0
        for week in range(1, weeks + 1):
            try:
                games.extend(self.scoreboard(season, week, season_type=2))
                consecutive_failures = 0
            except SourceError:
                consecutive_failures += 1
                if consecutive_failures >= 2 and not games:
                    raise SourceError(
                        "ESPN unreachable (first two weeks both failed); "
                        "skipping the rest of the season fetch"
                    ) from None
                continue
        return games

    def standings(self, season: int) -> list[dict]:
        try:
            payload = self.http.get_json(
                f"{SITE_V2}/standings", {"season": season}, cache_ttl=1800.0
            )
        except SourceError:
            return []
        rows: list[dict] = []
        for child in _dig(payload, "children", default=[]) or []:
            for entry in _dig(child, "standings", "entries", default=[]) or []:
                abbr = try_resolve(
                    _dig(entry, "team", "abbreviation") or _dig(entry, "team", "displayName")
                )
                if not abbr:
                    continue
                stats = {s.get("name"): s.get("value") for s in entry.get("stats") or []}
                rows.append(
                    {
                        "team": abbr,
                        "wins": stats.get("wins"),
                        "losses": stats.get("losses"),
                        "ties": stats.get("ties"),
                        "points_for": stats.get("pointsFor"),
                        "points_against": stats.get("pointsAgainst"),
                    }
                )
        return rows

    def injuries(self) -> list[dict]:
        try:
            payload = self.http.get_json(f"{WEB}/injuries", cache_ttl=900.0)
        except SourceError:
            return []
        rows: list[dict] = []
        for group in payload.get("injuries") or []:
            team = try_resolve(group.get("displayName") or group.get("abbreviation"))
            for item in group.get("injuries") or []:
                athlete = item.get("athlete") or {}
                name = athlete.get("displayName") or item.get("displayName")
                if not (team and name):
                    continue
                rows.append(
                    {
                        "team": team,
                        "player": name,
                        "position": _dig(athlete, "position", "abbreviation"),
                        "status": item.get("status") or _dig(item, "type", "description"),
                        "detail": (item.get("longComment") or item.get("shortComment") or "")[:400],
                        "injury": _injury_label(item),
                        "return_date": iso(_dig(item, "details", "returnDate")) or None,
                        "updated_at": iso(item.get("date")),
                    }
                )
        return rows

    def news(self, limit: int = 50) -> list[dict]:
        try:
            payload = self.http.get_json(f"{SITE}/news", {"limit": limit}, cache_ttl=600.0)
        except SourceError:
            return []
        items: list[dict] = []
        for article in payload.get("articles") or []:
            url = _dig(article, "links", "web", "href")
            title = article.get("headline") or ""
            if not title:
                continue
            teams = []
            for cat in article.get("categories") or []:
                abbr = try_resolve(
                    _dig(cat, "team", "abbreviation") or _dig(cat, "team", "description")
                )
                if abbr and abbr not in teams:
                    teams.append(abbr)
            items.append(
                {
                    "id": stable_id("espn", url or title),
                    "source": "ESPN",
                    "title": title,
                    "url": url,
                    "summary": article.get("description") or "",
                    "published_at": iso(article.get("published")),
                    "teams": teams,
                }
            )
        return items


def _injury_label(item: dict) -> str | None:
    """What is actually wrong, in two or three words.

    The feed carries this twice: as structured fields under ``details``, and as
    a paragraph of prose. Prefer the fields -- "Right Hamstring Strain" is a
    column, and "Smith was limited in Wednesday's session and is considered
    day-to-day with a hamstring issue" is not. The prose is still stored, it
    just stops being the only place the injury is recorded.

    Nothing here is guaranteed to be present, so every part is optional and an
    entry with none of them returns None rather than an empty-looking string.
    """
    details = item.get("details")
    if not isinstance(details, dict):
        return None
    parts = [
        str(details.get(key)).strip()
        for key in ("side", "location", "detail")
        if details.get(key) and str(details.get(key)).strip().lower() != "not specified"
    ]
    # "Left Knee Knee" happens when location and detail agree; say it once.
    seen: list[str] = []
    for part in parts:
        if part.lower() not in {p.lower() for p in seen}:
            seen.append(part)
    label = " ".join(seen).strip()
    return label[:60] or None
