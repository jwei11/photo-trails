import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app import storage

app = FastAPI()

storage.DATA_DIR.mkdir(exist_ok=True)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/photos")
async def upload_photo(file: UploadFile):
    photo_id = str(uuid.uuid4())
    extension = Path(file.filename or "").suffix
    stored_path = storage.DATA_DIR / f"{photo_id}{extension}"

    contents = await file.read()
    stored_path.write_bytes(contents)

    record = {
        "id": photo_id,
        "original_filename": file.filename,
        "content_type": file.content_type,
        "size_bytes": len(contents),
        "stored_path": str(stored_path),
    }
    storage.add_photo(record)
    return record


@app.get("/photos")
def list_photos():
    return storage.all_photos()


@app.get("/photos/{photo_id}")
def get_photo_file(photo_id: str):
    record = storage.get_photo(photo_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Photo not found")
    return FileResponse(record["stored_path"], media_type=record["content_type"])
