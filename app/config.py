import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", "uploads"))
DEFAULT_TIMEZONE = os.environ.get("DEFAULT_TIMEZONE", "UTC")
ZoneInfo(DEFAULT_TIMEZONE)  # fail at startup, not mid-upload, if it's misspelled
