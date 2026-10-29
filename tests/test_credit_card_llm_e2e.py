"""信用卡「追问 → 推荐 → LLM 改写」端到端测试（依赖真实 LLM）。

只在 LLM 已配置时运行（AGENT2_LLM_API_KEY + AGENT2_LLM_BASE_URL，见 local_secrets.py）。
无 LLM 时自动跳过，不影响 CI 的确定性规则测试。

运行:
    .venv/Scripts/python.exe -m unittest tests.test_credit_card_llm_e2e -v
"""
import unittest
from datetime import datetime, timezone

from agent2.knowledge import FixtureKnowledgeRead
from agent2.llm.factory import build_llm_components
from agent2.orchestration.domains.credit_card_repository import RdsCreditCardRepository
from agent2.service import Agent2Service
from agent2.store import Store

NOW = datetime.now(timezone.utc)


def _llm_available():
    try:
        return build_llm_components().understanding is not None
    except Exception:
        return False


@unittest.skipUnless(_llm_available(), "LLM 未配置（缺 AGENT2_LLM_API_KEY/BASE_URL），跳过 LLM 端到端测试")
class CreditCardLlmEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()  # 内存，进程结束即销毁
        self.addCleanup(self.store.close)
        self.service = Agent2Service(
            self.store, FixtureKnowledgeRead.from_file(), clock=lambda: NOW,
            credit_card_repository=RdsCreditCardRepository(),  # 只读 RDS
            llm_components=build_llm_components())             # 启用 LLM 两个端口
        self.user = self.service.demo_login("llm-e2e")["user_id"]
        profile = self.service.create_profile(self.user, synthetic=True)
        self.conversation = self.service.create_conversation(
            self.user, profile.profile_id)["conversation_id"]

    def test_free_text_understanding_records_llm_model(self):
        """LLM 理解自由文本：抽取 facts，只补问年龄，且记录 qwen 模型版本。"""
        first = self.service.post_message(
            self.user, self.conversation,
            "我在香港想办信用卡，年薪30万，每月全额还款，主要网购和旅行", "m1")

        self.assertEqual("needs_input", first.status)
        self.assertEqual(["applicant_age_band"],
                         [q.field for q in first.follow_up_questions])
        run = self.store.one(
            "SELECT model_version, prompt_version FROM answer_runs WHERE message_id=?",
            (first.message_id,))
        self.assertIn("qwen", run["model_version"])
        self.assertIn("understand-user-v3", run["prompt_version"])

    def test_full_chain_follow_up_then_llm_written_recommendation(self):
        """追问 → 纯规则推荐 → LLM 改写，整条链路跑通。"""
        first = self.service.post_message(
            self.user, self.conversation,
            "我在香港想办信用卡，年薪30万，每月全额还款，主要网购和旅行", "m1")
        self.assertEqual("needs_input", first.status)

        residency = self.service.post_message(
            self.user, self.conversation, "18 岁或以上", "m2",
            confirmed_fields={"applicant_age_band": "18_or_over"}, expected_profile_version=1)
        self.assertEqual("needs_input", residency.status)
        self.assertEqual(["hk_resident"], [q.field for q in residency.follow_up_questions])

        premier = self.service.post_message(
            self.user, self.conversation, "是，我是香港居民", "m3",
            confirmed_fields={"hk_resident": True}, expected_profile_version=2)
        self.assertEqual(["hsbc_premier_path"], [q.field for q in premier.follow_up_questions])

        value_inputs = self.service.post_message(
            self.user, self.conversation, "不考虑 Premier", "m4",
            confirmed_fields={"hsbc_premier_path": "not_applicable"},
            expected_profile_version=3)
        self.assertEqual(["monthly_card_spend_band_hkd"],
                         [q.field for q in value_inputs.follow_up_questions])

        spend = self.service.post_message(
            self.user, self.conversation, "每月五千至一万", "m5",
            confirmed_fields={"monthly_card_spend_band_hkd": "5k_10k"},
            expected_profile_version=4)
        self.assertEqual(["reward_preference"], [q.field for q in spend.follow_up_questions])

        final = self.service.post_message(
            self.user, self.conversation, "按预计价值比较", "m6",
            confirmed_fields={"reward_preference": "no_preference"},
            expected_profile_version=5)

        self.assertIn(final.status, ("answered", "needs_verification"))
        self.assertIn("主推荐", final.answer.summary)

        run = self.store.one(
            "SELECT model_version, prompt_version FROM answer_runs WHERE message_id=?",
            (final.message_id,))
        if "llm_fallback" in [n.code for n in final.notices]:
            # 改写失败时：必须正确回退，答案不丢
            self.assertTrue(final.answer.steps)
            self.assertNotIn("write-answer-v1", run["prompt_version"])
        else:
            # 改写成功时：writer 版本已记录
            self.assertIn("write-answer-v1", run["prompt_version"])
            self.assertIn("qwen", run["model_version"])


if __name__ == "__main__":
    unittest.main()
