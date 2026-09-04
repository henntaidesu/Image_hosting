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

# 缩略图缓存目录名。有意不在这里算成绝对路径：DATA_DIR 会被 app.py 的 _sync_runtime_paths
# 和测试改写，模块导入那一刻算死的绝对路径改不动，缩略图会落到旧目录里去。
DERIVATIVE_DIR_NAME = "derivatives"
# 缩略图只按固定档位生成：宽度由 URL 决定，不设白名单的话任何人都能用 ?w=1..1200 逼服务
# 生成上千份不同尺寸，把 CPU 和磁盘一起吃光。调用方请求的宽度向上取整到最近的档位。
DERIVATIVE_WIDTHS = (100, 200, 300, 400, 560, 800, 1200)

IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "ico", "avif"}
# 视频扩展名。视频不做 PIL 校验、不生成缩略图，按原文件存取（见 storage.py / derivatives.py）。
# 加它是为了让「显卡 GPU-Z / mods 测试」这类需要录屏的场景把视频也托管在图床上。
VIDEO_EXTENSIONS = {"mp4", "mov", "webm", "m4v", "mkv", "avi"}
ALLOWED_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS
# 视频单文件上限。图片沿用「系统设置」里的 max_upload_mb（面向几 MB 的图片），视频动辄上百
# MB，共用那个上限会被拦下，所以单列一个更宽松的值。需要更大时改这里。
MAX_VIDEO_UPLOAD_MB = 512
MAX_IMAGE_PIXELS = 25_000_000
MAX_IMAGE_FRAMES = 200
MAX_UPLOAD_FILES = 20
INSECURE_LOCAL_MODE = os.environ.get("PICTURE_BED_INSECURE_COOKIES") == "1"
LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]")
SERVER_HOST = os.environ.get("PICTURE_BED_HOST", "0.0.0.0").strip() or "0.0.0.0"
SERVER_PORT = int(os.environ.get("PICTURE_BED_PORT", "9990"))
