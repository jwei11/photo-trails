"""Accounts: every API route needs a signed-in user, and one user can never read or change
another's photos, trips, imports, or settings."""
import base64
import json
import re
import time
import uuid
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app import auth
from app.db import engine
from app.main import api
from tests.test_api import add_offset, jpeg, upload
from tests.test_imports import describe, start_import
from tests.test_trips_api import shoot

# Signing in and out, and share links, are the only API routes open to anyone.
PUBLIC = {"/api/auth/me", "/api/auth/google/start", "/api/auth/google/callback", "/api/auth/link",
          "/api/auth/logout", "/api/shared/{token}", "/api/shared/{token}/photos/{photo_id}/{kind}"}


def api_routes():
    for route in api.routes:
        if isinstance(route, APIRoute):
            for method in route.methods:
                yield method, route.path


def test_every_api_route_requires_a_session(client):
    client.cookies.clear()
    checked = 0
    for method, path in api_routes():
        if path in PUBLIC:
            continue
        url = re.sub(r"\{[^}]+\}", str(uuid.uuid4()), path)
        res = client.request(method, url, json={})
        assert res.status_code == 401, f"{method} {path} answered {res.status_code} without a session"
        checked += 1
    assert checked >= 25  # the walk really found the routes


@pytest.fixture
def a_data(client):
    """User A's things: a trip of three photos, a home, a clock offset, and an import."""
    photos = [shoot(client, f"a{h}.jpg", 1, h) for h in (10, 11, 12)]
    trip = client.get("/api/trips").json()[0]
    home = client.post("/api/homes", json={"name": "Home", "lat": 40.0, "lon": -3.7}).json()
    offset = add_offset(client).json()
    imp = start_import(client, describe(jpeg("2025:07:02 10:00:00", "+01:00", 38.7, -9.1)))
    return {"photos": photos, "trip": trip, "home": home, "offset": offset, "import": imp}


def test_user_b_cannot_read_user_a_photos(a_data, other_client):
    for photo in a_data["photos"]:
        for suffix in ("", "/metadata", "/thumb", "/web"):
            res = other_client.get(f"/api/photos/{photo['id']}{suffix}", follow_redirects=False)
            # 404, never 403: B can't even learn the photo exists.
            assert res.status_code == 404, suffix
    trip_id = a_data["trip"]["id"]
    assert other_client.get(f"/api/trips/{trip_id}").status_code == 404
    imp = a_data["import"]
    assert other_client.get(f"/api/imports/{imp['id']}").status_code == 404
    # And none of it shows up in B's lists.
    for path in ("/api/photos", "/api/trips", "/api/imports", "/api/homes", "/api/cameras",
                 "/api/camera-offsets"):
        assert other_client.get(path).json() == [], path
    timeline = other_client.get("/api/timeline").json()
    assert timeline == {"trips": [], "not_in_trip": [], "undated": []}


def test_user_b_cannot_change_user_a_things(client, a_data, other_client):
    trip_id = a_data["trip"]["id"]
    a_photo = a_data["photos"][0]["id"]
    b_photo = [shoot(other_client, f"b{h}.jpg", 9, h) for h in (10, 11, 12)][0]["id"]
    b_trip = other_client.get("/api/trips").json()[0]["id"]
    item = a_data["import"]["items"][0]
    imp = a_data["import"]["id"]

    attempts = [
        ("PATCH", f"/api/trips/{trip_id}", {"name": "mine now"}),
        ("POST", f"/api/trips/{trip_id}/split", {"at": "2025-07-01T10:30:00Z"}),
        ("DELETE", f"/api/trips/{trip_id}", None),
        # Merging A's trip into B's own trip, and moving photos across, in both directions.
        ("POST", f"/api/trips/{b_trip}/merge", {"trip_ids": [trip_id]}),
        ("PUT", f"/api/photos/{b_photo}/trip", {"trip_id": trip_id}),
        ("PUT", f"/api/photos/{a_photo}/trip", {"trip_id": b_trip}),
        ("PUT", f"/api/photos/{a_photo}/trip", {"trip_id": None}),
        ("DELETE", f"/api/homes/{a_data['home']['id']}", None),
        ("DELETE", f"/api/camera-offsets/{a_data['offset']['id']}", None),
        ("POST", f"/api/imports/{imp}/items/{item['id']}/upload-url", None),
        ("POST", f"/api/imports/{imp}/items/{item['id']}/complete", None),
        ("POST", f"/api/imports/{imp}/items/{item['id']}/failed", {"error": "x"}),
    ]
    for method, path, body in attempts:
        res = other_client.request(method, path, json=body)
        assert res.status_code == 404, f"{method} {path}: {res.status_code} {res.text}"

    # A's things are exactly as they were.
    trip = client.get(f"/api/trips/{trip_id}").json()
    assert trip["name"] is None and trip["photo_count"] == 3
    assert client.get(f"/api/photos/{a_photo}/metadata").json()["trip_id"] == trip_id
    assert len(client.get("/api/homes").json()) == 1
    assert len(client.get("/api/camera-offsets").json()) == 1
    assert client.get(f"/api/imports/{imp}").json()["items"][0]["status"] == "uploading"
    assert other_client.get(f"/api/photos/{b_photo}/metadata").json()["trip_id"] == b_trip


