"""前端支撑接口：档案列表、每档一会话、四类建议、纠错反馈、Plan 待办、字段目录、静态页面。"""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

from agent2 import api
from tests.test_agent2_s1 import UK_STUDENT, UK_STUDENT_READY, S1Base


class WebApiCases(S1Base):
    def setUp(self):
        self.service = self.make()
        self.token = self.service.demo_login("alice")["token"]
        self.bob = self.service.demo_login("bob")["token"]

    def call(self, method, path, body=None, token=None):
        raw = json.dumps(body).encode() if body is not None else b""
        status, payload, _headers = api.dispatch(
            self.service, method, path, {"Authorization": f"Bearer {token or self.token}"}, raw)
        return status, payload

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
        profile, _, answer = self.ask("需要准备哪些签证材料", UK_STUDENT_READY)
        status, payload = self.call("GET", f"/v1/profiles/{profile.profile_id}/advice")
        self.assertEqual(status, 200)
        self.assertEqual(list(payload["advice"]), ["visa"])
        cited = {c["citation_id"] for c in payload["advice"]["visa"]["payload"]["citations"]}
        self.assertEqual(cited, {c.citation_id for c in answer.citations})
        self.assertEqual(self.call("GET", f"/v1/profiles/{profile.profile_id}/advice", token=self.bob)[0], 404)

    def test_wrong_feedback_opens_numbered_ticket_without_touching_profile(self):
        profile, _, answer = self.ask("需要准备哪些签证材料", UK_STUDENT_READY)
        path = f"/v1/messages/{answer.message_id}/feedback"
        self.assertEqual(self.call("POST", path, {"kind": "helpful"})[0], 400)
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
        status, payload, _ = api.dispatch(self.service, "GET", "/v1/fields", {}, b"")
        self.assertEqual(status, 200)
        visa = payload["fields"]["visa_category"]
        self.assertEqual(visa["kind"], "enum")
        self.assertIn({"value": "student_visa", "label": "学生签证"}, visa["options"])
        self.assertEqual(payload["fields"]["planned_start_date"]["kind"], "date")
        self.assertEqual([o["value"] for o in payload["fields"]["stay_over_6_months"]["options"]], [True, False])
        self.assertEqual(payload["fields"]["beneficiary"]["label"], "服务对象")
        self.assertEqual(payload["fields"]["account_region"]["storage_class"], "session_only")
        self.assertEqual(payload["fields"]["destination_country"]["confirmation_mode"], "proposal")


class StaticFileCases(unittest.TestCase):
    def test_index_assets_and_spa_fallback(self):
        index, content_type = api.static_file("/")
        self.assertEqual(content_type, "text/html; charset=utf-8")
        self.assertIn(b"/app.js", index)
        self.assertEqual(api.static_file("/app.js")[1], "text/javascript; charset=utf-8")
        self.assertEqual(api.static_file("/report/abc?x=1")[0], index)

    def test_follow_up_choices_use_atomic_message_profile_update(self):
        source = api.static_file("/app.js")[0].decode("utf-8")
        answer_field = source.split("async function answerField", 1)[1].split("function proposalValue", 1)[0]
        self.assertNotIn('api("PATCH"', answer_field)
        self.assertIn("await send", answer_field)
        send = source.split("async function send", 1)[1]
        self.assertIn("confirmed_fields", send)
        self.assertIn("expected_profile_version", send)

    def test_credit_card_plan_reads_saved_advice(self):
        source = api.static_file("/app.js")[0].decode("utf-8")
        self.assertIn('if (t.key === "credit_card") return creditCardCard(t);', source)
        card = source.split("function creditCardCard(t)", 1)[1].split("function sourceChips", 1)[0]
        self.assertIn("S.advice.credit_card", card)
        self.assertIn("savedCardDisplay(a)", card)
        self.assertIn("application_note", card)

    @unittest.skipUnless(shutil.which("node"), "需要 Node.js 验证前端历史建议兼容逻辑")
    def test_credit_card_plan_recovers_names_and_fees_from_matching_answer(self):
        app_path = Path(__file__).resolve().parents[1] / "agent2" / "web" / "app.js"
        script = r"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const helpers = source.slice(source.indexOf('function cardsFromAnswerSteps('),
  source.indexOf('function buildCiteMap('));
