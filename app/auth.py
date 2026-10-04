"""Who's signed in: users, sessions, sign-in with Google, and one-time sign-in links.

Accounts are invite-only: a user exists only once someone creates it with
`python -m app.users create <email>`. Signing in proves you own that email, either through
Google or with a one-time link printed by `python -m app.users login-link <email>`.

A session is a random token in an HttpOnly cookie, so page scripts can't read it. The
database keeps only its SHA-256, so the table can't be used to sign in. Signing out deletes
the row, which ends the session at once, unlike a signed token that stays valid until it
expires.
"""
import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException, Request
from sqlalchemy import text

from app.config import APP_ORIGINS, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET
from app.db import engine

SESSION_COOKIE = "session"
SESSION_LIFETIME = timedelta(days=30)
LOGIN_LINK_LIFETIME = timedelta(minutes=15)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
OAUTH_COOKIE = "oauth_state"
OAUTH_LIFETIME = timedelta(minutes=10)


@dataclass(frozen=True)
class User:
    id: str
    email: str


def _hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def normalize_email(email: str) -> str:
    return email.strip().lower()


# Users


class UserExists(Exception):
    pass


def create_user(email: str) -> User:
    email = normalize_email(email)
    with engine.begin() as conn:
        row = conn.execute(
            text("INSERT INTO users (email) VALUES (:email) ON CONFLICT (email) DO NOTHING RETURNING id"),
            {"email": email},
        ).first()
    if row is None:
        raise UserExists(email)
    return User(str(row.id), email)


def find_user(email: str) -> User | None:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT id, email FROM users WHERE email = :email"), {"email": normalize_email(email)}
        ).first()
    return User(str(row.id), row.email) if row else None


# Sessions


def create_session(user_id: str) -> str:
    """Start a session; returns the token for the cookie."""
    token = secrets.token_urlsafe(32)  # 256 random bits: can't be guessed
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO sessions (token_hash, user_id, expires_at) "
                "VALUES (:hash, :user, now() + :lifetime)"
            ),
            {"hash": _hash(token), "user": user_id, "lifetime": SESSION_LIFETIME},
        )
    return token


def user_for_session(token: str | None) -> User | None:
    if not token:
        return None
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT u.id, u.email FROM sessions s JOIN users u ON u.id = s.user_id
                WHERE s.token_hash = :hash AND s.expires_at > now()
                """
            ),
            {"hash": _hash(token)},
        ).first()
    return User(str(row.id), row.email) if row else None


def end_session(token: str | None) -> None:
    if token:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM sessions WHERE token_hash = :hash"), {"hash": _hash(token)})


def current_user(request: Request) -> User:
    """FastAPI dependency: the signed-in user, or 401."""
    user = user_for_session(request.cookies.get(SESSION_COOKIE))
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in first")
    return user


def set_session_cookie(response, request: Request, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE, token,
        max_age=int(SESSION_LIFETIME.total_seconds()),
        httponly=True,  # not readable by page scripts, so an injected script can't steal it
        secure=app_origin(request).startswith("https://"),
        # Not sent with requests started by other sites (a form posting here, say), except
        # plain link clicks. That blocks cross-site request forgery for every change, since
        # changes are never GETs.
        samesite="lax",
        path="/",
    )


# One-time sign-in links, for setups without Google (a fresh clone, tests)


def create_login_link(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO login_links (token_hash, user_id, expires_at) "
                "VALUES (:hash, :user, now() + :lifetime)"
            ),
            {"hash": _hash(token), "user": user_id, "lifetime": LOGIN_LINK_LIFETIME},
        )
    return token


def use_login_link(token: str) -> User | None:
    """Spend a link: the user it was for, or None if it's unknown, expired, or used."""
    with engine.begin() as conn:
        row = conn.execute(
            text(
                """
                UPDATE login_links l SET used_at = now() FROM users u
                WHERE l.token_hash = :hash AND l.used_at IS NULL AND l.expires_at > now()
                  AND u.id = l.user_id
                RETURNING u.id, u.email
                """
            ),
            {"hash": _hash(token)},
        ).first()
    return User(str(row.id), row.email) if row else None


# Sign in with Google (OpenID Connect, authorization code flow with PKCE)


def app_origin(request: Request) -> str:
    """The origin the browser is using, if it's one of ours. The Host header is set by the
    client, so it's only trusted when it's on the APP_ORIGINS list."""
    origin = f"{request.url.scheme}://{request.url.netloc}"
    return origin if origin in APP_ORIGINS else APP_ORIGINS[0]


def google_enabled() -> bool:
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


def _redirect_uri(request: Request) -> str:
    return app_origin(request) + "/api/auth/google/callback"


def google_start(request: Request) -> tuple[str, str]:
    """Where to send the browser, and the value for the short-lived state cookie.

    state ties Google's answer to this browser (a forged callback has no matching cookie),
    the nonce ties the ID token to this sign-in, and PKCE's verifier means an intercepted
    code is useless without the cookie too."""
    state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": _redirect_uri(request),
        "response_type": "code",
        "scope": "openid email",
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "select_account",
    }
    # Three URL-safe random strings, which never contain a dot.
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}", f"{state}.{nonce}.{verifier}"


class SignInFailed(Exception):
    """Shown to the person: say what went wrong without detail an attacker could use."""


def google_finish(request: Request, code: str, state: str, cookie: str | None) -> User:
    parts = (cookie or "").split(".")
    if len(parts) != 3 or not secrets.compare_digest(parts[0].encode(), state.encode()):
        raise SignInFailed("The sign-in expired or was started in another browser. Try again.")
    expected = {"nonce": parts[1], "verifier": parts[2]}
    response = httpx.post(
        GOOGLE_TOKEN_URL,
        data={
            "code": code,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": _redirect_uri(request),
            "grant_type": "authorization_code",
            "code_verifier": expected["verifier"],
        },
        timeout=10,
    )
    if response.status_code != 200:
        raise SignInFailed("Google didn't accept the sign-in. Try again.")
    claims = id_token_claims(response.json()["id_token"])
    check_claims(claims, expected["nonce"])
    user = find_user(claims["email"])
    if user is None:
        raise SignInFailed(f"{claims['email']} hasn't been invited.")
    return user


def id_token_claims(id_token: str) -> dict:
    """The ID token's claims. Its signature isn't checked: the token came straight from
    Google's token endpoint over HTTPS in answer to our own request, which OpenID Connect
    accepts in place of a signature check (Core 1.0, section 3.1.3.7)."""
    payload = id_token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def check_claims(claims: dict, nonce: str) -> None:
    if claims.get("iss") not in GOOGLE_ISSUERS or claims.get("aud") != GOOGLE_CLIENT_ID:
        raise SignInFailed("That sign-in wasn't for this app.")
    if claims.get("exp", 0) < time.time() or claims.get("nonce") != nonce:
        raise SignInFailed("The sign-in expired. Try again.")
    # Google says whether it has confirmed the person owns the address. An unverified one
    # could be anyone's, and accounts are matched by email.
    if not claims.get("email") or claims.get("email_verified") is not True:
        raise SignInFailed("Your Google account's email isn't verified.")
