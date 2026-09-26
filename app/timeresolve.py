from bisect import bisect_left
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

NEIGHBOUR_MAX_GAP = timedelta(hours=48)

CameraKey = tuple[str | None, str | None, str | None]
TzLookup = Callable[[float, float], str | None]


@dataclass(frozen=True)
class PhotoTimeInput:
    id: str
    camera: CameraKey
    camera_time: datetime | None
    exif_offset_min: int | None
    gps_time: datetime | None
    lat: float | None
    lon: float | None


@dataclass(frozen=True)
class ClockOffset:
    camera: CameraKey
    offset_seconds: int
    valid_from: datetime | None = None
    valid_to: datetime | None = None


@dataclass(frozen=True)
class ResolvedTime:
    shot_at: datetime | None
    tz_name: str | None
    local_offset_min: int | None
    time_source: str | None

    @property
    def local_time(self) -> datetime | None:
        if self.shot_at is None or self.local_offset_min is None:
            return None
        return (self.shot_at + timedelta(minutes=self.local_offset_min)).replace(tzinfo=None)


UNDATED = ResolvedTime(None, None, None, None)


def resolve_all(
    photos: Iterable[PhotoTimeInput],
    offsets: Iterable[ClockOffset],
    tz_lookup: TzLookup,
    default_tz: str,
) -> dict[str, ResolvedTime]:
    photos = list(photos)
    offsets = list(offsets)
    corrected = {p.id: corrected_camera_time(p, offsets) for p in photos}

    results: dict[str, ResolvedTime] = {}
    anchors: list[tuple[datetime, str]] = []

    # Pass 1: photos with coordinates know their zone directly.
    for p in photos:
        if p.lat is None or p.lon is None:
            continue
        zone = tz_lookup(p.lat, p.lon)
        if zone is None:
            continue
        results[p.id] = _resolve(p, corrected[p.id], zone, "coordinates")
        local = results[p.id].local_time
        if local is not None:
            anchors.append((local, zone))

    # Pass 2: everything else borrows a zone from the nearest photo in (local) time.
    anchors.sort()
    for p in photos:
        if p.id in results:
            continue
        cam_time = corrected[p.id]
        zone = _nearest_zone(anchors, cam_time) if cam_time else None
        if zone is not None:
            results[p.id] = _resolve(p, cam_time, zone, "neighbour")
        elif cam_time is not None and p.exif_offset_min is not None:
            instant = (cam_time - timedelta(minutes=p.exif_offset_min)).replace(tzinfo=timezone.utc)
            results[p.id] = ResolvedTime(instant, None, p.exif_offset_min, "exif_offset")
        else:
            results[p.id] = _resolve(p, cam_time, default_tz, "default_zone")
    return results


def corrected_camera_time(p: PhotoTimeInput, offsets: list[ClockOffset]) -> datetime | None:
    if p.camera_time is None:
        return None
    for o in offsets:
        if o.camera != p.camera:
            continue
        if o.valid_from and p.camera_time < o.valid_from:
            continue
        if o.valid_to and p.camera_time >= o.valid_to:
            continue
        return p.camera_time + timedelta(seconds=o.offset_seconds)
    return p.camera_time


def _resolve(
    p: PhotoTimeInput, cam_time: datetime | None, zone: str, zone_source: str
) -> ResolvedTime:
    tz = ZoneInfo(zone)
    if cam_time is not None and p.exif_offset_min is not None:
        instant = (cam_time - timedelta(minutes=p.exif_offset_min)).replace(tzinfo=timezone.utc)
        source = "exif_offset"
    elif p.gps_time is not None:
        instant = p.gps_time.astimezone(timezone.utc)
        source = "gps"
    elif cam_time is not None:
        instant = cam_time.replace(tzinfo=tz).astimezone(timezone.utc)
        source = zone_source
    else:
        return UNDATED

    offset = instant.astimezone(tz).utcoffset()
    return ResolvedTime(
        shot_at=instant,
        tz_name=zone,
        local_offset_min=int(offset.total_seconds() // 60),
        time_source=source,
    )


def _nearest_zone(anchors: list[tuple[datetime, str]], when: datetime) -> str | None:
    if not anchors:
        return None
    i = bisect_left(anchors, (when, ""))
    candidates = [anchors[j] for j in (i - 1, i) if 0 <= j < len(anchors)]
    best_time, best_zone = min(candidates, key=lambda a: abs(a[0] - when))
    return best_zone if abs(best_time - when) <= NEIGHBOUR_MAX_GAP else None
