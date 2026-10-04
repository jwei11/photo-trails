import pytest

from tests.test_api import jpeg, upload

LISBON = (38.72, -9.14)


def shoot(client, name, day, hour, where=LISBON, minute=0):
    when = f"2025:07:{day:02d} {hour:02d}:{minute:02d}:00"
    return upload(client, name, jpeg(when, "+01:00", *where))


def trips(client):
    return client.get("/api/trips").json()


def photo_trip(client, photo_id):
    return client.get(f"/api/photos/{photo_id}/metadata").json()["trip_id"]


@pytest.fixture
def two_trips(client):
    """Lisbon on Jul 1 and again on Jul 5: a 4-day gap makes two trips."""
    first = [shoot(client, f"a{h}.jpg", 1, h) for h in (10, 11, 12)]
    second = [shoot(client, f"b{h}.jpg", 5, h) for h in (10, 11, 12)]
    return first, second


def test_time_gap_produces_two_trips_with_stats(client, two_trips):
    t = trips(client)
    assert [x["photo_count"] for x in t] == [3, 3]
    assert all(x["day_count"] == 1 and not x["is_locked"] and x["name"] is None for x in t)


def test_rename_locks_the_trip_and_late_photos_inside_it_join(client, two_trips):
    first_id = trips(client)[0]["id"]
    res = client.patch(f"/api/trips/{first_id}", json={"name": "Lisbon weekend"})
    assert res.status_code == 200 and res.json()["is_locked"]

    late = shoot(client, "friend.jpg", 1, 11, minute=30)  # inside the renamed trip's time span
    assert photo_trip(client, late["id"]) == first_id
    assert trips(client)[0]["name"] == "Lisbon weekend"


def test_merge_survives_regrouping(client, two_trips):
    first_id, second_id = (t["id"] for t in trips(client))
    res = client.post(f"/api/trips/{first_id}/merge", json={"trip_ids": [second_id]})
    assert res.status_code == 200 and res.json()["photo_count"] == 6

    shoot(client, "later.jpg", 9, 10)  # triggers regrouping; would split if merge weren't kept
    t = trips(client)
    assert t[0]["id"] == first_id and t[0]["photo_count"] == 6


def test_merge_then_split_back(client, two_trips):
    first_id, second_id = (t["id"] for t in trips(client))
    client.post(f"/api/trips/{first_id}/merge", json={"trip_ids": [second_id]})

    res = client.post(f"/api/trips/{first_id}/split", json={"at": "2025-07-03T00:00:00Z"})
    assert res.status_code == 200
    assert [t["photo_count"] for t in res.json()["trips"]] == [3, 3]
    assert all(t["is_locked"] for t in trips(client))


def test_split_that_leaves_one_side_empty_is_rejected(client, two_trips):
    first_id = trips(client)[0]["id"]
    res = client.post(f"/api/trips/{first_id}/split", json={"at": "2025-06-01T00:00:00Z"})
    assert res.status_code == 400


def test_split_time_without_timezone_is_rejected(client, two_trips):
    first_id = trips(client)[0]["id"]
    res = client.post(f"/api/trips/{first_id}/split", json={"at": "2025-07-01T11:00:00"})
    assert res.status_code == 422


def test_photo_removed_from_trip_stays_removed(client, two_trips):
    first, _ = two_trips
    assert client.put(f"/api/photos/{first[0]['id']}/trip", json={"trip_id": None}).status_code == 204

    shoot(client, "trigger.jpg", 20, 10)
    assert photo_trip(client, first[0]["id"]) is None
    assert trips(client)[0]["photo_count"] == 2


def test_dissolved_trip_photos_are_not_regrouped(client, two_trips):
    first_id = trips(client)[0]["id"]
    assert client.delete(f"/api/trips/{first_id}").status_code == 204

    shoot(client, "trigger.jpg", 20, 10)
    remaining = trips(client)
    assert len(remaining) == 1 and remaining[0]["id"] != first_id
    assert len(client.get("/api/timeline").json()["not_in_trip"]) >= 1


def test_adding_a_home_removes_unlocked_trips_there_but_keeps_locked_ones(client, two_trips):
    first_id, second_id = (t["id"] for t in trips(client))
    client.patch(f"/api/trips/{first_id}", json={"name": "Keep me"})

    res = client.post("/api/homes", json={"name": "Lisbon flat", "lat": LISBON[0], "lon": LISBON[1]})
    assert res.status_code == 201

    assert [t["id"] for t in trips(client)] == [first_id]
    client.delete(f"/api/homes/{res.json()['id']}")
    assert len(trips(client)) == 2  # removing the home brings the auto trip back


def test_unknown_trip_ids_are_404(client, two_trips):
    first_id = trips(client)[0]["id"]
    missing = "00000000-0000-0000-0000-000000000000"
    assert client.post(f"/api/trips/{first_id}/merge", json={"trip_ids": [missing]}).status_code == 404
    assert client.patch(f"/api/trips/{missing}", json={"name": "x"}).status_code == 404
    assert client.delete(f"/api/homes/{missing}").status_code == 404
