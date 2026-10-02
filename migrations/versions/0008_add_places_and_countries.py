"""add places, countries, and photo locations

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-01
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

# A point built from lat/lon columns. Note the order: PostGIS points are (x, y), so
# longitude comes first. Generated, so it can never disagree with the raw columns.
POINT = """
    geography(Point, 4326) GENERATED ALWAYS AS (
        CASE WHEN lat IS NOT NULL AND lon IS NOT NULL
             THEN ST_SetSRID(ST_MakePoint(lon, lat), 4326)::geography END
    ) STORED
"""


def upgrade() -> None:
    # PostGIS is installed in the image since M2; this makes sure it's enabled here too.
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")

    op.execute(f"ALTER TABLE photos ADD COLUMN location {POINT}")
    # GiST is the index type for shapes: it groups nearby points into boxes, so "what's
    # near here" and "nearest first" (the <-> operator) skip most of the table.
    op.execute("CREATE INDEX photos_location_idx ON photos USING gist (location)")

    # GeoNames places (cities1000: everywhere with 1,000+ people). Loaded by
    # `python -m app.geodata load`, not here: a migration describes the schema.
    op.execute(
        f"""
        CREATE TABLE places (
            geonameid    integer PRIMARY KEY,
            name         text NOT NULL,
            ascii_name   text NOT NULL,
            country_code text NOT NULL,
            admin1_code  text NOT NULL,
            population   bigint NOT NULL,
            feature_code text NOT NULL,
            timezone     text NOT NULL,
            lat          double precision NOT NULL,
            lon          double precision NOT NULL,
            location     {POINT}
        )
        """
    )
    op.execute("CREATE INDEX places_location_idx ON places USING gist (location)")

    # First-level regions (states, provinces), keyed like GeoNames: 'US.CA', 'PT.14'.
    op.execute("CREATE TABLE admin1_regions (code text PRIMARY KEY, name text NOT NULL)")

    # Natural Earth country shapes. geometry, not geography: shapes like Antarctica and
    # Fiji wrap a pole or the date line, which geography's curved edges handle badly,
    # and "is this point inside" needs no distances, so flat lat/lon maths is right.
    op.execute(
        """
        CREATE TABLE countries (
            id       serial PRIMARY KEY,
            iso_a2   text,
            name     text NOT NULL,
            geom     geometry(MultiPolygon, 4326) NOT NULL
        )
        """
    )
    op.execute("CREATE INDEX countries_geom_idx ON countries USING gist (geom)")

    # Derived from location and the place data, like shot_at is from camera_time:
    # recomputed, never edited. place_id is the place a person would name ("Lisbon");
    # nearest_place_id is the closest named place, often a neighbourhood ("Baixa").
    # Not foreign keys: reloading the place data replaces every row, and the photos are
    # re-matched in the same transaction. NULL until checked, which happens when a photo
    # arrives and again for every photo after the place data is reloaded.
    op.execute(
        """
        ALTER TABLE photos
            ADD COLUMN place_id                 integer,
            ADD COLUMN place_distance_m         real,
            ADD COLUMN nearest_place_id         integer,
            ADD COLUMN nearest_place_distance_m real,
            ADD COLUMN country_code             text,
            ADD COLUMN places_checked           boolean NOT NULL DEFAULT false
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE photos
            DROP COLUMN places_checked, DROP COLUMN country_code,
            DROP COLUMN nearest_place_distance_m, DROP COLUMN nearest_place_id,
            DROP COLUMN place_distance_m, DROP COLUMN place_id
        """
    )
    op.execute("DROP TABLE countries")
    op.execute("DROP TABLE admin1_regions")
    op.execute("DROP TABLE places")
    op.execute("DROP INDEX photos_location_idx")
    op.execute("ALTER TABLE photos DROP COLUMN location")
