"""Game-time weather from Open-Meteo (free, no API key).

Only matters for outdoor stadiums, and mostly for the total: wind above roughly
15 mph is the single biggest weather effect on NFL scoring.
"""

from __future__ import annotations

from ..teams import TEAMS
from ..util import to_utc
from .base import HttpClient, SourceError

API = "https://api.open-meteo.com/v1/forecast"


class WeatherSource:
    def __init__(self, client: HttpClient | None = None) -> None:
        self.http = client or HttpClient(cache_ttl=3600.0)

    def for_game(self, home_team: str, kickoff: str | None) -> dict | None:
        team = TEAMS.get(home_team)
        when = to_utc(kickoff)
        if not team or not when:
            return None
        if team.roof in {"dome", "retractable"}:
            # Retractable roofs are usually closed in bad weather; treat as indoor.
            return {"roof": team.roof, "temp_f": 70.0, "wind_mph": 0.0, "precip_pct": 0.0,
                    "indoor": True}
        date = when.date().isoformat()
        try:
            payload = self.http.get_json(
                API,
                {
                    "latitude": round(team.lat, 3),
                    "longitude": round(team.lon, 3),
                    "hourly": "temperature_2m,wind_speed_10m,precipitation_probability",
                    "temperature_unit": "fahrenheit",
                    "wind_speed_unit": "mph",
                    "timezone": "UTC",
                    "start_date": date,
                    "end_date": date,
                },
                cache_ttl=3600.0,
                retries=2,
            )
        except SourceError:
            return None

        hourly = payload.get("hourly") or {}
        times = hourly.get("time") or []
        if not times:
            return None
        target = when.strftime("%Y-%m-%dT%H:00")
        idx = min(
            range(len(times)),
            key=lambda i: abs(_hour_index(times[i]) - _hour_index(target)),
        )

        def _at(key: str) -> float | None:
            values = hourly.get(key) or []
            return float(values[idx]) if idx < len(values) and values[idx] is not None else None

        return {
            "roof": team.roof,
            "indoor": False,
            "temp_f": _at("temperature_2m"),
            "wind_mph": _at("wind_speed_10m"),
            "precip_pct": _at("precipitation_probability"),
        }


def _hour_index(stamp: str) -> int:
    parsed = to_utc(stamp)
    return int(parsed.timestamp() // 3600) if parsed else 0
