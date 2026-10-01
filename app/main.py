import logging
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import imports, objects, storage, trip_store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await run_in_threadpool(objects.setup_bucket)
    yield


app = FastAPI(lifespan=lifespan)

# Everything the frontend fetches lives under /api, so page URLs (/trips/abc) never
# collide with API routes. /healthz stays at the root: it's for infrastructure.
api = APIRouter(prefix="/api")

# The React app, built by `npm run build` in frontend/.
FRONTEND_DIST = Path(__file__).parent.parent / "frontend" / "dist"


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


MAX_UPLOAD_BYTES = 200 * 1024 * 1024

# The stored type is served back to browsers, so only allow types a browser won't run as a
# page. SVG is an image type that can contain script, so it's excluded.
UPLOAD_TYPE = re.compile(r"^(image/(?!svg)[\w.+-]+|application/octet-stream)$")


def check_content_type(content_type: str) -> str:
    if len(content_type) > 100 or not UPLOAD_TYPE.fullmatch(content_type):
        raise ValueError("Only image files (or application/octet-stream) can be uploaded")
    return content_type


@api.post("/photos", status_code=201)
async def upload_photo(file: UploadFile):
    if file.size is not None and file.size > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File is larger than 200 MB")
    content_type = file.content_type or "application/octet-stream"
    try:
        check_content_type(content_type)
    except ValueError as exc:
        raise HTTPException(status_code=415, detail=str(exc))
    contents = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(contents) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File is larger than 200 MB")
    try:
        return await run_in_threadpool(storage.add_photo, file.filename, content_type, contents)
    except storage.DuplicatePhoto as dup:
        raise HTTPException(
            status_code=409,
            detail={"message": "This photo is already imported", "photo_id": dup.original_id},
        )


class ImportFile(BaseModel):
    filename: str | None = Field(default=None, max_length=255)
    content_type: str
    size_bytes: int = Field(gt=0, le=MAX_UPLOAD_BYTES)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("content_type")
    @classmethod
    def _allowed_type(cls, value: str) -> str:
        return check_content_type(value)


class ImportRequest(BaseModel):
    files: list[ImportFile] = Field(min_length=1, max_length=2000)


class UploadFailure(BaseModel):
    error: str = Field(min_length=1, max_length=500)


def _import_errors(fn, *args):
    try:
        return fn(*args)
    except imports.ImportNotFound:
        raise HTTPException(status_code=404, detail="Import or item not found")
    except imports.InvalidItemState as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@api.post("/imports", status_code=201)
def create_import(body: ImportRequest):
    """Register a batch; returns a signed upload URL for each file not already stored."""
    return imports.create_import([f.model_dump() for f in body.files])


@api.get("/imports")
def list_imports():
    return imports.list_imports()


@api.get("/imports/{import_id}")
def get_import(import_id: uuid.UUID):
    return _import_errors(imports.get_import, str(import_id))


@api.post("/imports/{import_id}/items/{item_id}/upload-url")
def renew_upload_url(import_id: uuid.UUID, item_id: uuid.UUID):
    return _import_errors(imports.renew_upload_url, str(import_id), str(item_id))


@api.post("/imports/{import_id}/items/{item_id}/complete")
def complete_import_item(import_id: uuid.UUID, item_id: uuid.UUID):
    """After the file's PUT succeeds: process it. Safe to retry."""
    return _import_errors(imports.complete_item, str(import_id), str(item_id))


@api.post("/imports/{import_id}/items/{item_id}/failed")
def report_upload_failure(import_id: uuid.UUID, item_id: uuid.UUID, body: UploadFailure):
    return _import_errors(imports.report_upload_failure, str(import_id), str(item_id), body.error)


@api.get("/photos")
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


@api.get("/photos/{photo_id}")
def get_photo_file(photo_id: uuid.UUID):
    return _redirect_to_object(_photo_or_404(photo_id)["storage_key"])


@api.get("/photos/{photo_id}/metadata")
def get_photo_metadata(photo_id: uuid.UUID):
    return _photo_or_404(photo_id, include_exif=True)


@api.get("/photos/{photo_id}/thumb")
def get_thumb(photo_id: uuid.UUID):
    return _derivative(photo_id, "thumb_key")


@api.get("/photos/{photo_id}/web")
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


@api.get("/timeline")
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


@api.get("/trips")
def list_trips():
    return trip_store.list_trips()


@api.get("/trips/{trip_id}")
def get_trip(trip_id: uuid.UUID):
    return _trip_errors(trip_store.get_trip, str(trip_id))


@api.patch("/trips/{trip_id}")
def rename_trip(trip_id: uuid.UUID, body: TripRename):
    return _trip_errors(trip_store.rename_trip, str(trip_id), body.name)


