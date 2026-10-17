"""前端支撑接口：档案列表、每档一会话、四类建议、纠错反馈、Plan 待办、字段目录、静态页面。"""
import json
import unittest

from agent2 import api
from tests.test_agent2_s1 import UK_STUDENT, S1Base


class WebApiCases(S1Base):
    def setUp(self):
        self.service = self.make()
        self.token = self.service.demo_login("alice")["token"]
        self.bob = self.service.demo_login("bob")["token"]

    def call(self, method, path, body=None, token=None):
        raw = json.dumps(body).encode() if body is not None else b""
        return api.dispatch(self.service, method, path, {"Authorization": f"Bearer {token or self.token}"}, raw)

    def test_profile_list_is_serialized_and_scoped_to_owner(self):
        mine = self.service.create_profile(self.user, {"beneficiary": "王小明", **UK_STUDENT}, synthetic=True)
        status, payload = self.call("GET", "/v1/profiles")
        self.assertEqual(status, 200)
        self.assertEqual([p["profile_id"] for p in payload["profiles"]], [mine.profile_id])
        self.assertEqual(payload["profiles"][0]["fields"]["beneficiary"]["value"], "王小明")
        status, payload = self.call("GET", "/v1/profiles", token=self.bob)
        self.assertEqual((status, payload["profiles"]), (200, []))

    def test_each_profile_has_one_main_conversation(self):
        profile = self.service.create_profile(self.user, UK_STUDENT, synthetic=True)
        path = f"/v1/profiles/{profile.profile_id}/conversation"
        first = self.call("POST", path)[1]
        second = self.call("POST", path)[1]
        self.assertEqual(first["conversation_id"], second["conversation_id"])
        self.assertEqual(self.call("POST", path, token=self.bob)[0], 404)

    def test_advice_listing_carries_citations_for_chips(self):
        profile, _, answer = self.ask("需要准备哪些签证材料", UK_STUDENT)
        status, payload = self.call("GET", f"/v1/profiles/{profile.profile_id}/advice")
        self.assertEqual(status, 200)
        self.assertEqual(list(payload["advice"]), ["visa"])
        cited = {c["citation_id"] for c in payload["advice"]["visa"]["payload"]["citations"]}
        self.assertEqual(cited, {c.citation_id for c in answer.citations})
        self.assertEqual(self.call("GET", f"/v1/profiles/{profile.profile_id}/advice", token=self.bob)[0], 404)

    def test_wrong_feedback_opens_numbered_ticket_without_touching_profile(self):
        profile, _, answer = self.ask("需要准备哪些签证材料", UK_STUDENT)
        path = f"/v1/messages/{answer.message_id}/feedback"
        status, helpful = self.call("POST", path, {"kind": "helpful"})
        self.assertEqual((status, helpful["feedback_id"]), (200, None))
        status, wrong = self.call("POST", path, {"kind": "wrong", "note": "缺少 ATAS 说明"})
        self.assertEqual(status, 200)
        self.assertRegex(wrong["feedback_id"], r"^FB-20261014-[0-9A-F]{6}$")
        tickets = self.call("GET", "/v1/feedback")[1]["tickets"]
        self.assertEqual([(t["feedback_id"], t["note"], t["status"], t["answer_status"]) for t in tickets],
                         [(wrong["feedback_id"], "缺少 ATAS 说明", "open", "answered")])
        self.assertEqual(self.service.get_profile(self.user, profile.profile_id).version, 1)
        self.assertEqual(self.call("POST", path, {"kind": "wrong"}, token=self.bob)[0], 404)
        self.assertEqual(self.call("GET", "/v1/feedback", token=self.bob)[1]["tickets"], [])
        self.assertEqual(self.call("POST", path, {"kind": "love"})[0], 400)

    def test_plan_items_are_user_edited_and_owner_only(self):
        profile = self.service.create_profile(self.user, UK_STUDENT, synthetic=True)
        base = f"/v1/profiles/{profile.profile_id}/plan"
        status, item = self.call("POST", base, {"text": "  预约签证中心  "})
        self.assertEqual((status, item["text"], item["done"]), (201, "预约签证中心", False))
        self.assertEqual(self.call("POST", base, {"text": " "})[0], 400)
        item_path = f"/v1/plan-items/{item['item_id']}"
        self.assertEqual(self.call("PATCH", item_path, {"done": True}, token=self.bob)[0], 404)
        self.assertEqual(self.call("DELETE", item_path, token=self.bob)[0], 404)
        self.assertEqual(self.call("PATCH", item_path, {"done": True})[1]["done"], True)
        self.assertEqual(self.call("PATCH", item_path, {"done": "yes"})[0], 400)
        items = self.call("GET", base)[1]["items"]
        self.assertEqual([(i["text"], i["done"]) for i in items], [("预约签证中心", True)])
        self.assertEqual(self.call("GET", base, token=self.bob)[0], 404)
        self.assertEqual(self.call("DELETE", item_path)[1]["deleted"], True)
        self.assertEqual(self.call("GET", base)[1]["items"], [])

    def test_field_catalog_gives_labels_and_options_without_login(self):
        status, payload = api.dispatch(self.service, "GET", "/v1/fields", {}, b"")
        self.assertEqual(status, 200)
        visa = payload["fields"]["visa_category"]
        self.assertEqual(visa["kind"], "enum")
        self.assertIn({"value": "student_visa", "label": "学生签证"}, visa["options"])
        self.assertEqual(payload["fields"]["planned_start_date"]["kind"], "date")
        self.assertEqual([o["value"] for o in payload["fields"]["stay_over_6_months"]["options"]], [True, False])
        self.assertEqual(payload["fields"]["beneficiary"]["label"], "服务对象")


class StaticFileCases(unittest.TestCase):
    def test_index_assets_and_spa_fallback(self):
        index, content_type = api.static_file("/")
        self.assertEqual(content_type, "text/html; charset=utf-8")
        self.assertIn(b"/app.js", index)
        self.assertEqual(api.static_file("/app.js")[1], "text/javascript; charset=utf-8")
        self.assertEqual(api.static_file("/report/abc?x=1")[0], index)

    def test_paths_outside_web_root_never_escape(self):
        index = api.static_file("/")[0]
        for path in ("/../local_secrets.py", "/../../etc/passwd", "/..%2f..%2fagent2/api.py", "/../agent2/api.py"):
            with self.subTest(path=path):
                self.assertEqual(api.static_file(path)[0], index)

    def test_static_pages_forbid_inline_script(self):
        policy = api.STATIC_HEADERS["Content-Security-Policy"]
        self.assertIn("script-src 'self'", policy)
        self.assertNotIn("unsafe-inline", policy)


if __name__ == "__main__":
    unittest.main()
