"""Load the place data: GeoNames places and regions, Natural Earth country borders.

Run: python -m app.geodata load               downloads to data/geo/ the first time
     python -m app.geodata benchmark [--rows N]  compares insert methods on a sample

Loading replaces all place data in one transaction, so the app never sees it half-loaded,
and marks every photo to be matched to places again.
"""
import argparse
import io
import json
import logging
import time
import zipfile
from collections.abc import Iterable, Iterator
from pathlib import Path

import httpx
from sqlalchemy import Connection, text

from app import storage
from app.db import engine

log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data" / "geo"
GEONAMES = "https://download.geonames.org/export/dump"
SOURCES = {
    # Every place with 1,000+ people: about 150,000 rows, ~10 MB zipped.
    "cities1000.zip": f"{GEONAMES}/cities1000.zip",
    # Names of first-level regions (states, provinces): ~4,000 rows.
    "admin1CodesASCII.txt": f"{GEONAMES}/admin1CodesASCII.txt",
    # Country borders at 1:10 million scale, the most detailed Natural Earth offers.
    "ne_10m_admin_0_countries.geojson": "https://raw.githubusercontent.com/nvkelso/"
    "natural-earth-vector/master/geojson/ne_10m_admin_0_countries.geojson",
}

PLACE_COLUMNS = (
    "geonameid", "name", "ascii_name", "country_code", "admin1_code",
    "population", "feature_code", "timezone", "lat", "lon",
)


def fetch(name: str) -> Path:
    """The local copy of a source file, downloading it the first time."""
    path = DATA_DIR / name
    if path.exists():
        return path
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".partial")
    log.info("downloading %s", SOURCES[name])
    with httpx.stream("GET", SOURCES[name], follow_redirects=True, timeout=60) as res:
        res.raise_for_status()
        with partial.open("wb") as out:
            for chunk in res.iter_bytes():
                out.write(chunk)
    partial.rename(path)  # only a complete download gets the real name
    return path


def parse_places(lines: Iterable[str]) -> Iterator[tuple]:
    """GeoNames rows: 19 tab-separated fields per line, documented at download.geonames.org."""
    for line in lines:
        f = line.rstrip("\n").split("\t")
        yield (
            int(f[0]), f[1], f[2], f[8], f[10],
            int(f[14] or 0), f[7], f[17], float(f[4]), float(f[5]),
        )


def read_places(zip_path: Path) -> Iterator[tuple]:
    with zipfile.ZipFile(zip_path) as z, z.open("cities1000.txt") as raw:
        yield from parse_places(io.TextIOWrapper(raw, encoding="utf-8"))


def parse_admin1(lines: Iterable[str]) -> Iterator[tuple]:
    for line in lines:
        code, name, _ascii_name, _geonameid = line.rstrip("\n").split("\t")
        yield code, name


def parse_countries(geojson: dict) -> Iterator[tuple]:
    for feature in geojson["features"]:
        props = feature["properties"]
        # ISO_A2 is '-99' for a few countries (France, Norway) for historical reasons;
        # ISO_A2_EH ("eh": a cleaned-up version) fixes most of them.
        iso = props.get("ISO_A2_EH") or props.get("ISO_A2")
        yield (iso if iso and iso != "-99" else None), props["NAME"], json.dumps(feature["geometry"])


def copy_rows(conn: Connection, table: str, columns: tuple, rows: Iterable[tuple]) -> int:
    """Stream rows into a table with COPY: one continuous stream instead of a statement
    (and a network round trip) per row."""
    cursor = conn.connection.driver_connection.cursor()
    count = 0
    with cursor.copy(f"COPY {table} ({', '.join(columns)}) FROM STDIN") as copy:
        for row in rows:
            copy.write_row(row)
            count += 1
    return count


def load() -> dict:
    paths = {name: fetch(name) for name in SOURCES}
    timings = {}
    with engine.begin() as conn:
        storage.lock_writes(conn)
        conn.execute(text("TRUNCATE places, admin1_regions, countries"))

        start = time.perf_counter()
        places = copy_rows(conn, "places", PLACE_COLUMNS, read_places(paths["cities1000.zip"]))
        timings["places_s"] = round(time.perf_counter() - start, 2)

        with paths["admin1CodesASCII.txt"].open(encoding="utf-8") as f:
            regions = copy_rows(conn, "admin1_regions", ("code", "name"), parse_admin1(f))

        start = time.perf_counter()
        geojson = json.loads(paths["ne_10m_admin_0_countries.geojson"].read_text(encoding="utf-8"))
        countries = list(parse_countries(geojson))
        # Borders at this detail can have tiny self-intersections that make "is this
        # point inside" unreliable; ST_MakeValid repairs them. ST_Multi makes every shape a
        # MultiPolygon, since most countries are several pieces (islands).
        conn.execute(
            text(
                """
                INSERT INTO countries (iso_a2, name, geom)
                VALUES (:iso, :name, ST_Multi(ST_CollectionExtract(
                    ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(:geometry), 4326)), 3)))
                """
            ),
            [{"iso": iso, "name": name, "geometry": geometry} for iso, name, geometry in countries],
        )
        timings["countries_s"] = round(time.perf_counter() - start, 2)

        # The planner estimates how many rows a query touches from table statistics; fresh
        # tables have none until analyzed, and it may then skip the spatial index.
        conn.execute(text("ANALYZE places"))
        conn.execute(text("ANALYZE countries"))
        # Every photo's place may have changed: match them all again, in the same
        # transaction, so no photo is ever seen pointing at a place that no longer exists.
        conn.execute(text("UPDATE photos SET places_checked = false"))
        start = time.perf_counter()
        matched = storage.resolve_places(conn)
        timings["photos_s"] = round(time.perf_counter() - start, 2)
    result = {"places": places, "regions": regions, "countries": len(countries), "photos": matched, **timings}
    log.info("loaded %s", result)
    return result


def benchmark(rows: int = 20_000) -> dict:
    """The same rows inserted three ways, into a throwaway table, for the README's numbers."""
    sample = []
    for row in read_places(fetch("cities1000.zip")):
        sample.append(row)
        if len(sample) == rows:
            break
    insert = text(
        "INSERT INTO bench (geonameid, name, ascii_name, country_code, admin1_code, population,"
        " feature_code, timezone, lat, lon) VALUES (:geonameid, :name, :ascii_name,"
        " :country_code, :admin1_code, :population, :feature_code, :timezone, :lat, :lon)"
    )
    params = [dict(zip(PLACE_COLUMNS, row)) for row in sample]
    result = {"rows": len(sample)}
    for method in ("one_insert_per_row", "executemany", "copy"):
        with engine.begin() as conn:
            conn.execute(text("CREATE TEMP TABLE bench (LIKE places) ON COMMIT DROP"))
            start = time.perf_counter()
            if method == "one_insert_per_row":
                for p in params:  # what a first attempt looks like: a round trip per row
                    conn.execute(insert, p)
            elif method == "executemany":
                conn.execute(insert, params)  # the driver batches the statements
            else:
                copy_rows(conn, "bench", PLACE_COLUMNS, sample)
            seconds = time.perf_counter() - start
        result[method] = {"seconds": round(seconds, 2), "rows_per_s": round(len(sample) / seconds)}
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("load", help="download (first time) and load all place data")
    bench = sub.add_parser("benchmark", help="time three insert methods on a sample")
    bench.add_argument("--rows", type=int, default=20_000)
    args = parser.parse_args()
    print(load() if args.command == "load" else benchmark(args.rows))
