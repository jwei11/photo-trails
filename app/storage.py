import json
import logging
import re
import time
from pathlib import Path

import psycopg
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

from app import images, timeresolve, tz
from app.config import DEFAULT_TIMEZONE, UPLOAD_DIR
from app.db import engine

log = logging.getLogger(__name__)

PHOTO_COLUMNS = """
    id, original_filename, content_type, size_bytes, storage_key, created_at,
    camera_make, camera_model, camera_serial, camera_time, exif_offset_min, gps_time,
    lat, lon, altitude_m, width, height, orientation,
    shot_at, tz_name, local_offset_min, time_source,
    thumb_key, web_key, processing_error, processing_ms
"""

TIME_RESOLUTION_LOCK = 3_141_592


def file_path(storage_key: str) -> Path:
    return UPLOAD_DIR / storage_key


def all_photos() -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(f"SELECT {PHOTO_COLUMNS} FROM photos ORDER BY shot_at NULLS LAST, created_at")
        )
        return [dict(row._mapping) for row in rows]


def get_photo(photo_id: str, include_exif: bool = False) -> dict | None:
    columns = PHOTO_COLUMNS + (", exif" if include_exif else "")
    with engine.connect() as conn:
        row = conn.execute(
            text(f"SELECT {columns} FROM photos WHERE id = :id"), {"id": photo_id}
        ).first()
        return dict(row._mapping) if row else None


def _safe_extension(filename: str | None) -> str:
    ext = Path(filename or "").suffix.lower()
    return ext if re.fullmatch(r"\.[a-z0-9]{1,10}", ext) else ""


def add_photo(original_filename: str | None, content_type: str, contents: bytes) -> dict:
    started = time.perf_counter()
    try:
        processed = images.process(contents)
        error = None
    except Exception as exc:  # any undecodable upload is stored, flagged, and skipped
        processed, error = None, f"{type(exc).__name__}: {exc}"
    process_ms = round((time.perf_counter() - started) * 1000)

    meta = processed.metadata if processed else None
    with engine.begin() as conn:
        row = conn.execute(
            text(
                """
                WITH new AS (SELECT gen_random_uuid() AS id)
                INSERT INTO photos (
                    id, original_filename, content_type, size_bytes, storage_key,
                    camera_make, camera_model, camera_serial, camera_time, exif_offset_min,
                    gps_time, lat, lon, altitude_m, width, height, orientation, exif,
                    thumb_key, web_key, processing_error, processing_ms
                )
                SELECT
                    new.id, :filename, :content_type, :size, new.id::text || :ext,
                    :camera_make, :camera_model, :camera_serial, :camera_time, :exif_offset_min,
                    :gps_time, :lat, :lon, :altitude_m, :width, :height, :orientation,
                    CAST(:exif AS jsonb),
                    CASE WHEN :has_derivatives THEN new.id::text || '_thumb.webp' END,
                    CASE WHEN :has_derivatives THEN new.id::text || '_web.webp' END,
                    :error, :process_ms
                FROM new
                RETURNING id, storage_key, thumb_key, web_key
                """
            ),
            {
                "filename": original_filename,
                "content_type": content_type,
                "size": len(contents),
                "ext": _safe_extension(original_filename),
                "camera_make": meta and meta.camera_make,
                "camera_model": meta and meta.camera_model,
                "camera_serial": meta and meta.camera_serial,
                "camera_time": meta and meta.camera_time,
                "exif_offset_min": meta and meta.exif_offset_min,
                "gps_time": meta and meta.gps_time,
                "lat": meta and meta.lat,
                "lon": meta and meta.lon,
                "altitude_m": meta and meta.altitude_m,
                "width": meta and meta.width,
                "height": meta and meta.height,
                "orientation": meta and meta.orientation,
                "exif": json.dumps(meta.tags) if meta else None,
                "has_derivatives": processed is not None,
                "error": error,
                "process_ms": process_ms,
            },
        ).one()

        file_path(row.storage_key).write_bytes(contents)
        if processed:
            file_path(row.thumb_key).write_bytes(processed.thumb)
            file_path(row.web_key).write_bytes(processed.web)
        resolve_times(conn)

    total_ms = round((time.perf_counter() - started) * 1000)
    log.info(
        "photo stored id=%s bytes=%d process_ms=%d total_ms=%d error=%s",
        row.id, len(contents), process_ms, total_ms, error,
    )
    return get_photo(str(row.id))


