"""Copy stored photos from the local SeaweedFS bucket into the app's configured bucket.

Run: python -m app.copy_storage [--dry-run] [--from-endpoint URL] [--from-bucket NAME]

Objects keep their keys, so database rows stay valid with no migration. An object
already at the destination with the same size is skipped, so an interrupted copy can
simply be run again. Nothing is ever deleted, from either side.
"""
import argparse
import logging
import os

from sqlalchemy import text

from app import config, objects
from app.db import engine

log = logging.getLogger(__name__)


def sizes(s3, bucket: str) -> dict[str, int]:
    pages = s3.get_paginator("list_objects_v2").paginate(Bucket=bucket)
    return {o["Key"]: o["Size"] for page in pages for o in page.get("Contents", [])}


def copy_all(source, source_bucket: str, dest, dest_bucket: str, dry_run: bool = False) -> dict:
    present = sizes(dest, dest_bucket)
    copied = skipped = copied_bytes = 0
    for key, size in sorted(sizes(source, source_bucket).items()):
        if present.get(key) == size:
            skipped += 1
            continue
        if not dry_run:
            obj = source.get_object(Bucket=source_bucket, Key=key)
            # Streams from one store to the other: the file is never held whole in memory.
            dest.upload_fileobj(obj["Body"], dest_bucket, key, ExtraArgs={"ContentType": obj["ContentType"]})
        present[key] = size
        copied += 1
        copied_bytes += size
    return {"copied": copied, "skipped": skipped, "bytes": copied_bytes, "keys_at_destination": set(present)}


def referenced_keys() -> set[str]:
    """Every object key the database points at: originals, thumbnails, web copies."""
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT storage_key, thumb_key, web_key FROM photos WHERE status = 'ready'"))
        return {key for row in rows for key in row if key}


def main(from_endpoint: str, from_bucket: str, dry_run: bool) -> dict:
    if from_endpoint == config.S3_ENDPOINT and from_bucket == config.S3_BUCKET:
        raise SystemExit("Source and destination are the same bucket: point the app at the new one first.")
    source = objects.client(
        from_endpoint, os.environ["SEAWEEDFS_ACCESS_KEY"], os.environ["SEAWEEDFS_SECRET_KEY"], "us-east-1"
    )
    result = copy_all(source, from_bucket, objects._internal(), config.S3_BUCKET, dry_run)
    # The check that matters: after the copy, can every photo's files be found?
    missing = sorted(referenced_keys() - result.pop("keys_at_destination"))
    return {**result, "missing": len(missing), "missing_sample": missing[:10], "dry_run": dry_run,
            "to": f"{config.S3_ENDPOINT or 'Amazon S3'} / {config.S3_BUCKET}"}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="report what would be copied, copy nothing")
    parser.add_argument("--from-endpoint", default="http://localhost:8333")
    parser.add_argument("--from-bucket", default="photos")
    args = parser.parse_args()
    print(main(args.from_endpoint, args.from_bucket, args.dry_run))
