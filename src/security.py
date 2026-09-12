"""CSRF, authentication helpers, trusted hosts, and response headers."""

import secrets
from datetime import timedelta
from functools import wraps
from urllib.parse import urlsplit

from flask import abort, current_app, redirect, request, session, url_for

from . import config
from .database import get_setting


def parse_extra_hosts(value):
    """把「附加访问主机名」设置项拆成主机名列表（逗号或空白分隔，去掉端口与协议）。"""
    hosts = []
    for chunk in (value or "").replace(",", " ").split():
        candidate = chunk.strip().rstrip("/")
        if not candidate:
            continue
        if "//" in candidate:
            candidate = urlsplit(candidate).hostname or ""
        elif ":" in candidate and not candidate.startswith("["):
            candidate = candidate.rsplit(":", 1)[0]
        if candidate:
            hosts.append(candidate)
    return hosts


def configured_trusted_hosts(public_base_url=None, extra_hosts=None):
    """一旦配置了公开基地址，TRUSTED_HOSTS 就只剩那一个域名——**同机或局域网里的其它
    程序按 IP 直连就会被 Flask 判成非法主机而 400**。「附加访问主机名」正是给这种情形用的：
    图床挂在 https://img.example.com 对外，同时允许 192.168.1.5 之类的内网地址调 API。"""
    public_base_url = public_base_url if public_base_url is not None else get_setting("public_base_url")
    hostname = urlsplit(public_base_url).hostname if public_base_url else None
    hosts = [hostname] if hostname else []
    extra = extra_hosts if extra_hosts is not None else get_setting("extra_trusted_hosts")
    hosts.extend(parse_extra_hosts(extra))
    if config.INSECURE_LOCAL_MODE or not hosts:
        hosts.extend(config.LOCAL_HOSTS)
    return hosts


def request_body_limit_bytes(max_upload_mb=None):
    """WSGI 层允许的最大请求体（字节）。

    取「图片上限」与「视频上限」中的较大者。这是一道**粗闸门**，只用来挡住明显异常的请求体；
    按类型区分的判断在 ``storage.save_upload`` 里，那里能回一句「文件过大，最大允许 N MB」的
    JSON 错误。把闸门压到图片上限会让 ``config.MAX_VIDEO_UPLOAD_MB`` 形同虚设——比图片上限
    大的视频在进入视图之前就被 Werkzeug 用一张 HTML 413 页面拦掉，谁也说不清是哪一层拒的。
    """
    if max_upload_mb is None:
        max_upload_mb = get_setting("max_upload_mb")
    try:
        image_mb = int(max_upload_mb)
    except (TypeError, ValueError):
        image_mb = 0
    return max(image_mb, config.MAX_VIDEO_UPLOAD_MB) * 1024 * 1024


def refresh_upload_limit():
    """每个带请求体的请求，都按**当前设置**重算一次上限。

    ``MAX_CONTENT_LENGTH`` 原先只在进程启动和「保存系统设置」那一刻写进 ``app.config``，
    于是「设置里写着 200 MB、进程里仍拦在 2 MB」是可能的状态；而 ``/api/v1/.../ping`` 报的是
    **设置值**，它会理直气壮地告诉接入方 200 MB。接入方据此做的预检因此全部落空，最后只收到
    一张没有上下文的 HTML 413 错误页。报出去的数和拦下来的数必须同源，这里就是那一处。

    只在带请求体的方法上现查：GET 取图不该为此多开一次 sqlite 连接。多线程下两个请求可能同时
    写这个键，但写进去的是同一个值，无所谓。
    """
    if request.method in {"POST", "PUT", "PATCH"}:
        current_app.config["MAX_CONTENT_LENGTH"] = request_body_limit_bytes()


def configure_runtime_security(app, max_upload_mb=None, public_base_url=None, extra_hosts=None):
    app.config.update(
        MAX_CONTENT_LENGTH=request_body_limit_bytes(max_upload_mb),
        # 这个值**不只**限制普通表单字段，它同时是 multipart 解码器内部缓冲区的上限
        # （werkzeug/sansio/multipart.py::receive_data，文件分片一样计入），所以它必须
        # 显著大于 MultiPartParser 的 64 KB 读块。原先取的正是 64 KB：解码器每收到一个
        # 64 KB 分片，缓冲区里只要还剩一点上一片的尾巴，len(buffer)+len(data) 就越限并
        # 直接抛 413。尾巴留多长取决于分片末尾到最后一个 CR/LF 的距离，也就是取决于文件
        # 内容——于是表现为「同样大小的图，有的传得上去有的传不上去」，看着像随机故障。
        # 实测 120 张真实商品图：64 KB 时 19 张失败（657 KB ~ 6.9 MB），1 MB 时 0 张。
        MAX_FORM_MEMORY_SIZE=1024 * 1024,
        MAX_FORM_PARTS=config.MAX_UPLOAD_FILES + 10,
        TRUSTED_HOSTS=configured_trusted_hosts(public_base_url, extra_hosts),
    )


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
    if not config.INSECURE_LOCAL_MODE and parsed.scheme != "https":
        raise ValueError("公网部署的公开访问基地址必须使用 HTTPS。")
    return value.rstrip("/")


def validate_csrf():
    # ``api_*`` 是 Token 认证的机器接口：调用方没有会话，也就拿不到 CSRF token，
    # 而 CSRF 攻击的前提（浏览器自动附带的会话 Cookie）在这些端点上根本不成立。
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"} or (request.endpoint or "").startswith("api_"):
        return None
    expected = session.get("csrf_token")
    provided = request.form.get("csrf_token", "")
    if not expected or not provided or not secrets.compare_digest(expected, provided):
        abort(400, "CSRF token 无效或缺失。")
    return None


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


def init_app(app):
    app.config.update(
        SECRET_KEY=get_setting("secret_key"),
        SESSION_COOKIE_NAME="picture-bed-session" if config.INSECURE_LOCAL_MODE else "__Host-picture-bed-session",
        SESSION_COOKIE_SECURE=not config.INSECURE_LOCAL_MODE,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        PREFERRED_URL_SCHEME="http" if config.INSECURE_LOCAL_MODE else "https",
    )
    configure_runtime_security(app)
    # 必须排在 validate_csrf 前面：后者会读 request.form，表单一旦在那里被解析，
    # 用的就是刷新之前的旧上限。
    app.before_request(refresh_upload_limit)
    app.before_request(validate_csrf)
    app.after_request(set_security_headers)
