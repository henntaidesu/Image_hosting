"""SQLite initialization, settings, and request-scoped connections."""

import os
import secrets
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

from flask import g
from werkzeug.security import generate_password_hash

from . import config


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_storage():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    legacy_database = config.RUNTIME_DIR / "picture_bed.sqlite3"
    default_database = config.DATA_DIR / "picture_bed.sqlite3"
    if (
        config.DATABASE == default_database
        and legacy_database != config.DATABASE
        and not config.DATABASE.exists()
        and legacy_database.is_file()
    ):
        shutil.move(str(legacy_database), str(config.DATABASE))

    with closing(sqlite3.connect(config.DATABASE)) as db:
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
            (str(config.UPLOAD_DIR.resolve()) + os.sep,),
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
                    (project_id, "主存储位置", storage_path, 10, 1024, 1, 0, now()),
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


def get_setting(key):
    db = sqlite3.connect(config.DATABASE)
    try:
        row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    finally:
        db.close()
    return row[0] if row else ""


def set_setting(key, value):
    get_db().execute("UPDATE settings SET value = ? WHERE key = ?", (value, key))
    get_db().commit()


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(config.DATABASE)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_app(app):
    app.teardown_appcontext(close_db)
