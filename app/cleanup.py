"""Remove upload orphans. Run: python -m app.cleanup [--dry-run]"""
import argparse
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app import backup, objects
from app.db import engine

log = logging.getLogger(__name__)

# Age is measured from the last upload URL issued for a pending photo (URLs expire after
# 15 minutes, and a client that wants to upload gets a fresh one). One hour comfortably
# exceeds a URL's lifetime plus a completion, so the sweep never races an active upload.
DEFAULT_MAX_AGE = timedelta(hours=1)


def sweep(max_age: timedelta = DEFAULT_MAX_AGE, dry_run: bool = False) -> dict:
    # Each timestamp is compared against the clock that wrote it: the database's for rows,
    # ours for bucket objects. The Docker VM's clock can differ from the host's.
    cutoff = datetime.now(timezone.utc) - max_age

    with engine.begin() as conn:
        stale = conn.execute(
            text(
                "SELECT id, storage_key FROM photos WHERE status = 'pending' "
                "AND coalesce(upload_url_issued_at, created_at) <= now() - make_interval(secs => :max_age)"
            ),
            {"max_age": max_age.total_seconds()},
        ).all()
        if stale and not dry_run:
            # Re-check in the DELETE: since the SELECT, a completion may have finished or a
            # fresh upload URL may have been issued.
            stale = conn.execute(
                text(
                    "DELETE FROM photos WHERE id = ANY(:ids) AND status = 'pending' "
                    "AND coalesce(upload_url_issued_at, created_at) <= now() - make_interval(secs => :max_age) "
                    "RETURNING id, storage_key"
                ),
                {"ids": [r.id for r in stale], "max_age": max_age.total_seconds()},
            ).all()
        referenced = {
            key
            for row in conn.execute(text("SELECT storage_key, thumb_key, web_key FROM photos"))
            for key in row
            if key
        }

    pending_keys = [k for r in stale for k in (r.storage_key, f"{r.id}_thumb.webp", f"{r.id}_web.webp")]
    # Database backups share the bucket but aren't photos: never orphans.
    unreferenced = [
        key for key, modified in objects.list_keys()
        if key not in referenced and key not in pending_keys and modified < cutoff
        and not key.startswith(backup.PREFIX)
    ]
    if not dry_run:
        objects.delete(*pending_keys, *unreferenced)

    result = {"abandoned_uploads": len(stale), "unreferenced_objects": len(unreferenced), "dry_run": dry_run}
    log.info("cleanup %s", result)
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report what would be deleted")
    parser.add_argument("--max-age-minutes", type=int, default=int(DEFAULT_MAX_AGE.total_seconds() // 60))
    args = parser.parse_args()
    print(sweep(timedelta(minutes=args.max_age_minutes), dry_run=args.dry_run))
