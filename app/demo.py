"""The demo trip that "Try the demo" gives each guest a copy of. Load it once per server:

    python -m app.demo load <folder> --name "Death Valley and Sequoia"
    python -m app.demo load <folder> --name "..." --replace   # swap in a new set
    python -m app.demo status

Every image in the folder is uploaded to the `demo` account and processed here, in this
process (not by the worker), then the biggest trip is given the name. Camera and lens serial
numbers are blanked in the stored originals first: guests can download originals, and a
serial number identifies a particular camera.

Replacing deletes the old demo account. Guests who already started keep their copies: the
old files are still referenced by their rows, so the orphan sweep leaves them until the
last of those guests expires.
"""
import argparse
import io
import re
import sys
from pathlib import Path

from PIL import Image
from sqlalchemy import text

# storage imports app.images, which lets Pillow open HEIC.
from app import auth, exif, guests, storage, trip_store
from app.db import engine

DEMO_EMAIL = "demo@demo.invalid"
CONTENT_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".heic": "image/heic", ".heif": "image/heif",
                 ".png": "image/png", ".webp": "image/webp"}
SERIAL_TAG = re.compile(r"serial", re.IGNORECASE)


def scrub_serials(data: bytes) -> bytes:
    """The file with every serial number its metadata holds replaced by zeros of the same
    length, so nothing else in the file moves. In a JPEG only the part before the image
    data (the metadata segments) is touched, so pixels are never altered by a chance match."""
    with Image.open(io.BytesIO(data)) as img:
        tags = exif.read_metadata(img).tags
    serials = {
        v for k, v in tags.items()
        if SERIAL_TAG.search(k) and isinstance(v, str) and len(v) >= 4 and v.strip("0")
    }
    image_data = data.find(b"\xff\xda") if data.startswith(b"\xff\xd8") else len(data)
    head, rest = data[:image_data], data[image_data:]
    for serial in serials:
        head = head.replace(serial.encode(), b"0" * len(serial.encode()))
    return head + rest


def load(folder: Path, name: str | None, replace: bool = False) -> dict:
    files = sorted(p for p in folder.iterdir() if p.suffix.lower() in CONTENT_TYPES)
    if not files:
        raise SystemExit(f"No images in {folder} (looked for {', '.join(CONTENT_TYPES)}).")
    with engine.begin() as conn:
        existing = guests.demo_user_id(conn)
        if existing and not replace:
            raise SystemExit("A demo is already loaded. Run again with --replace to swap it.")
        if existing:
            conn.execute(text("DELETE FROM users WHERE id = :id"), {"id": existing})
        demo = auth.create_user(DEMO_EMAIL, "demo", conn)

    loaded = duplicates = 0
    for path in files:
        try:
            storage.add_photo(demo.id, path.name, CONTENT_TYPES[path.suffix.lower()],
                              scrub_serials(path.read_bytes()), kind="demo")
            loaded += 1
        except storage.DuplicatePhoto:
            duplicates += 1
        print(f"  {path.name}", file=sys.stderr)
    with engine.begin() as conn:
        conn.execute(text("UPDATE photos SET camera_serial = NULL WHERE user_id = :id"), {"id": demo.id})

    trips = sorted(trip_store.list_trips(demo.id), key=lambda t: t["photo_count"], reverse=True)
    if name and trips:
        trip_store.rename_trip(demo.id, trips[0]["id"], name)
    return {"photos": loaded, "duplicates_skipped": duplicates, **status()}


def status() -> dict:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT count(*) FILTER (WHERE p.status = 'ready') AS ready,
                       count(p.lat) AS located,
                       count(DISTINCT p.trip_id) AS trips,
                       count(DISTINCT p.place_id) AS places,
                       (SELECT count(*) FROM users WHERE kind = 'guest' AND expires_at > now()) AS live_guests
                FROM users u LEFT JOIN photos p ON p.user_id = u.id
                WHERE u.kind = 'demo'
                """
            )
        ).one()
    return dict(row._mapping)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    load_cmd = commands.add_parser("load", help="load a folder of photos as the demo trip")
    load_cmd.add_argument("folder", type=Path)
    load_cmd.add_argument("--name", help="name for the demo trip")
    load_cmd.add_argument("--replace", action="store_true", help="replace the demo that's loaded")
    commands.add_parser("status", help="what the demo holds, and how many guests are live")
    args = parser.parse_args()
    if args.command == "load":
        print(load(args.folder, args.name, args.replace))
    else:
        print(status())
