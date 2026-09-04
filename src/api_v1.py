"""项目 Token 认证的 JSON API（/api/v1）。

管理端页面是给人用的（Session + CSRF + 表单重定向）；这里是给**其它程序**用的：
只认 ``Authorization: Bearer <项目 API Token>``，只回 JSON，绝不重定向。

原先这套 API 只有「上传」一个动作，接入方一旦要删图、要确认某张图还在不在、要断点续传
地搬一批历史图片，就无从下手，只能去操作管理端 HTML 表单。这里把这几件事补齐：

    GET    /api/v1/projects/<slug>/ping             连接自检：项目信息 + 服务端限制
    POST   /api/v1/projects/<slug>/images           上传（支持 external_key 幂等）
    GET    /api/v1/projects/<slug>/images           列表（按 id 游标翻页）
    POST   /api/v1/projects/<slug>/images/lookup    按 external_key 批量查在不在
    GET    /api/v1/projects/<slug>/images/<name>    单张元数据
    DELETE /api/v1/projects/<slug>/images/<name>    删除

所有端点名都以 ``api_`` 开头——``security.validate_csrf`` 据此放行（外部程序没有会话，
拿不到 CSRF token；认证由 Bearer Token 承担）。
"""

import secrets

from flask import jsonify, request

from . import config, derivatives
from .database import get_db, get_setting
from .storage import (
    StorageUnavailableError,
    delete_image_record,
    image_for_external_key,
    image_with_location,
    project_for_slug,
    public_image_url,
    save_upload,
)

MAX_LOOKUP_KEYS = 500
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 500


class ApiError(Exception):
    """带 HTTP 状态码的 API 错误；统一由各端点转成 JSON 响应。"""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _tokens_match(provided, expected):
    """用字节比较：compare_digest 遇到非 ASCII 的 str 会抛 TypeError，
    而这个值完全由请求头控制——不转字节的话，随手发个中文 Token 就能把端点打成 500。"""
    try:
        return secrets.compare_digest(provided.encode("utf-8"), (expected or "").encode("utf-8"))
    except (AttributeError, UnicodeEncodeError):
        return False


def authenticated_project(slug):
    project = project_for_slug(slug)
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not project or not token or not _tokens_match(token, project["api_token"]):
        raise ApiError(401, "无效的项目或 API Token。")
    return project


def image_payload(project, image):
    stored_name = image["stored_name"]
    return {
        "stored_name": stored_name,
        "url": public_image_url(project["slug"], stored_name),
        "path": f"/images/{project['slug']}/{stored_name}",
        "project": project["slug"],
        "original_name": image["original_name"],
        "content_type": image["content_type"],
        "size": image["size"],
        "sha256": image["sha256"],
        "external_key": image["external_key"],
        "created_at": image["created_at"],
    }


def api_endpoint(view):
    """把 ApiError 转成 JSON 响应；未捕获的异常仍交给 Flask，不吞掉真正的 bug。"""

    def wrapped(*args, **kwargs):
        try:
            return view(*args, **kwargs)
        except ApiError as error:
            return jsonify(error=error.message), error.status

    wrapped.__name__ = view.__name__
    return wrapped


@api_endpoint
def api_ping(slug):
    """连接自检。接入方的「测试连接」按钮打这个端点，一次拿全所有需要对齐的服务端限制。"""
    project = authenticated_project(slug)
    row = get_db().execute(
        "SELECT COUNT(*) AS count, COALESCE(SUM(size), 0) AS total FROM images WHERE project_id = ?",
        (project["id"],),
    ).fetchone()
    return jsonify(
        ok=True,
        project=project["slug"],
        name=project["name"],
        image_count=row["count"],
        total_size=row["total"],
        max_upload_mb=int(get_setting("max_upload_mb") or 0),
        allowed_extensions=sorted(config.ALLOWED_EXTENSIONS),
        thumbnail_widths=list(config.DERIVATIVE_WIDTHS),
        public_base_url=get_setting("public_base_url"),
    )


