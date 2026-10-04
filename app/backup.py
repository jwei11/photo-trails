"""Database backups, kept in the photo bucket under backups/.

pg_dump runs in the database container; this moves its output to and from the bucket,
using the app's own S3 credentials. On the server, deploy/backup.sh runs nightly:

    pg_dump ... > dump  &&  python -m app.backup upload < dump  &&  python -m app.backup prune
    python -m app.backup list
    python -m app.backup download <key> > dump      (then pg_restore: see docs/deploy.md)
"""
import argparse
import sys
from datetime import datetime, timedelta, timezone
from typing import BinaryIO

from app import config, objects

PREFIX = "backups/"
KEEP_DAYS = 30
KEEP_AT_LEAST = 7
# pg_dump's custom format (-Fc) starts with this. Checking it stops an empty or failed
# dump (say, pg_dump couldn't connect) from being stored as if it were a backup.
DUMP_MAGIC = b"PGDMP"


class NotADump(Exception):
    pass


def upload(stream: BinaryIO, now: datetime | None = None) -> str:
    stream = stream if hasattr(stream, "peek") else open(stream.fileno(), "rb", closefd=False)
    if stream.peek(len(DUMP_MAGIC))[: len(DUMP_MAGIC)] != DUMP_MAGIC:
        raise NotADump("The input isn't a pg_dump custom-format (-Fc) dump.")
    now = now or datetime.now(timezone.utc)
    key = f"{PREFIX}{now:%Y-%m-%dT%H%M%SZ}.dump"
    # Streams in parts (multipart upload): the dump never has to fit in memory.
    objects._internal().upload_fileobj(stream, config.S3_BUCKET, key)
    return key


def list_backups() -> list[dict]:
    pages = objects._internal().get_paginator("list_objects_v2").paginate(
        Bucket=config.S3_BUCKET, Prefix=PREFIX
    )
    found = [
        {"key": o["Key"], "size": o["Size"], "modified": o["LastModified"]}
        for page in pages for o in page.get("Contents", [])
    ]
    return sorted(found, key=lambda b: b["modified"], reverse=True)


def download(key: str, out: BinaryIO) -> None:
    if not key.startswith(PREFIX):
        raise ValueError(f"Backups are under {PREFIX}")
    objects._internal().download_fileobj(config.S3_BUCKET, key, out)


def prune(now: datetime | None = None, keep_days: int = KEEP_DAYS, keep_at_least: int = KEEP_AT_LEAST) -> list[str]:
    """Delete backups older than keep_days, but never the newest keep_at_least: if backups
    stop working for a month, the last good ones are still there."""
    now = now or datetime.now(timezone.utc)
    old = [b["key"] for b in list_backups()[keep_at_least:] if b["modified"] < now - timedelta(days=keep_days)]
    if old:
        objects.delete(*old)
    return old


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("upload", help="store a dump read from stdin")
    commands.add_parser("list", help="list stored backups, newest first")
    get = commands.add_parser("download", help="write a stored backup to stdout")
    get.add_argument("key")
    commands.add_parser("prune", help=f"delete backups older than {KEEP_DAYS} days (keeping {KEEP_AT_LEAST})")
    args = parser.parse_args(argv)

    if args.command == "upload":
        try:
            print(upload(sys.stdin.buffer))
        except NotADump as exc:
            raise SystemExit(str(exc))
    elif args.command == "list":
        for b in list_backups():
            print(f"{b['key']}  {b['size'] / 1e6:.1f} MB  {b['modified']:%Y-%m-%d %H:%M}")
    elif args.command == "download":
        download(args.key, sys.stdout.buffer)
    elif args.command == "prune":
        print(f"deleted {len(prune())} old backups")


if __name__ == "__main__":
    main(sys.argv[1:])
