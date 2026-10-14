import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
DEFAULT_TIMEZONE = os.environ.get("DEFAULT_TIMEZONE", "UTC")
ZoneInfo(DEFAULT_TIMEZONE)  # fail at startup, not mid-upload, if it's misspelled

# Object storage. Leave S3_ENDPOINT empty for Amazon S3 (boto3 finds the regional
# endpoint itself); set it for an S3-compatible store such as the local SeaweedFS.
S3_ENDPOINT = os.environ.get("S3_ENDPOINT") or None
# The host baked into presigned URLs handed to browsers, if it differs from S3_ENDPOINT
# (e.g. once the API runs inside Docker and reaches the store by a container name).
S3_PUBLIC_ENDPOINT = os.environ.get("S3_PUBLIC_ENDPOINT") or S3_ENDPOINT
S3_ACCESS_KEY = os.environ["S3_ACCESS_KEY"]
S3_SECRET_KEY = os.environ["S3_SECRET_KEY"]
S3_BUCKET = os.environ.get("S3_BUCKET", "photos")
# Must be the bucket's region on S3: presigned URLs are signed for it.
S3_REGION = os.environ.get("S3_REGION", "us-east-1")
# Whether the app creates the bucket and sets its CORS rule at startup. Yes for a store we
# run ourselves (SeaweedFS); no for S3, where the bucket is set up outside the app and the
# app's credentials aren't allowed to change it.
_manage_default = "true" if S3_ENDPOINT else "false"
S3_MANAGE_BUCKET = os.environ.get("S3_MANAGE_BUCKET", _manage_default).lower() == "true"

# Where the app is opened from, for the bucket's CORS rule when the app manages it.
# Development uses two: Vite's dev server and FastAPI serving the build.
APP_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("APP_ORIGINS", "http://localhost:5173,http://localhost:8000").split(",")
    if origin.strip()
]

# Sign in with Google: an OAuth client from Google Cloud (see docs/development.md). Without them, the
# only way to sign in is a one-time link from `python -m app.users login-link`.
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID") or None
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET") or None
