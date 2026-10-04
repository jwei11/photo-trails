"""Time a 200-photo import end to end, the way the browser does it.

Run against an API whose storage and database you don't mind filling:
    python scripts/bench_import.py --api http://localhost:8001 [--photos 200]

Generates a synthetic trip (phone photos with GPS, camera photos without, a few
screenshots), then: hashes every file, registers the batch, uploads 4 at a time straight
to storage with the signed URLs, and calls complete for each. Prints per-phase times.
Images are generated into a temporary folder and deleted at the end.
"""
import argparse
import hashlib
import random
import shutil
import statistics
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, TiffImagePlugin

PARALLEL = 4  # what the browser uses
LISBON, MADRID = (38.7223, -9.1393), (40.4168, -3.7038)


def _dms(value: float):
    value = abs(value)
    d, m = int(value), int(value % 1 * 60)
    s = round((value - d - m / 60) * 3600, 2)
    return tuple(TiffImagePlugin.IFDRational(x) for x in (d, m, s))


def make_photo(path: Path, when: datetime, where: tuple | None, rng: random.Random, megapixels=12):
    """A JPEG about the size of a phone photo: noisy enough not to compress to nothing."""
    w = int((megapixels * 1e6 * 4 / 3) ** 0.5)
    h = int(w * 3 / 4)
    small = Image.effect_noise((w // 8, h // 8), 60).convert("RGB")
    img = small.resize((w, h), Image.BICUBIC)
    ImageDraw.Draw(img).rectangle((0, 0, w // 3, h // 3), fill=tuple(rng.randrange(256) for _ in range(3)))
    exif = Image.Exif()
    exif[0x010F], exif[0x0110] = ("Apple", "iPhone 15") if where else ("Fujifilm", "X-T5")
    ifd = exif.get_ifd(0x8769)
    ifd[0x9003] = when.strftime("%Y:%m:%d %H:%M:%S")
    if where:
        ifd[0x9011] = "+01:00"
        gps = exif.get_ifd(0x8825)
        lat, lon = where
        gps.update({1: "N", 2: _dms(lat), 3: "E" if lon >= 0 else "W", 4: _dms(lon)})
    img.save(path, "JPEG", quality=92, exif=exif)


def make_trip(folder: Path, count: int) -> list[Path]:
    rng = random.Random(42)
    start = datetime(2025, 7, 1, 9)
    paths = []
    for i in range(count - count // 20):
        when = start + timedelta(minutes=17 * i)
        on_phone = i % 5 != 0  # one in five from the no-GPS camera
        city = LISBON if i < count // 2 else MADRID
        where = (city[0] + rng.uniform(-0.02, 0.02), city[1] + rng.uniform(-0.02, 0.02)) if on_phone else None
        path = folder / f"IMG_{i:04d}.jpg"
        make_photo(path, when, where, rng)
        paths.append(path)
    for i in range(count // 20):  # screenshots: no metadata at all
        path = folder / f"Screenshot {i}.png"
        Image.effect_noise((1170, 2532), 30).convert("RGB").save(path, "PNG")
        paths.append(path)
    return paths


def run(api: str, paths: list[Path], wait_for_processing: bool) -> dict:
    client = httpx.Client(base_url=api, timeout=120)
    timings = {}

    t = time.perf_counter()
    files = []
    for p in paths:
        data = p.read_bytes()
        ctype = "image/png" if p.suffix == ".png" else "image/jpeg"
        files.append((p, ctype, {"filename": p.name, "content_type": ctype, "size_bytes": len(data),
                                 "sha256": hashlib.sha256(data).hexdigest()}))
    timings["hash_s"] = time.perf_counter() - t

    t = time.perf_counter()
    imp = client.post("/api/imports", json={"files": [d for _, _, d in files]}).json()
    timings["register_s"] = time.perf_counter() - t

    by_name = {p.name: (p, ctype) for p, ctype, _ in files}
    per_photo = []

    def upload(item):
        if item["status"] != "uploading":
            return item
        path, ctype = by_name[item["filename"]]
        t0 = time.perf_counter()
        httpx.put(item["upload_url"], content=path.read_bytes(), headers={"Content-Type": ctype}, timeout=120)
        t1 = time.perf_counter()
        done = client.post(f"/api/imports/{imp['id']}/items/{item['id']}/complete").json()
        per_photo.append((t1 - t0, time.perf_counter() - t1))
        return done

    t = time.perf_counter()
    with ThreadPoolExecutor(PARALLEL) as pool:
        results = list(pool.map(upload, imp["items"]))
    timings["upload_and_complete_s"] = time.perf_counter() - t

    if wait_for_processing:  # after M8: complete returns before processing; wait for the worker
        while True:
            counts = client.get(f"/api/imports/{imp['id']}").json()["counts"]
            if counts["uploading"] == 0 and counts.get("processing", 0) == 0:
                break
            time.sleep(0.5)
        timings["until_processed_s"] = time.perf_counter() - t
    statuses = [r["status"] for r in results]

    complete_ms = sorted(c * 1000 for _, c in per_photo)
    return {
        **{k: round(v, 1) for k, v in timings.items()},
        "total_s": round(sum(timings.values()) - (timings["upload_and_complete_s"] if wait_for_processing else 0), 1),
        "put_ms_p50": round(statistics.median(p * 1000 for p, _ in per_photo)),
        "complete_ms_p50": round(statistics.median(complete_ms)),
        "complete_ms_p95": round(complete_ms[int(len(complete_ms) * 0.95) - 1]),
        "result": client.get(f"/api/imports/{imp['id']}").json()["counts"],
        "statuses_returned": {s: statuses.count(s) for s in set(statuses)},
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", required=True)
    parser.add_argument("--photos", type=int, default=200)
    parser.add_argument("--wait", action="store_true", help="also wait until the worker has processed everything")
    args = parser.parse_args()
    folder = Path(tempfile.mkdtemp(prefix="trip-bench-"))
    try:
        t = time.perf_counter()
        paths = make_trip(folder, args.photos)
        size = sum(p.stat().st_size for p in paths) / 1e6
        print(f"generated {len(paths)} files, {size:.0f} MB, in {time.perf_counter() - t:.0f}s")
        print(run(args.api, paths, args.wait))
    finally:
        shutil.rmtree(folder)
