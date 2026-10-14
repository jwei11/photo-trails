"""Try the demo: a throwaway account holding a copy of the demo trip, deleted after a day.

The demo trip belongs to one `demo` account, loaded once with `python -m app.demo load`.
Starting a demo creates a `guest` account and copies the demo's trips and photo rows
into it in one transaction. The copies point at the demo's own files in storage, so a new
guest costs a few dozen rows: no images are copied or processed. The guest can then do
anything an account can (rename, split, merge, share, import a few photos of their own),
within the limits in app.limits, without touching the demo or anyone else.

Expired guests are deleted by the worker's sweep (delete_expired). Deleting the account
deletes its rows; files it uploaded itself are then unreferenced, and the orphan sweep
removes them. The demo's files stay, since the demo account still refers to them.
"""
import json
import secrets
import uuid

from sqlalchemy import Connection, text

from app import auth, limits
from app.db import engine

GUEST_LOCK = 1_414_213


class DemoUnavailable(Exception):
    pass


class DemoFull(Exception):
    pass


class TooManyFromHere(Exception):
    pass


def demo_user_id(conn: Connection) -> str | None:
    found = conn.execute(text("SELECT id FROM users WHERE kind = 'demo'")).scalar_one_or_none()
    return str(found) if found else None


def demo_available() -> bool:
    with engine.connect() as conn:
        return demo_user_id(conn) is not None


def start(client_ip: str) -> auth.User:
    """A new guest with a copy of the demo trip. Serialized, so the caps hold exactly."""
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": GUEST_LOCK})
        demo = demo_user_id(conn)
        if demo is None:
            raise DemoUnavailable
        live = conn.execute(
            text("SELECT count(*) FROM users WHERE kind = 'guest' AND expires_at > now()")
        ).scalar_one()
        if live >= limits.GUESTS_MAX:
            raise DemoFull
        if limits.recent_count(conn, "guest", client_ip, limits.DAY) >= limits.GUESTS_PER_IP_PER_DAY:
            raise TooManyFromHere
        limits.record(conn, "guest", client_ip)
        row = conn.execute(
            text(
                """
                INSERT INTO users (email, kind, expires_at)
                VALUES (:email, 'guest', now() + :lifetime)
                RETURNING id, email, kind, expires_at
                """
            ),
            # .invalid is reserved: it can never be a real address, so it can't collide
            # with anyone signing in with Google.
            {"email": f"guest-{secrets.token_hex(8)}@demo.invalid", "lifetime": limits.GUEST_LIFETIME},
        ).one()
        copy_demo(conn, demo, str(row.id))
    return auth.User(str(row.id), row.email, row.kind, row.expires_at)


def _columns(conn: Connection, table: str, leave_out: set[str]) -> list[str]:
    """Every column a copy should carry over, so a column added later is copied too.
    Generated columns (a photo's location) compute themselves."""
    names = conn.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = :table AND is_generated = 'NEVER' "
            "ORDER BY ordinal_position"
        ),
        {"table": table},
    ).scalars()
    return [n for n in names if n not in leave_out]


def copy_demo(conn: Connection, demo_id: str, guest_id: str) -> None:
    """Copy the demo account's trips and ready photos to the guest, with new ids."""
    trip_ids = {
        str(t): str(uuid.uuid4())
        for t in conn.execute(text("SELECT id FROM trips WHERE user_id = :demo"), {"demo": demo_id}).scalars()
    }
    new_ids = json.dumps(trip_ids)
    trip_cols = _columns(conn, "trips", {"id", "user_id"})
    conn.execute(
        text(
            f"""
            INSERT INTO trips (id, user_id, {", ".join(trip_cols)})
            SELECT CAST(m.value AS uuid), :guest, {", ".join(f"t.{c}" for c in trip_cols)}
            FROM trips t JOIN jsonb_each_text(CAST(:ids AS jsonb)) m ON m.key = t.id::text
            """
        ),
        {"guest": guest_id, "ids": new_ids},
    )
    photo_cols = _columns(conn, "photos", {"id", "user_id", "trip_id", "from_demo"})
    conn.execute(
        text(
            f"""
            INSERT INTO photos (user_id, trip_id, from_demo, {", ".join(photo_cols)})
            SELECT :guest, CAST(m.value AS uuid), true, {", ".join(f"p.{c}" for c in photo_cols)}
            FROM photos p LEFT JOIN jsonb_each_text(CAST(:ids AS jsonb)) m ON m.key = p.trip_id::text
            WHERE p.user_id = :demo AND p.status = 'ready'
            """
        ),
        {"guest": guest_id, "demo": demo_id, "ids": new_ids},
    )


def delete_expired() -> dict:
    """Run by the worker's sweep: expired guests (and with them everything they had),
    spent sessions and sign-in links, and rate-limit records older than any window."""
    with engine.begin() as conn:
        guests = conn.execute(
            text("DELETE FROM users WHERE kind = 'guest' AND expires_at <= now()")
        ).rowcount
        conn.execute(text("DELETE FROM sessions WHERE expires_at <= now()"))
        conn.execute(text("DELETE FROM login_links WHERE expires_at <= now()"))
        conn.execute(
            text("DELETE FROM rate_limit_events WHERE created_at <= now() - :window"), {"window": limits.DAY}
        )
    return {"expired_guests": guests}
