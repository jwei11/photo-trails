from datetime import datetime, timedelta

from sqlalchemy import Connection, text

from app import storage, trips
from app.db import engine


class NotFound(Exception):
    pass


class InvalidEdit(Exception):
    pass


def list_trips(user_id: str) -> list[dict]:
    with engine.connect() as conn:
        return _trips_with_stats(conn, user_id)


def get_trip(user_id: str, trip_id: str) -> dict:
    with engine.connect() as conn:
        found = [t for t in _trips_with_stats(conn, user_id, trip_id) if t["id"] == trip_id]
    if not found:
        raise NotFound
    return found[0]


def _trips_with_stats(conn: Connection, user_id: str, only: str | None = None) -> list[dict]:
    rows = conn.execute(
        text(
            """
            SELECT t.id, t.name, t.is_locked, p.id AS photo_id, p.shot_at, p.lat, p.lon,
                   p.local_offset_min, p.place_id, pl.name AS place_name, p.country_code,
                   (SELECT name FROM countries WHERE iso_a2 = p.country_code LIMIT 1)
                       AS country_name
            FROM trips t JOIN photos p ON p.trip_id = t.id
            LEFT JOIN places pl ON pl.geonameid = p.place_id
            WHERE t.user_id = :user AND (CAST(:only AS uuid) IS NULL OR t.id = CAST(:only AS uuid))
            """
        ),
        {"user": user_id, "only": only},
    ).all()
    by_trip: dict[str, dict] = {}
    for r in rows:
        trip = by_trip.setdefault(
            str(r.id), {"id": str(r.id), "name": r.name, "is_locked": r.is_locked, "photos": []}
        )
        trip["photos"].append(r)

    result = []
    for trip in by_trip.values():
        ps = trip.pop("photos")
        stats = trips.trip_stats(
            trips.TripPhoto(str(p.photo_id), p.shot_at, p.lat, p.lon) for p in ps
        )
        def local_day(p):
            return (p.shot_at + timedelta(minutes=p.local_offset_min)).date() if p.shot_at else None

        local_days = {local_day(p) for p in ps if p.shot_at is not None}
        places = trips.place_summary(
            trips.PlacedPhoto(
                p.shot_at, local_day(p), p.place_id, p.place_name, p.country_code, p.country_name
            )
            for p in ps
        )
        result.append({
            **trip,
            "start": stats.start,
            "end": stats.end,
            "day_count": len(local_days),
            "photo_count": stats.photo_count,
            "distance_km": stats.distance_km,
            "bbox": stats.bbox,
            **places,
        })
    result.sort(key=lambda t: (t["start"] is None, t["start"] or datetime.min))
    return result


def _require_trips(conn: Connection, user_id: str, ids: list[str]) -> None:
    """Every id is a trip of this user's. Another user's trip is "not found", exactly like
    one that doesn't exist, so trying ids reveals nothing."""
    found = conn.execute(
        text("SELECT count(*) FROM trips WHERE user_id = :user AND id = ANY(CAST(:ids AS uuid[]))"),
        {"user": user_id, "ids": ids},
    ).scalar_one()
    if found != len(set(ids)):
        raise NotFound


def _lock_trips(conn: Connection, ids: list[str]) -> None:
    conn.execute(
        text("UPDATE trips SET is_locked = true WHERE id = ANY(CAST(:ids AS uuid[]))"), {"ids": ids}
    )


def rename_trip(user_id: str, trip_id: str, name: str | None) -> dict:
    with engine.begin() as conn:
        storage.lock_writes(conn, user_id)
        _require_trips(conn, user_id, [trip_id])
        conn.execute(
            text("UPDATE trips SET name = :name, is_locked = true WHERE id = :id"),
            {"id": trip_id, "name": name},
        )
        storage.regroup_trips(conn, user_id)
    return get_trip(user_id, trip_id)


