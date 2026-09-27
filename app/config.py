import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
DEFAULT_TIMEZONE = os.environ.get("DEFAULT_TIMEZONE", "UTC")
ZoneInfo(DEFAULT_TIMEZONE)  # fail at startup, not mid-upload, if it's misspelled

S3_ENDPOINT = os.environ["S3_ENDPOINT"]
S3_PUBLIC_ENDPOINT = os.environ.get("S3_PUBLIC_ENDPOINT", S3_ENDPOINT)
S3_ACCESS_KEY = os.environ["S3_ACCESS_KEY"]
S3_SECRET_KEY = os.environ["S3_SECRET_KEY"]
S3_BUCKET = os.environ.get("S3_BUCKET", "photos")
S3_REGION = os.environ.get("S3_REGION", "us-east-1")
APP_ORIGIN = os.environ.get("APP_ORIGIN", "http://localhost:8000")
