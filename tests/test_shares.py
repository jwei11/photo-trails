"""Share links, and what a shared trip reveals: snapped places, day numbers instead of
dates, nothing near home, nothing recent, and images without metadata."""
import io
from datetime import datetime, timedelta, timezone

import h3
import httpx
import pytest
from PIL import Image

from app import geodata, trips
from tests.test_api import jpeg, upload
from tests.test_geodata import sample_files  # noqa: F401 (a fixture)
from tests.test_trips_api import shoot

BAIXA = (38.7108, -9.1365)  # a section of Lisbon in the sample place data
LISBON_POINT = (38.71667, -9.13333)
BELEM = (38.69735, -9.21526)


@pytest.fixture
def places(client, sample_files):  # noqa: F811
    geodata.load()


@pytest.fixture
def lisbon_trip(client, places):
    """Three photos at one spot in the Baixa on Jul 1, 2025: one trip."""
    photos = [shoot(client, f"IMG_{h}.jpg", 1, h, where=BAIXA) for h in (10, 11, 12)]
    return client.get("/api/trips").json()[0]["id"], photos


def share(client, trip_id, **options):
    res = client.post(f"/api/trips/{trip_id}/shares", json=options)
    assert res.status_code == 201, res.text
    return res.json()


def public(client, token):
    """The shared trip as someone without an account sees it."""
    cookies = dict(client.cookies)
    client.cookies.clear()
    try:
        return client.get(f"/api/shared/{token}")
    finally:
        client.cookies.update(cookies)


def test_a_share_link_is_random_and_works_without_an_account(client, lisbon_trip):
    trip_id, _ = lisbon_trip
    s = share(client, trip_id)
    assert s["precision"] == "city" and s["exact_dates"] is False  # the cautious defaults
    assert len(s["token"]) >= 32 and s["path"] == f"/s/{s['token']}"
    assert share(client, trip_id)["token"] != s["token"]
    res = public(client, s["token"])
    assert res.status_code == 200
    assert res.headers["cache-control"] == "no-store" and res.headers["x-robots-tag"] == "noindex"
    assert res.json()["photo_count"] == 3


def test_city_precision_shows_the_city_not_the_neighbourhood_or_the_spot(client, lisbon_trip):
    trip_id, _ = lisbon_trip
    res = public(client, share(client, trip_id)["token"])
    view = res.json()
    for p in view["photos"]:
        assert (p["lat"], p["lon"]) == LISBON_POINT
        assert p["place_name"] == "Lisbon"  # not "Baixa", the section it's matched to
    assert view["places"] == [{"name": "Lisbon", "country_code": "PT", "photos": 3}]
    # The real coordinates, the filenames, and the camera appear nowhere in the response.
    for secret in ("38.7108", "-9.1365", "IMG_", "Fujifilm", "Apple", "Baixa"):
        assert secret not in res.text


def test_neighbourhood_precision_snaps_to_a_hexagon_centre(client, lisbon_trip):
    trip_id, _ = lisbon_trip
    view = public(client, share(client, trip_id, precision="neighbourhood")["token"]).json()
    centre = h3.cell_to_latlng(h3.latlng_to_cell(*BAIXA, 8))
    for p in view["photos"]:
        assert (p["lat"], p["lon"]) == pytest.approx(centre)
        assert (p["lat"], p["lon"]) != BAIXA
        assert trips.haversine_km(p["lat"], p["lon"], *BAIXA) < 0.6
    assert view["photos"][0]["place_name"] == "Baixa"


def test_dates_are_a_month_and_day_numbers_unless_chosen(client, places):
    for day, hour in ((1, 10), (1, 11), (2, 9), (3, 18)):
        shoot(client, f"{day}-{hour}.jpg", day, hour, where=BAIXA)
    trip_id = client.get("/api/trips").json()[0]["id"]
    res = public(client, share(client, trip_id)["token"])
    view = res.json()
    assert view["period"] == "July 2025" and view["day_count"] == 3
    assert view["start"] is None and view["end"] is None
    assert [(p["day"], p["time"]) for p in view["photos"]] == [(1, "10:00"), (1, "11:00"), (2, "09:00"), (3, "18:00")]
    assert all("local_time" not in p for p in view["photos"])
    assert "2025-07-0" not in res.text  # no exact date anywhere

    exact = public(client, share(client, trip_id, exact_dates=True)["token"]).json()
    assert exact["start"] == "2025-07-01" and exact["end"] == "2025-07-03"
    assert exact["photos"][0]["local_time"] == "2025-07-01T10:00:00"


def test_photos_near_a_home_are_left_out(client, places):
    trip = [shoot(client, f"b{h}.jpg", 1, h, where=BELEM) for h in (9, 10)]
    trip += [shoot(client, f"l{h}.jpg", 1, h, where=BAIXA) for h in (12, 13, 14)]
    trip_id = client.get("/api/trips").json()[0]["id"]
    client.patch(f"/api/trips/{trip_id}", json={"name": "Lisbon"})  # locked: keeps its photos
    # A home in Belém (1 km radius): Baixa, 7 km away, is outside it.
    client.post("/api/homes", json={"name": "Home", "lat": BELEM[0], "lon": BELEM[1], "radius_km": 1})
    sharing = client.get(f"/api/trips/{trip_id}/shares").json()
    assert sharing["has_homes"] and sharing["hidden_near_home"] == 2

    s = share(client, trip_id)
    view = public(client, s["token"]).json()
    assert view["photo_count"] == 3 and all(p["place_name"] == "Lisbon" for p in view["photos"])
    # Not even by id: the image of a left-out photo isn't served.
    client.cookies.clear()
    res = client.get(f"/api/shared/{s['token']}/photos/{trip[0]['id']}/thumb", follow_redirects=False)
    assert res.status_code == 404


