import hashlib
from datetime import timedelta

import httpx

from app import cleanup, objects, storage
from tests.test_api import jpeg

LISBON = (38.72, -9.14)

# A negative age puts the cutoff in the future. A cutoff of exactly "now" would race: the
# database and object store run in a VM whose clock is tens of ms off from the host's.
EVERYTHING_IS_OLD = timedelta(minutes=-1)


def photo_bytes(hour=10):
    return jpeg(f"2025:07:01 {hour:02d}:00:00", "+01:00", *LISBON)


def describe(data: bytes, name="IMG.jpg", content_type="image/jpeg", sha256=None):
    return {
        "filename": name,
        "content_type": content_type,
        "size_bytes": len(data),
        "sha256": sha256 or hashlib.sha256(data).hexdigest(),
    }


def start_import(client, *files):
    res = client.post("/imports", json={"files": list(files)})
    assert res.status_code == 201, res.text
    return res.json()


def put(item, data: bytes, content_type="image/jpeg"):
    return httpx.put(item["upload_url"], content=data, headers={"Content-Type": content_type})


def complete(client, imp, item):
    res = client.post(f"/imports/{imp['id']}/items/{item['id']}/complete")
    assert res.status_code == 200, res.text
    return res.json()


def upload_one(client, data: bytes, **kw):
    imp = start_import(client, describe(data, **kw))
    item = imp["items"][0]
    assert put(item, data).status_code == 200
    return imp, complete(client, imp, item)


def test_import_uploads_directly_and_processes(client):
    data = photo_bytes()
    imp = start_import(client, describe(data))
    item = imp["items"][0]
    assert item["status"] == "uploading" and "X-Amz-Signature=" in item["upload_url"]
    assert client.get(f"/photos/{item['photo_id']}", follow_redirects=False).status_code == 404

    assert put(item, data).status_code == 200
    done = complete(client, imp, item)
    assert done["status"] == "done"

    photo = client.get(f"/photos/{done['photo_id']}/metadata").json()
    assert photo["time_source"] == "exif_offset" and photo["thumb_key"]


def test_already_imported_files_are_skipped_without_uploading(client):
    data = photo_bytes()
    _, first = upload_one(client, data)

    again = start_import(client, describe(data, name="copy.jpg"))
    item = again["items"][0]
    assert item["status"] == "duplicate" and item["photo_id"] == first["photo_id"]
    assert "upload_url" not in item


def test_same_file_twice_in_one_batch_resolves_whichever_arrives_first(client):
    data = photo_bytes()
    imp = start_import(client, describe(data, name="a.jpg"), describe(data, name="a (1).jpg"))
    first, second = imp["items"]
    assert first["status"] == second["status"] == "uploading"  # nothing claimed up front

    # The first copy never arrives; the second still imports.
    put(second, data)
    assert complete(client, imp, second)["status"] == "done"
    assert complete(client, imp, first)["status"] == "failed"

    # Retrying the first then resolves to the photo the second created.
    retry = client.post(f"/imports/{imp['id']}/items/{first['id']}/upload-url").json()
    put(retry, data)
    result = complete(client, imp, retry)
    assert result["status"] == "duplicate"
    assert result["photo_id"] == client.get(f"/imports/{imp['id']}").json()["items"][1]["photo_id"]
    assert len(client.get("/photos").json()) == 1


def test_retry_after_the_photo_already_finished_marks_the_item_done(client):
    data = photo_bytes()
    imp = start_import(client, describe(data))
    item = imp["items"][0]
    put(item, data)
    storage.complete_upload(item["photo_id"])  # completion ran, but the item was never updated

    res = client.post(f"/imports/{imp['id']}/items/{item['id']}/upload-url")
    assert res.status_code == 200
    renewed = res.json()
    assert renewed["status"] == "done" and renewed["photo_id"] == item["photo_id"]
    assert "upload_url" not in renewed


def set_pending_ages(photo_id, created_hours_ago, url_issued_hours_ago):
    from sqlalchemy import text

    from app.db import engine

    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE photos SET created_at = now() - make_interval(hours => :c), "
                "upload_url_issued_at = now() - make_interval(hours => :u) WHERE id = :id"
            ),
            {"id": photo_id, "c": created_hours_ago, "u": url_issued_hours_ago},
        )


