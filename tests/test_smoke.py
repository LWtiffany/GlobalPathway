"""Minimal smoke tests for the CI pipeline (no live database needed).

These verify the packaging is intact: every module imports, runtime config
points at the right RDS defaults, and the schema bootstrap covers the core
domain tables. They intentionally avoid any network/DB access so they run in
GitHub Actions without secrets.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class SmokeTests(unittest.TestCase):
    def test_modules_importable(self):
        import admin_console
        import admin_console.apply_review
        import admin_console.auth
        import admin_console.cli
        import admin_console.config
        import admin_console.console
        import admin_console.db
        import admin_console.server
        self.assertTrue(admin_console.__version__)

    def test_config_defaults_point_at_local_tunnel(self):
        """dev 分支本地默认走 SSH 隧道；prod 部署由 .env 覆盖为 RDS 内网地址。"""
        from admin_console import config
        self.assertEqual(config.DB_HOST, "127.0.0.1")
        self.assertEqual(config.DB_PORT, 15432)
        self.assertEqual(config.DB_NAME, "team03")
        self.assertEqual(config.DB_USER, "team03")

    def test_schema_covers_core_tables(self):
        from admin_console.db import SCHEMA
        for table in ("VISA_POLICY", "FX_POLICY", "TUITION",
                      "admin.review_queue", "admin.admin_user"):
            self.assertIn(table, SCHEMA, f"missing table in schema: {table}")

    def test_healthz_route_registered(self):
        from admin_console.server import ROUTES
        paths = [pattern.pattern for _, pattern, _, _ in ROUTES]
        self.assertIn(r"^/healthz$", paths)


if __name__ == "__main__":
    unittest.main()
