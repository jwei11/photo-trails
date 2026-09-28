"""Bulk imports: a batch of files, each tracked from upload URL to processed photo.

Item status: uploading -> done | duplicate | failed. A failed item records the stage:
'upload' means the file has to be sent again; 'processing' means it's in storage and
completing again retries on the server.
"""
import logging

from sqlalchemy import Connection, text

from app import objects, storage
from app.db import engine

log = logging.getLogger(__name__)

ITEM_COLUMNS = """
    id, position, filename, content_type, size_bytes, declared_sha256,
    status, failed_stage, error, photo_id
"""


class ImportNotFound(Exception):
    pass


class InvalidItemState(Exception):
    pass


def create_import(files: list[dict]) -> dict:
    """Register a batch. Files already stored come back as duplicates and need no upload.

    The same file twice within one batch is uploaded twice and resolved when the second one
    completes. The browser drops such repeats before sending; marking them duplicates up front
    would claim a photo exists before the first copy has actually arrived.
    """
    existing = storage.find_by_hashes(sorted({f["sha256"] for f in files}))

    with engine.begin() as conn:
        import_id = str(conn.execute(text("INSERT INTO imports DEFAULT VALUES RETURNING id")).scalar_one())
        for position, f in enumerate(files):
            sha = f["sha256"]
            if sha in existing:
                status, photo_id = "duplicate", existing[sha]
            else:
                pending = storage.create_pending(f["filename"], f["content_type"], f["size_bytes"], conn)
                status, photo_id = "uploading", pending["id"]
            conn.execute(
                text(
                    """
                    INSERT INTO import_items (import_id, position, filename, content_type,
                        size_bytes, declared_sha256, status, photo_id)
                    VALUES (:import_id, :position, :filename, :content_type, :size, :sha,
                        :status, :photo_id)
                    """
                ),
                {
                    "import_id": import_id, "position": position, "filename": f["filename"],
                    "content_type": f["content_type"], "size": f["size_bytes"], "sha": sha,
                    "status": status, "photo_id": photo_id,
                },
            )
    result = get_import(import_id)
    uploading = [item for item in result["items"] if item["status"] == "uploading"]
    for item, url in zip(uploading, _signed_upload_urls(uploading)):
        item["upload_url"] = url
    return result


def get_import(import_id: str) -> dict:
    with engine.connect() as conn:
        created = conn.execute(
            text("SELECT created_at FROM imports WHERE id = :id"), {"id": import_id}
        ).scalar_one_or_none()
        if created is None:
            raise ImportNotFound
        items = [
            _item_dict(r)
            for r in conn.execute(
                text(f"SELECT {ITEM_COLUMNS} FROM import_items WHERE import_id = :id ORDER BY position"),
                {"id": import_id},
            )
        ]
    return {"id": import_id, "created_at": created, "counts": _counts(items), "items": items}


