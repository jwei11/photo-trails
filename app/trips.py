import math
from bisect import bisect_left
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

EARTH_RADIUS_KM = 6371.0088


@dataclass(frozen=True)
class Params:
    gap: timedelta = timedelta(hours=48)
    jump_km: float = 300
    jump_gap: timedelta = timedelta(hours=24)
    min_photos: int = 3


@dataclass(frozen=True)
class TripPhoto:
    id: str
    shot_at: datetime | None
    lat: float | None
    lon: float | None
    trip_id: str | None = None
    pinned: bool = False  # in a locked trip, or placed by hand: the algorithm leaves it alone


@dataclass(frozen=True)
class Home:
    lat: float
    lon: float
    radius_km: float


@dataclass(frozen=True)
class LockedTrip:
    id: str
    start: datetime
    end: datetime


@dataclass
class Grouping:
    assignments: dict[str, str | None] = field(default_factory=dict)  # photo id -> trip id / "new:N"
    new_trips: list[str] = field(default_factory=list)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def group_photos(
    photos: Iterable[TripPhoto],
    homes: Iterable[Home],
    locked: Iterable[LockedTrip],
    params: Params = Params(),
) -> Grouping:
    photos, homes, locked = list(photos), list(homes), list(locked)
    result = Grouping()

    free = sorted((p for p in photos if not p.pinned and p.shot_at), key=lambda p: p.shot_at)
    for p in photos:
        if not p.pinned and p.shot_at is None:
            result.assignments[p.id] = None

    at_home = _home_flags(photos, homes, params.gap)

    remaining = []
    for p in free:
        if at_home[p.id]:
            result.assignments[p.id] = None
            continue
        inside = next((t for t in locked if t.start <= p.shot_at <= t.end), None)
        if inside:
            result.assignments[p.id] = inside.id
        else:
            remaining.append(p)

    home_times = sorted(p.shot_at for p in photos if p.shot_at and at_home.get(p.id))
    segments = _segment(remaining, home_times, locked, params)

    existing_trip_ids = {p.trip_id for p in photos if p.trip_id and not p.pinned}
    claimed: set[str] = set()
    for segment in sorted(segments, key=len, reverse=True):
        if len(segment) < params.min_photos:
            for p in segment:
                result.assignments[p.id] = None
            continue
        trip_id = _reuse_trip_id(segment, existing_trip_ids, claimed)
        if trip_id is None:
            trip_id = f"new:{len(result.new_trips)}"
            result.new_trips.append(trip_id)
        claimed.add(trip_id)
        for p in segment:
            result.assignments[p.id] = trip_id
    return result


def _home_flags(photos: list[TripPhoto], homes: list[Home], max_gap: timedelta) -> dict[str, bool]:
    flags: dict[str, bool] = {}
    located = sorted(
        ((p.shot_at, _near_home(p, homes)) for p in photos if p.shot_at and p.lat is not None),
        key=lambda x: x[0],
    )
    times = [t for t, _ in located]
    for p in photos:
        if p.lat is not None:
            flags[p.id] = _near_home(p, homes)
        elif p.shot_at and located:
            # No GPS: assume the same as the nearest located photo in time.
            i = bisect_left(times, p.shot_at)
            nearest = min(
                (located[j] for j in (i - 1, i) if 0 <= j < len(located)),
                key=lambda x: abs(x[0] - p.shot_at),
            )
            flags[p.id] = nearest[1] and abs(nearest[0] - p.shot_at) <= max_gap
        else:
            flags[p.id] = False
    return flags


def _near_home(p: TripPhoto, homes: list[Home]) -> bool:
    return any(haversine_km(p.lat, p.lon, h.lat, h.lon) <= h.radius_km for h in homes)


def _segment(
    photos: list[TripPhoto], home_times: list[datetime], locked: list[LockedTrip], params: Params
) -> list[list[TripPhoto]]:
    segments: list[list[TripPhoto]] = []
    current: list[TripPhoto] = []
    last_located: TripPhoto | None = None
    for p in photos:
        if current and _breaks(current[-1], p, last_located, home_times, locked, params):
            segments.append(current)
            current, last_located = [], None
        current.append(p)
        if p.lat is not None:
            last_located = p
    if current:
        segments.append(current)
    return segments


def _breaks(
    prev: TripPhoto,
    p: TripPhoto,
    last_located: TripPhoto | None,
    home_times: list[datetime],
    locked: list[LockedTrip],
    params: Params,
) -> bool:
    gap = p.shot_at - prev.shot_at
    if gap > params.gap:
        return True
    if gap > params.jump_gap and last_located and p.lat is not None:
        if haversine_km(last_located.lat, last_located.lon, p.lat, p.lon) > params.jump_km:
            return True
    i = bisect_left(home_times, prev.shot_at)
    if i < len(home_times) and home_times[i] <= p.shot_at:
        return True  # went home in between
    return any(prev.shot_at < t.start and t.end < p.shot_at for t in locked)


def _reuse_trip_id(segment: list[TripPhoto], existing: set[str], claimed: set[str]) -> str | None:
    votes = Counter(p.trip_id for p in segment if p.trip_id in existing and p.trip_id not in claimed)
    return votes.most_common(1)[0][0] if votes else None


@dataclass(frozen=True)
class TripStats:
    start: datetime | None
    end: datetime | None
    photo_count: int
    distance_km: float
    bbox: tuple[float, float, float, float] | None  # min_lat, min_lon, max_lat, max_lon


def trip_stats(photos: Iterable[TripPhoto]) -> TripStats:
    all_photos = list(photos)
    dated = sorted((p for p in all_photos if p.shot_at), key=lambda p: p.shot_at)
    located = [p for p in dated if p.lat is not None]
    distance = sum(
        haversine_km(a.lat, a.lon, b.lat, b.lon) for a, b in zip(located, located[1:])
    )
    bbox = None
    if located:
        lats, lons = [p.lat for p in located], [p.lon for p in located]
        bbox = (min(lats), min(lons), max(lats), max(lons))
    return TripStats(
        start=dated[0].shot_at if dated else None,
        end=dated[-1].shot_at if dated else None,
        photo_count=len(all_photos),
        distance_km=round(distance, 1),
        bbox=bbox,
    )
