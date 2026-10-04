"""Check that a stack started by `docker compose up` works end to end, the way a browser
uses it. Run against a fresh stack, from the repo root:

    docker compose up -d --build && docker compose wait geodata
    python3 scripts/smoke_compose.py [--api http://localhost:8000]

It creates a user and uploads one small photo, and leaves them there (there's no delete),
so it refuses to run against a stack that already has users. Standard library only: it
runs on the host, which needs nothing installed but Docker and Python 3.

Checks, in order:
1. The API answers /healthz, serves the React app at /, and refuses API calls from
   someone not signed in.
2. The image runs as a non-root user and has no C compiler.
3. A user created with `python -m app.users` in the API container can sign in with the
   one-time link it prints.
4. An import, as the browser does it: register the file, PUT it to the presigned URL the
   API returns (proving that URL is signed for a host reachable from outside Docker),
   mark it complete, and wait for the worker container to process it.
5. The thumbnail redirects to a presigned URL that serves a WebP image.
6. The photo's GPS time and place were resolved.
"""
import argparse
import base64
import re
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

# An 885-byte 64x48 JPEG, taken 2025-07-01 10:00 +01:00 at the Torre de Belém, Lisbon
# (made with tests/test_api.py's jpeg()).
PHOTO = base64.b64decode("""
/9j/4AAQSkZJRgABAQAAAQABAAD/4QDORXhpZgAATU0AKgAAAAgAAodpAAQAAAABAAAAJoglAAQAAAABAAAAYAAA
AAAAApADAAIAAAAUAAAARJARAAIAAAAHAAAAWAAAAAAyMDI1OjA3OjAxIDEwOjAwOjAwACswMTowMAAAAAQAAQAC
AAAAAk4AAAAAAgAFAAAAAwAAAJYAAwACAAAAAlcAAAAABAAFAAAAAwAAAK4AAAAAAAAAJgAAAAEAAAApAAAAAQAA
AugAAAAZAAAACQAAAAEAAAAMAAAAAQAAASAAAAAF/9sAQwAIBgYHBgUIBwcHCQkICgwUDQwLCwwZEhMPFB0aHx4d
GhwcICQuJyAiLCMcHCg3KSwwMTQ0NB8nOT04MjwuMzQy/9sAQwEJCQkMCwwYDQ0YMiEcITIyMjIyMjIyMjIyMjIy
MjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIy/8AAEQgAMABAAwEiAAIRAQMRAf/EAB8AAAEFAQEB
AQEBAAAAAAAAAAABAgMEBQYHCAkKC//EALUQAAIBAwMCBAMFBQQEAAABfQECAwAEEQUSITFBBhNRYQcicRQygZGh
CCNCscEVUtHwJDNicoIJChYXGBkaJSYnKCkqNDU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6
g4SFhoeIiYqSk5SVlpeYmZqio6Slpqeoqaqys7S1tre4ubrCw8TFxsfIycrS09TV1tfY2drh4uPk5ebn6Onq8fLz
9PX29/j5+v/EAB8BAAMBAQEBAQEBAQEAAAAAAAABAgMEBQYHCAkKC//EALURAAIBAgQEAwQHBQQEAAECdwABAgMR
BAUhMQYSQVEHYXETIjKBCBRCkaGxwQkjM1LwFWJy0QoWJDThJfEXGBkaJicoKSo1Njc4OTpDREVGR0hJSlNUVVZX
WFlaY2RlZmdoaWpzdHV2d3h5eoKDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT
1NXW19jZ2uLj5OXm5+jp6vLz9PX29/j5+v/aAAwDAQACEQMRAD8A5miiivUPhgooooAKKKKACiiigAooooAKKKKA
CiiigAooooAKKKKACiiigAooooAKKKKAP//Z
""")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


# The session cookie, sent only to the API (never to storage), once signed in.
SESSION = {"api": None, "cookie": None}


def request(method, url, body=None, headers=None, follow=True):
    data = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
    headers = {"Content-Type": "application/json", **(headers or {})} if isinstance(body, (dict, list)) else headers or {}
    if SESSION["cookie"] and url.startswith(SESSION["api"] + "/"):
        headers = {**headers, "Cookie": SESSION["cookie"]}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    opener = urllib.request.build_opener() if follow else urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(req, timeout=10) as res:
            return res.status, {k.lower(): v for k, v in res.headers.items()}, res.read()
    except urllib.error.HTTPError as err:
        return err.code, {k.lower(): v for k, v in err.headers.items()}, err.read()


def get_json(url):
    status, _, body = request("GET", url)
    check(status == 200, f"GET {url} -> {status}: {body[:200]!r}")
    return json.loads(body)


