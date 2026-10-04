"""Share links: a read-only view of one trip, for anyone with the link, and what it reveals.

A share never publishes what the owner's own pages show. The public view is built here,
and only from what's safe:

- Where: every position is snapped. 'city' (the default) puts a photo at the point of
  the town or city it's in (found with the same rule as a photo's place, but skipping
  GeoNames' sections of cities, like "Baixa", so it's never a neighbourhood);
  'neighbourhood' at the centre of the ~1 km hexagon it falls in, named by its place. Snapping always moves a spot to the same point, unlike random
  noise, which averages out: fifty jittered photos taken in one flat still centre on it.
- Not near home: photos within any of the owner's homes' radius are left out entirely.
- When: by default, a month ("July 2025") and day numbers ("day 2, 14:30"), not dates.
- Not now: a trip can only be shared 7 days after its last photo, and photos from the
  last 7 days are never shown, so a link never says "away right now".
- Nothing else: no filenames, cameras, EXIF, or exact coordinates, and images only as the
  derivatives, which are re-encoded without metadata. Never the original file.
"""
import secrets
from datetime import date, datetime, timedelta, timezone

import h3
from sqlalchemy import Connection, text

from app import storage, trips
from app.db import engine

PRECISIONS = ("city", "neighbourhood")
NEIGHBOURHOOD_H3_RES = 8  # hexagons about 0.9 km across
SHARE_DELAY = timedelta(days=7)


class NotFound(Exception):
    pass


class TooRecent(Exception):
    def __init__(self, shareable_from: datetime):
        super().__init__(shareable_from)
        self.shareable_from = shareable_from


SHARE_COLUMNS = "id, token, precision, exact_dates, created_at, revoked_at"


def _share_dict(row) -> dict:
    share = dict(row._mapping)
    share["id"] = str(share["id"])
    share["path"] = f"/s/{share['token']}"
    return share


def _trip_photos(conn: Connection, user_id: str, trip_id: str) -> list:
    return conn.execute(
        text(
            """
            SELECT p.id, p.shot_at, p.local_offset_min, p.lat, p.lon, p.thumb_key, p.web_key,
                   p.place_id, pl.name AS place_name, p.country_code,
                   (SELECT name FROM countries WHERE iso_a2 = p.country_code LIMIT 1) AS country_name,
                   city.geonameid AS city_id, city.name AS city_name, city.lat AS city_lat, city.lon AS city_lon
            FROM photos p LEFT JOIN places pl ON pl.geonameid = p.place_id
            -- The town or city: the place the photo is deepest inside (see storage), else
            -- the nearest, among places that aren't sections of a city (PPLX).
            LEFT JOIN LATERAL (
                SELECT geonameid, name, lat, lon FROM (
                    SELECT c.geonameid, c.name, c.lat, c.lon,
                           ST_Distance(c.location, p.location) AS d,
                           :per_sqrt * sqrt(greatest(c.population, 1)) AS r
                    FROM places c
                    WHERE c.feature_code <> 'PPLX' AND ST_DWithin(c.location, p.location, :search_limit)
                ) candidates
                ORDER BY d <= r DESC, CASE WHEN d <= r THEN d / r ELSE d END
                LIMIT 1
            ) city ON p.location IS NOT NULL
            WHERE p.user_id = :user AND p.trip_id = :trip AND p.status = 'ready'
            ORDER BY p.shot_at NULLS LAST, p.created_at
            """
        ),
        {"user": user_id, "trip": trip_id, "per_sqrt": storage.PLACE_RADIUS_M_PER_SQRT_PERSON,
         "search_limit": storage.PLACE_SEARCH_LIMIT_M},
    ).all()


def _homes(conn: Connection, user_id: str) -> list[trips.Home]:
    return [
        trips.Home(r.lat, r.lon, r.radius_km)
        for r in conn.execute(text("SELECT lat, lon, radius_km FROM homes WHERE user_id = :user"), {"user": user_id})
    ]


def _near_home(p, homes: list[trips.Home]) -> bool:
    return p.lat is not None and any(
        trips.haversine_km(p.lat, p.lon, h.lat, h.lon) <= h.radius_km for h in homes
    )


def _recent(p, now: datetime) -> bool:
    return p.shot_at is not None and p.shot_at > now - SHARE_DELAY


def _shareable_from(photos: list) -> datetime | None:
    """When the trip may first be shared: 7 days after its last photo."""
    dated = [p.shot_at for p in photos if p.shot_at]
    return max(dated) + SHARE_DELAY if dated else None


# ---- The owner's side ----------------------------------------------------------------


def sharing(user_id: str, trip_id: str) -> dict:
    """The trip's shares, and what a share would leave out."""
    with engine.connect() as conn:
        _require_trip(conn, user_id, trip_id)
        photos = _trip_photos(conn, user_id, trip_id)
        homes = _homes(conn, user_id)
        shares = conn.execute(
            text(f"SELECT {SHARE_COLUMNS} FROM shares WHERE trip_id = :trip AND user_id = :user ORDER BY created_at"),
            {"trip": trip_id, "user": user_id},
        ).all()
    now = datetime.now(timezone.utc)
    ready = _shareable_from(photos)
    return {
        "shares": [_share_dict(s) for s in shares],
        "can_share": ready is not None and ready <= now,
        "has_homes": bool(homes),
        "hidden_near_home": sum(_near_home(p, homes) for p in photos),
        "hidden_recent": sum(_recent(p, now) for p in photos),
        "shareable_from": ready,
    }


