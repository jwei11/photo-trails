import hashlib
import json
import logging
import re
import time
from pathlib import Path

import psycopg
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

from app import images, objects, timeresolve, trips, tz
from app.config import DEFAULT_TIMEZONE
from app.db import engine

log = logging.getLogger(__name__)

PHOTO_COLUMNS = """
    id, original_filename, content_type, size_bytes, storage_key, created_at,
    camera_make, camera_model, camera_serial, camera_time, exif_offset_min, gps_time,
    lat, lon, altitude_m, width, height, orientation,
    shot_at, tz_name, local_offset_min, time_source,
    thumb_key, web_key, processing_error, processing_ms, trip_id, trip_pinned,
    place_id, place_distance_m, nearest_place_id, nearest_place_distance_m, country_code,
    (SELECT name FROM places WHERE geonameid = photos.place_id) AS place_name,
    (SELECT name FROM places WHERE geonameid = photos.nearest_place_id) AS nearest_place_name,
    (SELECT r.name FROM places pl JOIN admin1_regions r ON r.code = pl.country_code || '.' || pl.admin1_code
     WHERE pl.geonameid = photos.place_id) AS region_name,
    (SELECT name FROM countries WHERE iso_a2 = photos.country_code LIMIT 1) AS country_name
"""

WRITE_LOCK = 3_141_592


def lock_writes(conn: Connection) -> None:
    """Serialize write transactions. Call first, before reading anything the write depends on.

    Every write ends by recomputing times and trips from the whole photo set, so two writes
    running at once could each compute from a stale view and overwrite the other's result.
    """
    conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": WRITE_LOCK})


def recompute(conn: Connection) -> None:
    resolve_places(conn)
    resolve_times(conn)
    regroup_trips(conn)


# 12 nautical miles: a country's territorial waters. A photo from a beach or a boat that
# falls just outside a country's (approximate) coastline still counts as in that country.
COAST_TOLERANCE_M = 22_224

# Which place a person would name. Each place "claims" a radius that grows with the square
# root of its population: 20 m x sqrt(population) gives Lisbon (518k) 14 km, a neighbourhood
# of 12k 2.3 km, a village of 1,000 630 m. The photo gets the place it's deepest inside,
# relative to that radius, so a big city counts from further out but a small town still
# wins right next to it. If no radius reaches the photo, it gets the nearest place.
# Tried against real spots before choosing: see the README.
PLACE_RADIUS_M_PER_SQRT_PERSON = 20
PLACE_SEARCH_LIMIT_M = 50_000  # the radius of a city of ~6 million; bigger ones are capped


