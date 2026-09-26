from functools import cache

from timezonefinder import TimezoneFinder


@cache
def _finder() -> TimezoneFinder:
    return TimezoneFinder()


def zone_at(lat: float, lon: float) -> str | None:
    return _finder().timezone_at(lat=lat, lng=lon)
