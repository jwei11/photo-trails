"""add exif and time columns

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-25
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE photos
            -- raw values as read from the file; never modified after upload
            ADD COLUMN camera_make      text,
            ADD COLUMN camera_model     text,
            ADD COLUMN camera_serial    text,
            ADD COLUMN camera_time      timestamp,
            ADD COLUMN exif_offset_min  smallint,
            ADD COLUMN gps_time         timestamptz,
            ADD COLUMN lat              double precision CHECK (lat BETWEEN -90 AND 90),
            ADD COLUMN lon              double precision CHECK (lon BETWEEN -180 AND 180),
            ADD COLUMN altitude_m       double precision,
            ADD COLUMN width            integer,
            ADD COLUMN height           integer,
            ADD COLUMN orientation      smallint,
            ADD COLUMN exif             jsonb,
            -- derived by time resolution; recomputed whenever inputs change
            ADD COLUMN shot_at          timestamptz,
            ADD COLUMN tz_name          text,
            ADD COLUMN local_offset_min smallint,
            ADD COLUMN time_source      text CHECK (time_source IN
                ('exif_offset', 'gps', 'coordinates', 'neighbour', 'default_zone')),
            -- processing results
            ADD COLUMN thumb_key        text,
            ADD COLUMN web_key          text,
            ADD COLUMN processing_error text,
            ADD COLUMN processing_ms    integer,
            ADD CONSTRAINT photos_lat_lon_together CHECK ((lat IS NULL) = (lon IS NULL))
        """
    )
    op.execute("CREATE INDEX photos_shot_at_idx ON photos (shot_at)")
    op.execute(
        """
        CREATE TABLE camera_clock_offsets (
            id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            camera_make    text NOT NULL,
            camera_model   text NOT NULL,
            camera_serial  text,
            offset_seconds integer NOT NULL,
            valid_from     timestamp,
            valid_to       timestamp,
            note           text,
            created_at     timestamptz NOT NULL DEFAULT now(),
            CHECK (valid_from IS NULL OR valid_to IS NULL OR valid_from < valid_to)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE camera_clock_offsets")
    op.execute("DROP INDEX photos_shot_at_idx")
    op.execute(
        """
        ALTER TABLE photos
            DROP CONSTRAINT photos_lat_lon_together,
            DROP COLUMN camera_make, DROP COLUMN camera_model, DROP COLUMN camera_serial,
            DROP COLUMN camera_time, DROP COLUMN exif_offset_min, DROP COLUMN gps_time,
            DROP COLUMN lat, DROP COLUMN lon, DROP COLUMN altitude_m,
            DROP COLUMN width, DROP COLUMN height, DROP COLUMN orientation, DROP COLUMN exif,
            DROP COLUMN shot_at, DROP COLUMN tz_name, DROP COLUMN local_offset_min,
            DROP COLUMN time_source, DROP COLUMN thumb_key, DROP COLUMN web_key,
            DROP COLUMN processing_error, DROP COLUMN processing_ms
        """
    )