def resolve_places(conn: Connection) -> int:
    """Match photos not yet checked to a place, their nearest place, and their country.

    Only unchecked photos: a photo's coordinates never change, so its place only needs
    finding when it arrives, or again after the place data is reloaded (which unchecks all).
    """
    return conn.execute(
        text(
            """
            UPDATE photos p
            SET place_id = coalesce(r.chosen_id, r.nearest_id),
                place_distance_m = coalesce(r.chosen_distance_m, r.nearest_distance_m),
                nearest_place_id = r.nearest_id, nearest_place_distance_m = r.nearest_distance_m,
                country_code = r.country_code, places_checked = true
            FROM (
                SELECT ph.id, near.geonameid AS nearest_id, near.distance_m AS nearest_distance_m,
                       chosen.geonameid AS chosen_id, chosen.distance_m AS chosen_distance_m,
                       CASE WHEN inside.found THEN inside.iso_a2 ELSE coast.iso_a2 END AS country_code
                FROM photos ph
                -- Nearest place: <-> lets the spatial index walk outwards from the point.
                LEFT JOIN LATERAL (
                    SELECT geonameid, ST_Distance(pl.location, ph.location) AS distance_m
                    FROM places pl ORDER BY pl.location <-> ph.location LIMIT 1
                ) near ON ph.location IS NOT NULL
                -- The place the photo is deepest inside (see PLACE_RADIUS_M_PER_SQRT_PERSON).
                -- ST_DWithin uses the index to fetch candidates within the search limit.
                LEFT JOIN LATERAL (
                    SELECT geonameid, distance_m FROM (
                        SELECT geonameid, ST_Distance(pl.location, ph.location) AS distance_m,
                               :per_sqrt * sqrt(greatest(pl.population, 1)) AS radius_m
                        FROM places pl WHERE ST_DWithin(pl.location, ph.location, :search_limit)
                    ) candidates
                    WHERE distance_m <= radius_m
                    ORDER BY distance_m / radius_m LIMIT 1
                ) chosen ON ph.location IS NOT NULL
                -- The country shape containing the point (geometry: flat lat/lon test).
                LEFT JOIN LATERAL (
                    SELECT true AS found, iso_a2 FROM countries c
                    WHERE ST_Covers(c.geom, ph.location::geometry) LIMIT 1
                ) inside ON ph.location IS NOT NULL
                -- Otherwise the nearest country within territorial waters. The bounding-box
                -- test (&&, about 0.3 degrees) uses the index to pick a few candidates
                -- before measuring true distances to their shapes.
                LEFT JOIN LATERAL (
                    SELECT iso_a2 FROM (
                        SELECT iso_a2, ST_Distance(c.geom::geography, ph.location) AS d
                        FROM countries c
                        WHERE c.geom && ST_Expand(ph.location::geometry, 0.3)
                    ) candidates
                    WHERE d <= :coast ORDER BY d LIMIT 1
                ) coast ON ph.location IS NOT NULL AND inside.found IS NULL
                -- Only photos whose file has arrived: a pending one has no coordinates yet,
                -- and checking it now would skip it for good. It's matched in the same
                -- transaction that makes it ready.
                WHERE NOT ph.places_checked AND ph.status = 'ready'
            ) r
            WHERE p.id = r.id
            """
        ),
        {"coast": COAST_TOLERANCE_M, "per_sqrt": PLACE_RADIUS_M_PER_SQRT_PERSON,
         "search_limit": PLACE_SEARCH_LIMIT_M},
    ).rowcount


def all_photos() -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                f"SELECT {PHOTO_COLUMNS} FROM photos WHERE status = 'ready' "
                "ORDER BY shot_at NULLS LAST, created_at"
            )
        )
        return [dict(row._mapping) for row in rows]


def get_photo(photo_id: str, include_exif: bool = False) -> dict | None:
    columns = PHOTO_COLUMNS + (", exif" if include_exif else "")
    with engine.connect() as conn:
        row = conn.execute(
            text(f"SELECT {columns} FROM photos WHERE id = :id AND status = 'ready'"),
            {"id": photo_id},
        ).first()
        return dict(row._mapping) if row else None


def _safe_extension(filename: str | None) -> str:
    ext = Path(filename or "").suffix.lower()
    return ext if re.fullmatch(r"\.[a-z0-9]{1,10}", ext) else ""


class UploadNotFound(Exception):
    pass


class UploadNotReceived(Exception):
    pass


def create_pending(
    original_filename: str | None, content_type: str, size_bytes: int, conn: Connection | None = None
) -> dict:
    if conn is None:
        with engine.begin() as own:
            return create_pending(original_filename, content_type, size_bytes, own)
    row = conn.execute(
        text(
            """
            WITH new AS (SELECT gen_random_uuid() AS id)
            INSERT INTO photos (id, original_filename, content_type, size_bytes, storage_key, status)
            SELECT new.id, :filename, :content_type, :size, new.id::text || :ext, 'pending'
            FROM new
            RETURNING id, storage_key
            """
        ),
        {
            "filename": original_filename,
            "content_type": content_type,
            "size": size_bytes,
            "ext": _safe_extension(original_filename),
        },
    ).one()
    return {"id": str(row.id), "storage_key": row.storage_key}


class DuplicatePhoto(Exception):
    def __init__(self, original_id: str):
        super().__init__(original_id)
        self.original_id = original_id


def find_by_hashes(hashes: list[str]) -> dict[str, str]:
    """Map content hash -> id of the ready photo that already has it."""
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT content_sha256, id FROM photos WHERE content_sha256 = ANY(:hashes)"),
            {"hashes": hashes},
        )
        return {r.content_sha256: str(r.id) for r in rows}


