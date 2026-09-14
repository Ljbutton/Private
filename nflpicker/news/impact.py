"""Turn a headline into something actionable.

A feed of 200 NFL headlines is noise.  What matters for picks is a small
subset: who is playing quarterback, who just landed on IR, who got suspended.
So each item is classified, attributed to teams, and given an estimated effect
on the point spread, and the dashboard sorts by that rather than by recency.

The line-impact numbers are deliberately coarse.  A starting-QB change is worth
somewhere around two to three points in the market; a backup skill player is
worth almost nothing.  Getting that ordering right is most of the value, and no
amount of precision in the estimate would make it a substitute for the market's
own reaction.

Sign convention: ``line_impact`` is the estimated change to the *affected
team's* projected margin.  Negative means the team is worse off (a starter is
out); positive means better off (a starter is back).
"""

from __future__ import annotations

import re

from ..teams import TEAMS, try_resolve

# Position -> approximate points of spread impact if the player is ruled OUT.
POSITION_IMPACT = {
    "QB": 2.6, "LT": 0.7, "OT": 0.5, "WR": 0.6, "RB": 0.4, "TE": 0.35,
    "EDGE": 0.6, "DE": 0.5, "CB": 0.5, "S": 0.35, "LB": 0.3, "DT": 0.4,
    "OL": 0.4, "G": 0.3, "C": 0.35, "K": 0.2, "P": 0.1,
}

# Negative = the team loses the player; positive = the team gets him back.
# Matched on word boundaries, because "fired" contains "ir" and "sprained"
# contains "rain" — substring matching here produces confident nonsense.
STATUS_MULTIPLIER = {
    "ruled out": -1.0, "out for the season": -1.0, "out": -1.0,
    "injured reserve": -1.0, "ir": -1.0, "suspended": -1.0, "suspension": -1.0,
    "doubtful": -0.75, "questionable": -0.35, "limited": -0.2, "probable": -0.1,
    "activated": 0.8, "activate": 0.8, "returns": 0.8, "return": 0.6,
    "cleared": 0.7, "expected to play": 0.5,
}
# Note: no bare "back" entry — it matches inside "running back" and
# "cornerback", which flipped injury headlines to good news.

# A recovery phrase overrides the injury word sitting next to it: "activated off
# injured reserve" is good news, even though it names injured reserve.
RECOVERY = re.compile(
    r"\b(activat\w*|reinstat\w*|clear\w*|returns?|back (?:in|at|to)|"
    r"designated to return|expected to play)\b",
    re.I,
)

CATEGORY_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("weather", re.compile(
        r"\b(weather|wind chill|high winds?|snow\w*|blizzard|storm\w*|hurricane)\b", re.I)),
    ("qb", re.compile(r"\b(quarterback|qb)\b.*\b(start|starter|named|benched|out|injur)", re.I)),
    ("qb", re.compile(r"\b(start|starter|benched)\b.*\b(quarterback|qb)\b", re.I)),
    ("injury", re.compile(
        r"\b(injur\w*|ir\b|injured reserve|out for|ruled out|doubtful|questionable|"
        r"concussion|acl|mcl|hamstring|ankle|knee|shoulder|strain|sprain|surgery|"
        r"week to week|day to day|activated|designated to return)\b", re.I)),
    ("suspension", re.compile(r"\b(suspend\w*|suspension|banned|reinstat\w*)\b", re.I)),
    ("transaction", re.compile(
        r"\b(sign\w*|trade[ds]?|trading|waive[ds]?|release[ds]?|claim\w*|"
        r"extension|restructur\w*|cut\b)\b", re.I)),
    ("coaching", re.compile(
        r"\b(head coach|coordinator|fired|hired|play.?call\w*|interim)\b", re.I)),
    ("weather", re.compile(
        r"\b(weather|wind\w*|snow\w*|blizzard|rain\w*|storm\w*|hurricane|"
        r"sub.?zero|wind chill)\b", re.I)),
]

# Headlines use short forms far more often than full position names —
# "receiver", "wideout", "corner", "signal-caller" — so the bare forms have to
# be here or the impact estimate silently reads zero for real news.
POSITION_PATTERN = re.compile(
    r"\b(quarterback|signal.?caller|qb|"
    r"running back|halfback|tailback|rb|"
    r"wide receiver|wideout|receiver|wr|"
    r"tight end|te|"
    r"left tackle|offensive tackle|tackle|lt|ot|offensive line\w*|ol|"
    r"guard|center|"
    r"edge rusher|pass rusher|edge|defensive end|de|defensive tackle|dt|"
    r"linebacker|lb|"
    r"cornerback|corner|cb|safety|kicker|punter)\b",
    re.I,
)

POSITION_ALIASES = {
    "quarterback": "QB", "signal caller": "QB", "signal-caller": "QB", "qb": "QB",
    "running back": "RB", "halfback": "RB", "tailback": "RB", "rb": "RB",
    "wide receiver": "WR", "wideout": "WR", "receiver": "WR", "wr": "WR",
    "tight end": "TE", "te": "TE",
    "left tackle": "LT", "lt": "LT", "offensive tackle": "OT", "tackle": "OT",
    "ot": "OT", "offensive line": "OL", "offensive lineman": "OL", "ol": "OL",
    "guard": "G", "center": "C",
    "edge rusher": "EDGE", "pass rusher": "EDGE", "edge": "EDGE",
    "defensive end": "DE", "de": "DE", "defensive tackle": "DT", "dt": "DT",
    "linebacker": "LB", "lb": "LB",
    "cornerback": "CB", "corner": "CB", "cb": "CB", "safety": "S",
    "kicker": "K", "punter": "P",
}

