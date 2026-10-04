import hashlib
import json
import logging
import re
import time
from pathlib import Path

import psycopg
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

from app import images, jobs, objects, timeresolve, trips, tz
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


def lock_writes(conn: Connection, user_id: str | None = None) -> None:
    """Serialize one user's write transactions, or with no user, everyone's. Call first,
    before reading anything the write depends on.

    Every write ends by recomputing a user's times and trips from all their photos, so two
    writes running at once could each compute from a stale view and overwrite the other's
    result. Different users' photos never mix, so their writes don't wait for each other:
    each takes the shared lock plus its user's own. A write across all users (reloading
    place data) takes the shared one exclusively, so it waits for, and blocks, all of them.
    """
    if user_id is None:
        conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": WRITE_LOCK})
        return
    conn.execute(text("SELECT pg_advisory_xact_lock_shared(:key)"), {"key": WRITE_LOCK})
    conn.execute(
        text("SELECT pg_advisory_xact_lock(:key, hashtext(:user))"), {"key": WRITE_LOCK, "user": user_id}
    )


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


def all_photos(user_id: str) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                f"SELECT {PHOTO_COLUMNS} FROM photos WHERE user_id = :user AND status = 'ready' "
                "ORDER BY shot_at NULLS LAST, created_at"
            ),
            {"user": user_id},
        )
        return [dict(row._mapping) for row in rows]


def get_photo(user_id: str | None, photo_id: str, include_exif: bool = False) -> dict | None:
    """A ready photo of this user's (None only for internal callers that already know the
    photo is theirs to read, like the worker)."""
    columns = PHOTO_COLUMNS + (", exif" if include_exif else "")
    with engine.connect() as conn:
        row = conn.execute(
            text(
                f"SELECT {columns} FROM photos WHERE id = :id AND status = 'ready' "
                "AND (CAST(:user AS uuid) IS NULL OR user_id = CAST(:user AS uuid))"
            ),
            {"id": photo_id, "user": user_id},
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
    user_id: str, original_filename: str | None, content_type: str, size_bytes: int,
    conn: Connection | None = None,
) -> dict:
    if conn is None:
        with engine.begin() as own:
            return create_pending(user_id, original_filename, content_type, size_bytes, own)
    row = conn.execute(
        text(
            """
            WITH new AS (SELECT gen_random_uuid() AS id)
            INSERT INTO photos (id, user_id, original_filename, content_type, size_bytes, storage_key, status)
            SELECT new.id, :user, :filename, :content_type, :size, new.id::text || :ext, 'pending'
            FROM new
            RETURNING id, storage_key
            """
        ),
        {
            "user": user_id,
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


def find_by_hashes(user_id: str, hashes: list[str]) -> dict[str, str]:
    """Map content hash -> id of this user's ready photo that already has it."""
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT content_sha256, id FROM photos WHERE user_id = :user AND content_sha256 = ANY(:hashes)"),
            {"user": user_id, "hashes": hashes},
        )
        return {r.content_sha256: str(r.id) for r in rows}


def _duplicate_of(conn: Connection, user_id: str, sha256: str, photo_id: str) -> str | None:
    """The same user's photo with these bytes. Another user's never counts: telling someone
    "already imported" would reveal that a stranger has the photo."""
    found = conn.execute(
        text("SELECT id FROM photos WHERE user_id = :user AND content_sha256 = :sha AND id != :id"),
        {"user": user_id, "sha": sha256, "id": photo_id},
    ).scalar_one_or_none()
    return str(found) if found else None


def _discard_upload(photo_id: str, storage_key: str) -> None:
    """Drop an upload that turned out to duplicate a stored photo: its row and its file."""
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM photos WHERE id = :id AND status <> 'ready'"), {"id": photo_id})
    objects.delete(storage_key)


def add_photo(user_id: str, original_filename: str | None, content_type: str, contents: bytes) -> dict:
    """Upload through the API (M5 pass A), for scripts and tests. Unlike an import, it's
    processed before returning, trips included, rather than by the worker."""
    pending = create_pending(user_id, original_filename, content_type, len(contents))
    objects.put(pending["storage_key"], contents, content_type)
    return process_upload(pending["id"], contents, regroup_now=True)


def mark_uploaded(conn: Connection, photo_id: str, job_payload: dict) -> str:
    """The client says the file is in storage: check, and queue it for processing, in the
    caller's transaction. Returns the photo's status afterwards. Safe to call again."""
    row = conn.execute(
        text("SELECT storage_key, size_bytes, status FROM photos WHERE id = :id FOR UPDATE"),
        {"id": photo_id},
    ).first()
    if row is None:
        raise UploadNotFound  # swept as abandoned
    if row.status in ("uploaded", "processing", "ready"):
        return row.status  # already queued or done: nothing more to do
    # A HEAD request: cheap, and catches a client that says "done" before its PUT finished.
    if objects.size(row.storage_key) != row.size_bytes:
        raise UploadNotReceived
    conn.execute(text("UPDATE photos SET status = 'uploaded' WHERE id = :id"), {"id": photo_id})
    # Same transaction as the status change: the job exists exactly when the photo is
    # 'uploaded'. The dedupe key means a retried completion can't queue it twice.
    jobs.enqueue(conn, "process_photo", {"photo_id": photo_id, **job_payload}, dedupe_key=photo_id)
    return "uploaded"


def mark_failed(photo_id: str, error: str) -> None:
    """The worker gave up on this photo: record why, so it's shown, and can be retried."""
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE photos SET status = 'failed', processing_error = :error WHERE id = :id AND status <> 'ready'"),
            {"id": photo_id, "error": error[:2000]},
        )