def test_sweep_spares_an_old_import_whose_upload_url_is_fresh(client):
    imp = start_import(client, describe(photo_bytes()))
    item = imp["items"][0]
    # Registered 3 hours ago as part of a long import, but its upload URL was just issued.
    set_pending_ages(item["photo_id"], created_hours_ago=3, url_issued_hours_ago=0)
    assert cleanup.sweep()["abandoned_uploads"] == 0

    set_pending_ages(item["photo_id"], created_hours_ago=3, url_issued_hours_ago=2)
    assert cleanup.sweep()["abandoned_uploads"] == 1


def test_server_verifies_the_hash_instead_of_trusting_the_client(client):
    data = photo_bytes()
    _, original = upload_one(client, data)

    # Claims a different hash, but uploads identical bytes.
    imp = start_import(client, describe(data, sha256="0" * 64))
    item = imp["items"][0]
    put(item, data)
    result = complete(client, imp, item)

    assert result["status"] == "duplicate" and result["photo_id"] == original["photo_id"]
    assert len(client.get("/photos").json()) == 1
    assert objects.size(f"{item['photo_id']}.jpg") is None  # the duplicate's upload was discarded


def test_one_failure_does_not_affect_the_rest_and_counts_are_reported(client):
    files = [photo_bytes(h) for h in (9, 10, 11)]
    imp = start_import(client, *(describe(d, name=f"{i}.jpg") for i, d in enumerate(files)))
    good, bad, never = imp["items"]

    put(good, files[0])
    complete(client, imp, good)
    client.post(f"/imports/{imp['id']}/items/{bad['id']}/failed", json={"error": "network dropped"})

    summary = client.get(f"/imports/{imp['id']}").json()
    assert summary["counts"] == {"total": 3, "uploading": 1, "done": 1, "duplicate": 0, "failed": 1}
    failed = summary["items"][1]
    assert failed["failed_stage"] == "upload" and failed["error"] == "network dropped"
    assert client.get("/imports").json()[0]["counts"]["failed"] == 1


def test_failed_upload_is_retried_with_a_fresh_url(client):
    data = photo_bytes()
    imp = start_import(client, describe(data))
    item = imp["items"][0]
    client.post(f"/imports/{imp['id']}/items/{item['id']}/failed", json={"error": "timeout"})

    res = client.post(f"/imports/{imp['id']}/items/{item['id']}/upload-url")
    assert res.status_code == 200
    retry = res.json()
    assert retry["status"] == "uploading" and retry["failed_stage"] is None
    assert put(retry, data).status_code == 200
    assert complete(client, imp, retry)["status"] == "done"


def test_completing_before_the_file_arrives_fails_at_the_upload_stage(client):
    imp = start_import(client, describe(photo_bytes()))
    result = complete(client, imp, imp["items"][0])
    assert result["status"] == "failed" and result["failed_stage"] == "upload"


def test_processing_failure_is_retried_on_the_server_without_reuploading(client, monkeypatch):
    data = photo_bytes()
    imp = start_import(client, describe(data))
    item = imp["items"][0]
    put(item, data)

    real = storage.complete_upload
    monkeypatch.setattr(storage, "complete_upload", lambda *a: (_ for _ in ()).throw(RuntimeError("db hiccup")))
    failed = complete(client, imp, item)
    assert failed["status"] == "failed" and failed["failed_stage"] == "processing"
    assert "db hiccup" in failed["error"]
    renew = client.post(f"/imports/{imp['id']}/items/{item['id']}/upload-url")
    assert renew.status_code == 409  # already uploaded: retry processing instead

    monkeypatch.setattr(storage, "complete_upload", real)
    assert complete(client, imp, item)["status"] == "done"


def test_completing_twice_is_safe(client):
    data = photo_bytes()
    imp = start_import(client, describe(data))
    item = imp["items"][0]
    put(item, data)
    first = complete(client, imp, item)
    second = complete(client, imp, item)
    assert first == second and first["status"] == "done"
    assert len(client.get("/photos").json()) == 1


