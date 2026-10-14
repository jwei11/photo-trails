import logging
import re
import uuid
from urllib.parse import urlencode
from datetime import datetime, timedelta
from pathlib import Path

from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import auth, guests, imports, limits, objects, shares, storage, trip_store
from app.config import APP_ORIGINS

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


@app.middleware("http")
async def refuse_cross_site_changes(request: Request, call_next):
    """A change requested by another site's page is refused. Browsers send Origin with
    every POST, PUT, PATCH, and DELETE, and a page can't fake it. This backs up the session
    cookie's SameSite rule: older browsers and other sites on the same domain aren't held
    back by SameSite alone."""
    origin = request.headers.get("origin")
    if request.method not in ("GET", "HEAD", "OPTIONS") and origin and origin not in APP_ORIGINS:
        return JSONResponse({"detail": "Cross-site request refused"}, status_code=403)
    return await call_next(request)


# Every API route takes the signed-in user, and passes their id down: each query is
# limited to that user's rows. A test checks no route is missing it.
CurrentUser = Annotated[auth.User, Depends(auth.current_user)]


# ---- Signing in ----------------------------------------------------------------------

def _user_json(user: auth.User) -> dict:
    return {"id": user.id, "email": user.email, "kind": user.kind, "expires_at": user.expires_at}


@api.get("/auth/me")
def me(request: Request):
    """Who's signed in (null if nobody), and what this server offers: sign-in with Google,
    whether Google sign-in creates new accounts right now, and the demo."""
    user = auth.user_for_session(request.cookies.get(auth.SESSION_COOKIE))
    google = auth.google_enabled()
    return {
        "user": user and _user_json(user),
        "google": google,
        "signup": google and auth.signups_open(),
        "demo": guests.demo_available(),
    }


@api.post("/auth/guest")
def start_demo(request: Request, response: Response):
    """Try the demo: a new guest account with a copy of the demo trip, signed in at once."""
    try:
        user = guests.start(request.client.host if request.client else "unknown")
    except guests.DemoUnavailable:
        raise HTTPException(status_code=404, detail="There's no demo on this server.")
    except guests.DemoFull:
        raise HTTPException(status_code=503, detail="The demo is full right now. Try again in a few hours.")
    except guests.TooManyFromHere:
        raise HTTPException(status_code=429, detail="You've started several demos today. Try again tomorrow.")
    auth.set_session_cookie(response, request, auth.create_session(user.id))
    # Where to start: the demo trip's map, the most telling page.
    trips = trip_store.list_trips(user.id)
    return {"user": _user_json(user), "trip_id": trips[0]["id"] if trips else None}


@api.get("/auth/google/start")
def google_start(request: Request):
    if not auth.google_enabled():
        raise HTTPException(status_code=404, detail="Sign-in with Google isn't set up on this server")
    url, state = auth.google_start(request)
    response = RedirectResponse(url, status_code=302)
    response.set_cookie(
        auth.OAUTH_COOKIE, state, max_age=int(auth.OAUTH_LIFETIME.total_seconds()),
        httponly=True, secure=auth.app_origin(request).startswith("https://"),
        # Lax, not Strict: the callback is a top-level navigation coming from Google's
        # site, and Lax still sends the cookie for that.
        samesite="lax", path="/api/auth/google",
    )
    return response


@api.get("/auth/google/callback")
def google_callback(request: Request, code: str | None = None, state: str | None = None,
                    error: str | None = None):
    def back_to_login(message: str) -> RedirectResponse:
        response = RedirectResponse("/login?" + urlencode({"error": message}), status_code=302)
        response.delete_cookie(auth.OAUTH_COOKIE, path="/api/auth/google")
        return response

    if error or not code or not state:
        return back_to_login("Sign-in was cancelled.")
    try:
        user = auth.google_finish(request, code, state, request.cookies.get(auth.OAUTH_COOKIE))
    except auth.SignInFailed as exc:
        return back_to_login(str(exc))
    response = RedirectResponse("/", status_code=302)
    response.delete_cookie(auth.OAUTH_COOKIE, path="/api/auth/google")
    auth.set_session_cookie(response, request, auth.create_session(user.id))
    return response


class LoginLink(BaseModel):
    token: str = Field(min_length=1, max_length=200)


@api.post("/auth/link")
def sign_in_with_link(body: LoginLink, request: Request, response: Response):
    user = auth.use_login_link(body.token)
    if user is None:
        raise HTTPException(status_code=400, detail="This sign-in link has expired or was already used.")
    auth.set_session_cookie(response, request, auth.create_session(user.id))
    return {"user": _user_json(user)}


@api.post("/auth/logout", status_code=204)
def logout(request: Request):
    auth.end_session(request.cookies.get(auth.SESSION_COOKIE))
    response = Response(status_code=204)
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return response