def process_upload(photo_id: str, contents: bytes | None = None, regroup_now: bool = False) -> dict:
    """Process an uploaded original and mark it ready. Run by the worker, or directly by
    add_photo. Idempotent: running it again for a ready photo returns it unchanged, and
    derivative keys depend only on the id, so a rerun overwrites rather than duplicates.

    Raises DuplicatePhoto (after discarding this upload) if identical bytes are already stored.
    """
    started = time.perf_counter()
    with engine.begin() as conn:
        row = conn.execute(
            text(
                """
                UPDATE photos SET status = 'processing'
                WHERE id = :id AND status IN ('pending', 'uploaded', 'processing', 'failed')
                RETURNING storage_key, size_bytes, user_id
                """
            ),
            {"id": photo_id},
        ).first()
        if row is None:
            if conn.execute(text("SELECT 1 FROM photos WHERE id = :id"), {"id": photo_id}).first():
                return get_photo(None, photo_id)  # already ready: a rerun changes nothing
            raise UploadNotFound
    if contents is None:
        if objects.size(row.storage_key) != row.size_bytes:
            raise UploadNotReceived
        contents = objects.get(row.storage_key)
    # The hash of the bytes actually stored, not the one the client declared.
    sha256 = hashlib.sha256(contents).hexdigest()
    user_id = str(row.user_id)
    with engine.connect() as conn:
        original = _duplicate_of(conn, user_id, sha256, photo_id)
    if original:
        _discard_upload(photo_id, row.storage_key)
        raise DuplicatePhoto(original)
    # Slow CPU work happens before taking the write lock, so photos still process in parallel.
    try:
        processed = images.process(contents)
        error = None
    except Exception as exc:  # any undecodable upload is stored, flagged, and skipped
        processed, error = None, f"{type(exc).__name__}: {exc}"
    process_ms = round((time.perf_counter() - started) * 1000)
    with engine.begin() as conn:
        lock_writes(conn, user_id)
        status = conn.execute(
            text("SELECT status FROM photos WHERE id = :id"), {"id": photo_id}
        ).scalar_one_or_none()
        if status is None:
            raise UploadNotFound  # swept as an orphan while we were processing
        if status == "ready":
            return get_photo(None, photo_id)  # a concurrent run finished first
        # Re-check under the lock: an identical file may have completed while we processed.
        original = _duplicate_of(conn, user_id, sha256, photo_id)
        if original is None:
            _store_processed(conn, user_id, photo_id, sha256, processed, error, process_ms, regroup_now)
    if original:
        _discard_upload(photo_id, row.storage_key)
        raise DuplicatePhoto(original)
    total_ms = round((time.perf_counter() - started) * 1000)
    log.info(
        "photo ready id=%s bytes=%d process_ms=%d total_ms=%d error=%s",
        photo_id, len(contents), process_ms, total_ms, error,
    )
    return get_photo(None, photo_id)


