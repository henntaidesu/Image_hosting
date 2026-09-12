"""``/api/v1`` 的回归测试：Token 认证、幂等上传、内容校验、查询、删除、缩略图。

全程在临时目录里跑（数据库 + 图片存储目录都是 ``TemporaryDirectory``），不碰开发机上的
真实图片目录。
"""

import hashlib
import io
import os
import random
import tempfile
import unittest
import uuid
from pathlib import Path

# config 在导入时读环境变量：必须在 import src.* 之前设好，否则 TRUSTED_HOSTS 不含 localhost，
# test_client 发出的请求会被当成非法主机而 400。
os.environ["PICTURE_BED_INSECURE_COOKIES"] = "1"

from PIL import Image  # noqa: E402

from src import config, database, derivatives  # noqa: E402
from src.app_factory import create_app  # noqa: E402


def png_bytes(color=(200, 30, 30), size=(64, 48)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def noisy_png_bytes(size=(1400, 1100), seed=0):
    """几 MB 的 PNG。像素填随机噪声，PNG 压不动它，体积才稳定得住。

    固定 seed 是为了可复现：multipart 解码器留多少尾巴在缓冲区里取决于字节内容，
    换一份随机数据可能就碰不到那个边界了。
    """
    raw = random.Random(seed).randbytes(size[0] * size[1] * 3)
    buffer = io.BytesIO()
    Image.frombytes("RGB", size, raw).save(buffer, format="PNG")
    return buffer.getvalue()


class ApiV1TestCase(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        root = Path(self._temp.name)
        config.RUNTIME_DIR = root
        config.DATA_DIR = root / "data"
        config.UPLOAD_DIR = config.DATA_DIR / "uploads"
        config.DATABASE = config.DATA_DIR / "picture_bed.sqlite3"
        self.storage_dir = root / "images"

        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.slug, self.token = self._create_project()

    def tearDown(self):
        self._temp.cleanup()

    # ── 夹具 ──────────────────────────────────────────────────────────── #

    def _create_project(self):
        # CSRF token 只在模板渲染时才生成；这里直接塞进会话，省掉解析 HTML 的一步。
        # 登录本身也是 POST、同样要过 validate_csrf，所以得**先**塞；而登录成功会重建
        # 会话（旧 token 随之作废），于是登录之后还要再塞一次。
        self._seed_csrf()
        self.client.post("/login", data={"password": "admin", "csrf_token": "test-csrf"})
        self._seed_csrf()
        response = self.client.post(
            "/projects",
            data={"name": "测试项目", "storage_path": str(self.storage_dir), "csrf_token": "test-csrf"},
        )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            row = database.get_db().execute("SELECT slug, api_token FROM projects").fetchone()
        self.assertIsNotNone(row, "项目未创建成功")
        return row[0], row[1]

    def _seed_csrf(self):
        with self.client.session_transaction() as session:
            session["csrf_token"] = "test-csrf"

    def _auth(self):
        return {"Authorization": f"Bearer {self.token}"}

    def _upload(self, content=None, filename="logo.png", **fields):
        data = {"file": (io.BytesIO(content if content is not None else png_bytes()), filename)}
        data.update(fields)
        return self.client.post(
            f"/api/v1/projects/{self.slug}/images",
            data=data, headers=self._auth(), content_type="multipart/form-data",
        )

    # ── 认证 ──────────────────────────────────────────────────────────── #

    def test_requires_bearer_token(self):
        for method, path in (
            ("get", f"/api/v1/projects/{self.slug}/ping"),
            ("get", f"/api/v1/projects/{self.slug}/images"),
            ("delete", f"/api/v1/projects/{self.slug}/images/whatever.png"),
        ):
            response = getattr(self.client, method)(path)
            self.assertEqual(response.status_code, 401, f"{method} {path} 应拒绝无 Token 的请求")

    def test_non_ascii_token_is_rejected_not_crashed(self):
        """Token 完全由请求头控制。compare_digest 遇到非 ASCII 的 str 会抛 TypeError，
        不转字节的话随手发个中文 Token 就能把端点打成 500。"""
        response = self.client.get(
            f"/api/v1/projects/{self.slug}/ping",
            headers={"Authorization": "Bearer 中文令牌"},
        )
        self.assertEqual(response.status_code, 401)

    def test_ping_reports_server_limits(self):
        response = self.client.get(f"/api/v1/projects/{self.slug}/ping", headers=self._auth())
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["project"], self.slug)
        self.assertEqual(payload["image_count"], 0)
        self.assertGreater(payload["max_upload_mb"], 0)
        self.assertIn("png", payload["allowed_extensions"])
        self.assertIn(300, payload["thumbnail_widths"])

    # ── 上传 ──────────────────────────────────────────────────────────── #

    def test_upload_returns_metadata_and_persists_file(self):
        response = self._upload()
        self.assertEqual(response.status_code, 201)
        payload = response.get_json()
        self.assertFalse(payload["reused"])
        self.assertEqual(payload["project"], self.slug)
        self.assertEqual(len(payload["sha256"]), 64)
        self.assertTrue((self.storage_dir / payload["stored_name"]).is_file())

    def test_upload_rejects_non_image_content(self):
        response = self._upload(content=b"not an image at all", filename="fake.png")
        self.assertEqual(response.status_code, 400)

    def test_multi_megabyte_upload_is_accepted(self):
        """几 MB 的图必须传得上去。

        MAX_FORM_MEMORY_SIZE 不只管普通表单字段，它同时是 multipart 解码器缓冲区的上限，
        文件分片一样计入。这个值一旦不明显大于解码器 64 KB 的读块，几 MB 的图片就会被
        Werkzeug 拦在视图之外、回一张 HTML 413——而且触发与否取决于文件内容，表现为
        「同样大小的图有的传得上去有的传不上去」，极难定位。实测 64 KB 时 80 张真实商品图
        失败 33 张。
        """
        payload = noisy_png_bytes()
        self.assertGreater(len(payload), 3 * 1024 * 1024, "样本图没到几 MB，测不出问题")
        response = self._upload(content=payload, filename="big.png")
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True)[:200])

    def test_oversized_image_reports_json_error_not_html_413(self):
        """超过单文件上限时，要回一句能读懂的 JSON，而不是 Werkzeug 的 HTML 413。

        MAX_CONTENT_LENGTH 是 WSGI 层的粗闸门，取图片与视频上限的较大者；真正按类型
        区分的判断在 storage.save_upload 里。闸门若压到图片上限，一来比它大的视频永远
        传不上去（config.MAX_VIDEO_UPLOAD_MB 形同虚设），二来调用方只能收到一张没有
        上下文的错误页。
        """
        with self.app.app_context():
            database.set_setting("max_upload_mb", "1")
        response = self._upload(content=noisy_png_bytes(), filename="big.png")
        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.is_json, "应当是 JSON 错误，实际是 " + response.content_type)
        self.assertIn("文件过大", response.get_json()["error"])

    def test_external_key_makes_upload_idempotent(self):
        """迁移会中断重跑；同一个 external_key 必须复用已有记录，绝不能产生第二份文件。"""
        first = self._upload(external_key="/imges/product_1.png")
        self.assertEqual(first.status_code, 201)
        second = self._upload(external_key="/imges/product_1.png")
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.get_json()["reused"])
        self.assertEqual(second.get_json()["stored_name"], first.get_json()["stored_name"])
        stored = list(self.storage_dir.glob("*.png"))
        self.assertEqual(len(stored), 1, "幂等上传不应在磁盘上留下第二份文件")

    def test_upload_rejects_sha256_mismatch(self):
        response = self._upload(sha256="0" * 64)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(list(self.storage_dir.glob("*.png")), [], "校验失败的图片不应留在磁盘上")

    def test_sha256_mismatch_does_not_squat_the_external_key(self):
        """坏数据若留着，它就占住了那个 external_key：调用方重试会命中幂等分支拿回它，
        校验反倒成了坏数据的护身符。"""
        key = "/imges/checked.png"
        self.assertEqual(self._upload(external_key=key, sha256="0" * 64).status_code, 422)
        retry = self._upload(external_key=key)
        self.assertEqual(retry.status_code, 201)
        self.assertFalse(retry.get_json()["reused"])

    def test_upload_accepts_matching_sha256(self):
        content = png_bytes(color=(10, 120, 200))
        digest = hashlib.sha256(content).hexdigest()
        response = self._upload(content=content, sha256=digest)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["sha256"], digest)

    # ── 查询 ──────────────────────────────────────────────────────────── #

    def test_lookup_splits_found_and_missing(self):
        self._upload(external_key="/imges/a.png")
        response = self.client.post(
            f"/api/v1/projects/{self.slug}/images/lookup",
            json={"external_keys": ["/imges/a.png", "/imges/missing.png"]},
            headers=self._auth(),
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIn("/imges/a.png", payload["found"])
        self.assertEqual(payload["missing"], ["/imges/missing.png"])

    def test_list_paginates_by_id_cursor(self):
        for index in range(3):
            self._upload(external_key=f"/imges/p{index}.png")
        first = self.client.get(
            f"/api/v1/projects/{self.slug}/images?limit=2", headers=self._auth()
        ).get_json()
        self.assertEqual(len(first["items"]), 2)
        self.assertIsNotNone(first["next_after_id"])
        second = self.client.get(
            f"/api/v1/projects/{self.slug}/images?limit=2&after_id={first['next_after_id']}",
            headers=self._auth(),
        ).get_json()
        self.assertEqual(len(second["items"]), 1)
        self.assertIsNone(second["next_after_id"])

    def test_detail_404_for_unknown_name(self):
        response = self.client.get(
            f"/api/v1/projects/{self.slug}/images/{uuid.uuid4().hex}.png", headers=self._auth()
        )
        self.assertEqual(response.status_code, 404)

    # ── 删除 ──────────────────────────────────────────────────────────── #

    def test_delete_removes_file_and_is_idempotent(self):
        stored_name = self._upload().get_json()["stored_name"]
        path = f"/api/v1/projects/{self.slug}/images/{stored_name}"
        first = self.client.delete(path, headers=self._auth())
        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.get_json()["deleted"])
        self.assertFalse((self.storage_dir / stored_name).exists())
        # 重复删除不算错误：接入方的清理逻辑不必先查再删
        second = self.client.delete(path, headers=self._auth())
        self.assertEqual(second.status_code, 200)
        self.assertFalse(second.get_json()["deleted"])

    def test_delete_purges_cached_thumbnails(self):
        stored_name = self._upload().get_json()["stored_name"]
        self.client.get(f"/images/{self.slug}/{stored_name}?w=100")
        thumb = derivatives.derivative_path(self.slug, stored_name, 100)
        self.assertTrue(thumb.is_file(), "缩略图应已生成并缓存")
        self.client.delete(f"/api/v1/projects/{self.slug}/images/{stored_name}", headers=self._auth())
        self.assertFalse(thumb.exists(), "原图删除后缩略图缓存不应残留")

    # ── 缩略图 ────────────────────────────────────────────────────────── #

    def test_thumbnail_width_snaps_to_allowed_bucket(self):
        """宽度不设白名单的话，连续变化的 ?w= 能逼服务生成上千份不同尺寸。"""
        stored_name = self._upload(content=png_bytes(size=(800, 600))).get_json()["stored_name"]
        response = self.client.get(f"/images/{self.slug}/{stored_name}?w=137")
        self.assertEqual(response.status_code, 200)
        # 137 向上取整到 200 这一档，不会另开一个 w=137 的缓存文件
        self.assertTrue(derivatives.derivative_path(self.slug, stored_name, 200).is_file())
        self.assertFalse(derivatives.derivative_path(self.slug, stored_name, 137).exists())
        with Image.open(io.BytesIO(response.data)) as thumbnail:
            self.assertEqual(max(thumbnail.size), 200)

    def test_public_image_without_width_returns_original(self):
        content = png_bytes(size=(800, 600))
        stored_name = self._upload(content=content).get_json()["stored_name"]
        response = self.client.get(f"/images/{self.slug}/{stored_name}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, content)

    def test_thumbnail_width_over_max_snaps_down_to_largest_bucket(self):
        stored_name = self._upload(content=png_bytes(size=(2000, 1500))).get_json()["stored_name"]
        response = self.client.get(f"/images/{self.slug}/{stored_name}?w=99999")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(derivatives.derivative_path(self.slug, stored_name, 1200).is_file())

    def test_invalid_width_falls_back_to_original(self):
        content = png_bytes(size=(300, 200))
        stored_name = self._upload(content=content).get_json()["stored_name"]
        response = self.client.get(f"/images/{self.slug}/{stored_name}?w=abc")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, content)


if __name__ == "__main__":
    unittest.main()