const context = { S: { messages: [
  { role: 'assistant', message_id: 'unrelated', body: { answer: { steps: ['主推荐：其他卡', '年费：1 美元'] } } },
  { role: 'assistant', message_id: 'saved-answer', body: { answer: { steps: [
    '汇丰美国发行的卡', '申请须有美国汇丰卓越理财支票账户。',
    '主推荐：HSBC Elite World Elite Mastercard', '为什么适合你：适合旅行消费。', '年费：495 美元',
    '其他选择：HSBC Premier World Mastercard', '推荐理由：年费较低。', '年费：95 美元'
  ] } } }
] , advice: {}, flash: [] },
  h: (_tag, _attrs, ...kids) => ({ kids }),
  cardHead: (topic, status) => ({ kids: [topic.title, status] }),
  fmtClock: () => '20:36' };
vm.runInNewContext(helpers, context);
const advice = { source_message_id: 'saved-answer', payload: { summary: '旧建议', market: 'US' },
  updated_at: '2026-10-26T20:36:00' };
const result = context.savedCardDisplay(advice);
const flatten = (node) => typeof node === 'string' ? node : !node ? '' :
  Array.isArray(node) ? node.map(flatten).join(' ') : flatten(node.kids || []);
context.S.advice.credit_card = advice;
const rendered = flatten(context.creditCardCard({ key: 'credit_card', title: '信用卡建议' }));
advice.payload.preview = true;
const preview = flatten(context.creditCardCard({ key: 'credit_card', title: '信用卡建议' }));
advice.payload.preview = false;
advice.payload.cards = result.cards.map((card) => ({ ...card, role: 'candidate' }));
const comparison = flatten(context.creditCardCard({ key: 'credit_card', title: '信用卡建议' }));
process.stdout.write(JSON.stringify({ result, rendered, preview, comparison }));
"""
        result = subprocess.run(["node", "-e", script, str(app_path)], check=True,
                                capture_output=True, text=True, encoding="utf-8")
        output = json.loads(result.stdout)
        cards = output["result"]["cards"]
        self.assertEqual(["HSBC Elite World Elite Mastercard", "HSBC Premier World Mastercard"],
                         [card["product_name"] for card in cards])
        self.assertEqual(["495 美元", "95 美元"], [card["fee_text"] for card in cards])
        self.assertEqual("申请须有美国汇丰卓越理财支票账户。", cards[0]["application_note"])
        self.assertIn("首选推荐", output["rendered"])
        self.assertIn("适合旅行消费。", output["rendered"])
        self.assertIn("年费较低。", output["rendered"])
        self.assertIn("95 美元", output["rendered"])
        self.assertIn("申请前请确认", output["rendered"])
        self.assertIn("已推荐", output["preview"])
        self.assertIn("首选推荐", output["preview"])
        self.assertNotIn("提前了解", output["preview"])
        self.assertNotIn("优先美国", output["rendered"])
        self.assertNotIn("可以先比较产品权益和费用", output["rendered"])
        self.assertIn("供您比较", output["comparison"])
        self.assertNotIn("已推荐", output["comparison"])

    @unittest.skipUnless(shutil.which("node"), "需要 Node.js 验证开户建议卡数据映射")
    def test_account_plan_reads_product_documents_and_rules_from_candidate_result(self):
        app_path = Path(__file__).resolve().parents[1] / "agent2" / "web" / "app.js"
        script = r"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const helpers = source.slice(source.indexOf('function accountDocumentLabel('),
  source.indexOf('function accountCard('));
const context = {};
vm.createContext(context);
vm.runInContext(helpers, context);
const display = context.accountAdviceDisplay({ payload: { result_status: 'answered', domain_payload: {
  account_region: 'UK',
  conversation_state: { focused_product_id: 'hsbc_uk_bank', unresolved_fields: ['applicant_age'] },
  candidate_results: [{ status: 'needs_input',
    product: { account_id: 'hsbc_uk_bank', product_name: 'HSBC Bank Account' },
    rule_results: [
      { rule: { field_code: 'applicant_age', operator: 'gte', value_numeric: '18' } },
      { rule: { field_code: 'bank_confirmation', operator: 'note' } }
    ],
    documents: [
      { document_type: 'identity', requirement_level: 'required' },
      { document_type: 'address', requirement_level: 'required' },
      { document_type: 'identity', requirement_level: 'required' }
    ]
  }]
} } });
process.stdout.write(JSON.stringify(display));
"""
        result = subprocess.run(["node", "-e", script, str(app_path)], check=True,
                                capture_output=True, text=True, encoding="utf-8")
        display = json.loads(result.stdout)
        self.assertEqual("HSBC Bank Account", display["productName"])
        self.assertEqual(["身份证明", "地址证明"], display["documentNames"])
        self.assertEqual(["年龄至少 18 岁", "需通过银行的身份与地址核验"],
                         display["ruleLabels"])
        self.assertTrue(display["needsInput"])

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
