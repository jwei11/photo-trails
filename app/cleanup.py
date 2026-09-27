"""Remove upload orphans. Run: python -m app.cleanup [--dry-run]"""
import argparse
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app import objects
from app.db import engine

log = logging.getLogger(__name__)

# Must comfortably exceed the upload URL lifetime, and the time a completion takes, so the
# sweep never races an upload or completion that's still legitimately in progress.
DEFAULT_MAX_AGE = timedelta(hours=1)


def sweep(max_age: timedelta = DEFAULT_MAX_AGE, dry_run: bool = False) -> dict:
    # Each timestamp is compared against the clock that wrote it: the database's for rows,
    # ours for bucket objects. The Docker VM's clock can differ from the host's.
    cutoff = datetime.now(timezone.utc) - max_age

    with engine.begin() as conn:
        stale = conn.execute(
            text(
                "SELECT id, storage_key FROM photos WHERE status = 'pending' "
                "AND created_at <= now() - make_interval(secs => :max_age)"
            ),
            {"max_age": max_age.total_seconds()},
        ).all()
        if stale and not dry_run:
            # Re-check status in the DELETE: a completion may have finished since the SELECT.
            stale = conn.execute(
                text(
                    "DELETE FROM photos WHERE id = ANY(:ids) AND status = 'pending' "
                    "RETURNING id, storage_key"
                ),
                {"ids": [r.id for r in stale]},
            ).all()
        referenced = {
            key
            for row in conn.execute(text("SELECT storage_key, thumb_key, web_key FROM photos"))
            for key in row
            if key
        }

    pending_keys = [k for r in stale for k in (r.storage_key, f"{r.id}_thumb.webp", f"{r.id}_web.webp")]
    unreferenced = [
        key for key, modified in objects.list_keys()
        if key not in referenced and key not in pending_keys and modified < cutoff
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
