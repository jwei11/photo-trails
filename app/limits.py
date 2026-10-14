"""What accounts that weren't invited may use, so strangers can't exhaust one small server.

Invited accounts (and the demo account) have no limits beyond the 200 MB per-file cap.
Self sign-ups and demo guests get a photo quota and a smaller per-file cap; guests'
background work runs after everyone else's; the number of guests and sign-ups is capped;
and each IP address may start only a few demos a day.
"""
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import Connection, text

MB = 1024 * 1024

# Live guest accounts at once, and sign-ups ever. 0 sign-ups means invite-only.
GUESTS_MAX = int(os.environ.get("GUESTS_MAX", "50"))
SIGNUPS_MAX = int(os.environ.get("SIGNUPS_MAX", "0"))

GUEST_LIFETIME = timedelta(hours=24)
GUESTS_PER_IP_PER_DAY = 5
DAY = timedelta(days=1)


@dataclass(frozen=True)
class Quota:
    photos: int  # photos the account has uploaded itself (a guest's demo copies don't count)
    file_bytes: int


QUOTAS = {
    "signup": Quota(photos=200, file_bytes=50 * MB),
    "guest": Quota(photos=30, file_bytes=25 * MB),
}


class QuotaExceeded(Exception):
    """Shown to the person as is."""


def check_upload(conn: Connection, user_id: str, kind: str, sizes: list[int]) -> None:
    """Refuse a batch of new files that would take the account over its quota. Run in the
    transaction that creates their rows, after taking the user's write lock, so two batches
    started at once can't both fit in the last free space."""
    quota = QUOTAS.get(kind)
    if quota is None or not sizes:
        return
    if max(sizes) > quota.file_bytes:
        raise QuotaExceeded(f"Files over {quota.file_bytes // MB} MB can't be uploaded on this account.")
    used = conn.execute(
        text("SELECT count(*) FROM photos WHERE user_id = :user AND NOT from_demo AND status <> 'failed'"),
        {"user": user_id},
    ).scalar_one()
    if used + len(sizes) > quota.photos:
        left = max(quota.photos - used, 0)
        raise QuotaExceeded(
            f"This account can hold {quota.photos} photos of its own, and has room for {left} more."
        )


def job_priority(conn: Connection, user_id: str) -> int:
    """Background work for invited accounts runs first; guests' and sign-ups' after."""
    kind = conn.execute(text("SELECT kind FROM users WHERE id = :user"), {"user": user_id}).scalar_one_or_none()
    return 0 if kind in ("invited", "demo", None) else 1


# Rate limits are counted by a keyed hash of the IP address. The key is random for each
# process: a hash of an IPv4 address alone could be reversed by trying all 4 billion of
# them, and this way nothing stored can be. A restart forgets the counts, which only
# loosens a limit that the hard cap on live guests backs up anyway.
_RATE_KEY = secrets.token_bytes(32)


def _key_hash(key: str) -> bytes:
    return hmac.new(_RATE_KEY, key.encode(), hashlib.sha256).digest()


def recent_count(conn: Connection, action: str, key: str, window: timedelta) -> int:
    return conn.execute(
        text(
            "SELECT count(*) FROM rate_limit_events "
            "WHERE action = :action AND key_hash = :key AND created_at > now() - :window"
        ),
        {"action": action, "key": _key_hash(key), "window": window},
    ).scalar_one()


def record(conn: Connection, action: str, key: str) -> None:
    conn.execute(
        text("INSERT INTO rate_limit_events (action, key_hash) VALUES (:action, :key)"),
        {"action": action, "key": _key_hash(key)},
    )