@api_endpoint
def api_upload(slug):
    project = authenticated_project(slug)
    file = request.files.get("file")
    if not file:
        raise ApiError(400, "请以 multipart/form-data 的 file 字段提交图片。")
    external_key = (request.form.get("external_key") or "").strip() or None
    expected_sha256 = (request.form.get("sha256") or "").strip().lower() or None
    try:
        stored_name, reused = save_upload(project, file, external_key=external_key)
    except StorageUnavailableError as error:
        raise ApiError(507, str(error)) from error
    except ValueError as error:
        raise ApiError(400, str(error)) from error
    image = image_with_location(project["id"], stored_name)
    # 传了 sha256 就当场核对：写盘和入库都成功、内容却对不上，说明传输途中被截断或改动过。
    # 迁移场景下这是唯一能在「删除本地原图」之前发现问题的机会，所以这里直接判失败。
    if expected_sha256 and image["sha256"] and image["sha256"] != expected_sha256:
        actual = image["sha256"]
        # 必须把这份坏数据删掉再报错。留着的话它占着那个 external_key：调用方重试时会命中
        # 幂等分支，拿回这条**校验失败过**的记录并当成成功——校验反而成了坏数据的护身符。
        if not reused:
            delete_image_record(project, image)
        raise ApiError(422, f"内容校验不一致：期望 {expected_sha256}，实际 {actual}。")
    return jsonify(reused=reused, **image_payload(project, image)), (200 if reused else 201)


@api_endpoint
def api_image_detail(slug, stored_name):
    project = authenticated_project(slug)
    image = image_with_location(project["id"], stored_name)
    if not image:
        raise ApiError(404, "图片不存在。")
    return jsonify(**image_payload(project, image))


@api_endpoint
def api_delete_image(slug, stored_name):
    project = authenticated_project(slug)
    image = image_with_location(project["id"], stored_name)
    if not image:
        # 删除做成幂等：重复删同一张不算错误，接入方的清理逻辑不必先查再删。
        return jsonify(ok=True, deleted=False, stored_name=stored_name)
    try:
        delete_image_record(project, image)
    except OSError as error:
        raise ApiError(500, f"删除失败：{error}") from error
    derivatives.purge_derivatives(project["slug"], stored_name)
    return jsonify(ok=True, deleted=True, stored_name=stored_name)


@api_endpoint
def api_lookup_images(slug):
    """按 external_key 批量查询已存在的图片。

    迁移续传的核心：接入方把这一批要搬的 key 丢过来，一次就知道哪些已经在图床上了，
    不用对每张图各发一次请求。
    """
    project = authenticated_project(slug)
    body = request.get_json(silent=True) or {}
    keys = body.get("external_keys")
    if not isinstance(keys, list):
        raise ApiError(400, "请以 JSON 提交 external_keys 数组。")
    keys = [str(key).strip() for key in keys if str(key).strip()]
    if len(keys) > MAX_LOOKUP_KEYS:
        raise ApiError(400, f"单次最多查询 {MAX_LOOKUP_KEYS} 个 external_key。")
    found = {}
    for key in keys:
        image = image_for_external_key(project["id"], key)
        if image is not None:
            found[key] = image_payload(project, image)
    return jsonify(found=found, missing=[key for key in keys if key not in found])


@api_endpoint
def api_list_images(slug):
    """按 id 升序游标翻页。用 id 而不是 offset：翻页途中有新增/删除也不会漏行或重复。"""
    project = authenticated_project(slug)
    try:
        after_id = int(request.args.get("after_id", "0"))
        limit = int(request.args.get("limit", DEFAULT_PAGE_SIZE))
    except ValueError as error:
        raise ApiError(400, "after_id 与 limit 必须是整数。") from error
    limit = max(1, min(limit, MAX_PAGE_SIZE))
    rows = get_db().execute(
        """
        SELECT i.*, l.path AS location_path, l.name AS location_name
        FROM images i
        LEFT JOIN storage_locations l ON l.id = i.storage_location_id
        WHERE i.project_id = ? AND i.id > ?
        ORDER BY i.id ASC LIMIT ?
        """,
        (project["id"], after_id, limit),
    ).fetchall()
    items = [dict(image_payload(project, row), id=row["id"]) for row in rows]
    return jsonify(
        items=items,
        next_after_id=items[-1]["id"] if len(items) == limit else None,
    )


def register_api_routes(app):
    app.add_url_rule("/api/v1/projects/<slug>/ping", "api_ping", api_ping, methods=["GET"])
    app.add_url_rule("/api/v1/projects/<slug>/images", "api_upload", api_upload, methods=["POST"])
    app.add_url_rule("/api/v1/projects/<slug>/images", "api_list_images", api_list_images, methods=["GET"])
    app.add_url_rule("/api/v1/projects/<slug>/images/lookup", "api_lookup_images", api_lookup_images, methods=["POST"])
    app.add_url_rule("/api/v1/projects/<slug>/images/<stored_name>", "api_image_detail", api_image_detail, methods=["GET"])
    app.add_url_rule("/api/v1/projects/<slug>/images/<stored_name>", "api_delete_image", api_delete_image, methods=["DELETE"])


__all__ = ["register_api_routes", "ApiError"]
