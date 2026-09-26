import uuid

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse

from app import storage
from app.config import UPLOAD_DIR

app = FastAPI()

UPLOAD_DIR.mkdir(exist_ok=True)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


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


@app.get("/photos/{photo_id}")
def get_photo_file(photo_id: uuid.UUID):
    record = storage.get_photo(str(photo_id))
    if record is None:
        raise HTTPException(status_code=404, detail="Photo not found")
    return FileResponse(storage.file_path(record["storage_key"]), media_type=record["content_type"])