def test_swept_upload_gets_a_new_pending_photo_on_retry(client):
    data = photo_bytes()
    imp = start_import(client, describe(data))
    item = imp["items"][0]
    cleanup.sweep(max_age=EVERYTHING_IS_OLD)  # abandoned: pending photo removed

    assert client.get(f"/imports/{imp['id']}").json()["items"][0]["photo_id"] is None
    retry = client.post(f"/imports/{imp['id']}/items/{item['id']}/upload-url").json()
    assert retry["photo_id"] and retry["photo_id"] != item["photo_id"]
    put(retry, data)
    assert complete(client, imp, retry)["status"] == "done"


def test_bucket_rejects_a_different_size_or_type_than_was_signed(client):
    imp = start_import(client, describe(b"x" * 10))
    item = imp["items"][0]
    assert put(item, b"x" * 11).status_code == 403
    assert put(item, b"x" * 10, content_type="text/html").status_code == 403


def test_import_request_validation(client):
    ok = describe(b"x" * 10)
    assert client.post("/imports", json={"files": []}).status_code == 422
    assert client.post("/imports", json={"files": [{**ok, "size_bytes": 201 * 1024 * 1024}]}).status_code == 422
    assert client.post("/imports", json={"files": [{**ok, "sha256": "not-a-hash"}]}).status_code == 422
    for allowed in ("image/jpeg", "image/heic", "image/x-canon-cr3", "application/octet-stream"):
        assert client.post("/imports", json={"files": [{**ok, "content_type": allowed}]}).status_code == 201
    # Types a browser would run as a page, served back from our bucket by /photos/{id}.
    for blocked in ("text/html", "image/svg+xml", "application/xhtml+xml", "image/jpeg\r\nX-Evil: 1"):
        assert client.post("/imports", json={"files": [{**ok, "content_type": blocked}]}).status_code == 422


def test_unknown_import_or_item_is_404(client):
    missing = "00000000-0000-0000-0000-000000000000"
    assert client.get(f"/imports/{missing}").status_code == 404
    imp = start_import(client, describe(b"x" * 10))
    assert client.post(f"/imports/{imp['id']}/items/{missing}/complete").status_code == 404


def test_direct_upload_of_a_duplicate_is_a_conflict(client):
    data = photo_bytes()
    first = client.post("/photos", files={"file": ("a.jpg", data, "image/jpeg")})
    second = client.post("/photos", files={"file": ("b.jpg", data, "image/jpeg")})
    assert first.status_code == 201
    assert second.status_code == 409 and second.json()["detail"]["photo_id"] == first.json()["id"]


def test_sweep_removes_abandoned_uploads_and_unreferenced_objects(client):
    abandoned_data, kept_data = photo_bytes(9), photo_bytes(10)
    abandoned = start_import(client, describe(abandoned_data))["items"][0]
    put(abandoned, abandoned_data)  # uploaded, but never completed
    objects.put("stray-from-failed-commit.jpg", b"orphan", "image/jpeg")
    _, kept = upload_one(client, kept_data)

    assert cleanup.sweep()["abandoned_uploads"] == 0  # everything is newer than an hour

    result = cleanup.sweep(max_age=EVERYTHING_IS_OLD)
    assert result == {"abandoned_uploads": 1, "unreferenced_objects": 1, "dry_run": False}
    keys = {k for k, _ in objects.list_keys()}
    assert "stray-from-failed-commit.jpg" not in keys
    assert not any(k.startswith(abandoned["photo_id"]) for k in keys)
    assert client.get(f"/photos/{kept['photo_id']}/thumb", follow_redirects=False).status_code == 302


def test_dry_run_deletes_nothing(client):
    start_import(client, describe(b"x" * 10))
    objects.put("stray.jpg", b"orphan", "image/jpeg")
    result = cleanup.sweep(max_age=EVERYTHING_IS_OLD, dry_run=True)
    assert result["abandoned_uploads"] == 1 and result["unreferenced_objects"] >= 1
    assert "stray.jpg" in {k for k, _ in objects.list_keys()}