MAX_UPLOAD_BYTES = 200 * 1024 * 1024

# The stored type is served back to browsers, so only allow types a browser won't run as a
# page. SVG is an image type that can contain script, so it's excluded.
UPLOAD_TYPE = re.compile(r"^(image/(?!svg)[\w.+-]+|application/octet-stream)$")


def check_content_type(content_type: str) -> str:
    if len(content_type) > 100 or not UPLOAD_TYPE.fullmatch(content_type):
        raise ValueError("Only image files (or application/octet-stream) can be uploaded")
    return content_type


@api.post("/photos", status_code=201)
async def upload_photo(file: UploadFile, user: CurrentUser):
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
        return await run_in_threadpool(storage.add_photo, user.id, file.filename, content_type, contents, user.kind)
    except limits.QuotaExceeded as exc:
        raise HTTPException(status_code=403, detail=str(exc))
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
def create_import(body: ImportRequest, user: CurrentUser):
    """Register a batch; returns a signed upload URL for each file not already stored."""
    try:
        return imports.create_import(user.id, [f.model_dump() for f in body.files], user.kind)
    except limits.QuotaExceeded as exc:
        raise HTTPException(status_code=403, detail=str(exc))


@api.get("/imports")
def list_imports(user: CurrentUser):
    return imports.list_imports(user.id)


@api.get("/imports/{import_id}")
def get_import(import_id: uuid.UUID, user: CurrentUser):
    return _import_errors(imports.get_import, user.id, str(import_id))


@api.post("/imports/{import_id}/items/{item_id}/upload-url")
def renew_upload_url(import_id: uuid.UUID, item_id: uuid.UUID, user: CurrentUser):
    return _import_errors(imports.renew_upload_url, user.id, str(import_id), str(item_id))


@api.post("/imports/{import_id}/items/{item_id}/complete")
def complete_import_item(import_id: uuid.UUID, item_id: uuid.UUID, user: CurrentUser):
    """After the file's PUT succeeds: process it. Safe to retry."""
    return _import_errors(imports.complete_item, user.id, str(import_id), str(item_id))


@api.post("/imports/{import_id}/items/{item_id}/failed")
def report_upload_failure(import_id: uuid.UUID, item_id: uuid.UUID, body: UploadFailure, user: CurrentUser):
    return _import_errors(imports.report_upload_failure, user.id, str(import_id), str(item_id), body.error)


@api.get("/photos")
def list_photos(user: CurrentUser):
    return storage.all_photos(user.id)


def _photo_or_404(user: auth.User, photo_id: uuid.UUID, include_exif: bool = False) -> dict:
    # Another user's photo is a 404, the same as a missing one: a 403 would confirm it exists.
    record = storage.get_photo(user.id, str(photo_id), include_exif=include_exif)
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
def get_photo_file(photo_id: uuid.UUID, user: CurrentUser):
    return _redirect_to_object(_photo_or_404(user, photo_id)["storage_key"])


@api.get("/photos/{photo_id}/metadata")
def get_photo_metadata(photo_id: uuid.UUID, user: CurrentUser):
    return _photo_or_404(user, photo_id, include_exif=True)


@api.get("/photos/{photo_id}/thumb")
def get_thumb(photo_id: uuid.UUID, user: CurrentUser):
    return _derivative(user, photo_id, "thumb_key")


@api.get("/photos/{photo_id}/web")
def get_web(photo_id: uuid.UUID, user: CurrentUser):
    return _derivative(user, photo_id, "web_key")


def _derivative(user: auth.User, photo_id: uuid.UUID, key: str):
    record = _photo_or_404(user, photo_id)
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
def timeline(user: CurrentUser):
    trips = trip_store.list_trips(user.id)
    photos_by_trip: dict[str | None, list] = {}
    undated_loose = []
    for p in storage.all_photos(user.id):
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
def list_trips(user: CurrentUser):
    return trip_store.list_trips(user.id)


@api.get("/trips/{trip_id}")
def get_trip(trip_id: uuid.UUID, user: CurrentUser):
    return _trip_errors(trip_store.get_trip, user.id, str(trip_id))


@api.patch("/trips/{trip_id}")
def rename_trip(trip_id: uuid.UUID, body: TripRename, user: CurrentUser):
    return _trip_errors(trip_store.rename_trip, user.id, str(trip_id), body.name)


@api.post("/trips/{trip_id}/merge")
def merge_trips(trip_id: uuid.UUID, body: TripMerge, user: CurrentUser):
    return _trip_errors(trip_store.merge_trips, user.id, str(trip_id), [str(t) for t in body.trip_ids])


