import os
from urllib.parse import urlparse

import pytest
from dotenv import load_dotenv

load_dotenv()
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
TEST_S3_ENDPOINT = os.environ.get("TEST_S3_ENDPOINT", "http://localhost:8333")

# API tests delete every object in their bucket, so they only ever run against the local
# SeaweedFS, never the S3 bucket the app uses in development. Checked before anything else.
if urlparse(TEST_S3_ENDPOINT).hostname not in ("localhost", "127.0.0.1"):
    raise pytest.UsageError(f"TEST_S3_ENDPOINT must be a local store, not {TEST_S3_ENDPOINT}")

# Must happen before app.config is imported, so the app never points at real data.
os.environ.update({
    "S3_ENDPOINT": TEST_S3_ENDPOINT,
    "S3_PUBLIC_ENDPOINT": TEST_S3_ENDPOINT,
    "S3_ACCESS_KEY": os.environ.get("SEAWEEDFS_ACCESS_KEY", ""),
    "S3_SECRET_KEY": os.environ.get("SEAWEEDFS_SECRET_KEY", ""),
    "S3_REGION": "us-east-1",
    "S3_BUCKET": os.environ.get("TEST_S3_BUCKET", "photos-test"),
    "S3_MANAGE_BUCKET": "true",
    # Google sign-in is off unless a test turns it on, whatever .env says.
    "GOOGLE_CLIENT_ID": "",
    "GOOGLE_CLIENT_SECRET": "",
})
if TEST_DATABASE_URL:
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL


@pytest.fixture(scope="session")
def migrated_db():
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    from alembic import command
    from alembic.config import Config

    command.upgrade(Config("alembic.ini"), "head")


@pytest.fixture
def client(migrated_db):
    from fastapi.testclient import TestClient
    from sqlalchemy import text

    from app.db import engine
    from app.main import app

    from app import objects

    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE users, sessions, login_links, shares, photos, camera_clock_offsets, trips, homes, "
                "imports, import_items, places, admin1_regions, countries, jobs"
            )
        )
    with TestClient(app) as c:  # startup creates the test bucket if needed
        objects.delete(*(key for key, _ in objects.list_keys()))
        sign_in(c, "a@example.com")
        yield c


def sign_in(client, email: str):
    """Create the user (if needed) and give this client a session for them, as signing in
    would. Returns the user."""
    from app import auth

    user = auth.find_user(email) or auth.create_user(email)
    client.cookies.set(auth.SESSION_COOKIE, auth.create_session(user.id))
    return user


@pytest.fixture
def other_client(client):
    """A second browser, signed in as a different user, B."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        sign_in(c, "b@example.com")
        yield c