def create_share(user_id: str, trip_id: str, precision: str = "city", exact_dates: bool = False) -> dict:
    if precision not in PRECISIONS:
        raise ValueError(precision)
    with engine.begin() as conn:
        _require_trip(conn, user_id, trip_id)
        ready = _shareable_from(_trip_photos(conn, user_id, trip_id))
        if ready is None or ready > datetime.now(timezone.utc):
            raise TooRecent(ready)
        row = conn.execute(
            text(
                f"""
                INSERT INTO shares (user_id, trip_id, token, precision, exact_dates)
                VALUES (:user, :trip, :token, :precision, :exact_dates)
                RETURNING {SHARE_COLUMNS}
                """
            ),
            {
                "user": user_id, "trip": trip_id, "precision": precision, "exact_dates": exact_dates,
                # 192 random bits: unguessable, and not derived from anything (unlike an id).
                "token": secrets.token_urlsafe(24),
            },
        ).one()
    return _share_dict(row)


def revoke_share(user_id: str, share_id: str) -> None:
    with engine.begin() as conn:
        found = conn.execute(
            text(
                "UPDATE shares SET revoked_at = coalesce(revoked_at, now()) "
                "WHERE id = :id AND user_id = :user"
            ),
            {"id": share_id, "user": user_id},
        ).rowcount
    if not found:
        raise NotFound


def _require_trip(conn: Connection, user_id: str, trip_id: str) -> None:
    if not conn.execute(
        text("SELECT 1 FROM trips WHERE id = :trip AND user_id = :user"), {"trip": trip_id, "user": user_id}
    ).first():
        raise NotFound


# ---- The public side -------------------------------------------------------------------


def public_view(token: str) -> dict:
    """What anyone with the link sees. NotFound for an unknown or revoked link: the two
    look the same, so a revoked link reveals nothing either."""
    with engine.connect() as conn:
        share = conn.execute(
            text(
                """
                SELECT s.user_id, s.trip_id, s.precision, s.exact_dates, t.name
                FROM shares s JOIN trips t ON t.id = s.trip_id
                WHERE s.token = :token AND s.revoked_at IS NULL
                """
            ),
            {"token": token},
        ).first()
        if share is None:
            raise NotFound
        photos = _trip_photos(conn, str(share.user_id), str(share.trip_id))
        homes = _homes(conn, str(share.user_id))
    now = datetime.now(timezone.utc)
    shown = [p for p in photos if not _near_home(p, homes) and not _recent(p, now)]
    return _view(share.name, shown, share.precision, share.exact_dates)


def public_photo_key(token: str, photo_id: str, kind: str) -> str:
    """The storage key of a shown photo's derivative. A photo the view leaves out (near a
    home, too recent) is NotFound here too, even to someone who knows its id."""
    assert kind in ("thumb", "web")  # a column name, so never from the request as is
    view = public_view(token)
    for p in view["photos"]:
        if p["id"] == photo_id:
            with engine.connect() as conn:
                key = conn.execute(
                    text(f"SELECT {kind}_key FROM photos WHERE id = :id"), {"id": photo_id}
                ).scalar_one_or_none()
            if key:
                return key
    raise NotFound


def _view(name: str | None, photos: list, precision: str, exact_dates: bool) -> dict:
    def local(p) -> datetime | None:
        return (p.shot_at + timedelta(minutes=p.local_offset_min)).replace(tzinfo=None) if p.shot_at else None

    def place(p) -> tuple[int | None, str | None]:
        return (p.city_id, p.city_name) if precision == "city" else (p.place_id, p.place_name)

    first_day = min((local(p).date() for p in photos if p.shot_at), default=None)
    out = []
    for p in photos:
        lat, lon = _snap(p, precision)
        when = local(p)
        item = {
            "id": str(p.id),
            "lat": lat,
            "lon": lon,
            "place_name": place(p)[1],
            "country_name": p.country_name,
            "has_preview": p.thumb_key is not None,
            # Day 1 is the trip's first local day.
            "day": (when.date() - first_day).days + 1 if when else None,
            "time": when.strftime("%H:%M") if when else None,
        }
        if exact_dates:
            item["local_time"] = when.isoformat() if when else None
        out.append(item)

    located = [(o["lat"], o["lon"]) for o in out if o["lat"] is not None and o["day"] is not None]
    distance = sum(trips.haversine_km(*a, *b) for a, b in zip(located, located[1:]))
    local_days = [local(p).date() for p in photos if p.shot_at]
    summary = trips.place_summary(
        trips.PlacedPhoto(p.shot_at, local(p).date() if p.shot_at else None, *place(p),
                          p.country_code, p.country_name)
        for p in photos
    )
    return {
        "name": name,
        "precision": precision,
        "exact_dates": exact_dates,
        "period": _period(min(local_days), max(local_days)) if local_days else None,
        "start": min(local_days).isoformat() if exact_dates and local_days else None,
        "end": max(local_days).isoformat() if exact_dates and local_days else None,
        "day_count": len(set(local_days)),
        "photo_count": len(out),
        "distance_km": round(distance),
        **summary,
        "photos": out,
    }


def _snap(p, precision: str) -> tuple[float | None, float | None]:
    if p.lat is None:
        return None, None
    if precision == "neighbourhood":
        return h3.cell_to_latlng(h3.latlng_to_cell(p.lat, p.lon, NEIGHBOURHOOD_H3_RES))
    # City: the town's own point. A photo with no town within 50 km (or no place data
    # loaded) gets no position at all, rather than its real one.
    if p.city_lat is None:
        return None, None
    return p.city_lat, p.city_lon


def _period(first: date, last: date) -> str:
    """"July 2025", "July – August 2025", or "December 2025 – January 2026"."""
    if (first.year, first.month) == (last.year, last.month):
        return first.strftime("%B %Y")
    if first.year == last.year:
        return f"{first:%B} – {last:%B %Y}"
    return f"{first:%B %Y} – {last:%B %Y}"