def merge_trips(user_id: str, target_id: str, other_ids: list[str]) -> dict:
    others = [i for i in other_ids if i != target_id]
    if not others:
        raise InvalidEdit("Choose at least one other trip to merge in.")
    with engine.begin() as conn:
        storage.lock_writes(conn, user_id)
        _require_trips(conn, user_id, [target_id, *others])
        conn.execute(
            text("UPDATE photos SET trip_id = :target WHERE trip_id = ANY(CAST(:others AS uuid[]))"),
            {"target": target_id, "others": others},
        )
        conn.execute(text("DELETE FROM trips WHERE id = ANY(CAST(:others AS uuid[]))"), {"others": others})
        _lock_trips(conn, [target_id])
        storage.regroup_trips(conn, user_id)
    return get_trip(user_id, target_id)


def split_trip(user_id: str, trip_id: str, at: datetime) -> tuple[dict, dict]:
    with engine.begin() as conn:
        storage.lock_writes(conn, user_id)
        _require_trips(conn, user_id, [trip_id])
        before, after = conn.execute(
            text(
                """
                SELECT count(*) FILTER (WHERE shot_at < :at OR shot_at IS NULL),
                       count(*) FILTER (WHERE shot_at >= :at)
                FROM photos WHERE trip_id = :id
                """
            ),
            {"id": trip_id, "at": at},
        ).one()
        if not before or not after:
            raise InvalidEdit("The split point must leave photos on both sides.")
        new_id = str(
            conn.execute(
                text("INSERT INTO trips (user_id, is_locked) VALUES (:user, true) RETURNING id"),
                {"user": user_id},
            ).scalar_one()
        )
        conn.execute(
            text("UPDATE photos SET trip_id = :new WHERE trip_id = :id AND shot_at >= :at"),
            {"id": trip_id, "new": new_id, "at": at},
        )
        _lock_trips(conn, [trip_id])
        storage.regroup_trips(conn, user_id)
    return get_trip(user_id, trip_id), get_trip(user_id, new_id)


def dissolve_trip(user_id: str, trip_id: str) -> None:
    with engine.begin() as conn:
        storage.lock_writes(conn, user_id)
        _require_trips(conn, user_id, [trip_id])
        conn.execute(
            text("UPDATE photos SET trip_id = NULL, trip_pinned = true WHERE trip_id = :id"),
            {"id": trip_id},
        )
        conn.execute(text("DELETE FROM trips WHERE id = :id"), {"id": trip_id})
        storage.regroup_trips(conn, user_id)


def move_photo(user_id: str, photo_id: str, trip_id: str | None) -> None:
    with engine.begin() as conn:
        storage.lock_writes(conn, user_id)
        previous = conn.execute(
            text("SELECT trip_id FROM photos WHERE id = :id AND user_id = :user"),
            {"id": photo_id, "user": user_id},
        ).first()
        if previous is None:
            raise NotFound
        if trip_id:
            _require_trips(conn, user_id, [trip_id])
        conn.execute(
            text("UPDATE photos SET trip_id = :trip, trip_pinned = true WHERE id = :id"),
            {"id": photo_id, "trip": trip_id},
        )
        _lock_trips(conn, [str(t) for t in (previous.trip_id, trip_id) if t])
        storage.regroup_trips(conn, user_id)


HOME_COLUMNS = "id, name, lat, lon, radius_km, created_at"


def list_homes(user_id: str) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(f"SELECT {HOME_COLUMNS} FROM homes WHERE user_id = :user ORDER BY created_at"),
            {"user": user_id},
        )
        return [dict(r._mapping) for r in rows]


def add_home(user_id: str, values: dict) -> dict:
    with engine.begin() as conn:
        storage.lock_writes(conn, user_id)
        row = conn.execute(
            text(
                "INSERT INTO homes (user_id, name, lat, lon, radius_km) "
                f"VALUES (:user, :name, :lat, :lon, :radius_km) RETURNING {HOME_COLUMNS}"
            ),
            {**values, "user": user_id},
        ).one()
        storage.regroup_trips(conn, user_id)
    return dict(row._mapping)


def delete_home(user_id: str, home_id: str) -> None:
    with engine.begin() as conn:
        storage.lock_writes(conn, user_id)
        deleted = conn.execute(
            text("DELETE FROM homes WHERE id = :id AND user_id = :user"), {"id": home_id, "user": user_id}
        ).rowcount
        if not deleted:
            raise NotFound
        storage.regroup_trips(conn, user_id)