STARTER_HINT = re.compile(
    r"\b(starting|starter|no\.? 1|top|star|pro bowl|all.?pro|franchise)\b", re.I
)
# "reserve" must not match inside "injured reserve" — that collision scored a
# starter landing on IR as though he were a backup, at a third of the impact.
BACKUP_HINT = re.compile(
    r"\b(backup|(?<!injured )reserve|practice squad|rookie|third.?string|"
    r"second.?string)\b",
    re.I,
)


def detect_teams(text: str) -> list[str]:
    """Teams mentioned by city or nickname. Nicknames are unambiguous; cities are not."""
    found: list[str] = []
    lowered = f" {text.lower()} "
    for abbr, team in TEAMS.items():
        nickname = team.name.lower()
        location = team.location.lower()
        if re.search(rf"\b{re.escape(nickname)}\b", lowered) or (
            location not in {"new york", "los angeles"}
            and re.search(rf"\b{re.escape(location)}\b", lowered)
        ):
            if abbr not in found:
                found.append(abbr)
    # An explicit abbreviation in the text is decisive.
    for token in re.findall(r"\b[A-Z]{2,3}\b", text):
        abbr = try_resolve(token)
        if abbr and abbr not in found:
            found.append(abbr)
    return found


def detect_position(text: str) -> str | None:
    match = POSITION_PATTERN.search(text)
    if not match:
        return None
    key = match.group(1).lower().replace("-", " ").replace(".", "")
    return POSITION_ALIASES.get(key)


def detect_status(text: str) -> tuple[str | None, float]:
    """Best-matching availability keyword and its signed multiplier.

    Longest match wins ("ruled out" beats "out"), and a recovery phrase anywhere
    in the headline forces the positive reading.
    """
    recovering = bool(RECOVERY.search(text))
    best: tuple[str | None, float] = (None, 0.0)
    for keyword, multiplier in STATUS_MULTIPLIER.items():
        if not re.search(rf"\b{re.escape(keyword)}\b", text, re.I):
            continue
        if recovering and multiplier < 0:
            continue  # "activated off injured reserve" is good news
        # Longest match wins; magnitude only breaks ties between equal lengths.
        if (len(keyword), abs(multiplier)) > (len(best[0] or ""), abs(best[1])):
            best = (keyword, multiplier)
    return best


def classify(title: str, summary: str = "") -> dict:
    """Category, teams, position, and an estimated spread impact in points."""
    text = f"{title} {summary}".strip()
    category = "general"
    for name, pattern in CATEGORY_PATTERNS:
        if pattern.search(text):
            category = name
            break

    teams = detect_teams(text)
    position = detect_position(text)
    status, multiplier = detect_status(text)

    base = POSITION_IMPACT.get(position or "", 0.0)
    if position == "QB" and category in {"qb", "injury", "suspension"}:
        base = POSITION_IMPACT["QB"]
    if STARTER_HINT.search(text):
        base *= 1.25
    if BACKUP_HINT.search(text):
        base *= 0.35

    line_impact = round(base * multiplier, 2) if multiplier else 0.0
    if category in {"weather", "coaching"}:
        # Real effects, but not ones a position-based estimate can size.
        line_impact = 0.0

    # `impact` is a 0-1 "how much should this be near the top of the feed" score,
    # which is not the same as points of line movement.
    impact = min(1.0, abs(line_impact) / 3.0)
    if category in {"qb", "suspension"}:
        impact = max(impact, 0.6)
    elif category == "injury":
        impact = max(impact, 0.35)
    elif category == "coaching":
        impact = max(impact, 0.3)
    elif category in {"transaction", "weather"}:
        impact = max(impact, 0.2)
    if not teams:
        impact *= 0.5

    return {
        "category": category,
        "teams": teams,
        "position": position,
        "status": status,
        "line_impact": line_impact,
        "impact": round(impact, 3),
    }


def tag_items(items: list[dict]) -> list[dict]:
    """Classify a batch, preserving any tags a source already supplied."""
    out: list[dict] = []
    for item in items:
        tags = classify(item.get("title", ""), item.get("summary", "") or "")
        merged = {**item}
        merged["category"] = item.get("category") or tags["category"]
        existing_teams = item.get("teams") or []
        merged["teams"] = list(dict.fromkeys([*existing_teams, *tags["teams"]]))
        merged["line_impact"] = tags["line_impact"]
        merged["impact"] = max(float(item.get("impact") or 0.0), tags["impact"])
        merged["position"] = tags["position"]
        merged["status"] = tags["status"]
        out.append(merged)
    return sorted(out, key=lambda x: (x["impact"], x.get("published_at") or ""), reverse=True)


def affected_games(items: list[dict], games: list[dict]) -> dict[str, list[dict]]:
    """Map game_id -> the news items touching either team in that game."""
    by_team: dict[str, list[dict]] = {}
    for item in items:
        for team in item.get("teams") or []:
            by_team.setdefault(team, []).append(item)
    out: dict[str, list[dict]] = {}
    for game in games:
        hits: list[dict] = []
        seen: set[str] = set()
        for team in (game.get("home"), game.get("away")):
            for item in by_team.get(team, []):
                if item["id"] not in seen:
                    seen.add(item["id"])
                    hits.append(item)
        if hits:
            out[str(game.get("game_id"))] = sorted(
                hits, key=lambda x: x.get("impact", 0), reverse=True
            )[:6]
    return out
