import tempfile
import unittest
from pathlib import Path

import app as picture_bed


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.original_data_dir = picture_bed.DATA_DIR
        self.original_upload_dir = picture_bed.UPLOAD_DIR
        self.original_database = picture_bed.DATABASE
        self.original_security_config = {
            key: picture_bed.app.config[key]
            for key in ("MAX_CONTENT_LENGTH", "MAX_FORM_MEMORY_SIZE", "MAX_FORM_PARTS", "TRUSTED_HOSTS")
        }
        picture_bed.DATA_DIR = self.root / "data"
        picture_bed.UPLOAD_DIR = picture_bed.DATA_DIR / "uploads"
        picture_bed.DATABASE = self.root / "picture_bed.sqlite3"
        picture_bed.init_storage()
        picture_bed.configure_runtime_security(20, "")
        picture_bed.app.config.update(TESTING=True, SECRET_KEY=picture_bed.get_setting("secret_key"))
        self.client = picture_bed.app.test_client()

    def tearDown(self):
        picture_bed.DATA_DIR = self.original_data_dir
        picture_bed.UPLOAD_DIR = self.original_upload_dir
        picture_bed.DATABASE = self.original_database
        picture_bed.app.config.update(self.original_security_config)
        self.temp_dir.cleanup()

    def csrf_token(self):
        response = self.client.get("/login")
        self.assertEqual(response.status_code, 200)
        with self.client.session_transaction() as client_session:
            return client_session["csrf_token"]

    def test_mutating_routes_require_csrf_tokens(self):
        response = self.client.post("/login", data={"password": "admin"})
        self.assertEqual(response.status_code, 400)

        response = self.client.post(
            "/login",
            data={"password": "admin", "csrf_token": self.csrf_token(), "next": "https://attacker.example"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/")

        response = self.client.post("/logout")
        self.assertEqual(response.status_code, 400)

    def test_security_headers_cookie_and_host_validation(self):
        response = self.client.get("/login")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])
        self.assertEqual(response.headers["Cache-Control"], "no-store")

        response = self.client.post("/login", data={"password": "admin", "csrf_token": self.csrf_token()})
        cookie = response.headers["Set-Cookie"]
        self.assertIn("Secure", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)

        response = self.client.get("/login", headers={"Host": "attacker.example"})
        self.assertEqual(response.status_code, 400)

    def test_runtime_upload_limit_is_applied(self):
        self.assertEqual(picture_bed.app.config["MAX_CONTENT_LENGTH"], 20 * 1024 * 1024)
        self.assertEqual(picture_bed.app.config["MAX_FORM_PARTS"], picture_bed.MAX_UPLOAD_FILES + 10)


if __name__ == "__main__":
    unittest.main()