def list_imports(limit: int = 20) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT i.id, i.created_at, count(*) AS total,
                       count(*) FILTER (WHERE it.status = 'uploading') AS uploading,
                       count(*) FILTER (WHERE it.status = 'done') AS done,
                       count(*) FILTER (WHERE it.status = 'duplicate') AS duplicate,
                       count(*) FILTER (WHERE it.status = 'failed') AS failed
                FROM imports i JOIN import_items it ON it.import_id = i.id
                GROUP BY i.id ORDER BY i.created_at DESC LIMIT :limit
                """
            ),
            {"limit": limit},
        )
        return [
            {
                "id": str(r.id),
                "created_at": r.created_at,
                "counts": {k: getattr(r, k) for k in ("total", "uploading", "done", "duplicate", "failed")},
            }
            for r in rows
        ]


def renew_upload_url(import_id: str, item_id: str) -> dict:
    """A fresh upload URL, for retrying a failed upload or after the first one expired."""
    with engine.begin() as conn:
        item = _get_item(conn, import_id, item_id, for_update=True)
        if item["status"] in ("done", "duplicate"):
            raise InvalidItemState("This file is already imported.")
        if item["failed_stage"] == "processing":
            raise InvalidItemState("This file is already uploaded; retry processing instead.")
        photo_status = item["photo_id"] and conn.execute(
            text("SELECT status FROM photos WHERE id = :id"), {"id": item["photo_id"]}
        ).scalar_one_or_none()
        if photo_status == "ready":
            # Completion finished but the item wasn't updated (e.g. a crash in between).
            _set_status(conn, item_id, "done", photo_id=item["photo_id"])
            return _get_item(conn, import_id, item_id)
        if photo_status != "pending":  # swept as abandoned; start a new pending photo
            item["photo_id"] = storage.create_pending(
                item["filename"], item["content_type"], item["size_bytes"], conn
            )["id"]
        _set_status(conn, item_id, "uploading", photo_id=item["photo_id"])
    item = get_item(import_id, item_id)
    item["upload_url"] = _signed_upload_urls([item])[0]
    return item


def complete_item(import_id: str, item_id: str) -> dict:
    """Process an uploaded file. Safe to call again, including to retry a processing failure."""
    with engine.connect() as conn:
        item = _get_item(conn, import_id, item_id)
    if item["status"] in ("done", "duplicate"):
        return item

    status, stage, error, photo_id = "done", None, None, item["photo_id"]
    if photo_id is None:
        status, stage, error = "failed", "upload", "The upload expired. Upload the file again."
    else:
        try:
            photo_id = storage.complete_upload(photo_id)["id"]
        except storage.DuplicatePhoto as dup:
            status, photo_id = "duplicate", dup.original_id
        except storage.UploadNotReceived:
            status, stage, error = "failed", "upload", "Storage didn't receive the complete file."
        except storage.UploadNotFound:
            status, stage, error = "failed", "upload", "The upload expired. Upload the file again."
        except Exception as exc:
            log.exception("processing failed for import item %s", item_id)
            status, stage, error = "failed", "processing", f"{type(exc).__name__}: {exc}"

    with engine.begin() as conn:
        _set_status(conn, item_id, status, stage, error, photo_id)
    return get_item(import_id, item_id)


def report_upload_failure(import_id: str, item_id: str, error: str) -> dict:
    with engine.begin() as conn:
        item = _get_item(conn, import_id, item_id, for_update=True)
        if item["status"] == "uploading":
            _set_status(conn, item_id, "failed", "upload", error, item["photo_id"])
    return get_item(import_id, item_id)


def get_item(import_id: str, item_id: str) -> dict:
    with engine.connect() as conn:
        return _get_item(conn, import_id, item_id)


def _get_item(conn: Connection, import_id: str, item_id: str, for_update: bool = False) -> dict:
    row = conn.execute(
        text(
            f"SELECT {ITEM_COLUMNS} FROM import_items WHERE id = :item AND import_id = :import"
            + (" FOR UPDATE" if for_update else "")
        ),
        {"item": item_id, "import": import_id},
    ).first()
    if row is None:
        raise ImportNotFound
    return _item_dict(row)


def _set_status(
    conn: Connection, item_id: str, status: str, stage: str | None = None,
    error: str | None = None, photo_id: str | None = None,
) -> None:
    conn.execute(
        text(
            """
            UPDATE import_items SET status = :status, failed_stage = :stage, error = :error,
                photo_id = :photo_id, updated_at = now()
            WHERE id = :id
            """
        ),
        {"id": item_id, "status": status, "stage": stage, "error": error, "photo_id": photo_id},
    )


def _signed_upload_urls(items: list[dict]) -> list[str]:
    """Sign upload URLs, recording the time so the orphan sweep knows the upload may be active."""
    if not items:
        return []
    with engine.begin() as conn:
        keys = dict(
            conn.execute(
                text(
                    "UPDATE photos SET upload_url_issued_at = now() WHERE id = ANY(CAST(:ids AS uuid[])) "
                    "RETURNING id::text, storage_key"
                ),
                {"ids": [item["photo_id"] for item in items]},
            ).all()
        )
    return [objects.upload_url(keys[i["photo_id"]], i["content_type"], i["size_bytes"]) for i in items]


def _item_dict(row) -> dict:
    item = dict(row._mapping)
    item["id"] = str(item["id"])
    item["photo_id"] = str(item["photo_id"]) if item["photo_id"] else None
    return item


def _counts(items: list[dict]) -> dict:
    counts = {"total": len(items), "uploading": 0, "done": 0, "duplicate": 0, "failed": 0}
    for item in items:
        counts[item["status"]] += 1
    return counts