def _store_processed(
    conn: Connection, user_id: str, photo_id: str, sha256: str, processed, error: str | None,
    process_ms: int, regroup_now: bool,
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
    # Place and time are cheap and per photo. Regrouping trips looks at every photo, so in
    # an import it waits for one coalesced job instead of running after each photo.
    resolve_places(conn)
    resolve_times(conn, user_id)
    if regroup_now:
        regroup_trips(conn, user_id)
    else:
        jobs.enqueue(conn, "regroup_trips", {"user_id": user_id}, dedupe_key=user_id)


def resolve_times(conn: Connection, user_id: str) -> int:
    """Work out times for all of one user's photos (a photo's zone can come from the
    owner's other photos, never from someone else's)."""
    rows = conn.execute(
        text(
            """
            SELECT id, camera_make, camera_model, camera_serial, camera_time,
                   exif_offset_min, gps_time, lat, lon,
                   shot_at, tz_name, local_offset_min, time_source
            FROM photos WHERE user_id = :user
            """
        ),
        {"user": user_id},
    ).all()
    offsets = [
        timeresolve.ClockOffset(
            (o.camera_make, o.camera_model, o.camera_serial),
            o.offset_seconds, o.valid_from, o.valid_to,
        )
        for o in conn.execute(
            text(
                "SELECT camera_make, camera_model, camera_serial, offset_seconds, "
                "valid_from, valid_to FROM camera_clock_offsets WHERE user_id = :user ORDER BY created_at"
            ),
            {"user": user_id},
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


def regroup_trips(conn: Connection, user_id: str) -> None:
    """Regroup one user's photos into trips."""
    rows = conn.execute(
        text(
            """
            SELECT p.id, p.shot_at, p.lat, p.lon, p.trip_id,
                   p.trip_pinned OR coalesce(t.is_locked, false) AS pinned
            FROM photos p LEFT JOIN trips t ON t.id = p.trip_id
            WHERE p.user_id = :user
            """
        ),
        {"user": user_id},
    ).all()
    locked = [
        trips.LockedTrip(str(r.id), r.start, r.end)
        for r in conn.execute(
            text(
                """
                SELECT t.id, min(p.shot_at) AS start, max(p.shot_at) AS end
                FROM trips t JOIN photos p ON p.trip_id = t.id
                WHERE t.user_id = :user AND t.is_locked AND p.shot_at IS NOT NULL
                GROUP BY t.id
                """
            ),
            {"user": user_id},
        )
    ]
    homes = [
        trips.Home(r.lat, r.lon, r.radius_km)
        for r in conn.execute(text("SELECT lat, lon, radius_km FROM homes WHERE user_id = :user"), {"user": user_id})
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
        ref: str(conn.execute(text("INSERT INTO trips (user_id) VALUES (:user) RETURNING id"), {"user": user_id}).scalar_one())
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
        text(
            "DELETE FROM trips t WHERE t.user_id = :user "
            "AND NOT EXISTS (SELECT 1 FROM photos p WHERE p.trip_id = t.id)"
        ),
        {"user": user_id},
    )


def list_cameras(user_id: str) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT camera_make, camera_model, camera_serial, count(*) AS photo_count,
                       min(camera_time) AS first_camera_time, max(camera_time) AS last_camera_time
                FROM photos
                WHERE user_id = :user AND camera_make IS NOT NULL AND camera_model IS NOT NULL
                GROUP BY camera_make, camera_model, camera_serial
                ORDER BY photo_count DESC
                """
            ),
            {"user": user_id},
        )
        return [dict(r._mapping) for r in rows]


OFFSET_COLUMNS = "id, camera_make, camera_model, camera_serial, offset_seconds, valid_from, valid_to, note, created_at"


def list_offsets(user_id: str) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(f"SELECT {OFFSET_COLUMNS} FROM camera_clock_offsets WHERE user_id = :user ORDER BY created_at"),
            {"user": user_id},
        )
        return [dict(r._mapping) for r in rows]


class OffsetOverlapError(Exception):
    pass


def add_offset(user_id: str, values: dict, replace_overlapping: bool = False) -> dict:
    """Add a clock offset. With replace_overlapping, the camera's offsets covering any of the
    same camera-clock times are deleted first, in the same transaction: either the new
    offset replaces them, or (if anything fails) they're all still there."""
    try:
        values = {**values, "user": user_id}
        with engine.begin() as conn:
            lock_writes(conn, user_id)
            replaced = 0
            if replace_overlapping:
                # The same test as the exclusion constraint, so exactly the rows that would
                # block the insert are removed. NULL bounds make a range unbounded.
                replaced = conn.execute(
                    text(
                        """
                        DELETE FROM camera_clock_offsets
                        WHERE user_id = :user
                          AND camera_make = :camera_make AND camera_model = :camera_model
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
                    f"""
                    INSERT INTO camera_clock_offsets
                        (user_id, camera_make, camera_model, camera_serial, offset_seconds,
                         valid_from, valid_to, note)
                    VALUES (:user, :camera_make, :camera_model, :camera_serial, :offset_seconds,
                            :valid_from, :valid_to, :note)
                    RETURNING {OFFSET_COLUMNS}
                    """
                ),
                values,
            ).one()
            changed = resolve_times(conn, user_id)
            regroup_trips(conn, user_id)
    except IntegrityError as exc:
        if isinstance(exc.orig, psycopg.errors.ExclusionViolation):
            raise OffsetOverlapError from exc
        raise
    return {**dict(row._mapping), "photos_updated": changed, "replaced": replaced}


def delete_offset(user_id: str, offset_id: str) -> int | None:
    with engine.begin() as conn:
        lock_writes(conn, user_id)
        deleted = conn.execute(
            text("DELETE FROM camera_clock_offsets WHERE id = :id AND user_id = :user"),
            {"id": offset_id, "user": user_id},
        ).rowcount
        if not deleted:
            return None
        changed = resolve_times(conn, user_id)
        regroup_trips(conn, user_id)
        return changed
