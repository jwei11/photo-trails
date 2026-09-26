import os
import tempfile

import pytest
from dotenv import load_dotenv

load_dotenv()
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

# Must happen before app.config is imported, so the app never points at the real database.
if TEST_DATABASE_URL:
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    os.environ["UPLOAD_DIR"] = tempfile.mkdtemp(prefix="trip-mapper-test-")


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

    with engine.begin() as conn:
        conn.execute(text("TRUNCATE photos, camera_clock_offsets"))
    with TestClient(app) as c:
        yield c
