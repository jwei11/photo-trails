"""Try the demo, self sign-up, and the limits on accounts that weren't invited: guests get
a copy of the demo trip, can't touch the demo or exceed their quota, expire, and their
work waits behind invited users'."""
import io
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import text

from app import auth, cleanup, demo, guests, jobs, limits, objects, trip_store
from app.db import engine
from tests.test_api import jpeg, upload
from tests.test_auth import google, google_claims, start_google  # noqa: F401 (google is a fixture)
from tests.test_imports import complete, describe, put, start_import

BAIXA = (38.7108, -9.1365)
SERIAL = "022031002092"


def camera_jpeg(when: str, serial: str) -> bytes:
    """A photo from a camera that records its serial number, and no GPS."""
    exif = Image.Exif()
    exif[0x010F], exif[0x0110] = "Canon", "Canon EOS R10"
    exif.get_ifd(0x8769).update({0x9003: when, 0x9011: "+01:00", 0xA431: serial})
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), "orange").save(buf, "JPEG", exif=exif)
    return buf.getvalue()


@pytest.fixture
def demo_trip(client, tmp_path):
    """A demo of four photos on Jul 1, 2025 in Lisbon (one from a camera with a serial
    number), loaded as the server's admin would, named "Lisbon weekend"."""
    for hour in (10, 11, 12):
        (tmp_path / f"IMG_{hour}.jpg").write_bytes(jpeg(f"2025:07:01 {hour}:00:00", "+01:00", *BAIXA))
    (tmp_path / "IMG_13.jpg").write_bytes(camera_jpeg("2025:07:01 13:00:00", SERIAL))
    (tmp_path / "clip.mov").write_bytes(b"not an image: skipped")
    demo.load(tmp_path, "Lisbon weekend")
    with engine.connect() as conn:
        return guests.demo_user_id(conn)


def start_guest(**kwargs):
    from app.main import app

    c = TestClient(app, **kwargs)
    return c, c.post("/api/auth/guest")


@pytest.fixture
def guest(demo_trip):
    c, res = start_guest()
    assert res.status_code == 200, res.text
    with c:
        yield c


def test_no_demo_until_one_is_loaded(client):
    client.cookies.clear()
    assert client.get("/api/auth/me").json()["demo"] is False
    assert client.post("/api/auth/guest").status_code == 404


def test_starting_a_demo_opens_the_guests_trip(demo_trip):
    c, res = start_guest()
    assert res.json()["trip_id"] == c.get("/api/trips").json()[0]["id"]


def test_a_guest_gets_their_own_copy_of_the_demo_trip(client, demo_trip, guest):
    assert client.get("/api/auth/me").json()["demo"] is True
    me = guest.get("/api/auth/me").json()["user"]
    assert me["kind"] == "guest" and me["email"].endswith("@demo.invalid")
    expires = datetime.fromisoformat(me["expires_at"])
    assert timedelta(hours=23) < expires - datetime.now(timezone.utc) <= limits.GUEST_LIFETIME

    [trip] = guest.get("/api/trips").json()
    assert trip["name"] == "Lisbon weekend" and trip["photo_count"] == 4
    [demo_trip_row] = trip_store.list_trips(demo_trip)
    assert trip["id"] != demo_trip_row["id"]  # new ids, owned by the guest

    # The copies use the demo's files: nothing was copied in storage.
    photo = guest.get("/api/photos").json()[0]
    res = guest.get(f"/api/photos/{photo['id']}/thumb", follow_redirects=False)
    assert res.status_code == 302 and httpx.get(res.headers["location"]).status_code == 200
    with engine.connect() as conn:
        demo_keys = set(conn.execute(text("SELECT thumb_key FROM photos WHERE user_id = :d"), {"d": demo_trip}).scalars())
    assert photo["thumb_key"] in demo_keys


