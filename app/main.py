import logging
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, model_validator

from app import storage
from app.config import UPLOAD_DIR

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

app = FastAPI()

UPLOAD_DIR.mkdir(exist_ok=True)
STATIC_DIR = Path(__file__).parent / "static"


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def timeline_page():
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/photos", status_code=201)
async def upload_photo(file: UploadFile):
    contents = await file.read()
    return await run_in_threadpool(
        storage.add_photo,
        file.filename, file.content_type or "application/octet-stream", contents
    )


@app.get("/photos")
def list_photos():
    return storage.all_photos()


def _photo_or_404(photo_id: uuid.UUID, include_exif: bool = False) -> dict:
    record = storage.get_photo(str(photo_id), include_exif=include_exif)
    if record is None:
        raise HTTPException(status_code=404, detail="Photo not found")
    return record


@app.get("/photos/{photo_id}")
def get_photo_file(photo_id: uuid.UUID):
    record = _photo_or_404(photo_id)
    return FileResponse(storage.file_path(record["storage_key"]), media_type=record["content_type"])


@app.get("/photos/{photo_id}/metadata")
def get_photo_metadata(photo_id: uuid.UUID):
    return _photo_or_404(photo_id, include_exif=True)


@app.get("/photos/{photo_id}/thumb")
def get_thumb(photo_id: uuid.UUID):
    return _derivative(photo_id, "thumb_key")


@app.get("/photos/{photo_id}/web")
def get_web(photo_id: uuid.UUID):
    return _derivative(photo_id, "web_key")


def _derivative(photo_id: uuid.UUID, key: str):
    record = _photo_or_404(photo_id)
    if record[key] is None:
        raise HTTPException(status_code=404, detail="No preview for this file")
    return FileResponse(
        storage.file_path(record[key]),
        media_type="image/webp",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@app.get("/timeline")
def timeline():
    days: dict[str, list] = {}
    undated = []
    for p in storage.all_photos():
        if p["shot_at"] is None:
            undated.append(p)
            continue
        local = (p["shot_at"] + timedelta(minutes=p["local_offset_min"])).replace(tzinfo=None)
        p["local_time"] = local
        days.setdefault(local.date().isoformat(), []).append(p)
    return {
        "days": [{"date": d, "photos": ps} for d, ps in sorted(days.items())],
        "undated": undated,
    }


@app.get("/cameras")
def list_cameras():
    return storage.list_cameras()


class ClockOffsetIn(BaseModel):
    camera_make: str
    camera_model: str
    camera_serial: str | None = None
    offset_seconds: int = Field(ge=-7 * 24 * 3600, le=7 * 24 * 3600)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    note: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _range_order(self):
        for value in (self.valid_from, self.valid_to):
            if value is not None and value.tzinfo is not None:
                raise ValueError("valid_from/valid_to are camera clock times: omit the timezone")
        if self.valid_from and self.valid_to and self.valid_from >= self.valid_to:
            raise ValueError("valid_from must be before valid_to")
        return self


@app.get("/camera-offsets")
def list_offsets():
    return storage.list_offsets()


@app.post("/camera-offsets", status_code=201)
def add_offset(body: ClockOffsetIn):
    try:
        return storage.add_offset(body.model_dump())
    except storage.OffsetOverlapError:
        raise HTTPException(
            status_code=409,
            detail="This camera already has an offset covering these dates. Remove it first.",
        )


@app.delete("/camera-offsets/{offset_id}", status_code=204)
def delete_offset(offset_id: uuid.UUID):
    if storage.delete_offset(str(offset_id)) is None:
        raise HTTPException(status_code=404, detail="Offset not found")
    return Response(status_code=204)
