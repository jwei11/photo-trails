import re
from pathlib import Path

from sqlalchemy import text

from app.config import UPLOAD_DIR
from app.db import engine

PHOTO_COLUMNS = "id, original_filename, content_type, size_bytes, storage_key, created_at"


def file_path(storage_key: str) -> Path:
    return UPLOAD_DIR / storage_key


def all_photos() -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(text(f"SELECT {PHOTO_COLUMNS} FROM photos ORDER BY created_at"))
        return [dict(row._mapping) for row in rows]


def get_photo(photo_id: str) -> dict | None:
    with engine.connect() as conn:
        row = conn.execute(
            text(f"SELECT {PHOTO_COLUMNS} FROM photos WHERE id = :id"), {"id": photo_id}
        ).first()
        return dict(row._mapping) if row else None


def _safe_extension(filename: str | None) -> str:
    ext = Path(filename or "").suffix.lower()
    return ext if re.fullmatch(r"\.[a-z0-9]{1,10}", ext) else ""


def add_photo(original_filename: str | None, content_type: str, contents: bytes) -> dict:
    with engine.begin() as conn:
        row = conn.execute(
            text(
                f"""
                WITH new AS (SELECT gen_random_uuid() AS id)
                INSERT INTO photos (id, original_filename, content_type, size_bytes, storage_key)
                SELECT new.id, :filename, :content_type, :size, new.id::text || :ext FROM new
                RETURNING {PHOTO_COLUMNS}
                """
            ),
            {
                "filename": original_filename,
                "content_type": content_type,
                "size": len(contents),
                "ext": _safe_extension(original_filename),
            },
        ).one()
        file_path(row.storage_key).write_bytes(contents)
        return dict(row._mapping)