@api.post("/trips/{trip_id}/split")
def split_trip(trip_id: uuid.UUID, body: TripSplit, user: CurrentUser):
    first, second = _trip_errors(trip_store.split_trip, user.id, str(trip_id), body.at)
    return {"trips": [first, second]}


@api.delete("/trips/{trip_id}", status_code=204)
def dissolve_trip(trip_id: uuid.UUID, user: CurrentUser):
    _trip_errors(trip_store.dissolve_trip, user.id, str(trip_id))
    return Response(status_code=204)


@api.put("/photos/{photo_id}/trip", status_code=204)
def move_photo(photo_id: uuid.UUID, body: PhotoTrip, user: CurrentUser):
    _trip_errors(trip_store.move_photo, user.id, str(photo_id), str(body.trip_id) if body.trip_id else None)
    return Response(status_code=204)


@api.get("/homes")
def list_homes(user: CurrentUser):
    return trip_store.list_homes(user.id)


@api.get("/homes/suggestions")
def home_suggestions(user: CurrentUser):
    """Places that look like home (photos on many days over weeks), not yet a home."""
    return trip_store.suggest_homes(user.id)


@api.post("/homes", status_code=201)
def add_home(body: HomeIn, user: CurrentUser):
    return trip_store.add_home(user.id, body.model_dump())


@api.delete("/homes/{home_id}", status_code=204)
def delete_home(home_id: uuid.UUID, user: CurrentUser):
    _trip_errors(trip_store.delete_home, user.id, str(home_id))
    return Response(status_code=204)


# ---- Sharing --------------------------------------------------------------------------

class ShareIn(BaseModel):
    precision: Literal["city", "neighbourhood"] = "city"
    exact_dates: bool = False


def _share_errors(fn, *args):
    try:
        return fn(*args)
    except shares.NotFound:
        raise HTTPException(status_code=404, detail="Not found")


@api.get("/trips/{trip_id}/shares")
def trip_sharing(trip_id: uuid.UUID, user: CurrentUser):
    return _share_errors(shares.sharing, user.id, str(trip_id))


@api.post("/trips/{trip_id}/shares", status_code=201)
def create_share(trip_id: uuid.UUID, body: ShareIn, user: CurrentUser):
    try:
        return _share_errors(shares.create_share, user.id, str(trip_id), body.precision, body.exact_dates)
    except shares.TooRecent as exc:
        when = exc.shareable_from
        raise HTTPException(
            status_code=409,
            detail=(
                f"Trips can be shared a week after they end, so a link never says you're away "
                f"right now. This one can be shared from {when:%B} {when.day}."
                if when else "This trip has no dated photos to share."
            ),
        )


@api.delete("/shares/{share_id}", status_code=204)
def revoke_share(share_id: uuid.UUID, user: CurrentUser):
    _share_errors(shares.revoke_share, user.id, str(share_id))
    return Response(status_code=204)


# The public side: no session, just the link's token. Not cached and not indexed, so a
# revoked link stops working at once and never shows up in a search engine.
PUBLIC_HEADERS = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex"}


@api.get("/shared/{token}")
def shared_trip(token: str):
    view = _share_errors(shares.public_view, token)
    return JSONResponse(jsonable_encoder(view), headers=PUBLIC_HEADERS)


@api.get("/shared/{token}/photos/{photo_id}/{kind}")
def shared_photo(token: str, photo_id: uuid.UUID, kind: Literal["thumb", "web"]):
    key = _share_errors(shares.public_photo_key, token, str(photo_id), kind)
    # Only ever a derivative (re-encoded, no metadata), never the original file. The signed
    # URL works for an hour; the browser may reuse this redirect for five minutes.
    return RedirectResponse(
        objects.download_url(key), status_code=302,
        headers={"Cache-Control": "private, max-age=300", "X-Robots-Tag": "noindex"},
    )


@api.get("/cameras")
def list_cameras(user: CurrentUser):
    return storage.list_cameras(user.id)


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
def list_offsets(user: CurrentUser):
    return storage.list_offsets(user.id)


@api.post("/camera-offsets", status_code=201)
def add_offset(body: ClockOffsetIn, user: CurrentUser):
    try:
        values = body.model_dump(exclude={"replace_overlapping"})
        return storage.add_offset(user.id, values, replace_overlapping=body.replace_overlapping)
    except storage.OffsetOverlapError:
        raise HTTPException(
            status_code=409,
            detail=(
                "This camera already has an offset covering these dates. "
                "Remove it, or send replace_overlapping to replace it."
            ),
        )


@api.delete("/camera-offsets/{offset_id}", status_code=204)
def delete_offset(offset_id: uuid.UUID, user: CurrentUser):
    if storage.delete_offset(user.id, str(offset_id)) is None:
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
