import io

import httpx
from PIL import Image, TiffImagePlugin

from app.exif import IFD_EXIF, IFD_GPS

FUJI = {"camera_make": "Fujifilm", "camera_model": "X-T5", "camera_serial": "FX1"}


def R(value):
    return TiffImagePlugin.IFDRational(value)


def dms(value):
    value = abs(value)
    degrees, minutes = int(value), int(value % 1 * 60)
    return (R(degrees), R(minutes), R(round((value - degrees - minutes / 60) * 3600, 2)))


def jpeg(when: str, offset: str, lat: float, lon: float) -> bytes:
    exif = Image.Exif()
    exif.get_ifd(IFD_EXIF).update({0x9003: when, 0x9011: offset})
    gps = exif.get_ifd(IFD_GPS)
    gps.update({1: "N" if lat >= 0 else "S", 2: dms(lat), 3: "E" if lon >= 0 else "W", 4: dms(lon)})
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), "teal").save(buf, "JPEG", exif=exif)
    return buf.getvalue()


def upload(client, name: str, data: bytes):
    res = client.post("/api/photos", files={"file": (name, data, "image/jpeg")})
    assert res.status_code == 201, res.text
    return res.json()


def add_offset(client, **overrides):
    return client.post("/api/camera-offsets", json={**FUJI, "offset_seconds": 3600, **overrides})


def test_api_is_under_prefix_and_healthz_is_not(client):
    assert client.get("/api/photos").status_code == 200
    # The old root path is now a page URL: it gets the React app (or a 404 if it isn't
    # built), never the API's JSON.
    old = client.get("/photos")
    assert old.status_code == 404 or "text/html" in old.headers["content-type"]
    assert client.get("/healthz").json() == {"status": "ok"}


def test_files_are_served_by_redirect_to_signed_bucket_urls(client):
    data = jpeg("2025:07:01 10:00:00", "+01:00", 38.72, -9.14)
    photo = upload(client, "a.jpg", data)

    for path, expected_type in [("", "image/jpeg"), ("/thumb", "image/webp"), ("/web", "image/webp")]:
        res = client.get(f"/api/photos/{photo['id']}{path}", follow_redirects=False)
        assert res.status_code == 302
        assert "X-Amz-Signature=" in res.headers["location"]
        assert res.headers["cache-control"].startswith("private")
        fetched = httpx.get(res.headers["location"])
        assert fetched.status_code == 200
        assert fetched.headers["content-type"] == expected_type
    assert httpx.get(client.get(f"/api/photos/{photo['id']}", follow_redirects=False).headers["location"]).content == data


def test_direct_upload_rejects_non_image_types(client):
    res = client.post("/api/photos", files={"file": ("page.html", b"<script>alert(1)</script>", "text/html")})
    assert res.status_code == 415
    assert client.get("/api/photos").json() == []


def test_direct_upload_rejects_files_over_the_size_limit(client, monkeypatch):
    from app import main

    monkeypatch.setattr(main, "MAX_UPLOAD_BYTES", 100)
    res = client.post("/api/photos", files={"file": ("big.jpg", b"x" * 101, "image/jpeg")})
    assert res.status_code == 413
    assert client.get("/api/photos").json() == []


def test_timeline_days_are_in_date_order_across_the_date_line(client):
    # Tokyo morning of Jul 2 (00:00Z) happens *before* Honolulu afternoon of Jul 1 (02:00Z Jul 2).
    upload(client, "tokyo.jpg", jpeg("2025:07:02 09:00:00", "+09:00", 35.68, 139.69))
    upload(client, "honolulu.jpg", jpeg("2025:07:01 16:00:00", "-10:00", 21.31, -157.86))

    days = client.get("/api/timeline").json()["not_in_trip"]  # 2 photos: too few for a trip

    assert [d["date"] for d in days] == ["2025-07-01", "2025-07-02"]
    assert [p["original_filename"] for d in days for p in d["photos"]] == ["honolulu.jpg", "tokyo.jpg"]


def test_overlapping_clock_offsets_are_rejected(client):
    assert add_offset(client, valid_to="2025-07-02T00:00:00").status_code == 201

    res = add_offset(client)  # unbounded range overlaps the first
    assert res.status_code == 409
    assert "already has an offset" in res.json()["detail"]
    assert len(client.get("/api/camera-offsets").json()) == 1


def test_adjacent_offset_ranges_and_other_cameras_are_allowed(client):
    assert add_offset(client, valid_to="2025-07-02T00:00:00").status_code == 201
    # Ranges are half-open [from, to), so one ending where the next starts don't overlap.
    assert add_offset(client, valid_from="2025-07-02T00:00:00").status_code == 201
    assert add_offset(client, camera_serial="OTHER").status_code == 201
    assert add_offset(client, camera_serial=None).status_code == 201
    assert add_offset(client, camera_serial=None).status_code == 409  # NULL serials still collide