def _duplicate_of(conn: Connection, sha256: str, photo_id: str) -> str | None:
    found = conn.execute(
        text("SELECT id FROM photos WHERE content_sha256 = :sha AND id != :id"),
        {"sha": sha256, "id": photo_id},
    ).scalar_one_or_none()
    return str(found) if found else None


def _discard_pending(photo_id: str, storage_key: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM photos WHERE id = :id AND status = 'pending'"), {"id": photo_id}
        )
    objects.delete(storage_key)


def add_photo(original_filename: str | None, content_type: str, contents: bytes) -> dict:
    """Upload through the API (M5 pass A): the same steps a presigned upload goes through."""
    pending = create_pending(original_filename, content_type, len(contents))
    objects.put(pending["storage_key"], contents, content_type)
    return complete_upload(pending["id"], contents)


def complete_upload(photo_id: str, contents: bytes | None = None) -> dict:
    """Process an uploaded original and mark it ready. Safe to call more than once.

    Raises DuplicatePhoto (after discarding this upload) if identical bytes are already stored.
    """
    started = time.perf_counter()
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT storage_key, size_bytes, status FROM photos WHERE id = :id"),
            {"id": photo_id},
        ).first()
    if row is None:
        raise UploadNotFound
    if row.status == "ready":
        return get_photo(photo_id)
    if contents is None:
        if objects.size(row.storage_key) != row.size_bytes:
            raise UploadNotReceived
        contents = objects.get(row.storage_key)

    # The hash of the bytes actually stored, not the one the client declared.
    sha256 = hashlib.sha256(contents).hexdigest()
    with engine.connect() as conn:
        original = _duplicate_of(conn, sha256, photo_id)
    if original:
        _discard_pending(photo_id, row.storage_key)
        raise DuplicatePhoto(original)

    # Slow CPU work happens before taking the write lock, so uploads still process in parallel.
    try:
        processed = images.process(contents)
        error = None
    except Exception as exc:  # any undecodable upload is stored, flagged, and skipped
        processed, error = None, f"{type(exc).__name__}: {exc}"
    process_ms = round((time.perf_counter() - started) * 1000)

    with engine.begin() as conn:
        lock_writes(conn)
        status = conn.execute(
            text("SELECT status FROM photos WHERE id = :id"), {"id": photo_id}
        ).scalar_one_or_none()
        if status is None:
            raise UploadNotFound  # swept as an orphan while we were processing
        if status == "ready":
            return get_photo(photo_id)  # a concurrent completion finished first
        # Re-check under the lock: an identical file may have completed while we processed.
        original = _duplicate_of(conn, sha256, photo_id)
        if original is None:
            _store_processed(conn, photo_id, sha256, processed, error, process_ms)
    if original:
        _discard_pending(photo_id, row.storage_key)
        raise DuplicatePhoto(original)

    total_ms = round((time.perf_counter() - started) * 1000)
    log.info(
        "photo ready id=%s bytes=%d process_ms=%d total_ms=%d error=%s",
        photo_id, len(contents), process_ms, total_ms, error,
    )
    return get_photo(photo_id)


def _store_processed(
    conn: Connection, photo_id: str, sha256: str, processed, error: str | None, process_ms: int
) -> None:
    meta = processed.metadata if processed else None
    # Keys depend only on the id, so a retry overwrites rather than duplicates.
    thumb_key = web_key = None
    if processed:
        thumb_key, web_key = f"{photo_id}_thumb.webp", f"{photo_id}_web.webp"
        objects.put(thumb_key, processed.thumb, "image/webp")
        objects.put(web_key, processed.web, "image/webp")

    conn.execute(
        text(
            """
            UPDATE photos SET
                status = 'ready', content_sha256 = :sha256,
                camera_make = :camera_make, camera_model = :camera_model,
                camera_serial = :camera_serial, camera_time = :camera_time,
                exif_offset_min = :exif_offset_min, gps_time = :gps_time,
                lat = :lat, lon = :lon, altitude_m = :altitude_m,
                width = :width, height = :height, orientation = :orientation,
                exif = CAST(:exif AS jsonb), thumb_key = :thumb_key, web_key = :web_key,
                processing_error = :error, processing_ms = :process_ms
            WHERE id = :id
            """
        ),
        {
            "id": photo_id,
            "sha256": sha256,
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
            "thumb_key": thumb_key,
            "web_key": web_key,
            "error": error,
            "process_ms": process_ms,
        },
    )
    recompute(conn)