@api.post("/trips/{trip_id}/merge")
def merge_trips(trip_id: uuid.UUID, body: TripMerge):
    return _trip_errors(trip_store.merge_trips, str(trip_id), [str(t) for t in body.trip_ids])


@api.post("/trips/{trip_id}/split")
def split_trip(trip_id: uuid.UUID, body: TripSplit):
    first, second = _trip_errors(trip_store.split_trip, str(trip_id), body.at)
    return {"trips": [first, second]}


@api.delete("/trips/{trip_id}", status_code=204)
def dissolve_trip(trip_id: uuid.UUID):
    _trip_errors(trip_store.dissolve_trip, str(trip_id))
    return Response(status_code=204)


@api.put("/photos/{photo_id}/trip", status_code=204)
def move_photo(photo_id: uuid.UUID, body: PhotoTrip):
    _trip_errors(trip_store.move_photo, str(photo_id), str(body.trip_id) if body.trip_id else None)
    return Response(status_code=204)


@api.get("/homes")
def list_homes():
    return trip_store.list_homes()


@api.post("/homes", status_code=201)
def add_home(body: HomeIn):
    return trip_store.add_home(body.model_dump())


@api.delete("/homes/{home_id}", status_code=204)
def delete_home(home_id: uuid.UUID):
    _trip_errors(trip_store.delete_home, str(home_id))
    return Response(status_code=204)


@api.get("/cameras")
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
    # Replace the camera's offsets that cover any of the same times, atomically, instead
    # of rejecting the overlap with 409.
    replace_overlapping: bool = False

    @model_validator(mode="after")
    def _range_order(self):
        for value in (self.valid_from, self.valid_to):
            if value is not None and value.tzinfo is not None:
                raise ValueError("valid_from/valid_to are camera clock times: omit the timezone")
        if self.valid_from and self.valid_to and self.valid_from >= self.valid_to:
            raise ValueError("valid_from must be before valid_to")
        return self


@api.get("/camera-offsets")
def list_offsets():
    return storage.list_offsets()


@api.post("/camera-offsets", status_code=201)
def add_offset(body: ClockOffsetIn):
    try:
        values = body.model_dump(exclude={"replace_overlapping"})
        return storage.add_offset(values, replace_overlapping=body.replace_overlapping)
    except storage.OffsetOverlapError:
        raise HTTPException(
            status_code=409,
            detail=(
                "This camera already has an offset covering these dates. "
                "Remove it, or send replace_overlapping to replace it."
            ),
        )


@api.delete("/camera-offsets/{offset_id}", status_code=204)
def delete_offset(offset_id: uuid.UUID):
    if storage.delete_offset(str(offset_id)) is None:
        raise HTTPException(status_code=404, detail="Offset not found")
    return Response(status_code=204)


# Registered last: include_router copies the routes defined on `api` at the moment it runs.
app.include_router(api)


# The React app is whatever the router couldn't match. Handling it here, after routing,
# rather than with a catch-all route keeps FastAPI's own behaviour for API paths: the
# trailing-slash redirect (/api/photos/) and 405 for a wrong method both happen first.
@app.exception_handler(StarletteHTTPException)
async def frontend_or_error(request: Request, exc: StarletteHTTPException):
    path = request.url.path
    is_api = path == "/api" or path.startswith("/api/")
    if exc.status_code == 404 and request.method in ("GET", "HEAD") and not is_api:
        return _frontend(path.lstrip("/"))
    # Everything else, including unknown /api paths, keeps the normal JSON error: answering
    # a bad API call with the app's HTML would hide the bug.
    return await http_exception_handler(request, exc)


def _frontend(path: str) -> Response:
    """A real file from the build as is; for any other URL, index.html, so the browser
    loads the app and React Router picks the page from the URL."""
    dist = FRONTEND_DIST.resolve()
    if not (dist / "index.html").is_file():
        return JSONResponse(
            {"detail": "Frontend not built: run `npm run build` in frontend/"}, status_code=404
        )
    try:
        file = (dist / path).resolve()
        # resolve() collapses ../ segments; the check keeps requests inside the build folder.
        found = bool(path) and file.is_relative_to(dist) and file.is_file()
    except (OSError, ValueError):  # e.g. a null byte (/%00) or a name too long for the OS
        found = False
    if found:
        # Vite puts a content hash in every asset's filename, so a changed file gets a new
        # URL and an asset can be cached forever.
        if file.is_relative_to(dist / "assets"):
            return FileResponse(file, headers={"Cache-Control": "public, max-age=31536000, immutable"})
        return FileResponse(file)
    # index.html names the current hashed assets, so the browser must re-check it every time.
    return FileResponse(dist / "index.html", headers={"Cache-Control": "no-cache"})
