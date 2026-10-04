import json
import zipfile

import pytest
from sqlalchemy import text

from app import geodata
from app.db import engine


def geonames_line(geonameid, name, lat, lon, country, admin1, population, feature="PPL", tz="Europe/Lisbon"):
    # The 19 tab-separated GeoNames columns; the loader reads 10 of them.
    fields = [str(geonameid), name, name, f"{name},Alt", str(lat), str(lon), "P", feature, country,
              "", admin1, "", "", "", str(population), "", "50", tz, "2024-01-01"]
    return "\t".join(fields) + "\n"


PLACES = [
    geonames_line(2267057, "Lisbon", 38.71667, -9.13333, "PT", "14", 517802, "PPLC"),
    geonames_line(2268339, "Belém", 38.69735, -9.21526, "PT", "14", 16528),
    geonames_line(3117735, "Madrid", 40.4165, -3.70256, "ES", "29", 3255944, "PPLC", "Europe/Madrid"),
    geonames_line(8010505, "Baixa", 38.7108, -9.1365, "PT", "14", 12765, "PPLX"),
]

# Two squares standing in for country borders, one marked the way Natural Earth marks
# France (ISO_A2 '-99', with the real code only in ISO_A2_EH).
COUNTRIES = {"type": "FeatureCollection", "features": [
    {"type": "Feature", "properties": {"NAME": "Portugal", "ISO_A2": "PT", "ISO_A2_EH": "PT"},
     "geometry": {"type": "Polygon", "coordinates": [[[-10, 37], [-7, 37], [-7, 42], [-10, 42], [-10, 37]]]}},
    {"type": "Feature", "properties": {"NAME": "Spain", "ISO_A2": "-99", "ISO_A2_EH": "ES"},
     "geometry": {"type": "MultiPolygon", "coordinates": [[[[-7, 36], [3, 36], [3, 44], [-7, 44], [-7, 36]]]]}},
]}


@pytest.fixture
def sample_files(tmp_path, monkeypatch):
    with zipfile.ZipFile(tmp_path / "cities1000.zip", "w") as z:
        z.writestr("cities1000.txt", "".join(PLACES))
    (tmp_path / "admin1CodesASCII.txt").write_text("PT.14\tLisbon\tLisbon\t2267056\nES.29\tMadrid\tMadrid\t3117732\n")
    (tmp_path / "ne_10m_admin_0_countries.geojson").write_text(json.dumps(COUNTRIES))
    monkeypatch.setattr(geodata, "DATA_DIR", tmp_path)
    monkeypatch.setattr(geodata, "SOURCES", {name: "http://never-downloaded" for name in geodata.SOURCES})
    return tmp_path


def test_parses_the_geonames_columns_it_needs():
    (row,) = geodata.parse_places([PLACES[0]])
    assert row == (2267057, "Lisbon", "Lisbon", "PT", "14", 517802, "PPLC", "Europe/Lisbon", 38.71667, -9.13333)


def test_uses_the_cleaned_up_country_code_and_drops_unknown_ones():
    features = {"features": [
        {"properties": {"NAME": "France", "ISO_A2": "-99", "ISO_A2_EH": "FR"}, "geometry": {}},
        {"properties": {"NAME": "N. Cyprus", "ISO_A2": "-99", "ISO_A2_EH": "-99"}, "geometry": {}},
    ]}
    assert [(iso, name) for iso, name, _ in geodata.parse_countries(features)] == [("FR", "France"), (None, "N. Cyprus")]


def test_loads_places_regions_and_countries_and_marks_photos_for_rechecking(client, sample_files):
    with engine.begin() as conn:
        conn.execute(text("UPDATE photos SET places_checked = true"))

    result = geodata.load()

    assert (result["places"], result["regions"], result["countries"]) == (4, 2, 2)
    with engine.connect() as conn:
        # The generated location column was filled from lat/lon.
        assert conn.execute(text("SELECT count(*) FROM places WHERE location IS NOT NULL")).scalar() == 4
        # Nearest place to the Torre de Belém, ordered by true distance through the index.
        nearest = conn.execute(text(
            "SELECT name FROM places ORDER BY location <-> 'SRID=4326;POINT(-9.2160 38.6916)'::geography LIMIT 1"
        )).scalar()
        assert nearest == "Belém"
        # Which country shape contains a point in Madrid.
        country = conn.execute(text(
            "SELECT iso_a2 FROM countries WHERE ST_Covers(geom, 'SRID=4326;POINT(-3.70 40.42)'::geometry)"
        )).scalar()
        assert country == "ES"
        assert conn.execute(text("SELECT bool_or(places_checked) FROM photos")).scalar() in (False, None)


