import json
import threading
from pathlib import Path

DATA_DIR = Path("uploads")
METADATA_FILE = DATA_DIR / "metadata.json"

_lock = threading.Lock()


def _load() -> dict:
    if not METADATA_FILE.exists():
        return {}
    with METADATA_FILE.open("r") as f:
        return json.load(f)


def _save(data: dict) -> None:
    with METADATA_FILE.open("w") as f:
        json.dump(data, f, indent=2)


def all_photos() -> list[dict]:
    with _lock:
        return list(_load().values())


def get_photo(photo_id: str) -> dict | None:
    with _lock:
        return _load().get(photo_id)


def add_photo(record: dict) -> None:
    with _lock:
        data = _load()
        data[record["id"]] = record
        _save(data)