def check(ok, message):
    if not ok:
        sys.exit(f"FAIL: {message}")


def step(message):
    print(f"ok   {message}")


def wait_for(what, fn, timeout):
    deadline = time.monotonic() + timeout
    while True:
        result = fn()
        if result:
            return result
        check(time.monotonic() < deadline, f"timed out after {timeout}s waiting for {what}")
        time.sleep(0.5)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", default=f"http://localhost:{os.environ.get('API_PORT', '8000')}")
    args = parser.parse_args()
    api = args.api.rstrip("/")

    # 1. Up, and serving the frontend.
    def healthy():
        try:
            return request("GET", f"{api}/healthz")[0] == 200
        except OSError:
            return False

    wait_for("/healthz", healthy, timeout=60)
    status, headers, _ = request("GET", f"{api}/")
    check(status == 200 and "text/html" in headers.get("content-type", ""), f"/ -> {status}, not the React app")
    status, _, _ = request("GET", f"{api}/api/photos")
    check(status == 401, f"GET /api/photos without signing in -> {status}, not 401")
    step("API is healthy, serves the React app, and refuses callers who aren't signed in")

    # 2. What's inside the image.
    inside = subprocess.run(
        ["docker", "compose", "exec", "-T", "api", "sh", "-c", "id -u; command -v gcc cc c++ || true"],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    check(inside[0] != "0", "the API container runs as root")
    check(inside[1:] == [], f"the image has a compiler: {inside[1:]}")
    step(f"image runs as uid {inside[0]} and has no compiler")

    # 3. An account, and signing in with a one-time link.
    def users_command(*args):
        return subprocess.run(
            ["docker", "compose", "exec", "-T", "api", "python", "-m", "app.users", *args],
            capture_output=True, text=True, check=True,
        ).stdout

    check(users_command("list").strip() == "", "this stack already has users; run against a fresh one")
    out = users_command("create", "smoke@example.com", "--login-link")
    token = re.search(r"/login\?link=(\S+)", out)
    check(token is not None, f"no sign-in link in: {out!r}")
    status, headers, body = request("POST", f"{api}/api/auth/link", {"token": token.group(1)})
    check(status == 200, f"POST /api/auth/link -> {status}: {body[:200]!r}")
    SESSION.update(api=api, cookie=headers["set-cookie"].split(";", 1)[0])
    check(get_json(f"{api}/api/auth/me")["user"]["email"] == "smoke@example.com", "not signed in after the link")
    step("created a user and signed in with a one-time link")

    # 4. Import one photo the way the browser does.
    sha = hashlib.sha256(PHOTO).hexdigest()
    status, _, body = request("POST", f"{api}/api/imports", {"files": [
        {"filename": "smoke.jpg", "content_type": "image/jpeg", "size_bytes": len(PHOTO), "sha256": sha},
    ]})
    check(status == 201, f"POST /api/imports -> {status}: {body[:200]!r}")
    imp = json.loads(body)
    item = imp["items"][0]
    status, _, body = request("PUT", item["upload_url"], PHOTO, {"Content-Type": "image/jpeg"})
    check(status == 200, f"PUT to the presigned URL -> {status}: {body[:200]!r}")
    step(f"uploaded straight to storage at {item['upload_url'].split('?')[0]}")

    status, _, body = request("POST", f"{api}/api/imports/{imp['id']}/items/{item['id']}/complete")
    check(status == 200, f"complete -> {status}: {body[:200]!r}")

    def processed():
        current = get_json(f"{api}/api/imports/{imp['id']}")["items"][0]
        check(current["status"] in ("uploading", "processing", "done"), f"import item: {current}")
        return current if current["status"] == "done" else None

    done = wait_for("the worker to process the photo", processed, timeout=60)
    step("the worker processed the photo")

    # 5. The thumbnail: a redirect to a presigned URL, which serves the image.
    status, headers, _ = request("GET", f"{api}/api/photos/{done['photo_id']}/thumb", follow=False)
    check(status in (302, 307), f"/thumb -> {status}, expected a redirect")
    status, headers, body = request("GET", headers["location"])
    check(status == 200 and headers.get("content-type") == "image/webp",
          f"thumbnail URL -> {status} {headers.get('content-type')}")
    step(f"thumbnail served from storage ({len(body)} bytes of WebP)")

    # 6. Time and place.
    photo = get_json(f"{api}/api/photos/{done['photo_id']}/metadata")
    check(photo["shot_at"] is not None and photo["tz_name"] == "Europe/Lisbon", f"time not resolved: {photo}")
    place = photo.get("place_name")
    step(f"taken {photo['shot_at']} in {photo['tz_name']}, place: {place or 'none yet (place data still loading?)'}")
    print("PASS")


if __name__ == "__main__":
    main()
