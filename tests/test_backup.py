"""Database backups in the bucket: stored, listed, fetched, pruned, and never swept up as
orphan photos."""
import io
from datetime import datetime, timedelta, timezone

import pytest

from app import backup, cleanup, objects
from tests.test_imports import EVERYTHING_IS_OLD

DUMP = b"PGDMP" + b"\x01\x0e\x00" + bytes(range(256)) * 100  # pg_dump's header, then anything


def stream(data: bytes):
    return io.BufferedReader(io.BytesIO(data))


def test_a_dump_goes_to_the_bucket_and_comes_back(client):
    key = backup.upload(stream(DUMP), now=datetime(2026, 10, 4, 3, 15, tzinfo=timezone.utc))
    assert key == "backups/2026-10-04T031500Z.dump"
    assert [b["key"] for b in backup.list_backups()] == [key]
    out = io.BytesIO()
    backup.download(key, out)
    assert out.getvalue() == DUMP


def test_something_that_isnt_a_dump_is_refused(client):
    for data in (b"", b"pg_dump: error: connection to server failed"):
        with pytest.raises(backup.NotADump):
            backup.upload(stream(data))
    assert backup.list_backups() == []


def test_only_backups_can_be_downloaded(client):
    with pytest.raises(ValueError):
        backup.download("some-photo.jpg", io.BytesIO())


def test_prune_deletes_old_backups_but_keeps_the_newest_few(client):
    for i in range(10):
        backup.upload(stream(DUMP), now=datetime(2026, 9, 1, tzinfo=timezone.utc) + timedelta(days=i))
    assert backup.prune() == []  # all just stored: none is 30 days old
    # Sixty days on, all are old, but the newest seven stay.
    later = datetime.now(timezone.utc) + timedelta(days=60)
    assert len(backup.prune(now=later)) == 3
    assert len(backup.list_backups()) == 7


def test_the_orphan_sweep_leaves_backups_alone(client):
    key = backup.upload(stream(DUMP))
    objects.put("stray.jpg", b"x", "image/jpeg")  # a real orphan, for comparison
    result = cleanup.sweep(EVERYTHING_IS_OLD)
    assert result["unreferenced_objects"] == 1
    assert [b["key"] for b in backup.list_backups()] == [key]
