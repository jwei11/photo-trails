from datetime import timedelta

import httpx

from app import cleanup, objects
from tests.test_api import jpeg

LISBON = (38.72, -9.14)

# A negative age puts the cutoff in the future. A cutoff of exactly "now" would race: the
# database and object store run in a VM whose clock is tens of ms off from the host's.
EVERYTHING_IS_OLD = timedelta(minutes=-1)


def request_upload(client, data: bytes, name="IMG_1.jpg", content_type="image/jpeg"):
    res = client.post(
        "/uploads", json={"filename": name, "content_type": content_type, "size_bytes": len(data)}
    )
    assert res.status_code == 201, res.text
    return res.json()


def put(ticket, data: bytes, content_type="image/jpeg"):
    return httpx.put(ticket["upload_url"], content=data, headers={"Content-Type": content_type})


def test_presigned_upload_flow(client):
    data = jpeg("2025:07:01 10:00:00", "+01:00", *LISBON)
    ticket = request_upload(client, data)
    assert "X-Amz-Signature=" in ticket["upload_url"]

    assert client.get(f"/photos/{ticket['photo_id']}", follow_redirects=False).status_code == 404
    assert put(ticket, data).status_code == 200

    res = client.post(f"/uploads/{ticket['photo_id']}/complete")
    assert res.status_code == 200
    photo = res.json()
    assert photo["lat"] is not None and photo["thumb_key"] is not None
    assert photo["time_source"] == "exif_offset"


def test_pending_uploads_are_hidden_until_complete(client):
    ticket = request_upload(client, b"x" * 10)
    assert ticket["photo_id"] not in {p["id"] for p in client.get("/photos").json()}


def test_completing_before_uploading_is_a_conflict(client):
    ticket = request_upload(client, b"x" * 10)
    assert client.post(f"/uploads/{ticket['photo_id']}/complete").status_code == 409


def test_completing_twice_returns_the_same_photo(client):
    data = jpeg("2025:07:01 10:00:00", "+01:00", *LISBON)
    ticket = request_upload(client, data)
    put(ticket, data)
    first = client.post(f"/uploads/{ticket['photo_id']}/complete").json()
    second = client.post(f"/uploads/{ticket['photo_id']}/complete")
    assert second.status_code == 200 and second.json() == first
    assert len(client.get("/photos").json()) == 1


def test_bucket_rejects_a_different_size_or_type_than_was_signed(client):
    ticket = request_upload(client, b"x" * 10)
    assert put(ticket, b"x" * 11).status_code == 403
    assert put(ticket, b"x" * 10, content_type="text/html").status_code == 403


def test_upload_request_validation(client):
    too_big = {"filename": "a.jpg", "content_type": "image/jpeg", "size_bytes": 201 * 1024 * 1024}
    assert client.post("/uploads", json=too_big).status_code == 422
    bad_type = {"filename": "a.jpg", "content_type": "image/jpeg\r\nX-Evil: 1", "size_bytes": 10}
    assert client.post("/uploads", json=bad_type).status_code == 422


def test_only_image_types_can_be_uploaded(client):
    def ask(content_type):
        return client.post(
            "/uploads", json={"filename": "a", "content_type": content_type, "size_bytes": 10}
        ).status_code

    for allowed in ("image/jpeg", "image/heic", "image/x-canon-cr3", "application/octet-stream"):
        assert ask(allowed) == 201, allowed
    # Types a browser would run as a page, served back from our bucket by /photos/{id}.
    for blocked in ("text/html", "image/svg+xml", "application/xhtml+xml", "application/javascript"):
        assert ask(blocked) == 422, blocked


def test_unknown_upload_is_404(client):
    missing = "00000000-0000-0000-0000-000000000000"
    assert client.post(f"/uploads/{missing}/complete").status_code == 404


def test_sweep_removes_abandoned_uploads_and_unreferenced_objects(client):
    data = jpeg("2025:07:01 10:00:00", "+01:00", *LISBON)
    abandoned = request_upload(client, data)
    put(abandoned, data)  # uploaded, but never completed
    objects.put("stray-from-failed-commit.jpg", b"orphan", "image/jpeg")
    kept = request_upload(client, data)
    put(kept, data)
    client.post(f"/uploads/{kept['photo_id']}/complete")

    assert cleanup.sweep()["abandoned_uploads"] == 0  # everything is newer than an hour

    result = cleanup.sweep(max_age=EVERYTHING_IS_OLD)
    assert result == {"abandoned_uploads": 1, "unreferenced_objects": 1, "dry_run": False}
    keys = {k for k, _ in objects.list_keys()}
    assert "stray-from-failed-commit.jpg" not in keys
    assert not any(k.startswith(abandoned["photo_id"]) for k in keys)
    assert client.get(f"/photos/{kept['photo_id']}/thumb", follow_redirects=False).status_code == 302


def test_dry_run_deletes_nothing(client):
    request_upload(client, b"x" * 10)
    objects.put("stray.jpg", b"orphan", "image/jpeg")
    result = cleanup.sweep(max_age=EVERYTHING_IS_OLD, dry_run=True)
    assert result["abandoned_uploads"] == 1 and result["unreferenced_objects"] >= 1
    assert "stray.jpg" in {k for k, _ in objects.list_keys()}
