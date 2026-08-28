"""CSRF, authentication helpers, trusted hosts, and response headers."""

import secrets
from datetime import timedelta
from functools import wraps
from urllib.parse import urlsplit

from flask import abort, redirect, request, session, url_for

from . import config
from .database import get_setting


def configured_trusted_hosts(public_base_url=None):
    public_base_url = public_base_url if public_base_url is not None else get_setting("public_base_url")
    hostname = urlsplit(public_base_url).hostname if public_base_url else None
    hosts = [hostname] if hostname else []
    if config.INSECURE_LOCAL_MODE or not hosts:
        hosts.extend(config.LOCAL_HOSTS)
    return hosts


def configure_runtime_security(app, max_upload_mb=None, public_base_url=None):
    max_upload_mb = int(max_upload_mb if max_upload_mb is not None else get_setting("max_upload_mb"))
    app.config.update(
        MAX_CONTENT_LENGTH=max_upload_mb * 1024 * 1024,
        MAX_FORM_MEMORY_SIZE=64 * 1024,
        MAX_FORM_PARTS=config.MAX_UPLOAD_FILES + 10,
        TRUSTED_HOSTS=configured_trusted_hosts(public_base_url),
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
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"} or request.endpoint == "api_upload":
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
    app.before_request(validate_csrf)
    app.after_request(set_security_headers)