def test_a_guests_changes_never_reach_the_demo_or_other_guests(demo_trip, guest):
    trip_id = guest.get("/api/trips").json()[0]["id"]
    assert guest.patch(f"/api/trips/{trip_id}", json={"name": "Mine now"}).status_code == 200
    assert guest.delete(f"/api/trips/{trip_id}").status_code == 204
    assert trip_store.list_trips(demo_trip)[0]["name"] == "Lisbon weekend"
    other, res = start_guest()
    assert res.status_code == 200
    assert [t["name"] for t in other.get("/api/trips").json()] == ["Lisbon weekend"]
    # Nobody can sign in as the demo account itself: it has no sign-in of any kind.
    assert auth.find_user(demo.DEMO_EMAIL).kind == "demo"


def test_the_demo_loader_blanks_serial_numbers(demo_trip):
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT storage_key, camera_serial, camera_model FROM photos WHERE user_id = :d"), {"d": demo_trip}
        ).all()
    camera = [r for r in rows if r.camera_model == "Canon EOS R10"]
    assert len(camera) == 1 and all(r.camera_serial is None for r in rows)
    original = objects.get(camera[0].storage_key)
    assert SERIAL.encode() not in original and b"0" * len(SERIAL) in original
    assert Image.open(io.BytesIO(original)).size == (64, 48)  # still a valid image


def test_live_guests_are_capped(demo_trip, monkeypatch):
    monkeypatch.setattr(limits, "GUESTS_MAX", 2)
    assert [start_guest()[1].status_code for _ in range(3)] == [200, 200, 503]


def test_each_address_can_start_only_a_few_demos_a_day(demo_trip, monkeypatch):
    monkeypatch.setattr(limits, "GUESTS_PER_IP_PER_DAY", 2)
    assert [start_guest()[1].status_code for _ in range(3)] == [200, 200, 429]
    # Someone else, at another address, isn't affected.
    assert start_guest(client=("203.0.113.9", 50000))[1].status_code == 200
    # Only a keyed hash of the address is kept.
    with engine.connect() as conn:
        stored = conn.execute(text("SELECT key_hash FROM rate_limit_events")).scalars().all()
    assert len(stored) == 3 and all(b"testclient" not in k and len(k) == 32 for k in stored)


def test_expired_guests_are_deleted_with_what_they_uploaded(client, demo_trip, guest):
    own = upload(guest, "mine.jpg", jpeg("2025:08:01 10:00:00", "+01:00", 40.4, -3.7))
    guest_id = guest.get("/api/auth/me").json()["user"]["id"]
    with engine.begin() as conn:
        conn.execute(text("UPDATE users SET expires_at = now() - interval '1 second' WHERE id = :id"), {"id": guest_id})
    assert guest.get("/api/auth/me").json()["user"] is None  # the session ended with it
    assert guests.delete_expired() == {"expired_guests": 1}
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM users WHERE id = :id"), {"id": guest_id}).scalar_one() == 0
    # The orphan sweep then removes the guest's own files, but never the demo's.
    cleanup.sweep(max_age=timedelta(0))
    keys = {k for k, _ in objects.list_keys()}
    assert own["storage_key"] not in keys and own["thumb_key"] not in keys
    with engine.connect() as conn:
        demo_keys = conn.execute(
            text("SELECT storage_key, thumb_key, web_key FROM photos WHERE user_id = :d"), {"d": demo_trip}
        ).all()
    assert len(demo_keys) == 4 and all(k in keys for row in demo_keys for k in row)


def test_guests_have_a_photo_quota_not_counting_the_demo(demo_trip, guest, monkeypatch):
    monkeypatch.setitem(limits.QUOTAS, "guest", limits.Quota(photos=1, file_bytes=10_000))
    first = jpeg("2025:08:01 10:00:00", "+01:00", 40.4, -3.7)
    upload(guest, "one.jpg", first)  # the four demo photos don't count
    res = guest.post("/api/photos", files={"file": ("two.jpg", jpeg("2025:08:02 10:00:00", "+01:00", 40.4, -3.7), "image/jpeg")})
    assert res.status_code == 403 and "room for 0 more" in res.json()["detail"]
    res = guest.post("/api/imports", json={"files": [describe(jpeg("2025:08:03 10:00:00", "+01:00", 40.4, -3.7))]})
    assert res.status_code == 403
    # Re-importing a file it already has costs nothing, so it isn't refused.
    assert guest.post("/api/imports", json={"files": [describe(first)]}).status_code == 201
    big = describe(first) | {"size_bytes": 20_000, "sha256": "0" * 64}
    assert "Files over" in guest.post("/api/imports", json={"files": [big]}).json()["detail"]


