import io
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from PIL import Image

import app as picture_bed


def png_file(name="image.png"):
    data = io.BytesIO()
    Image.new("RGB", (2, 2), "cyan").save(data, "PNG")
    data.seek(0)
    return data, name


class DatabaseLocationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.original_data_dir = picture_bed.DATA_DIR
        self.original_upload_dir = picture_bed.UPLOAD_DIR
        self.original_database = picture_bed.DATABASE

    def tearDown(self):
        picture_bed.DATA_DIR = self.original_data_dir
        picture_bed.UPLOAD_DIR = self.original_upload_dir
        picture_bed.DATABASE = self.original_database
        self.temp_dir.cleanup()

    def test_init_storage_moves_legacy_database_to_runtime_root(self):
        picture_bed.DATA_DIR = self.root / "data"
        picture_bed.UPLOAD_DIR = picture_bed.DATA_DIR / "uploads"
        picture_bed.DATABASE = self.root / "picture_bed.sqlite3"
        legacy_database = picture_bed.DATA_DIR / "picture_bed.sqlite3"
        legacy_database.parent.mkdir(parents=True)
        with closing(sqlite3.connect(legacy_database)) as db:
            db.execute("CREATE TABLE migration_marker (value TEXT)")
            db.execute("INSERT INTO migration_marker VALUES ('kept')")
            db.commit()

        picture_bed.init_storage()

        self.assertTrue(picture_bed.DATABASE.is_file())
        self.assertFalse(legacy_database.exists())
        with closing(sqlite3.connect(picture_bed.DATABASE)) as db:
            self.assertEqual(db.execute("SELECT value FROM migration_marker").fetchone()[0], "kept")


class StorageRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.original_data_dir = picture_bed.DATA_DIR
        self.original_upload_dir = picture_bed.UPLOAD_DIR
        self.original_database = picture_bed.DATABASE
        picture_bed.DATA_DIR = self.root / "data"
        picture_bed.UPLOAD_DIR = picture_bed.DATA_DIR / "uploads"
        picture_bed.DATABASE = picture_bed.DATA_DIR / "picture_bed.sqlite3"
        picture_bed.init_storage()
        picture_bed.app.config.update(TESTING=True, SECRET_KEY=picture_bed.get_setting("secret_key"))
        self.client = picture_bed.app.test_client()
        self.client.post("/login", data={"password": "admin"})
        self.primary = self.root / "primary"
        self.secondary = self.root / "secondary"
        self.client.post("/projects", data={"name": "测试项目", "storage_path": str(self.primary)})
        with picture_bed.app.app_context():
            self.project = picture_bed.get_db().execute("SELECT * FROM projects").fetchone()
            self.primary_location = picture_bed.get_db().execute(
                "SELECT * FROM storage_locations WHERE project_id = ?", (self.project["id"],)
            ).fetchone()

    def tearDown(self):
        picture_bed.DATA_DIR = self.original_data_dir
        picture_bed.UPLOAD_DIR = self.original_upload_dir
        picture_bed.DATABASE = self.original_database
        self.temp_dir.cleanup()

    def add_secondary_location(self):
        response = self.client.post(
            f"/projects/{self.project['slug']}/storage-locations",
            data={"name": "第二硬盘", "path": str(self.secondary), "priority": "20", "reserve_mb": "0"},
        )
        self.assertEqual(response.status_code, 302)
        with picture_bed.app.app_context():
            return picture_bed.get_db().execute(
                "SELECT * FROM storage_locations WHERE project_id = ? AND name = ?",
                (self.project["id"], "第二硬盘"),
            ).fetchone()

    def upload_and_get_location(self, filename):
        image, filename = png_file(filename)
        response = self.client.post(
            f"/projects/{self.project['slug']}/upload",
            data={"files": (image, filename)},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        with picture_bed.app.app_context():
            saved_image = picture_bed.get_db().execute(
                "SELECT * FROM images WHERE project_id = ? ORDER BY id DESC LIMIT 1", (self.project["id"],)
            ).fetchone()
        self.assertIsNotNone(saved_image, response.get_data(as_text=True))
        return saved_image

    def test_add_switch_and_full_disk_failover_keep_writes_on_new_location(self):
        secondary_location = self.add_secondary_location()

        # Adding a location activates it, so uploads from this moment use it.
        added_image = self.upload_and_get_location("added.png")
        self.assertEqual(added_image["storage_location_id"], secondary_location["id"])
        self.assertTrue((self.secondary / added_image["stored_name"]).is_file())

        # A manual switch immediately changes the target of new uploads.
        response = self.client.post(
            f"/projects/{self.project['slug']}/storage-locations/{self.primary_location['id']}/activate"
        )
        self.assertEqual(response.status_code, 302)
        switched_image = self.upload_and_get_location("switched.png")
        self.assertEqual(switched_image["storage_location_id"], self.primary_location["id"])
        self.assertTrue((self.primary / switched_image["stored_name"]).is_file())

        # If the current folder is full, the next available folder is selected and persisted as current.
        original_disk_usage = picture_bed.shutil.disk_usage
        real_usage = original_disk_usage(self.secondary)
        usage_type = type(real_usage)

        def fake_disk_usage(path):
            if Path(path).resolve() == self.primary.resolve():
                return usage_type(real_usage.total, real_usage.total, 0)
            return usage_type(real_usage.total, 0, real_usage.total)

        picture_bed.shutil.disk_usage = fake_disk_usage
        try:
            failover_image = self.upload_and_get_location("failover.png")
        finally:
            picture_bed.shutil.disk_usage = original_disk_usage

        self.assertEqual(failover_image["storage_location_id"], secondary_location["id"])
        with picture_bed.app.app_context():
            active_location = picture_bed.get_db().execute(
                "SELECT id FROM storage_locations WHERE project_id = ? AND is_active = 1", (self.project["id"],)
            ).fetchone()
        self.assertEqual(active_location["id"], secondary_location["id"])
        self.assertTrue((self.secondary / failover_image["stored_name"]).is_file())

        # Existing URLs still read the folder recorded for their individual image.
        response = self.client.get(f"/images/{self.project['slug']}/{switched_image['stored_name']}")
        self.assertEqual(response.status_code, 200)
        response.close()


if __name__ == "__main__":
    unittest.main()
