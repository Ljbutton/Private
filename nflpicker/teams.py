"""Static NFL team reference data: identity, divisions, venues, aliases.

Every external source spells teams differently (ESPN uses ``WSH``, the odds
feeds use full names, nflverse uses ``LA``).  Everything funnels through
:func:`resolve` so the rest of the codebase only ever sees our canonical
abbreviation.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Team:
    abbr: str
    name: str          # "Bills"
    location: str      # "Buffalo"
    conference: str    # "AFC" / "NFC"
    division: str      # "AFC East"
    lat: float
    lon: float
    roof: str          # "outdoor" | "dome" | "retractable"
    color: str         # primary hex, for the UI

    @property
    def full_name(self) -> str:
        return f"{self.location} {self.name}"


TEAMS: dict[str, Team] = {
    t.abbr: t
    for t in [
        # ---- AFC East ----
        Team("BUF", "Bills", "Buffalo", "AFC", "AFC East", 42.7738, -78.7870, "outdoor", "#00338D"),
        Team("MIA", "Dolphins", "Miami", "AFC", "AFC East", 25.9580, -80.2389, "outdoor", "#008E97"),
        Team("NE", "Patriots", "New England", "AFC", "AFC East", 42.0909, -71.2643, "outdoor", "#002244"),
        Team("NYJ", "Jets", "New York", "AFC", "AFC East", 40.8128, -74.0742, "outdoor", "#125740"),
        # ---- AFC North ----
        Team("BAL", "Ravens", "Baltimore", "AFC", "AFC North", 39.2780, -76.6227, "outdoor", "#241773"),
        Team("CIN", "Bengals", "Cincinnati", "AFC", "AFC North", 39.0955, -84.5161, "outdoor", "#FB4F14"),
        Team("CLE", "Browns", "Cleveland", "AFC", "AFC North", 41.5061, -81.6995, "outdoor", "#311D00"),
        Team("PIT", "Steelers", "Pittsburgh", "AFC", "AFC North", 40.4468, -80.0158, "outdoor", "#FFB612"),
        # ---- AFC South ----
        Team("HOU", "Texans", "Houston", "AFC", "AFC South", 29.6847, -95.4107, "retractable", "#03202F"),
        Team("IND", "Colts", "Indianapolis", "AFC", "AFC South", 39.7601, -86.1639, "retractable", "#002C5F"),
        Team("JAX", "Jaguars", "Jacksonville", "AFC", "AFC South", 30.3239, -81.6373, "outdoor", "#006778"),
        Team("TEN", "Titans", "Tennessee", "AFC", "AFC South", 36.1665, -86.7713, "outdoor", "#4B92DB"),
        # ---- AFC West ----
        Team("DEN", "Broncos", "Denver", "AFC", "AFC West", 39.7439, -105.0201, "outdoor", "#FB4F14"),
        Team("KC", "Chiefs", "Kansas City", "AFC", "AFC West", 39.0489, -94.4839, "outdoor", "#E31837"),
        Team("LAC", "Chargers", "Los Angeles", "AFC", "AFC West", 33.9535, -118.3392, "dome", "#0080C6"),
        Team("LV", "Raiders", "Las Vegas", "AFC", "AFC West", 36.0909, -115.1833, "dome", "#000000"),
        # ---- NFC East ----
        Team("DAL", "Cowboys", "Dallas", "NFC", "NFC East", 32.7473, -97.0945, "retractable", "#003594"),
        Team("NYG", "Giants", "New York", "NFC", "NFC East", 40.8128, -74.0742, "outdoor", "#0B2265"),
        Team("PHI", "Eagles", "Philadelphia", "NFC", "NFC East", 39.9008, -75.1675, "outdoor", "#004C54"),
        Team("WAS", "Commanders", "Washington", "NFC", "NFC East", 38.9076, -76.8645, "outdoor", "#5A1414"),
        # ---- NFC North ----
        Team("CHI", "Bears", "Chicago", "NFC", "NFC North", 41.8623, -87.6167, "outdoor", "#0B162A"),
        Team("DET", "Lions", "Detroit", "NFC", "NFC North", 42.3400, -83.0456, "dome", "#0076B6"),
        Team("GB", "Packers", "Green Bay", "NFC", "NFC North", 44.5013, -88.0622, "outdoor", "#203731"),
        Team("MIN", "Vikings", "Minnesota", "NFC", "NFC North", 44.9736, -93.2575, "dome", "#4F2683"),
        # ---- NFC South ----
        Team("ATL", "Falcons", "Atlanta", "NFC", "NFC South", 33.7554, -84.4008, "retractable", "#A71930"),
        Team("CAR", "Panthers", "Carolina", "NFC", "NFC South", 35.2258, -80.8528, "outdoor", "#0085CA"),
        Team("NO", "Saints", "New Orleans", "NFC", "NFC South", 29.9511, -90.0812, "dome", "#D3BC8D"),
        Team("TB", "Buccaneers", "Tampa Bay", "NFC", "NFC South", 27.9759, -82.5033, "outdoor", "#D50A0A"),
        # ---- NFC West ----
        Team("ARI", "Cardinals", "Arizona", "NFC", "NFC West", 33.5276, -112.2626, "retractable", "#97233F"),
        Team("LAR", "Rams", "Los Angeles", "NFC", "NFC West", 33.9535, -118.3392, "dome", "#003594"),
        Team("SF", "49ers", "San Francisco", "NFC", "NFC West", 37.4030, -121.9698, "outdoor", "#AA0000"),
        Team("SEA", "Seahawks", "Seattle", "NFC", "NFC West", 47.5952, -122.3316, "outdoor", "#002244"),
    ]
}

# ESPN spells three of these differently, and its logo CDN is keyed on its own
# spelling. Only the exceptions are listed; everything else is our abbreviation
# lowercased.
_ESPN_ABBR = {"WAS": "WSH"}


def espn_abbr(abbr: str) -> str:
    """The abbreviation ESPN uses, which is how its team marks are addressed."""
    return _ESPN_ABBR.get(abbr, abbr).lower()


def reference() -> dict[str, dict]:
    """Team identity for the UI: name, colour, and the logo key.

    The dashboard draws a mark and a name for every team on the board, and the
    only place that data lives is here -- shipping it to the browser beats
    keeping a second copy of thirty-two names in JavaScript that can drift.
    """
    return {
        abbr: {
            "abbr": abbr,
            "name": t.name,
            "location": t.location,
            "full_name": t.full_name,
            "color": t.color,
            "conference": t.conference,
            "division": t.division,
            "espn": espn_abbr(abbr),
        }
        for abbr, t in TEAMS.items()
    }


ABBRS: list[str] = sorted(TEAMS)
DIVISIONS: dict[str, list[str]] = {}
for _abbr, _t in TEAMS.items():
    DIVISIONS.setdefault(_t.division, []).append(_abbr)
for _d in DIVISIONS.values():
    _d.sort()

# Spellings seen in the wild, mapped to our canonical abbreviation.
_EXTRA_ALIASES = {
    "ARZ": "ARI", "BLT": "BAL", "CLV": "CLE", "HST": "HOU",
    "JAC": "JAX", "KAN": "KC", "KCC": "KC", "LA": "LAR", "STL": "LAR",
    "SL": "LAR", "LARM": "LAR", "SD": "LAC", "SDG": "LAC", "LACH": "LAC",
    "NWE": "NE", "NEP": "NE", "NOR": "NO", "NOS": "NO", "GNB": "GB",
    "TAM": "TB", "TBB": "TB", "SFO": "SF", "OAK": "LV", "RAI": "LV",
    "LVR": "LV", "WSH": "WAS", "WFT": "WAS", "WAS": "WAS", "NYJ": "NYJ",
    "PHO": "ARI", "RAM": "LAR",
    # Relocations and renames still present in historical data feeds.
    "WASHINGTONFOOTBALLTEAM": "WAS", "WASHINGTONREDSKINS": "WAS", "REDSKINS": "WAS",
    "FOOTBALLTEAM": "WAS", "OAKLANDRAIDERS": "LV", "SANDIEGOCHARGERS": "LAC",
    "STLOUISRAMS": "LAR", "STLOUIS": "LAR",
}

_LOOKUP: dict[str, str] = {}


def _register(key: str, abbr: str) -> None:
    key = "".join(ch for ch in key.upper() if ch.isalnum())
    if key:
        _LOOKUP.setdefault(key, abbr)


for _abbr, _t in TEAMS.items():
    _register(_abbr, _abbr)
    _register(_t.name, _abbr)
    _register(_t.full_name, _abbr)
    _register(_t.location, _abbr)
for _alias, _abbr in _EXTRA_ALIASES.items():
    _register(_alias, _abbr)

# "New York" and "Los Angeles" are ambiguous by themselves; drop those.
for _ambiguous in ("NEWYORK", "LOSANGELES"):
    _LOOKUP.pop(_ambiguous, None)


class UnknownTeam(KeyError):
    """Raised when a team string cannot be mapped to a canonical abbreviation."""


def resolve(value: str | None) -> str:
    """Map any reasonable team spelling to the canonical abbreviation."""
    if not value:
        raise UnknownTeam("empty team value")
    key = "".join(ch for ch in str(value).upper() if ch.isalnum())
    if key in _LOOKUP:
        return _LOOKUP[key]
    # Fall back to a nickname match inside a longer string ("The Buffalo Bills").
    for abbr, team in TEAMS.items():
        nickname = "".join(ch for ch in team.name.upper() if ch.isalnum())
        if nickname and nickname in key:
            return abbr
    raise UnknownTeam(f"unrecognised team: {value!r}")


def try_resolve(value: str | None) -> str | None:
    try:
        return resolve(value)
    except (UnknownTeam, TypeError):
        return None


def get(abbr: str) -> Team:
    return TEAMS[resolve(abbr)]


def same_division(a: str, b: str) -> bool:
    return TEAMS[resolve(a)].division == TEAMS[resolve(b)].division


def distance_miles(a: str, b: str) -> float:
    """Great-circle distance between two teams' home venues."""
    import math

    ta, tb = TEAMS[resolve(a)], TEAMS[resolve(b)]
    lat1, lon1, lat2, lon2 = map(math.radians, (ta.lat, ta.lon, tb.lat, tb.lon))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * 3958.8 * math.asin(min(1.0, math.sqrt(h)))