def jpeg_days_ago(days, hour=10):
    when = datetime.now(timezone.utc) - timedelta(days=days)
    return jpeg(when.strftime(f"%Y:%m:%d {hour:02d}:00:00"), "+00:00", *BAIXA)


def test_a_trip_can_only_be_shared_a_week_after_it_ends(client, places):
    for hour in (10, 11, 12):
        upload(client, f"{hour}.jpg", jpeg_days_ago(2, hour))
    trip_id = client.get("/api/trips").json()[0]["id"]
    sharing = client.get(f"/api/trips/{trip_id}/shares").json()
    assert sharing["hidden_recent"] == 3
    assert datetime.fromisoformat(sharing["shareable_from"]) > datetime.now(timezone.utc)
    res = client.post(f"/api/trips/{trip_id}/shares", json={})
    assert res.status_code == 409 and "a week after they end" in res.json()["detail"]


def test_photos_from_the_last_week_never_show_on_an_existing_share(client, places):
    old = [upload(client, f"old{h}.jpg", jpeg_days_ago(30, h)) for h in (10, 11, 12)]
    trip_id = old[0]["trip_id"] or client.get("/api/trips").json()[0]["id"]
    s = share(client, trip_id)
    # Later, the owner adds a photo from two days ago to the shared trip.
    recent = upload(client, "now.jpg", jpeg_days_ago(2))
    assert client.put(f"/api/photos/{recent['id']}/trip", json={"trip_id": trip_id}).status_code == 204
    view = public(client, s["token"]).json()
    assert recent["id"] not in [p["id"] for p in view["photos"]] and view["photo_count"] == 3


def test_a_revoked_link_stops_working(client, lisbon_trip):
    trip_id, photos = lisbon_trip
    s = share(client, trip_id)
    assert client.delete(f"/api/shares/{s['id']}").status_code == 204
    assert public(client, s["token"]).status_code == 404
    client.cookies.clear()
    res = client.get(f"/api/shared/{s['token']}/photos/{photos[0]['id']}/thumb", follow_redirects=False)
    assert res.status_code == 404


def test_unknown_tokens_and_other_trips_photos_are_404(client, lisbon_trip):
    trip_id, _ = lisbon_trip
    s = share(client, trip_id)
    other = [shoot(client, f"m{h}.jpg", 20, h) for h in (10, 11, 12)]  # a second trip, not shared
    client.cookies.clear()
    assert client.get("/api/shared/" + "x" * 32).status_code == 404
    res = client.get(f"/api/shared/{s['token']}/photos/{other[0]['id']}/web", follow_redirects=False)
    assert res.status_code == 404


def test_only_the_owner_can_share_list_or_revoke(client, lisbon_trip, other_client):
    trip_id, _ = lisbon_trip
    s = share(client, trip_id)
    assert other_client.get(f"/api/trips/{trip_id}/shares").status_code == 404
    assert other_client.post(f"/api/trips/{trip_id}/shares", json={}).status_code == 404
    assert other_client.delete(f"/api/shares/{s['id']}").status_code == 404
    assert public(client, s["token"]).status_code == 200  # still works


def test_shared_images_carry_no_metadata(client, lisbon_trip):
    """The guide's rule: a shared JPEG with GPS intact defeats everything else. The files
    a share link actually serves are fetched and checked byte by byte."""
    trip_id, photos = lisbon_trip
    s = share(client, trip_id)
    client.cookies.clear()
    for kind in ("thumb", "web"):
        res = client.get(f"/api/shared/{s['token']}/photos/{photos[0]['id']}/{kind}", follow_redirects=False)
        assert res.status_code == 302
        data = httpx.get(res.headers["location"]).content
        with Image.open(io.BytesIO(data)) as img:
            assert img.format == "WEBP"
            assert not img.getexif() and "exif" not in img.info and "xmp" not in img.info
        for marker in (b"Exif", b"EXIF", b"GPS", b"xmpmeta", b"XMP "):
            assert marker not in data, (kind, marker)


# ---- Home suggestions --------------------------------------------------------------------


def test_a_place_with_photos_on_many_days_is_suggested_as_home(client, places):
    for day in (1, 5, 9, 13, 17, 21):
        shoot(client, f"home{day}.jpg", day, 20, where=BELEM)
    suggestions = client.get("/api/homes/suggestions").json()
    assert len(suggestions) == 1
    home = suggestions[0]
    assert home["day_count"] == 6 and home["place_name"] == "Belém"
    assert trips.haversine_km(home["lat"], home["lon"], *BELEM) < 1
    # Once it's a home, it's no longer suggested.
    client.post("/api/homes", json={"name": "Home", "lat": home["lat"], "lon": home["lon"], "radius_km": 2})
    assert client.get("/api/homes/suggestions").json() == []


def test_a_trip_is_not_mistaken_for_home(client, places):
    # Lots of photos in one spot, but on three days in a row: a visit, not where you live.
    for day in (1, 2, 3):
        for hour in range(9, 21):
            shoot(client, f"{day}-{hour}.jpg", day, hour, where=BAIXA)
    assert client.get("/api/homes/suggestions").json() == []


def test_home_suggestions_are_per_user(client, other_client, places):
    for day in (1, 5, 9, 13, 17, 21):
        shoot(client, f"home{day}.jpg", day, 20, where=BELEM)
    assert other_client.get("/api/homes/suggestions").json() == []
