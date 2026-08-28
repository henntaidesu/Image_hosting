"""HTTP routes for the Image Hosting web application."""

import secrets
import sqlite3

from flask import abort, current_app, flash, jsonify, redirect, render_template, request, send_from_directory, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from . import config
from .database import get_db, get_setting, now, set_setting
from .security import admin_required, configure_runtime_security, csrf_token, safe_redirect_target, validate_public_base_url
from .storage import (
    StorageUnavailableError,
    ensure_storage_path_available,
    image_with_location,
    make_storage_location_active,
    project_for_slug,
    public_image_url,
    resolve_image_file,
    save_upload,
    storage_locations_for_project,
    storage_status,
    validate_storage_path,
)


def template_helpers():
    return {
        "csrf_token": csrf_token,
        "get_setting": get_setting,
        "public_image_url": public_image_url,
        "storage_status": storage_status,
    }


def login():
    if request.method == "POST":
        if check_password_hash(get_setting("admin_password_hash"), request.form.get("password", "")):
            session.clear()
            session["is_admin"] = True
            session.permanent = True
            return redirect(safe_redirect_target(request.form.get("next")) or url_for("dashboard"))
        flash("密码不正确。", "error")
    return render_template("login.html", next=request.args.get("next", ""))


def logout():
    session.clear()
    return redirect(url_for("login"))


@admin_required
def dashboard():
    projects = get_db().execute("""
        SELECT p.*, COUNT(i.id) AS image_count, COALESCE(SUM(i.size), 0) AS total_size
        FROM projects p LEFT JOIN images i ON p.id = i.project_id
        GROUP BY p.id ORDER BY p.created_at DESC
    """).fetchall()
    return render_template("dashboard.html", projects=projects, using_default_password=check_password_hash(get_setting("admin_password_hash"), "admin"))


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
        configure_runtime_security(current_app, max_upload_number, public_base_url)
        flash("系统设置已保存。", "success")
        return redirect(url_for("settings"))
    return render_template("settings.html", max_upload_mb=get_setting("max_upload_mb"), public_base_url=get_setting("public_base_url"))


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


@admin_required
def admin_upload(slug):
    project = project_for_slug(slug)
    if not project:
        abort(404)
    files = [file for file in request.files.getlist("files") if file and file.filename]
    if len(files) > config.MAX_UPLOAD_FILES:
        flash(f"单次最多上传 {config.MAX_UPLOAD_FILES} 个文件。", "error")
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


@admin_required
def rotate_token(slug):
    project = project_for_slug(slug)
    if not project:
        abort(404)
    get_db().execute("UPDATE projects SET api_token = ? WHERE id = ?", (secrets.token_urlsafe(32), project["id"]))
    get_db().commit()
    flash("API Token 已重新生成，旧 Token 已失效。", "success")
    return redirect(url_for("project_detail", slug=slug))


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


def register_routes(app):
    app.context_processor(template_helpers)
    app.add_url_rule("/login", "login", login, methods=["GET", "POST"])
    app.add_url_rule("/logout", "logout", logout, methods=["POST"])
    app.add_url_rule("/", "dashboard", dashboard, methods=["GET"])
    app.add_url_rule("/settings", "settings", settings, methods=["GET", "POST"])
    app.add_url_rule("/projects", "create_project", create_project, methods=["POST"])
    app.add_url_rule("/projects/<slug>", "project_detail", project_detail, methods=["GET"])
    app.add_url_rule("/projects/<slug>/storage-locations", "add_storage_location", add_storage_location, methods=["POST"])
    app.add_url_rule("/projects/<slug>/storage-locations/<int:location_id>/update", "update_storage_location", update_storage_location, methods=["POST"])
    app.add_url_rule("/projects/<slug>/storage-locations/<int:location_id>/activate", "activate_storage_location", activate_storage_location, methods=["POST"])
    app.add_url_rule("/projects/<slug>/storage-locations/<int:location_id>/toggle", "toggle_storage_location", toggle_storage_location, methods=["POST"])
    app.add_url_rule("/projects/<slug>/storage-locations/<int:location_id>/delete", "delete_storage_location", delete_storage_location, methods=["POST"])
    app.add_url_rule("/projects/<slug>/upload", "admin_upload", admin_upload, methods=["POST"])
    app.add_url_rule("/projects/<slug>/images/<stored_name>/delete", "delete_image", delete_image, methods=["POST"])
    app.add_url_rule("/projects/<slug>/token", "rotate_token", rotate_token, methods=["POST"])
    app.add_url_rule("/api/v1/projects/<slug>/images", "api_upload", api_upload, methods=["POST"])
    app.add_url_rule("/images/<slug>/<stored_name>", "public_image", public_image, methods=["GET"])
