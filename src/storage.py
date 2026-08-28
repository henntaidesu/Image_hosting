"""Image validation, storage routing, and upload persistence."""

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


def save_upload(project, file):
    original_name = secure_filename(file.filename or "")
    extension = original_name.rsplit(".", 1)[-1].lower() if "." in original_name else ""
    if not original_name or extension not in config.ALLOWED_EXTENSIONS:
        raise ValueError("只允许上传 PNG、JPG、GIF、WebP、BMP、ICO 或 AVIF 图片。")
    if not image_is_safe(file):
        raise ValueError("文件内容不是有效图片。")
    file.stream.seek(0, os.SEEK_END)
    size = file.stream.tell()
    file.stream.seek(0)
    if size > upload_limit_bytes():
        raise ValueError(f"文件过大，最大允许 {get_setting('max_upload_mb')} MB。")
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
                "INSERT INTO images (project_id, storage_location_id, stored_name, original_name, content_type, size, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (project["id"], location["id"], stored_name, original_name, file.mimetype or "application/octet-stream", target.stat().st_size, now()),
            )
            db.commit()
            return stored_name
        except sqlite3.Error as error:
            target.unlink(missing_ok=True)
            raise ValueError(f"图片已写入但无法保存索引：{error}") from error

    detail = "；".join(rejected + write_errors)
    raise StorageUnavailableError(f"所有可用存储位置均无法写入：{detail}。")
