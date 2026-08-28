import os
import secrets
import shutil
import sqlite3
import sys
import uuid
import warnings
from contextlib import closing
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, abort, flash, g, jsonify, redirect, render_template, request, send_from_directory, session, url_for
from PIL import Image, UnidentifiedImageError
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename
from werkzeug.security import check_password_hash, generate_password_hash


BASE_DIR = Path(__file__).resolve().parent
# PyInstaller extracts bundled templates and static files to a temporary folder.
# Keep mutable application data beside the executable instead so it persists
# across launches of the packaged application.
RUNTIME_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else BASE_DIR
DATA_DIR = RUNTIME_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
DATABASE = RUNTIME_DIR / "picture_bed.sqlite3"
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "ico", "avif"}
MAX_IMAGE_PIXELS = 25_000_000
MAX_IMAGE_FRAMES = 200
MAX_UPLOAD_FILES = 20
INSECURE_LOCAL_MODE = os.environ.get("PICTURE_BED_INSECURE_COOKIES") == "1"
LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]")

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)


class StorageUnavailableError(ValueError):
    """Raised when no configured storage folder can accept an upload."""


def init_storage():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    legacy_database = DATA_DIR / "picture_bed.sqlite3"
    if legacy_database != DATABASE and not DATABASE.exists() and legacy_database.is_file():
        shutil.move(str(legacy_database), str(DATABASE))
    with closing(sqlite3.connect(DATABASE)) as db:
        db.execute("PRAGMA foreign_keys = ON")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                slug TEXT NOT NULL UNIQUE,
                storage_path TEXT,
                api_token TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                storage_location_id INTEGER,
                stored_name TEXT NOT NULL,
                original_name TEXT NOT NULL,
                content_type TEXT NOT NULL,
                size INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(project_id, stored_name)
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS storage_locations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                path TEXT NOT NULL,
                priority INTEGER NOT NULL DEFAULT 100,
                reserve_mb INTEGER NOT NULL DEFAULT 1024,
                is_enabled INTEGER NOT NULL DEFAULT 1,
                is_active INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                UNIQUE(project_id, path)
            );
        """)
        columns = {row[1] for row in db.execute("PRAGMA table_info(projects)")}
        if "storage_path" not in columns:
            db.execute("ALTER TABLE projects ADD COLUMN storage_path TEXT")
        db.execute(
            "UPDATE projects SET storage_path = ? || slug WHERE storage_path IS NULL OR storage_path = ''",
            (str(UPLOAD_DIR.resolve()) + os.sep,),
        )
        image_columns = {row[1] for row in db.execute("PRAGMA table_info(images)")}
        if "storage_location_id" not in image_columns:
            db.execute("ALTER TABLE images ADD COLUMN storage_location_id INTEGER")
        location_columns = {row[1] for row in db.execute("PRAGMA table_info(storage_locations)")}
        if "is_active" not in location_columns:
            db.execute("ALTER TABLE storage_locations ADD COLUMN is_active INTEGER NOT NULL DEFAULT 0")
        for project_id, storage_path in db.execute("SELECT id, storage_path FROM projects"):
            location = db.execute(
                "SELECT id FROM storage_locations WHERE project_id = ? AND path = ?",
                (project_id, storage_path),
            ).fetchone()
            if location is None:
                cursor = db.execute(
                    "INSERT INTO storage_locations (project_id, name, path, priority, reserve_mb, is_enabled, is_active, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (project_id, "主存储位置", storage_path, 10, 1024, 1, 0, datetime.now(timezone.utc).isoformat(timespec="seconds")),
                )
                location_id = cursor.lastrowid
            else:
                location_id = location[0]
            db.execute(
                "UPDATE images SET storage_location_id = ? WHERE project_id = ? AND storage_location_id IS NULL",
                (location_id, project_id),
            )
            active_location = db.execute(
                "SELECT id FROM storage_locations WHERE project_id = ? AND is_active = 1 AND is_enabled = 1 LIMIT 1",
                (project_id,),
            ).fetchone()
            if active_location is None:
                fallback_location = db.execute(
                    "SELECT id FROM storage_locations WHERE project_id = ? AND is_enabled = 1 ORDER BY priority ASC, id ASC LIMIT 1",
                    (project_id,),
                ).fetchone()
                if fallback_location:
                    db.execute("UPDATE storage_locations SET is_active = 0 WHERE project_id = ?", (project_id,))
                    db.execute("UPDATE storage_locations SET is_active = 1 WHERE id = ?", (fallback_location[0],))
        defaults = {
            "secret_key": secrets.token_urlsafe(48),
            "admin_password_hash": generate_password_hash("admin"),
            "max_upload_mb": "20",
            "public_base_url": "",
        }
        for key, value in defaults.items():
            db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))
        db.commit()


init_storage()


def get_setting(key):
    db = sqlite3.connect(DATABASE)
    try:
        row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    finally:
        db.close()
    return row[0] if row else ""


def set_setting(key, value):
    get_db().execute("UPDATE settings SET value = ? WHERE key = ?", (value, key))
    get_db().commit()


def configured_trusted_hosts(public_base_url=None):
    public_base_url = public_base_url if public_base_url is not None else get_setting("public_base_url")
    hostname = urlsplit(public_base_url).hostname if public_base_url else None
    hosts = [hostname] if hostname else []
    if INSECURE_LOCAL_MODE or not hosts:
        hosts.extend(LOCAL_HOSTS)
    return hosts


def configure_runtime_security(max_upload_mb=None, public_base_url=None):
    max_upload_mb = int(max_upload_mb if max_upload_mb is not None else get_setting("max_upload_mb"))
    app.config.update(
        MAX_CONTENT_LENGTH=max_upload_mb * 1024 * 1024,
        MAX_FORM_MEMORY_SIZE=64 * 1024,
        MAX_FORM_PARTS=MAX_UPLOAD_FILES + 10,
        TRUSTED_HOSTS=configured_trusted_hosts(public_base_url),
    )


app.config.update(
    SECRET_KEY=get_setting("secret_key"),
    SESSION_COOKIE_NAME="picture-bed-session" if INSECURE_LOCAL_MODE else "__Host-picture-bed-session",
    SESSION_COOKIE_SECURE=not INSECURE_LOCAL_MODE,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    PREFERRED_URL_SCHEME="http" if INSECURE_LOCAL_MODE else "https",
)
configure_runtime_security()


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_error):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def csrf_token():
    token = session.get("csrf_token")
    if token is None:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def safe_redirect_target(value):
    if not value or not value.startswith("/") or value.startswith("//"):
        return None
    parsed = urlsplit(value)
    return value if not parsed.scheme and not parsed.netloc else None


def validate_public_base_url(value):
    if not value:
        return ""
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("公开访问基地址必须是有效的 HTTP(S) 地址。")
    if parsed.query or parsed.fragment:
        raise ValueError("公开访问基地址不能包含查询参数或片段。")
    if not INSECURE_LOCAL_MODE and parsed.scheme != "https":
        raise ValueError("公网部署的公开访问基地址必须使用 HTTPS。")
    return value.rstrip("/")


@app.before_request
def validate_csrf():
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"} or request.endpoint == "api_upload":
        return
    expected = session.get("csrf_token")
    provided = request.form.get("csrf_token", "")
    if not expected or not provided or not secrets.compare_digest(expected, provided):
        abort(400, "CSRF token 无效或缺失。")


@app.after_request
def set_security_headers(response):
    response.headers.setdefault("Content-Security-Policy", "default-src 'self'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'; object-src 'none'; img-src 'self' data:; style-src 'self'; script-src 'self'")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), geolocation=(), microphone=()")
    if not request.path.startswith(("/images/", "/static/")):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("login", next=request.full_path))
        return view(*args, **kwargs)
    return wrapped


def valid_slug(value):
    return value and all(char.islower() or char.isdigit() or char == "-" for char in value) and not value.startswith("-") and not value.endswith("-")


def project_storage_path(project):
    return Path(project["storage_path"])


def validate_storage_path(value):
    if not value:
        raise ValueError("图片存储路径不能为空。")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("图片存储路径必须是绝对路径，例如 D:\\PictureBed\\website。")
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


@app.context_processor
def template_helpers():
    return {
        "csrf_token": csrf_token,
        "get_setting": get_setting,
        "public_image_url": public_image_url,
        "storage_status": storage_status,
    }


def image_is_safe(file):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            image = Image.open(file.stream)
            if image.width * image.height > MAX_IMAGE_PIXELS or getattr(image, "n_frames", 1) > MAX_IMAGE_FRAMES:
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
    if not original_name or extension not in ALLOWED_EXTENSIONS:
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


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if check_password_hash(get_setting("admin_password_hash"), request.form.get("password", "")):
            session.clear()
            session["is_admin"] = True
            session.permanent = True
            return redirect(safe_redirect_target(request.form.get("next")) or url_for("dashboard"))
        flash("密码不正确。", "error")
    return render_template("login.html", next=request.args.get("next", ""))


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
@admin_required
def dashboard():
    projects = get_db().execute("""
        SELECT p.*, COUNT(i.id) AS image_count, COALESCE(SUM(i.size), 0) AS total_size
        FROM projects p LEFT JOIN images i ON p.id = i.project_id
        GROUP BY p.id ORDER BY p.created_at DESC
    """).fetchall()
    return render_template("dashboard.html", projects=projects, using_default_password=check_password_hash(get_setting("admin_password_hash"), "admin"))


@app.route("/settings", methods=["GET", "POST"])
@admin_required
def settings():
    if request.method == "POST":
        max_upload_mb = request.form.get("max_upload_mb", "").strip()
        public_base_url = request.form.get("public_base_url", "").strip()
        password = request.form.get("password", "")
        password_confirm = request.form.get("password_confirm", "")
        try:
            max_upload_number = int(max_upload_mb)
            if not 1 <= max_upload_number <= 1024:
                raise ValueError
        except ValueError:
            flash("单文件大小上限必须是 1 到 1024 之间的整数（MB）。", "error")
            return redirect(url_for("settings"))
        try:
            public_base_url = validate_public_base_url(public_base_url)
        except ValueError as error:
            flash(str(error), "error")
            return redirect(url_for("settings"))
        if password:
            if len(password) < 8:
                flash("管理员密码至少需要 8 个字符。", "error")
                return redirect(url_for("settings"))
            if password != password_confirm:
                flash("两次输入的管理员密码不一致。", "error")
                return redirect(url_for("settings"))
            set_setting("admin_password_hash", generate_password_hash(password))
        set_setting("max_upload_mb", str(max_upload_number))
        set_setting("public_base_url", public_base_url)
        configure_runtime_security(max_upload_number, public_base_url)
        flash("系统设置已保存。", "success")
        return redirect(url_for("settings"))
    return render_template("settings.html", max_upload_mb=get_setting("max_upload_mb"), public_base_url=get_setting("public_base_url"))


@app.post("/projects")
@admin_required
def create_project():
    name = request.form.get("name", "").strip()
    storage_path_input = request.form.get("storage_path", "").strip()
    if not name:
        flash("项目名称不能为空。", "error")
        return redirect(url_for("dashboard"))
    try:
        storage_path = validate_storage_path(storage_path_input)
        ensure_storage_path_available(storage_path)
        storage_path.mkdir(parents=True, exist_ok=True)
        slug = f"project-{secrets.token_hex(6)}"
        db = get_db()
        cursor = db.execute(
            "INSERT INTO projects (name, slug, storage_path, api_token, created_at) VALUES (?, ?, ?, ?, ?)",
            (name, slug, str(storage_path), secrets.token_urlsafe(32), now()),
        )
        db.execute(
            "INSERT INTO storage_locations (project_id, name, path, priority, reserve_mb, is_enabled, is_active, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (cursor.lastrowid, "主存储位置", str(storage_path), 10, 1024, 1, 1, now()),
        )
        db.commit()
        flash(f"已创建项目「{name}」。", "success")
    except (ValueError, OSError, sqlite3.Error) as error:
        flash(f"无法使用该图片存储路径：{error}", "error")
    return redirect(url_for("dashboard"))


@app.get("/projects/<slug>")
@admin_required
def project_detail(slug):
    project = project_for_slug(slug)
    if not project:
        abort(404)
    images = get_db().execute(
        """
        SELECT i.*, l.name AS location_name, l.path AS location_path
        FROM images i LEFT JOIN storage_locations l ON l.id = i.storage_location_id
        WHERE i.project_id = ? ORDER BY i.created_at DESC
        """,
        (project["id"],),
    ).fetchall()
    locations = get_db().execute(
        """
        SELECT l.*, COUNT(i.id) AS image_count
        FROM storage_locations l LEFT JOIN images i ON i.storage_location_id = l.id
        WHERE l.project_id = ? GROUP BY l.id ORDER BY l.priority ASC, l.id ASC
        """,
        (project["id"],),
    ).fetchall()
    return render_template("project.html", project=project, images=images, locations=locations)


@app.post("/projects/<slug>/storage-locations")
@admin_required
def add_storage_location(slug):
    project = project_for_slug(slug)
    if not project:
        abort(404)
    name = request.form.get("name", "").strip()
    path_input = request.form.get("path", "").strip()
    try:
        priority = int(request.form.get("priority", "100"))
        reserve_mb = int(request.form.get("reserve_mb", "1024"))
        if not 1 <= priority <= 9999 or not 0 <= reserve_mb <= 1048576:
            raise ValueError("优先级或保留空间的值超出允许范围。")
        path = validate_storage_path(path_input)
        ensure_storage_path_available(path, project["id"])
        path.mkdir(parents=True, exist_ok=True)
        if not name:
            name = f"存储位置 {len(storage_locations_for_project(project['id'])) + 1}"
        db = get_db()
        cursor = db.execute(
            "INSERT INTO storage_locations (project_id, name, path, priority, reserve_mb, is_enabled, is_active, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (project["id"], name, str(path), priority, reserve_mb, 1, 0, now()),
        )
        make_storage_location_active(project["id"], cursor.lastrowid)
        db.commit()
        flash(f"已添加存储位置「{name}」，后续上传将立即写入此位置。", "success")
    except (ValueError, OSError, sqlite3.IntegrityError) as error:
        flash(f"无法添加存储位置：{error}", "error")
    return redirect(url_for("project_detail", slug=slug))


@app.post("/projects/<slug>/storage-locations/<int:location_id>/update")
@admin_required
def update_storage_location(slug, location_id):
    project = project_for_slug(slug)
    location = get_db().execute(
        "SELECT * FROM storage_locations WHERE id = ? AND project_id = ?", (location_id, project["id"])
    ).fetchone() if project else None
    if not location:
        abort(404)
    name = request.form.get("name", "").strip()
    try:
        priority = int(request.form.get("priority", ""))
        reserve_mb = int(request.form.get("reserve_mb", ""))
        if not name or not 1 <= priority <= 9999 or not 0 <= reserve_mb <= 1048576:
            raise ValueError("请填写名称、1 到 9999 的优先级，以及有效的保留空间。")
        get_db().execute(
            "UPDATE storage_locations SET name = ?, priority = ?, reserve_mb = ? WHERE id = ?",
            (name, priority, reserve_mb, location_id),
        )
        get_db().commit()
        flash(f"已更新存储位置「{name}」。", "success")
    except ValueError as error:
        flash(str(error), "error")
    return redirect(url_for("project_detail", slug=slug))


@app.post("/projects/<slug>/storage-locations/<int:location_id>/activate")
@admin_required
def activate_storage_location(slug, location_id):
    project = project_for_slug(slug)
    location = get_db().execute(
        "SELECT * FROM storage_locations WHERE id = ? AND project_id = ?", (location_id, project["id"])
    ).fetchone() if project else None
    if not location:
        abort(404)
    if not location["is_enabled"]:
        flash("请先启用该存储位置，再将其设为当前写入位置。", "error")
    else:
        make_storage_location_active(project["id"], location_id)
        get_db().commit()
        flash(f"已切换到「{location['name']}」，后续上传将只写入此位置。", "success")
    return redirect(url_for("project_detail", slug=slug))


@app.post("/projects/<slug>/storage-locations/<int:location_id>/toggle")
@admin_required
def toggle_storage_location(slug, location_id):
    project = project_for_slug(slug)
    location = get_db().execute(
        "SELECT * FROM storage_locations WHERE id = ? AND project_id = ?", (location_id, project["id"])
    ).fetchone() if project else None
    if not location:
        abort(404)
    if location["is_enabled"]:
        enabled_count = get_db().execute(
            "SELECT COUNT(*) FROM storage_locations WHERE project_id = ? AND is_enabled = 1", (project["id"],)
        ).fetchone()[0]
        if enabled_count <= 1:
            flash("至少需要保留一个启用的存储位置。", "error")
            return redirect(url_for("project_detail", slug=slug))
    new_value = 0 if location["is_enabled"] else 1
    db = get_db()
    db.execute("UPDATE storage_locations SET is_enabled = ? WHERE id = ?", (new_value, location_id))
    if location["is_enabled"] and location["is_active"]:
        fallback = db.execute(
            "SELECT id FROM storage_locations WHERE project_id = ? AND is_enabled = 1 ORDER BY priority ASC, id ASC LIMIT 1",
            (project["id"],),
        ).fetchone()
        make_storage_location_active(project["id"], fallback[0])
    db.commit()
    flash(f"已{'停用' if location['is_enabled'] else '启用'}存储位置「{location['name']}」。", "success")
    return redirect(url_for("project_detail", slug=slug))


@app.post("/projects/<slug>/storage-locations/<int:location_id>/delete")
@admin_required
def delete_storage_location(slug, location_id):
    project = project_for_slug(slug)
    location = get_db().execute(
        "SELECT * FROM storage_locations WHERE id = ? AND project_id = ?", (location_id, project["id"])
    ).fetchone() if project else None
    if not location:
        abort(404)
    image_count = get_db().execute("SELECT COUNT(*) FROM images WHERE storage_location_id = ?", (location_id,)).fetchone()[0]
    if image_count:
        flash("该位置仍有关联图片，不能删除；可先停用它。", "error")
    else:
        db = get_db()
        if location["is_active"]:
            fallback = db.execute(
                "SELECT id FROM storage_locations WHERE project_id = ? AND id != ? AND is_enabled = 1 ORDER BY priority ASC, id ASC LIMIT 1",
                (project["id"], location_id),
            ).fetchone()
            if fallback is None:
                flash("当前写入位置是项目唯一可用的位置，不能删除。", "error")
                return redirect(url_for("project_detail", slug=slug))
            make_storage_location_active(project["id"], fallback[0])
        db.execute("DELETE FROM storage_locations WHERE id = ?", (location_id,))
        db.commit()
        flash(f"已删除存储位置「{location['name']}」的配置，磁盘上的文件未被删除。", "success")
    return redirect(url_for("project_detail", slug=slug))


@app.post("/projects/<slug>/upload")
@admin_required
def admin_upload(slug):
    project = project_for_slug(slug)
    if not project:
        abort(404)
    files = [file for file in request.files.getlist("files") if file and file.filename]
    if len(files) > MAX_UPLOAD_FILES:
        flash(f"单次最多上传 {MAX_UPLOAD_FILES} 个文件。", "error")
        return redirect(url_for("project_detail", slug=slug))
    uploaded, errors = 0, []
    for file in files:
        try:
            save_upload(project, file)
            uploaded += 1
        except ValueError as error:
            errors.append(f"{file.filename}: {error}")
    if uploaded:
        flash(f"已上传 {uploaded} 个文件。", "success")
    for error in errors:
        flash(error, "error")
    return redirect(url_for("project_detail", slug=slug))


@app.post("/projects/<slug>/images/<stored_name>/delete")
@admin_required
def delete_image(slug, stored_name):
    project = project_for_slug(slug)
    image = image_with_location(project["id"], stored_name) if project else None
    if not image:
        abort(404)
    path = resolve_image_file(project, image)
    if path:
        path.unlink()
    get_db().execute("DELETE FROM images WHERE id = ?", (image["id"],))
    get_db().commit()
    flash("图片已删除。", "success")
    return redirect(url_for("project_detail", slug=slug))


@app.post("/projects/<slug>/token")
@admin_required
def rotate_token(slug):
    project = project_for_slug(slug)
    if not project:
        abort(404)
    get_db().execute("UPDATE projects SET api_token = ? WHERE id = ?", (secrets.token_urlsafe(32), project["id"]))
    get_db().commit()
    flash("API Token 已重新生成，旧 Token 已失效。", "success")
    return redirect(url_for("project_detail", slug=slug))


@app.post("/api/v1/projects/<slug>/images")
def api_upload(slug):
    project = project_for_slug(slug)
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not project or not token or not secrets.compare_digest(token, project["api_token"]):
        return jsonify(error="无效的项目或 API Token。"), 401
    file = request.files.get("file")
    if not file:
        return jsonify(error="请以 multipart/form-data 的 file 字段提交图片。"), 400
    try:
        stored_name = save_upload(project, file)
    except StorageUnavailableError as error:
        return jsonify(error=str(error)), 507
    except ValueError as error:
        return jsonify(error=str(error)), 400
    image_url = public_image_url(slug, stored_name)
    return jsonify(url=image_url, path=f"/images/{slug}/{stored_name}", project=slug), 201


@app.get("/images/<slug>/<stored_name>")
def public_image(slug, stored_name):
    project = project_for_slug(slug)
    if not project:
        abort(404)
    image = image_with_location(project["id"], stored_name)
    if not image:
        abort(404)
    path = resolve_image_file(project, image)
    if not path:
        abort(404)
    return send_from_directory(path.parent, path.name, conditional=True)


if __name__ == "__main__":
    from waitress import serve

    serve(app, host="127.0.0.1", port=8000, threads=4)
