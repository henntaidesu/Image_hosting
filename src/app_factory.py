"""Flask application construction."""

from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

from . import config, database, routes, security


def create_app():
    app = Flask(
        "picture_bed",
        template_folder=str(config.ASSET_DIR / "templates"),
        static_folder=str(config.ASSET_DIR / "static"),
    )
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)
    database.init_storage()
    database.init_app(app)
    security.init_app(app)
    routes.register_routes(app)
    return app
