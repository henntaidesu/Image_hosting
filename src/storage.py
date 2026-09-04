"""Image validation, storage routing, and upload persistence."""

import hashlib
import os
import shutil
import sqlite3
import uuid
import warnings
from pathlib import Path

from flask import url_for
from PIL import Image, UnidentifiedImageError
from werkzeug.utils import secure_filename

from . import config
from .database import get_db, get_setting, now


class StorageUnavailableError(ValueError):
    """Raised when no configured storage folder can accept an upload."""


def validate_storage_path(value):
    if not value:
        raise ValueError("图片存储路径不能为空。")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("图片存储路径必须是绝对路径，例如 D:\\ImageHosting\\website。")
    if path.exists() and not path.is_dir():
        raise ValueError("图片存储路径指向了一个文件，请选择目录。")
    return path.resolve()


def ensure_storage_path_available(path, project_id=None):
    query = "SELECT p.name, l.path FROM storage_locations l JOIN projects p ON p.id = l.project_id"
    values = []
    if project_id is not None:
        query += " WHERE l.project_id != ?"
        values.append(project_id)
    candidate = os.path.normcase(str(path.resolve()))
    for existing in get_db().execute(query, values):
        existing_path = os.path.normcase(str(Path(existing["path"]).resolve()))
        try:
            overlap = os.path.commonpath((candidate, existing_path)) in (candidate, existing_path)
        except ValueError:
            overlap = False
        if overlap:
            raise ValueError(f"该目录与项目「{existing['name']}」的存储路径重叠。")


def format_bytes(value):
    units = ("B", "KB", "MB", "GB", "TB")
    amount = float(value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} TB"


def storage_status(location):
    try:
        usage = shutil.disk_usage(location["path"])
        return {
            "available": True,
            "free": usage.free,
            "total": usage.total,
            "free_text": format_bytes(usage.free),
            "total_text": format_bytes(usage.total),
        }
    except OSError:
        return {"available": False, "free": 0, "total": 0, "free_text": "不可用", "total_text": ""}


def storage_locations_for_project(project_id, enabled_only=False):
    query = "SELECT * FROM storage_locations WHERE project_id = ?"
    if enabled_only:
        query += " AND is_enabled = 1"
    query += " ORDER BY priority ASC, id ASC"
    return get_db().execute(query, (project_id,)).fetchall()


def make_storage_location_active(project_id, location_id):
    db = get_db()
    db.execute("UPDATE storage_locations SET is_active = 0 WHERE project_id = ?", (project_id,))
    db.execute("UPDATE storage_locations SET is_active = 1 WHERE id = ? AND project_id = ?", (location_id, project_id))


def available_storage_locations(project, file_size):
    """Try the current write folder first, then use priority only as failover order."""
    eligible = []
    rejected = []
    locations = storage_locations_for_project(project["id"], enabled_only=True)
    active_location = next((location for location in locations if location["is_active"]), None)
    ordered_locations = ([active_location] if active_location else []) + [
        location for location in locations if not active_location or location["id"] != active_location["id"]
    ]
    for location in ordered_locations:
        path = Path(location["path"])
        try:
            path.mkdir(parents=True, exist_ok=True)
            usage = shutil.disk_usage(path)
        except OSError:
            rejected.append(f"{location['name']}（目录不可访问）")
            continue
        required = file_size + location["reserve_mb"] * 1024 * 1024
        if usage.free >= required:
            eligible.append(location)
            continue
        rejected.append(f"{location['name']}（剩余 {format_bytes(usage.free)}）")
    return eligible, rejected


def stream_sha256(stream):
    """读完流算 sha256，再把游标复位——调用方后面还要用同一个流写盘。"""
    digest = hashlib.sha256()
    stream.seek(0)
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    stream.seek(0)
    return digest.hexdigest()


def image_for_external_key(project_id, external_key):
    """按调用方的稳定标识查已存在的图片；上传幂等与迁移续传都靠它。"""
    if not external_key:
        return None
    return get_db().execute(
        """
        SELECT i.*, l.path AS location_path, l.name AS location_name
        FROM images i
        LEFT JOIN storage_locations l ON l.id = i.storage_location_id
        WHERE i.project_id = ? AND i.external_key = ?
        """,
        (project_id, external_key),
    ).fetchone()


def image_with_location(project_id, stored_name):
    return get_db().execute(
        """
        SELECT i.*, l.path AS location_path, l.name AS location_name
        FROM images i
        LEFT JOIN storage_locations l ON l.id = i.storage_location_id
        WHERE i.project_id = ? AND i.stored_name = ?
        """,
        (project_id, stored_name),
    ).fetchone()


def project_storage_path(project):
    return Path(project["storage_path"])


def resolve_image_file(project, image):
    """Use the persisted image-to-folder mapping; priority only affects future uploads."""
    storage_path = image["location_path"] or str(project_storage_path(project))
    candidate = Path(storage_path) / image["stored_name"]
    return candidate if candidate.is_file() else None