def test_the_same_file_is_a_new_photo_for_another_user(client, other_client):
    data = jpeg("2025:07:01 10:00:00", "+01:00", 38.72, -9.14)
    a_photo = upload(client, "a.jpg", data)
    # For B it's not "already imported" (which would reveal A has it, and A's photo id).
    imp = start_import(other_client, describe(data))
    assert imp["items"][0]["status"] == "uploading"
    b_photo = upload(other_client, "b.jpg", data)
    assert b_photo["id"] != a_photo["id"]
    # Each user's own duplicate is still caught.
    assert client.post("/api/photos", files={"file": ("again.jpg", data, "image/jpeg")}).status_code == 409


def test_trips_and_clock_offsets_are_per_user(client, other_client):
    # The same moments for both users: each gets their own trip, never a shared one.
    for h in (10, 11, 12):
        shoot(client, f"a{h}.jpg", 1, h)
        shoot(other_client, f"b{h}.jpg", 1, h, minute=30)
    a_trips, b_trips = client.get("/api/trips").json(), other_client.get("/api/trips").json()
    assert len(a_trips) == len(b_trips) == 1 and a_trips[0]["id"] != b_trips[0]["id"]
    # Two people can own the same camera model; their offsets don't collide.
    assert add_offset(client).status_code == 201
    assert add_offset(other_client).status_code == 201
    assert other_client.get("/api/cameras").json() == []


def test_the_database_refuses_a_photo_in_another_users_trip(client, other_client):
    a_photo = shoot(client, "a.jpg", 1, 10)["id"]
    for h in (10, 11, 12):
        shoot(other_client, f"b{h}.jpg", 1, h)
    b_trip = other_client.get("/api/trips").json()[0]["id"]
    # Even if a bug in the code tried it, the foreign key on (trip_id, user_id) won't allow it.
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(text("UPDATE photos SET trip_id = :trip WHERE id = :id"), {"trip": b_trip, "id": a_photo})


# ---- Sessions --------------------------------------------------------------------------


def test_signing_out_ends_the_session(client):
    assert client.get("/api/photos").status_code == 200
    token = client.cookies.get(auth.SESSION_COOKIE)
    assert client.post("/api/auth/logout").status_code == 204
    # Even a copy of the old cookie no longer works: the session row is gone.
    client.cookies.set(auth.SESSION_COOKIE, token)
    assert client.get("/api/photos").status_code == 401


def test_expired_and_made_up_sessions_are_refused(client):
    token = client.cookies.get(auth.SESSION_COOKIE)
    client.cookies.set(auth.SESSION_COOKIE, token + "x")
    assert client.get("/api/photos").status_code == 401
    with engine.begin() as conn:
        conn.execute(text("UPDATE sessions SET expires_at = now() - interval '1 second'"))
    client.cookies.set(auth.SESSION_COOKIE, token)
    assert client.get("/api/photos").status_code == 401


def test_the_database_holds_only_hashes_of_tokens(client):
    token = client.cookies.get(auth.SESSION_COOKIE)
    with engine.connect() as conn:
        stored = [bytes(r.token_hash) for r in conn.execute(text("SELECT token_hash FROM sessions"))]
    assert token.encode() not in stored and all(len(h) == 32 for h in stored)


def test_me_says_who_is_signed_in(client):
    me = client.get("/api/auth/me").json()
    assert me["user"]["email"] == "a@example.com"
    client.cookies.clear()
    assert client.get("/api/auth/me").json()["user"] is None


def test_a_login_link_works_once(client):
    client.cookies.clear()
    user = auth.find_user("a@example.com")
    token = auth.create_login_link(user.id)
    res = client.post("/api/auth/link", json={"token": token})
    assert res.status_code == 200 and res.json()["user"]["email"] == "a@example.com"
    cookie = res.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert client.get("/api/photos").status_code == 200
    client.cookies.clear()
    assert client.post("/api/auth/link", json={"token": token}).status_code == 400


def test_an_expired_login_link_is_refused(client):
    client.cookies.clear()
    token = auth.create_login_link(auth.find_user("a@example.com").id)
    with engine.begin() as conn:
        conn.execute(text("UPDATE login_links SET expires_at = now() - interval '1 second'"))
    assert client.post("/api/auth/link", json={"token": token}).status_code == 400


