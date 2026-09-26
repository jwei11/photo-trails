import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", "uploads"))