def resolve_times(conn: Connection) -> int:
    # Serialize: two concurrent resolutions would each overwrite the other's results.
    conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": TIME_RESOLUTION_LOCK})

    rows = conn.execute(
        text(
            """
            SELECT id, camera_make, camera_model, camera_serial, camera_time,
                   exif_offset_min, gps_time, lat, lon,
                   shot_at, tz_name, local_offset_min, time_source
            FROM photos
            """
        )
    ).all()
    offsets = [
        timeresolve.ClockOffset(
            (o.camera_make, o.camera_model, o.camera_serial),
            o.offset_seconds, o.valid_from, o.valid_to,
        )
        for o in conn.execute(
            text(
                "SELECT camera_make, camera_model, camera_serial, offset_seconds, "
                "valid_from, valid_to FROM camera_clock_offsets ORDER BY created_at"
            )
        )
    ]
    inputs = [
        timeresolve.PhotoTimeInput(
            str(r.id), (r.camera_make, r.camera_model, r.camera_serial),
            r.camera_time, r.exif_offset_min, r.gps_time, r.lat, r.lon,
        )
        for r in rows
    ]
    results = timeresolve.resolve_all(inputs, offsets, tz.zone_at, DEFAULT_TIMEZONE)

    changed = []
    for r in rows:
        new = results[str(r.id)]
        current = timeresolve.ResolvedTime(r.shot_at, r.tz_name, r.local_offset_min, r.time_source)
        if new != current:
            changed.append({
                "id": r.id, "shot_at": new.shot_at, "tz_name": new.tz_name,
                "local_offset_min": new.local_offset_min, "time_source": new.time_source,
            })
    if changed:
        conn.execute(
            text(
                "UPDATE photos SET shot_at = :shot_at, tz_name = :tz_name, "
                "local_offset_min = :local_offset_min, time_source = :time_source "
                "WHERE id = :id"
            ),
            changed,
        )
    return len(changed)


def list_cameras() -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT camera_make, camera_model, camera_serial, count(*) AS photo_count,
                       min(camera_time) AS first_camera_time, max(camera_time) AS last_camera_time
                FROM photos
                WHERE camera_make IS NOT NULL AND camera_model IS NOT NULL
                GROUP BY camera_make, camera_model, camera_serial
                ORDER BY photo_count DESC
                """
            )
        )
        return [dict(r._mapping) for r in rows]


def list_offsets() -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT * FROM camera_clock_offsets ORDER BY created_at"))
        return [dict(r._mapping) for r in rows]


class OffsetOverlapError(Exception):
    pass


def add_offset(values: dict) -> dict:
    try:
        with engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    INSERT INTO camera_clock_offsets
                        (camera_make, camera_model, camera_serial, offset_seconds,
                         valid_from, valid_to, note)
                    VALUES (:camera_make, :camera_model, :camera_serial, :offset_seconds,
                            :valid_from, :valid_to, :note)
                    RETURNING *
                    """
                ),
                values,
            ).one()
            changed = resolve_times(conn)
    except IntegrityError as exc:
        if isinstance(exc.orig, psycopg.errors.ExclusionViolation):
            raise OffsetOverlapError from exc
        raise
    return {**dict(row._mapping), "photos_updated": changed}


def delete_offset(offset_id: str) -> int | None:
    with engine.begin() as conn:
        deleted = conn.execute(
            text("DELETE FROM camera_clock_offsets WHERE id = :id"), {"id": offset_id}
        ).rowcount
        if not deleted:
            return None
        return resolve_times(conn)