def test_changes_requested_by_another_site_are_refused(client):
    home = {"name": "Home", "lat": 40.0, "lon": -3.7}
    res = client.post("/api/homes", json=home, headers={"Origin": "https://evil.example"})
    assert res.status_code == 403
    assert client.get("/api/homes").json() == []
    assert client.post("/api/homes", json=home, headers={"Origin": "http://localhost:5173"}).status_code == 201


def test_emails_are_matched_ignoring_case(client):
    assert auth.find_user("  A@Example.COM ").email == "a@example.com"
    with pytest.raises(auth.UserExists):
        auth.create_user("A@example.com")


# ---- Sign in with Google -----------------------------------------------------------------

CLIENT_ID = "test-client.apps.googleusercontent.com"


@pytest.fixture
def google(monkeypatch):
    monkeypatch.setattr(auth, "GOOGLE_CLIENT_ID", CLIENT_ID)
    monkeypatch.setattr(auth, "GOOGLE_CLIENT_SECRET", "test-secret")
    sent = {}

    def token_endpoint(claims):
        def post(url, data, timeout):
            sent.update(data)
            payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()

            class Response:
                status_code = 200

                def json(self):
                    return {"id_token": f"header.{payload}.signature"}

            return Response()

        monkeypatch.setattr(auth.httpx, "post", post)

    return token_endpoint, sent


def google_claims(expected_nonce, **overrides):
    claims = {"iss": "https://accounts.google.com", "aud": CLIENT_ID, "exp": time.time() + 300,
              "nonce": expected_nonce, "email": "a@example.com", "email_verified": True}
    return {**claims, **overrides}


def start_google(client):
    res = client.get("/api/auth/google/start", follow_redirects=False)
    assert res.status_code == 302 and res.headers["location"].startswith(auth.GOOGLE_AUTH_URL)
    params = dict(p.split("=", 1) for p in res.headers["location"].split("?", 1)[1].split("&"))
    state, nonce, verifier = client.cookies.get(auth.OAUTH_COOKIE).split(".")
    return params, {"state": state, "nonce": nonce, "verifier": verifier}


def test_google_sign_in_for_an_invited_user(client, google):
    token_endpoint, sent = google
    client.cookies.clear()
    params, cookie = start_google(client)
    assert params["code_challenge_method"] == "S256" and params["state"] == cookie["state"]
    token_endpoint(google_claims(cookie["nonce"]))
    res = client.get("/api/auth/google/callback", params={"code": "abc", "state": cookie["state"]},
                     follow_redirects=False)
    assert res.status_code == 302 and res.headers["location"] == "/"
    assert sent["code_verifier"] == cookie["verifier"]  # PKCE: the code alone isn't enough
    assert client.get("/api/auth/me").json()["user"]["email"] == "a@example.com"


@pytest.mark.parametrize("overrides, message", [
    ({"email": "stranger@example.com"}, "hasn't been invited"),
    ({"email_verified": False}, "isn't verified"),
    ({"aud": "someone-elses-app"}, "wasn't for this app"),
    ({"nonce": "replayed"}, "expired"),
    ({"exp": time.time() - 10}, "expired"),
])
def test_google_sign_in_refusals(client, google, overrides, message):
    token_endpoint, _ = google
    client.cookies.clear()
    _, cookie = start_google(client)
    token_endpoint(google_claims(cookie["nonce"], **overrides))
    res = client.get("/api/auth/google/callback", params={"code": "abc", "state": cookie["state"]},
                     follow_redirects=False)
    assert res.status_code == 302 and res.headers["location"].startswith("/login?error=")
    assert message in parse_qs(urlsplit(res.headers["location"]).query)["error"][0]
    assert client.get("/api/auth/me").json()["user"] is None


def test_google_callback_without_the_matching_state_is_refused(client, google):
    token_endpoint, _ = google
    client.cookies.clear()
    _, cookie = start_google(client)
    token_endpoint(google_claims(cookie["nonce"]))
    # An attacker's own callback URL, opened in the victim's browser: the state won't match.
    res = client.get("/api/auth/google/callback", params={"code": "attacker", "state": "forged"},
                     follow_redirects=False)
    assert res.headers["location"].startswith("/login?error=")
    assert client.get("/api/auth/me").json()["user"] is None


def test_google_is_off_without_a_client(client):
    assert client.get("/api/auth/me").json()["google"] is False
    assert client.get("/api/auth/google/start", follow_redirects=False).status_code == 404


def test_users_command_creates_and_links(client, capsys):
    from app import users

    users.main(["create", "new@example.com", "--login-link"])
    out = capsys.readouterr().out
    token = re.search(r"/login\?link=(\S+)", out).group(1)
    client.cookies.clear()
    assert client.post("/api/auth/link", json={"token": token}).json()["user"]["email"] == "new@example.com"
    with pytest.raises(SystemExit):
        users.main(["create", "NEW@example.com"])
    with pytest.raises(SystemExit):
        users.main(["login-link", "nobody@example.com"])