def resolve_times(conn: Connection) -> int:
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


def regroup_trips(conn: Connection) -> None:
    rows = conn.execute(
        text(
            """
            SELECT p.id, p.shot_at, p.lat, p.lon, p.trip_id,
                   p.trip_pinned OR coalesce(t.is_locked, false) AS pinned
            FROM photos p LEFT JOIN trips t ON t.id = p.trip_id
            """
        )
    ).all()
    locked = [
        trips.LockedTrip(str(r.id), r.start, r.end)
        for r in conn.execute(
            text(
                """
                SELECT t.id, min(p.shot_at) AS start, max(p.shot_at) AS end
                FROM trips t JOIN photos p ON p.trip_id = t.id
                WHERE t.is_locked AND p.shot_at IS NOT NULL
                GROUP BY t.id
                """
            )
        )
    ]
    homes = [
        trips.Home(r.lat, r.lon, r.radius_km)
        for r in conn.execute(text("SELECT lat, lon, radius_km FROM homes"))
    ]
    grouping = trips.group_photos(
        (
            trips.TripPhoto(
                str(r.id), r.shot_at, r.lat, r.lon,
                str(r.trip_id) if r.trip_id else None, r.pinned,
            )
            for r in rows
        ),
        homes,
        locked,
    )

    new_ids = {
        ref: str(conn.execute(text("INSERT INTO trips DEFAULT VALUES RETURNING id")).scalar_one())
        for ref in grouping.new_trips
    }
    current = {str(r.id): (str(r.trip_id) if r.trip_id else None) for r in rows}
    changes = [
        {"id": pid, "trip_id": new_ids.get(trip, trip)}
        for pid, trip in grouping.assignments.items()
        if new_ids.get(trip, trip) != current[pid]
    ]
    if changes:
        conn.execute(text("UPDATE photos SET trip_id = :trip_id WHERE id = :id"), changes)
    conn.execute(
        text("DELETE FROM trips t WHERE NOT EXISTS (SELECT 1 FROM photos p WHERE p.trip_id = t.id)")
    )


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


def add_offset(values: dict, replace_overlapping: bool = False) -> dict:
    """Add a clock offset. With replace_overlapping, the camera's offsets covering any of the
    same camera-clock times are deleted first, in the same transaction: either the new
    offset replaces them, or (if anything fails) they're all still there."""
    try:
        with engine.begin() as conn:
            lock_writes(conn)
            replaced = 0
            if replace_overlapping:
                # The same test as the exclusion constraint, so exactly the rows that would
                # block the insert are removed. NULL bounds make a range unbounded.
                replaced = conn.execute(
                    text(
                        """
                        DELETE FROM camera_clock_offsets
                        WHERE camera_make = :camera_make AND camera_model = :camera_model
                          AND coalesce(camera_serial, '')
                              = coalesce(CAST(:camera_serial AS text), '')
                          AND tsrange(valid_from, valid_to) && tsrange(
                              CAST(:valid_from AS timestamp), CAST(:valid_to AS timestamp))
                        """
                    ),
                    values,
                ).rowcount
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
            regroup_trips(conn)
    except IntegrityError as exc:
        if isinstance(exc.orig, psycopg.errors.ExclusionViolation):
            raise OffsetOverlapError from exc
        raise
    return {**dict(row._mapping), "photos_updated": changed, "replaced": replaced}


def delete_offset(offset_id: str) -> int | None:
    with engine.begin() as conn:
        lock_writes(conn)
        deleted = conn.execute(
            text("DELETE FROM camera_clock_offsets WHERE id = :id"), {"id": offset_id}
        ).rowcount
        if not deleted:
            return None
        changed = resolve_times(conn)
        regroup_trips(conn)
        return changed