def test_invited_users_have_no_quota(client, monkeypatch):
    monkeypatch.setitem(limits.QUOTAS, "guest", limits.Quota(photos=0, file_bytes=0))
    upload(client, "a.jpg", jpeg("2025:08:01 10:00:00", "+01:00", 40.4, -3.7))


def test_a_guests_share_shows_only_the_demo_photos(demo_trip, guest):
    trip_id = guest.get("/api/trips").json()[0]["id"]
    # A photo of their own, taken during the trip: it joins the (named, so locked) trip.
    upload(guest, "mine.jpg", jpeg("2025:07:01 12:30:00", "+01:00", *BAIXA))
    assert guest.get(f"/api/trips/{trip_id}").json()["photo_count"] == 5
    token = guest.post(f"/api/trips/{trip_id}/shares", json={}).json()["token"]
    assert guest.get(f"/api/shared/{token}").json()["photo_count"] == 4


def test_guests_background_work_waits_behind_invited_users(client, demo_trip, guest):
    def enqueued_priority(c, data):
        imp = start_import(c, describe(data))
        item = imp["items"][0]
        put(item, data)
        complete(c, imp, item, process=False)
        with engine.connect() as conn:
            return conn.execute(
                text("SELECT priority FROM jobs WHERE payload->>'item_id' = :id"), {"id": item["id"]}
            ).scalar_one()

    guest_job = enqueued_priority(guest, jpeg("2025:08:01 10:00:00", "+01:00", 40.4, -3.7))
    invited_job = enqueued_priority(client, jpeg("2025:08:01 11:00:00", "+01:00", 40.4, -3.7))
    assert (guest_job, invited_job) == (1, 0)
    # The invited user's job was queued second but is claimed first.
    first = jobs.claim("test", ["process_photo"])
    with engine.connect() as conn:
        owner = conn.execute(
            text("SELECT u.kind FROM photos p JOIN users u ON u.id = p.user_id WHERE p.id = :id"),
            {"id": first.payload["photo_id"]},
        ).scalar_one()
    assert owner == "invited"


# ---- Signing up with Google ------------------------------------------------------------


def callback(client, token_endpoint, email):
    client.cookies.clear()
    _, cookie = start_google(client)
    token_endpoint(google_claims(cookie["nonce"], email=email))
    return client.get("/api/auth/google/callback", params={"code": "abc", "state": cookie["state"]},
                      follow_redirects=False)


def test_google_creates_accounts_while_sign_ups_are_open(client, google, monkeypatch):  # noqa: F811
    token_endpoint, _ = google
    monkeypatch.setattr(limits, "SIGNUPS_MAX", 1)
    assert client.get("/api/auth/me").json()["signup"] is True
    res = callback(client, token_endpoint, "New@Example.com")
    assert res.headers["location"] == "/"
    me = client.get("/api/auth/me").json()
    assert me["user"]["email"] == "new@example.com" and me["user"]["kind"] == "signup"
    assert me["signup"] is False  # that was the last place
    res = callback(client, token_endpoint, "later@example.com")
    assert "Sign-ups are full" in res.headers["location"].replace("+", " ")
    # Existing accounts still sign in, whatever the cap.
    assert callback(client, token_endpoint, "a@example.com").headers["location"] == "/"


def test_sign_ups_are_closed_by_default(client, google):  # noqa: F811
    assert client.get("/api/auth/me").json()["signup"] is False
