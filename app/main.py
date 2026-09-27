import logging
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field, model_validator

from app import objects, storage, trip_store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await run_in_threadpool(objects.ensure_bucket)
    yield


app = FastAPI(lifespan=lifespan)

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


def _redirect_to_object(key: str) -> RedirectResponse:
    # The signed URL is valid for an hour; let the browser reuse this redirect for most of
    # that, so repeat views skip the API and hit its image cache instead.
    max_age = objects.DOWNLOAD_URL_TTL - 600
    return RedirectResponse(
        objects.download_url(key),
        status_code=302,
        headers={"Cache-Control": f"private, max-age={max_age}"},
    )


@app.get("/photos/{photo_id}")
def get_photo_file(photo_id: uuid.UUID):
    return _redirect_to_object(_photo_or_404(photo_id)["storage_key"])


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
    return _redirect_to_object(record[key])


def _by_local_day(photos: list[dict]) -> list[dict]:
    days: dict[str, list] = {}
    for p in photos:
        local = (p["shot_at"] + timedelta(minutes=p["local_offset_min"])).replace(tzinfo=None)
        p["local_time"] = local
        days.setdefault(local.date().isoformat(), []).append(p)
    return [{"date": d, "photos": ps} for d, ps in sorted(days.items())]


@app.get("/timeline")
def timeline():
    trips = trip_store.list_trips()
    photos_by_trip: dict[str | None, list] = {}
    undated_loose = []
    for p in storage.all_photos():
        trip_id = str(p["trip_id"]) if p["trip_id"] else None
        if trip_id is None and p["shot_at"] is None:
            undated_loose.append(p)
        else:
            photos_by_trip.setdefault(trip_id, []).append(p)

    def split_dated(photos):
        return [p for p in photos if p["shot_at"]], [p for p in photos if not p["shot_at"]]

    trip_sections = []
    for trip in trips:
        dated, undated = split_dated(photos_by_trip.get(trip["id"], []))
        trip_sections.append({**trip, "days": _by_local_day(dated), "undated": undated})
    return {
        "trips": trip_sections,
        "not_in_trip": _by_local_day(photos_by_trip.get(None, [])),
        "undated": undated_loose,
    }


class TripRename(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)


class TripMerge(BaseModel):
    trip_ids: list[uuid.UUID] = Field(min_length=1)


class TripSplit(BaseModel):
    at: datetime

    @model_validator(mode="after")
    def _aware(self):
        if self.at.tzinfo is None:
            raise ValueError("'at' must include a timezone, e.g. 2025-07-02T00:00:00Z")
        return self


class PhotoTrip(BaseModel):
    trip_id: uuid.UUID | None


class HomeIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    radius_km: float = Field(default=25, gt=0, le=500)


def _trip_errors(fn, *args):
    try:
        return fn(*args)
    except trip_store.NotFound:
        raise HTTPException(status_code=404, detail="Trip or photo not found")
    except trip_store.InvalidEdit as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/trips")
def list_trips():
    return trip_store.list_trips()


@app.get("/trips/{trip_id}")
def get_trip(trip_id: uuid.UUID):
    return _trip_errors(trip_store.get_trip, str(trip_id))


@app.patch("/trips/{trip_id}")
def rename_trip(trip_id: uuid.UUID, body: TripRename):
    return _trip_errors(trip_store.rename_trip, str(trip_id), body.name)


@app.post("/trips/{trip_id}/merge")
def merge_trips(trip_id: uuid.UUID, body: TripMerge):
    return _trip_errors(trip_store.merge_trips, str(trip_id), [str(t) for t in body.trip_ids])


@app.post("/trips/{trip_id}/split")
def split_trip(trip_id: uuid.UUID, body: TripSplit):
    first, second = _trip_errors(trip_store.split_trip, str(trip_id), body.at)
    return {"trips": [first, second]}


@app.delete("/trips/{trip_id}", status_code=204)
def dissolve_trip(trip_id: uuid.UUID):
    _trip_errors(trip_store.dissolve_trip, str(trip_id))
    return Response(status_code=204)


@app.put("/photos/{photo_id}/trip", status_code=204)
def move_photo(photo_id: uuid.UUID, body: PhotoTrip):
    _trip_errors(trip_store.move_photo, str(photo_id), str(body.trip_id) if body.trip_id else None)
    return Response(status_code=204)


@app.get("/homes")
def list_homes():
    return trip_store.list_homes()


@app.post("/homes", status_code=201)
def add_home(body: HomeIn):
    return trip_store.add_home(body.model_dump())


@app.delete("/homes/{home_id}", status_code=204)
def delete_home(home_id: uuid.UUID):
    _trip_errors(trip_store.delete_home, str(home_id))
    return Response(status_code=204)


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
