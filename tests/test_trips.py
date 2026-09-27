from datetime import datetime, timedelta, timezone

import pytest

from app.trips import Home, LockedTrip, TripPhoto, group_photos, haversine_km, trip_stats

HOME = (37.77, -122.42)  # San Francisco
NEAR_HOME = (37.80, -122.27)  # Oakland, ~14 km
DAY_TRIP = (38.29, -122.46)  # Sonoma, ~58 km
LISBON = (38.72, -9.14)
PORTO = (41.15, -8.61)  # ~274 km from Lisbon
MADRID = (40.42, -3.70)  # ~503 km from Lisbon
TOKYO = (35.68, 139.69)
HOMES = [Home(*HOME, radius_km=25)]


def at(day, hour=12, minute=0):
    return datetime(2025, 7, 1, tzinfo=timezone.utc) + timedelta(days=day - 1, hours=hour, minutes=minute)


def photos(prefix, where, day, count=3, start_hour=10, **kw):
    lat, lon = where if where else (None, None)
    return [
        TripPhoto(f"{prefix}{i}", at(day, start_hour + i), lat, lon, **kw) for i in range(count)
    ]


def trips_of(grouping):
    """Photo ids grouped by assigned trip, ignoring the trip ids themselves."""
    groups = {}
    for pid, trip in grouping.assignments.items():
        if trip is not None:
            groups.setdefault(trip, set()).add(pid)
    return sorted(groups.values(), key=lambda g: sorted(g))


def unassigned(grouping):
    return {pid for pid, trip in grouping.assignments.items() if trip is None}


def test_haversine_lisbon_to_madrid():
    assert haversine_km(*LISBON, *MADRID) == pytest.approx(503, abs=3)


def test_long_time_gap_starts_a_new_trip():
    g = group_photos(photos("a", LISBON, day=1) + photos("b", LISBON, day=5), HOMES, [])
    assert trips_of(g) == [{"a0", "a1", "a2"}, {"b0", "b1", "b2"}]


def test_multi_city_trip_with_overnight_move_stays_one_trip():
    g = group_photos(
        photos("a", LISBON, day=1) + photos("b", PORTO, day=2) + photos("c", MADRID, day=3),
        HOMES, [],
    )
    assert len(trips_of(g)) == 1


def test_far_jump_after_a_day_without_photos_splits():
    # Lisbon on day 1 evening, then Tokyo 36h later with no photos in between.
    g = group_photos(
        photos("a", LISBON, day=1, start_hour=18) + photos("b", TOKYO, day=3, start_hour=8),
        HOMES, [],
    )
    assert len(trips_of(g)) == 2


def test_same_place_after_a_day_without_photos_stays_one_trip():
    g = group_photos(
        photos("a", LISBON, day=1, start_hour=18) + photos("b", LISBON, day=3, start_hour=8),
        HOMES, [],
    )
    assert len(trips_of(g)) == 1


def test_weekend_at_home_between_two_legs_splits_the_trip():
    g = group_photos(
        photos("a", LISBON, day=1) + photos("h", HOME, day=2) + photos("b", LISBON, day=3),
        HOMES, [],
    )
    assert trips_of(g) == [{"a0", "a1", "a2"}, {"b0", "b1", "b2"}]
    assert {"h0", "h1", "h2"} <= unassigned(g)


def test_photos_near_home_are_not_a_trip_but_a_day_trip_farther_away_is():
    g = group_photos(photos("n", NEAR_HOME, day=1) + photos("d", DAY_TRIP, day=4), HOMES, [])
    assert trips_of(g) == [{"d0", "d1", "d2"}]
    assert {"n0", "n1", "n2"} <= unassigned(g)


def test_camera_photos_without_gps_inherit_home_status():
    home_phone = photos("h", HOME, day=1, count=2, start_hour=10)
    home_camera = [TripPhoto("cam", at(1, 11, 30), None, None)]
    g = group_photos(home_phone + home_camera, HOMES, [])
    assert "cam" in unassigned(g)


def test_undated_photos_are_never_assigned():
    g = group_photos(
        photos("a", LISBON, day=1) + [TripPhoto("screenshot", None, None, None)], HOMES, []
    )
    assert "screenshot" in unassigned(g)


def test_lone_photo_is_not_a_trip():
    g = group_photos(photos("x", TOKYO, day=1, count=1), HOMES, [])
    assert trips_of(g) == []
    assert unassigned(g) == {"x0"}


def test_friends_photos_interleaved_in_time_join_the_same_trip():
    mine = photos("me", LISBON, day=1, start_hour=10)
    friend = [TripPhoto(f"f{i}", at(1, 10, 30 + i * 60), None, None) for i in range(3)]
    g = group_photos(mine + friend, HOMES, [])
    assert len(trips_of(g)) == 1


def test_no_homes_configured_means_everything_can_be_a_trip():
    g = group_photos(photos("h", HOME, day=1), [], [])
    assert len(trips_of(g)) == 1


def test_pinned_photos_are_left_alone():
    pinned = [TripPhoto(f"p{i}", at(1, 10 + i), *LISBON, trip_id="mine", pinned=True) for i in range(3)]
    g = group_photos(pinned + photos("b", LISBON, day=5), HOMES, [])
    assert not ({"p0", "p1", "p2"} & g.assignments.keys())


def test_free_photos_inside_a_locked_trip_span_join_it():
    locked = [LockedTrip("renamed-trip", at(1, 9), at(3, 20))]
    g = group_photos(photos("late", LISBON, day=2), HOMES, locked)
    assert all(g.assignments[f"late{i}"] == "renamed-trip" for i in range(3))
    assert g.new_trips == []


def test_locked_trip_between_free_photos_separates_them():
    locked = [LockedTrip("middle", at(2, 0), at(2, 23))]
    g = group_photos(
        photos("a", LISBON, day=1, start_hour=20) + photos("b", LISBON, day=3, start_hour=2),
        HOMES, locked,
    )
    assert len(trips_of(g)) == 2


def test_existing_trip_ids_are_reused_on_regrouping():
    existing = [TripPhoto(f"a{i}", at(1, 10 + i), *LISBON, trip_id="trip-1") for i in range(3)]
    added = [TripPhoto("a3", at(1, 15), *LISBON)]
    g = group_photos(existing + added, HOMES, [])
    assert {g.assignments[f"a{i}"] for i in range(4)} == {"trip-1"}
    assert g.new_trips == []


def test_split_regrouping_keeps_the_id_on_the_larger_part():
    big = [TripPhoto(f"a{i}", at(1, 8 + i), *LISBON, trip_id="t") for i in range(5)]
    small = [TripPhoto(f"b{i}", at(6, 8 + i), *LISBON, trip_id="t") for i in range(3)]
    g = group_photos(big + small, HOMES, [])
    assert g.assignments["a0"] == "t"
    assert g.assignments["b0"] == "new:0"


def test_trip_stats():
    ps = photos("a", LISBON, day=1, count=2) + photos("b", MADRID, day=2, count=1) + [
        TripPhoto("nogps", at(1, 20), None, None)
    ]
    s = trip_stats(ps)
    assert s.photo_count == 4
    assert s.start == at(1, 10) and s.end == at(2, 10)
    assert s.distance_km == pytest.approx(503, abs=3)
    assert s.bbox == (*LISBON, *MADRID)  # Lisbon is south-west of Madrid
