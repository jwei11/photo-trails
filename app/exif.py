import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from PIL import ExifTags, Image, TiffImagePlugin

IFD_EXIF = 0x8769
IFD_GPS = 0x8825

TAG_MAKE = 0x010F
TAG_MODEL = 0x0110
TAG_ORIENTATION = 0x0112
TAG_DATETIME_ORIGINAL = 0x9003
TAG_DATETIME_DIGITIZED = 0x9004
TAG_OFFSET_TIME_ORIGINAL = 0x9011
TAG_OFFSET_TIME_DIGITIZED = 0x9012
TAG_SUBSEC_ORIGINAL = 0x9291
TAG_BODY_SERIAL = 0xA431

GPS_LAT_REF, GPS_LAT, GPS_LON_REF, GPS_LON = 1, 2, 3, 4
GPS_ALT_REF, GPS_ALT, GPS_TIMESTAMP, GPS_DATESTAMP = 5, 6, 7, 29

MAX_BYTES_IN_JSON = 64


@dataclass
class PhotoMetadata:
    camera_make: str | None = None
    camera_model: str | None = None
    camera_serial: str | None = None
    camera_time: datetime | None = None
    exif_offset_min: int | None = None
    gps_time: datetime | None = None
    lat: float | None = None
    lon: float | None = None
    altitude_m: float | None = None
    width: int | None = None
    height: int | None = None
    orientation: int | None = None
    tags: dict = field(default_factory=dict)


def read_metadata(img: Image.Image) -> PhotoMetadata:
    exif = img.getexif()
    exif_ifd = exif.get_ifd(IFD_EXIF)
    gps_ifd = exif.get_ifd(IFD_GPS)

    meta = PhotoMetadata(
        camera_make=_clean_str(exif.get(TAG_MAKE)),
        camera_model=_clean_str(exif.get(TAG_MODEL)),
        camera_serial=_clean_str(exif_ifd.get(TAG_BODY_SERIAL)),
        width=img.width,
        height=img.height,
        orientation=_int_or_none(exif.get(TAG_ORIENTATION)),
    )

    raw_time = exif_ifd.get(TAG_DATETIME_ORIGINAL) or exif_ifd.get(TAG_DATETIME_DIGITIZED)
    meta.camera_time = parse_exif_datetime(raw_time, exif_ifd.get(TAG_SUBSEC_ORIGINAL))
    meta.exif_offset_min = parse_exif_offset(
        exif_ifd.get(TAG_OFFSET_TIME_ORIGINAL) or exif_ifd.get(TAG_OFFSET_TIME_DIGITIZED)
    )

    meta.lat = _gps_coord(gps_ifd.get(GPS_LAT), gps_ifd.get(GPS_LAT_REF), "S")
    meta.lon = _gps_coord(gps_ifd.get(GPS_LON), gps_ifd.get(GPS_LON_REF), "W")
    if meta.lat is None or meta.lon is None or not _valid_position(meta.lat, meta.lon):
        meta.lat = meta.lon = None
    meta.altitude_m = _gps_altitude(gps_ifd.get(GPS_ALT), gps_ifd.get(GPS_ALT_REF))
    meta.gps_time = parse_gps_datetime(gps_ifd.get(GPS_DATESTAMP), gps_ifd.get(GPS_TIMESTAMP))

    meta.tags = {
        **_named(exif, ExifTags.TAGS, skip={IFD_EXIF, IFD_GPS}),
        **_named(exif_ifd, ExifTags.TAGS),
        **({"GPS": _named(gps_ifd, ExifTags.GPSTAGS)} if gps_ifd else {}),
    }
    return meta


def parse_exif_datetime(value, subsec=None) -> datetime | None:
    text = _clean_str(value)
    if not text:
        return None
    try:
        parsed = datetime.strptime(text[:19], "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None
    digits = re.sub(r"\D", "", _clean_str(subsec) or "")
    if digits:
        parsed = parsed.replace(microsecond=int(digits[:6].ljust(6, "0")))
    return parsed


def parse_exif_offset(value) -> int | None:
    match = re.fullmatch(r"([+-])(\d{2}):(\d{2})", _clean_str(value) or "")
    if not match:
        return None
    sign, hours, minutes = match.groups()
    total = int(hours) * 60 + int(minutes)
    if total > 14 * 60:
        return None
    return total if sign == "+" else -total


def parse_gps_datetime(datestamp, timestamp) -> datetime | None:
    date_text = _clean_str(datestamp)
    if not date_text or not timestamp or len(timestamp) != 3:
        return None
    try:
        day = datetime.strptime(date_text, "%Y:%m:%d")
        hours, minutes, seconds = (_to_float(v) for v in timestamp)
    except (ValueError, TypeError):
        return None
    if any(v is None for v in (hours, minutes, seconds)):
        return None
    return day.replace(tzinfo=timezone.utc) + timedelta(hours=hours, minutes=minutes, seconds=seconds)


def _gps_coord(dms, ref, negative_ref) -> float | None:
    if not dms or len(dms) != 3:
        return None
    degrees, minutes, seconds = (_to_float(v) for v in dms)
    if any(v is None for v in (degrees, minutes, seconds)):
        return None
    value = degrees + minutes / 60 + seconds / 3600
    return -value if _clean_str(ref) == negative_ref else value


def _gps_altitude(alt, ref) -> float | None:
    value = _to_float(alt)
    if value is None:
        return None
    below_sea_level = ref in (1, b"\x01")
    return -value if below_sea_level else value


def _valid_position(lat: float, lon: float) -> bool:
    # (0, 0) is what many devices write when they have no fix
    return -90 <= lat <= 90 and -180 <= lon <= 180 and not (lat == 0 and lon == 0)


def _named(ifd, names: dict, skip: set = frozenset()) -> dict:
    out = {}
    for tag, value in ifd.items():
        if tag in skip:
            continue
        json_value = _jsonable(value)
        if json_value is not None:
            out[names.get(tag, f"0x{tag:04X}")] = json_value
    return out


def _jsonable(value):
    if isinstance(value, TiffImagePlugin.IFDRational):
        return _to_float(value)
    if isinstance(value, bytes):
        if len(value) > MAX_BYTES_IN_JSON:
            return None
        return _clean_str(value)
    if isinstance(value, str):
        return _clean_str(value)
    if isinstance(value, (tuple, list)):
        return [_jsonable(v) for v in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (int, bool)):
        return value
    return None


def _clean_str(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if not isinstance(value, str):
        return None
    # Postgres JSONB and text reject NUL characters, which EXIF strings often contain
    value = value.replace("\x00", "").strip()
    return value or None


def _to_float(value) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return result if math.isfinite(result) else None


def _int_or_none(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
