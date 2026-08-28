"""Runtime paths and application limits."""

import os
import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", PROJECT_DIR))
ASSET_DIR = BUNDLE_DIR / "src"
RUNTIME_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else PROJECT_DIR
DATA_DIR = RUNTIME_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
DATABASE = DATA_DIR / "picture_bed.sqlite3"

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "ico", "avif"}
MAX_IMAGE_PIXELS = 25_000_000
MAX_IMAGE_FRAMES = 200
MAX_UPLOAD_FILES = 20
INSECURE_LOCAL_MODE = os.environ.get("PICTURE_BED_INSECURE_COOKIES") == "1"
LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]")
SERVER_HOST = os.environ.get("PICTURE_BED_HOST", "0.0.0.0").strip() or "0.0.0.0"
SERVER_PORT = int(os.environ.get("PICTURE_BED_PORT", "9990"))