def project_for_slug(slug):
    return get_db().execute("SELECT * FROM projects WHERE slug = ?", (slug,)).fetchone()


def public_image_url(slug, stored_name):
    base_url = get_setting("public_base_url")
    path = url_for("public_image", slug=slug, stored_name=stored_name)
    return f"{base_url}{path}" if base_url else url_for("public_image", slug=slug, stored_name=stored_name, _external=True)


def delete_image_record(project, image):
    """删除一张图片的文件与索引行；文件已不在时也照常清掉索引（避免留下死记录）。"""
    path = resolve_image_file(project, image)
    if path:
        path.unlink(missing_ok=True)
    db = get_db()
    db.execute("DELETE FROM images WHERE id = ?", (image["id"],))
    db.commit()


def image_is_safe(file):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            image = Image.open(file.stream)
            if image.width * image.height > config.MAX_IMAGE_PIXELS or getattr(image, "n_frames", 1) > config.MAX_IMAGE_FRAMES:
                return False
            image.verify()
        return True
    except (Image.DecompressionBombError, Image.DecompressionBombWarning, UnidentifiedImageError, OSError):
        return False
    finally:
        file.stream.seek(0)


def upload_limit_bytes():
    return int(get_setting("max_upload_mb")) * 1024 * 1024


def save_upload(project, file, external_key=None):
    """保存一次上传，返回 ``(stored_name, reused)``。

    ``external_key`` 非空且已存在时不再写第二份文件，直接复用旧记录并返回 ``reused=True``；
    迁移这类「跑一半断掉再重跑」的场景全靠这一条保证不产生重复图片。
    """
    external_key = (external_key or "").strip() or None
    if external_key:
        existing = image_for_external_key(project["id"], external_key)
        if existing is not None:
            return existing["stored_name"], True
    original_name = secure_filename(file.filename or "")
    extension = original_name.rsplit(".", 1)[-1].lower() if "." in original_name else ""
    if not original_name or extension not in config.ALLOWED_EXTENSIONS:
        raise ValueError("只允许上传图片（PNG/JPG/GIF/WebP/BMP/ICO/AVIF）或视频（MP4/MOV/WebM/M4V/MKV/AVI）。")
    is_video = extension in config.VIDEO_EXTENSIONS
    # 视频没法用 PIL 校验像素/帧数，也不生成缩略图——按原文件存取。图片仍走原来的安全校验。
    if not is_video and not image_is_safe(file):
        raise ValueError("文件内容不是有效图片。")
    file.stream.seek(0, os.SEEK_END)
    size = file.stream.tell()
    file.stream.seek(0)
    # 视频用单独的、更宽松的上限；图片沿用系统设置里的 max_upload_mb。
    if is_video:
        video_limit = config.MAX_VIDEO_UPLOAD_MB * 1024 * 1024
        if size > video_limit:
            raise ValueError(f"视频过大，最大允许 {config.MAX_VIDEO_UPLOAD_MB} MB。")
    elif size > upload_limit_bytes():
        raise ValueError(f"文件过大，最大允许 {get_setting('max_upload_mb')} MB。")
    digest = stream_sha256(file.stream)
    stored_name = f"{uuid.uuid4().hex}.{extension}"
    locations, rejected = available_storage_locations(project, size)
    if not locations:
        detail = "；".join(rejected) or "未配置可用目录"
        raise StorageUnavailableError(f"没有空间足够的存储位置，已跳过：{detail}。")

    write_errors = []
    for location in locations:
        project_dir = Path(location["path"])
        target = project_dir / stored_name
        temporary = project_dir / f".{uuid.uuid4().hex}.uploading"
        try:
            file.stream.seek(0)
            file.save(temporary)
            with temporary.open("rb+") as uploaded_file:
                os.fsync(uploaded_file.fileno())
            os.replace(temporary, target)
        except OSError as error:
            temporary.unlink(missing_ok=True)
            write_errors.append(f"{location['name']}（写入失败：{error}）")
            continue
        try:
            db = get_db()
            if not location["is_active"]:
                make_storage_location_active(project["id"], location["id"])
            db.execute(
                "INSERT INTO images (project_id, storage_location_id, stored_name, original_name, content_type, size, sha256, external_key, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (project["id"], location["id"], stored_name, original_name, file.mimetype or "application/octet-stream", target.stat().st_size, digest, external_key, now()),
            )
            db.commit()
            return stored_name, False
        except sqlite3.IntegrityError:
            # 同一个 external_key 的并发上传：另一路已经写进去了，删掉自己这份，复用对方的记录。
            target.unlink(missing_ok=True)
            get_db().rollback()
            existing = image_for_external_key(project["id"], external_key) if external_key else None
            if existing is None:
                raise
            return existing["stored_name"], True
        except sqlite3.Error as error:
            target.unlink(missing_ok=True)
            raise ValueError(f"图片已写入但无法保存索引：{error}") from error

    detail = "；".join(rejected + write_errors)
    raise StorageUnavailableError(f"所有可用存储位置均无法写入：{detail}。")
