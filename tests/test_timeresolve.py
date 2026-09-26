from datetime import datetime, timedelta, timezone

from app.timeresolve import UNDATED, ClockOffset, PhotoTimeInput, resolve_all

LISBON = (38.72, -9.14)
DENVER = (39.74, -104.99)
TOKYO = (35.68, 139.69)
ZONES = {LISBON: "Europe/Lisbon", DENVER: "America/Denver", TOKYO: "Asia/Tokyo"}

PHONE = ("Apple", "iPhone 15", None)
CAMERA = ("Fujifilm", "X-T5", "SN123")


def lookup(lat, lon):
    return ZONES.get((lat, lon))


def photo(id, camera=PHONE, camera_time=None, offset=None, gps_time=None, at=None):
    lat, lon = at if at else (None, None)
    return PhotoTimeInput(id, camera, camera_time, offset, gps_time, lat, lon)


def resolve(*photos, offsets=(), default_tz="America/Los_Angeles"):
    return resolve_all(photos, offsets, lookup, default_tz)


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def test_same_wall_clock_time_in_two_cities_is_hours_apart():
    r = resolve(
        photo("lisbon", camera_time=datetime(2025, 7, 1, 14, 0), at=LISBON),
        photo("denver", camera_time=datetime(2025, 7, 1, 14, 0), at=DENVER),
    )
    assert r["lisbon"].shot_at == utc(2025, 7, 1, 13, 0)  # Lisbon is UTC+1 in summer
    assert r["denver"].shot_at == utc(2025, 7, 1, 20, 0)  # Denver is UTC-6 in summer
    assert r["denver"].shot_at - r["lisbon"].shot_at == timedelta(hours=7)
    assert r["lisbon"].time_source == "coordinates"


def test_daylight_saving_is_applied_by_date():
    r = resolve(photo("winter", camera_time=datetime(2025, 1, 15, 12, 0), at=LISBON))
    assert r["winter"].shot_at == utc(2025, 1, 15, 12, 0)  # Lisbon is UTC+0 in winter
    assert r["winter"].local_offset_min == 0


def test_camera_left_on_home_zone_still_gets_right_instant_and_local_display():
    # Camera still on New York time (-04:00) while shooting in Lisbon.
    r = resolve(photo("p", camera_time=datetime(2025, 7, 1, 9, 0), offset=-240, at=LISBON))
    assert r["p"].shot_at == utc(2025, 7, 1, 13, 0)
    assert r["p"].time_source == "exif_offset"
    assert r["p"].tz_name == "Europe/Lisbon"
    assert r["p"].local_time == datetime(2025, 7, 1, 14, 0)


def test_gps_time_beats_a_wrong_camera_clock():
    r = resolve(
        photo(
            "p",
            camera=CAMERA,
            camera_time=datetime(2025, 7, 1, 3, 0),  # clock is nonsense
            gps_time=utc(2025, 7, 1, 13, 0),
            at=LISBON,
        )
    )
    assert r["p"].shot_at == utc(2025, 7, 1, 13, 0)
    assert r["p"].time_source == "gps"


def test_photo_without_gps_borrows_zone_from_nearest_neighbour():
    r = resolve(
        photo("phone1", camera_time=datetime(2025, 4, 2, 10, 0), at=TOKYO),
        photo("phone2", camera_time=datetime(2025, 4, 2, 18, 0), at=TOKYO),
        photo("camera", camera=CAMERA, camera_time=datetime(2025, 4, 2, 12, 30)),
    )
    assert r["camera"].tz_name == "Asia/Tokyo"
    assert r["camera"].time_source == "neighbour"
    assert r["camera"].shot_at == utc(2025, 4, 2, 3, 30)


def test_neighbour_too_far_away_falls_back_to_default_zone():
    r = resolve(
        photo("tokyo", camera_time=datetime(2025, 4, 2, 10, 0), at=TOKYO),
        photo("camera", camera=CAMERA, camera_time=datetime(2025, 4, 9, 10, 0)),
    )
    assert r["camera"].time_source == "default_zone"
    assert r["camera"].tz_name == "America/Los_Angeles"


def test_recorded_offset_without_location_displays_in_that_offset():
    r = resolve(photo("p", camera_time=datetime(2025, 3, 1, 8, 0), offset=330))
    assert r["p"].shot_at == utc(2025, 3, 1, 2, 30)
    assert r["p"].tz_name is None
    assert r["p"].local_time == datetime(2025, 3, 1, 8, 0)


def test_clock_offset_corrects_one_camera_only():
    offsets = [ClockOffset(CAMERA, offset_seconds=3600)]  # camera was an hour slow
    r = resolve(
        photo("cam", camera=CAMERA, camera_time=datetime(2025, 7, 1, 13, 0), at=LISBON),
        photo("phone", camera=PHONE, camera_time=datetime(2025, 7, 1, 13, 0), at=LISBON),
        offsets=offsets,
    )
    assert r["cam"].local_time == datetime(2025, 7, 1, 14, 0)
    assert r["phone"].local_time == datetime(2025, 7, 1, 13, 0)


def test_clock_offset_respects_its_valid_range():
    offsets = [ClockOffset(CAMERA, 3600, valid_from=datetime(2025, 7, 1))]
    r = resolve(
        photo("before", camera=CAMERA, camera_time=datetime(2025, 6, 30, 12, 0), at=LISBON),
        photo("after", camera=CAMERA, camera_time=datetime(2025, 7, 2, 12, 0), at=LISBON),
        offsets=offsets,
    )
    assert r["before"].local_time == datetime(2025, 6, 30, 12, 0)
    assert r["after"].local_time == datetime(2025, 7, 2, 13, 0)


def test_photo_with_no_time_information_is_undated():
    r = resolve(photo("screenshot"), photo("located_but_undated", at=LISBON))
    assert r["screenshot"] == UNDATED
    assert r["located_but_undated"] == UNDATED


def test_merged_photographers_sort_by_real_time_not_clock_time():
    # Two friends on the same day: one in Lisbon, one in Denver.
    r = resolve(
        photo("lisbon_4pm", camera_time=datetime(2025, 7, 1, 16, 0), at=LISBON),
        photo("denver_1pm", camera_time=datetime(2025, 7, 1, 13, 0), at=DENVER),
    )
    by_clock = sorted(["lisbon_4pm", "denver_1pm"], key=lambda k: k.split("_")[1])
    by_instant = sorted(r, key=lambda k: r[k].shot_at)
    assert by_clock == ["denver_1pm", "lisbon_4pm"]
    assert by_instant == ["lisbon_4pm", "denver_1pm"]  # 15:00Z comes before 19:00Z
