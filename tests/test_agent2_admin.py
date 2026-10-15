"""审核后台 /admin：管理员登录、REVIEW_QUEUE 列表 / 详情、批准入库与驳回。"""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agent1 import formal_pipeline as fp
from agent1 import storage
from agent1.environment import paths_for
from agent2 import api
from agent2.admin import SESSION_SECONDS, AdminConsole
from tests.test_agent2_s1 import S1Base

PASSWORD = "correct horse"


def visa_payload(title, **fields):
    return {"policy": {"country": "UK", "policy_type": "student", "title": title, **fields}, "documents": []}


class AdminCases(S1Base):
    def setUp(self):
        directory = TemporaryDirectory(prefix="agent2-admin-")
        self.addCleanup(directory.cleanup)
        self.paths = paths_for(Path(directory.name), "development")
        storage.initialize(self.paths)
        with storage.session(self.paths, readonly=False) as db:
            self.first = fp._enqueue_review(
                db, table="VISA_POLICY", key="visa-a", action="CREATE",
                payload=visa_payload("Student visa", cas_required=1, funds_living_monthly=1529,
                                     funds_living_currency="GBP"),
                evidence=[{"field": "funds_living_monthly", "evidence_quote": "£1,529 per month",
                           "source_id": "uk_student_visa_money", "snapshot_id": "snap-x"}],
                reason="证据不足")
            self.second = fp._enqueue_review(
                db, table="VISA_POLICY", key="visa-b", action="CREATE",
                payload=visa_payload("Graduate visa"), evidence=[], reason="跨页面冲突")
        self.now = [0.0]
        self.make()
        self.service.admin = AdminConsole(self.paths, PASSWORD, clock=lambda: self.now[0])
        self.token = self.call("POST", "/v1/admin/login", {"name": "lily", "password": PASSWORD})[1]["token"]

    def call(self, method, path, body=None, token=None):
        raw = json.dumps(body).encode() if body is not None else b""
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return api.dispatch(self.service, method, path, headers, raw)

    def admin(self, method, path, body=None):
        return self.call(method, path, body, token=self.token)

    def visa_row(self, key):
        with storage.session(self.paths) as db:
            row = db.execute("SELECT * FROM VISA_POLICY WHERE policy_id=?", (key,)).fetchone()
        return dict(row) if row else None

    def test_login_requires_configured_password(self):
        self.assertEqual(self.call("POST", "/v1/admin/login", {"name": "lily", "password": "nope"})[0], 401)
        self.assertEqual(self.call("POST", "/v1/admin/login", {"name": " ", "password": PASSWORD})[0], 400)
        self.service.admin = AdminConsole(self.paths, None)
        status, payload = self.call("POST", "/v1/admin/login", {"name": "lily", "password": ""})
        self.assertEqual((status, payload["code"]), (503, "ADMIN_DISABLED"))
        self.service.admin = None
        self.assertEqual(self.call("GET", "/v1/admin/reviews", token=self.token)[0], 503)

    def test_admin_routes_reject_missing_user_and_expired_tokens(self):
        user_token = self.service.demo_login("alice")["token"]
        self.assertEqual(self.call("GET", "/v1/admin/reviews")[0], 401)
        self.assertEqual(self.call("GET", "/v1/admin/reviews", token=user_token)[0], 401)
        self.assertEqual(self.call("GET", "/v1/me", token=self.token)[0], 401)
        self.now[0] = SESSION_SECONDS + 1
        self.assertEqual(self.admin("GET", "/v1/admin/reviews")[0], 401)

    def test_lists_open_reviews_with_summary_counts_and_detail(self):
        status, payload = self.admin("GET", "/v1/admin/reviews")
        self.assertEqual(status, 200)
        self.assertEqual(payload["counts"], {"OPEN": 2, "APPROVED": 0, "REJECTED": 0})
        self.assertEqual(payload["domains"], ["VISA_POLICY"])
        by_id = {r["review_id"]: r for r in payload["reviews"]}
        self.assertEqual(by_id[self.first]["summary"], "UK · Student visa")
        self.assertTrue(by_id[self.first]["applicable"])
        self.assertEqual(self.admin("GET", "/v1/admin/reviews?status=REJECTED")[1]["reviews"], [])
        self.assertEqual(self.admin("GET", "/v1/admin/reviews?domain=TUITION")[1]["counts"]["OPEN"], 0)
        self.assertEqual(self.admin("GET", "/v1/admin/reviews?status=BOGUS")[0], 400)

        status, detail = self.admin("GET", f"/v1/admin/reviews/{self.first}")
        self.assertEqual(status, 200)
        self.assertEqual(detail["proposed_value"]["policy"]["funds_living_monthly"], 1529)
        self.assertEqual(detail["evidence"][0]["evidence_quote"], "£1,529 per month")
        self.assertIsNone(detail["current"])
        self.assertEqual(self.admin("GET", "/v1/admin/reviews/missing")[0], 404)

    def test_approve_writes_domain_row_and_records_decision(self):
        status, detail = self.admin("POST", f"/v1/admin/reviews/{self.first}/approve", {"note": "人工核对原文"})
        self.assertEqual(status, 200)
        self.assertEqual((detail["status"], detail["decided_by"], detail["decision_note"]),
                         ("APPROVED", "admin:lily", "人工核对原文"))
        self.assertEqual(self.visa_row("visa-a")["funds_living_monthly"], 1529)
        self.assertEqual(detail["current"]["policy_id"], "visa-a")
        status, payload = self.admin("POST", f"/v1/admin/reviews/{self.first}/approve")
        self.assertEqual((status, payload["code"]), (409, "REVIEW_CONFLICT"))
        counts = self.admin("GET", "/v1/admin/reviews")[1]["counts"]
        self.assertEqual(counts, {"OPEN": 1, "APPROVED": 1, "REJECTED": 0})

    def test_constraint_violation_is_reported_and_rolled_back(self):
        with storage.session(self.paths, readonly=False) as db:
            bad = fp._enqueue_review(db, table="VISA_POLICY", key="visa-c", action="CREATE",
                                     payload=visa_payload("No currency", funds_living_monthly=1529),
                                     evidence=[], reason="币种缺失")
        status, payload = self.admin("POST", f"/v1/admin/reviews/{bad}/approve")
        self.assertEqual((status, payload["code"]), (409, "REVIEW_CONFLICT"))
        self.assertIsNone(self.visa_row("visa-c"))
        self.assertEqual(self.admin("GET", f"/v1/admin/reviews/{bad}")[1]["status"], "OPEN")

    def test_reject_requires_note_and_leaves_domain_untouched(self):
        self.assertEqual(self.admin("POST", f"/v1/admin/reviews/{self.second}/reject", {"note": "  "})[0], 400)
        status, detail = self.admin("POST", f"/v1/admin/reviews/{self.second}/reject", {"note": "来源不可靠"})
        self.assertEqual((status, detail["status"], detail["decision_note"]), (200, "REJECTED", "来源不可靠"))
        self.assertIsNone(self.visa_row("visa-b"))
        self.assertEqual(self.admin("POST", f"/v1/admin/reviews/{self.second}/approve")[0], 409)
        self.assertEqual(self.admin("POST", "/v1/admin/reviews/missing/reject", {"note": "x"})[0], 404)


class AdminPageCases(unittest.TestCase):
    def test_admin_path_serves_admin_page_and_assets(self):
        for path in ("/admin", "/admin/", "/admin?x=1"):
            data, content_type = api.static_file(path)
            self.assertIn(b"/admin.js", data)
            self.assertTrue(content_type.startswith("text/html"))
        self.assertTrue(api.static_file("/admin.js")[1].startswith("text/javascript"))
        self.assertTrue(api.static_file("/admin.css")[1].startswith("text/css"))
        self.assertIn(b"/app.js", api.static_file("/chat")[0])


if __name__ == "__main__":
    unittest.main()