def test_loading_again_replaces_rather_than_duplicates(client, sample_files):
    geodata.load()
    assert geodata.load()["places"] == 4
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM places")).scalar() == 4


def test_if_empty_loads_into_an_empty_database(client, sample_files):
    assert geodata.load(if_empty=True)["places"] == 4


def test_if_empty_skips_without_downloading_when_places_are_already_loaded(client, sample_files):
    geodata.load()
    for path in sample_files.iterdir():
        path.unlink()  # any attempt to fetch would now try the never-downloaded URLs

    assert geodata.load(if_empty=True) == {"skipped": "place data is already loaded"}
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM places")).scalar() == 4


def test_copy_is_used_by_the_benchmark_and_all_methods_insert_every_row(client, sample_files):
    result = geodata.benchmark(rows=4)
    assert result["rows"] == 4
    assert set(result) == {"rows", "one_insert_per_row", "executemany", "copy"}


def upload_at(client, name, lat, lon):
    from tests.test_api import jpeg, upload

    return upload(client, name, jpeg("2025:07:01 10:00:00", "+01:00", lat, lon))


def place_of(client, photo):
    p = client.get(f"/api/photos/{photo['id']}/metadata").json()
    return p["place_name"], p["region_name"], p["country_name"], p["country_code"]


def test_a_new_photo_gets_its_nearest_place_region_and_country(client, sample_files):
    geodata.load()
    photo = upload_at(client, "belem.jpg", 38.6916, -9.2160)  # Torre de Belém

    assert place_of(client, photo) == ("Belém", "Lisbon", "Portugal", "PT")


def test_a_photo_just_off_the_coast_still_gets_a_country_but_not_one_at_sea(client, sample_files):
    geodata.load()
    # The sample "Portugal" ends at longitude -10: 0.1 degrees west is ~9 km offshore.
    boat = upload_at(client, "boat.jpg", 38.70, -10.1)
    ocean = upload_at(client, "ocean.jpg", 38.70, -11.0)  # ~87 km out

    assert place_of(client, boat)[3] == "PT"
    assert place_of(client, ocean)[3] is None


def test_photos_are_rematched_when_place_data_is_reloaded(client, sample_files):
    photo = upload_at(client, "belem.jpg", 38.6916, -9.2160)
    assert place_of(client, photo) == (None, None, None, None)  # no place data yet

    assert geodata.load()["photos"] >= 1

    assert place_of(client, photo)[0] == "Belém"


def names(client, photo):
    p = client.get(f"/api/photos/{photo['id']}/metadata").json()
    return p["place_name"], p["nearest_place_name"]


def test_a_photo_in_a_city_centre_is_named_after_the_city_not_the_neighbourhood(client, sample_files):
    geodata.load()
    # Praça do Comércio: 370 m from Baixa (radius 2.3 km), 1 km from Lisbon's point (14 km).
    photo = upload_at(client, "praca.jpg", 38.7075, -9.1365)

    assert names(client, photo) == ("Lisbon", "Baixa")


def test_a_small_place_right_next_to_the_photo_beats_a_big_city_further_away(client, sample_files):
    geodata.load()
    photo = upload_at(client, "belem.jpg", 38.6916, -9.2160)  # 650 m from Belém, 7 km from Lisbon

    assert names(client, photo) == ("Belém", "Belém")


def test_a_photo_beyond_every_places_radius_gets_the_nearest_place(client, sample_files):
    geodata.load()
    photo = upload_at(client, "countryside.jpg", 39.2, -8.6)  # ~60 km from Lisbon

    place, nearest = names(client, photo)
    assert place == nearest == "Lisbon"


def test_every_photo_in_a_bulk_import_gets_a_place_whichever_finishes_first(client, sample_files):
    # An import creates every photo up front as pending, with no coordinates yet. The
    # first one to finish recomputes derived values for all photos; the rest must still
    # be matched when their own files arrive.
    from tests.test_api import jpeg
    from tests.test_imports import complete, describe, put, start_import

    geodata.load()
    first = jpeg("2025:07:01 10:00:00", "+01:00", 38.7075, -9.1365)
    second = jpeg("2025:07:01 11:00:00", "+01:00", 40.4169, -3.7035)
    imp = start_import(client, describe(first, name="a.jpg"), describe(second, name="b.jpg"))
    item_a, item_b = imp["items"]

    assert put(item_a, first).status_code == 200
    complete(client, imp, item_a)  # the other photo is still pending at this point
    assert put(item_b, second).status_code == 200
    done_b = complete(client, imp, item_b)

    photo_b = client.get(f"/api/photos/{done_b['photo_id']}/metadata").json()
    assert (photo_b["place_name"], photo_b["country_code"]) == ("Madrid", "ES")
