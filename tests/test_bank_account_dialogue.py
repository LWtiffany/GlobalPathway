"""Multi-turn contract tests for the bank-account dialogue state."""

from __future__ import annotations

import copy
import unittest
from datetime import datetime, timezone

from agent2.knowledge import FixtureKnowledgeRead
from agent2.llm.factory import LlmComponents
from agent2.orchestration.domains.bank_account_repository import RdsBankAccountRepository
from agent2.service import Agent2Service, ServiceError
from agent2.store import Store


NOW = datetime(2026, 10, 29, tzinfo=timezone.utc)


class FullUsCatalogRepository:
    """Self-contained six-product catalog mirroring the production US shape."""

    async def read(self, market):
        catalog = await RdsBankAccountRepository().read(market)
        if market != "US":
            return catalog
        base = catalog["products"][0]
        definitions = [
            ("hsbc_us_premier_checking", "HSBC Premier Checking", "checking"),
            ("hsbc_us_global_money", "HSBC Global Money Account", "savings"),
            ("hsbc_us_premier_savings", "HSBC Premier Savings", "savings"),
            ("hsbc_us_premier_relationship_savings", "HSBC Premier Relationship Savings", "savings"),
            ("hsbc_us_new_money_cd", "HSBC New Money CD", "cd"),
            ("hsbc_us_online_cd", "HSBC Online CD", "cd"),
        ]
        products = []
        for account_id, name, account_type in definitions:
            row = copy.deepcopy(base)
            row.update(account_id=account_id, product_name=name, account_type=account_type,
                       customer_segment="premier", summary=f"Official information for {name}.")
            products.append(row)
        catalog["products"] = products
        return catalog


class BankAccountDialogueServiceTests(unittest.TestCase):
    def setUp(self):
        self.store = Store.temporary()
        self.addCleanup(self.store.close)
        self.service = Agent2Service(
            self.store, FixtureKnowledgeRead.from_file(), clock=lambda: NOW,
            bank_account_repository=FullUsCatalogRepository(),
            llm_components=LlmComponents(),
        )
        self.user = self.service.demo_login("bank-dialogue")['user_id']
        self.profile = self.service.create_profile(self.user, synthetic=True)
        self.conversation = self.service.create_conversation(
            self.user, self.profile.profile_id)["conversation_id"]

    def _ask(self, text, key):
        answer = self.service.post_message(self.user, self.conversation, text, key)
        advice = self.service.get_advice(self.user, self.profile.profile_id, "account")
        return answer, advice["payload"]["domain_payload"]

    def test_focus_alternatives_confirmation_and_pronoun_are_stateful(self):
        _first, payload1 = self._ask("美国 Premier Checking 呢", "a1")
        shown1 = set(payload1["conversation_state"]["presented_product_ids"])
        self.assertEqual("focus_product", payload1["dialogue_act"])
        self.assertEqual(1, len(shown1))

        _second, payload2 = self._ask("别的呢", "a2")
        shown2 = set(payload2["conversation_state"]["presented_product_ids"])
        self.assertEqual("request_alternatives", payload2["dialogue_act"])
        self.assertFalse(shown1 & shown2)

        focus, payload3 = self._ask("Premier Checking 呢", "a3")
        candidates3 = payload3["candidate_results"]
        self.assertEqual("focus_product", payload3["dialogue_act"])
        self.assertEqual(["hsbc_us_premier_checking"],
                         [item["product"]["account_id"] for item in candidates3])
        self.assertEqual([], focus.profile_change_proposals)

        _confirmed, payload4 = self._ask("那就这个", "a4")
        state4 = payload4["conversation_state"]
        self.assertEqual("confirm_selection", payload4["dialogue_act"])
        self.assertEqual("hsbc_us_premier_checking", state4["selected_product_id"])

        _process, payload5 = self._ask("这个怎么开", "a5")
        self.assertEqual("ask_process", payload5["dialogue_act"])
        self.assertEqual(["hsbc_us_premier_checking"], [
            item["product"]["account_id"] for item in payload5["candidate_results"]])

    def test_catalog_scope_lists_the_complete_verified_catalog(self):
        self._ask("美国 Premier Checking 呢", "c1")
        _answer, payload = self._ask("只有这俩？", "c2")
        self.assertEqual("ask_catalog_scope", payload["dialogue_act"])
        self.assertEqual(6, payload["catalog_product_count"])
        self.assertEqual(6, len(payload["candidate_results"]))
        self.assertIn("6 个", self.service.get_advice(
            self.user, self.profile.profile_id, "account")["payload"]["summary"])

    def test_pronoun_does_not_silently_choose_between_two_products(self):
        self._ask("美国开户有哪些账户", "p1")
        answer, payload = self._ask("那这个怎么开", "p2")
        self.assertTrue(payload["ambiguous_reference"])
        self.assertIsNone(payload["conversation_state"]["selected_product_id"])
        self.assertIn("不能确定", answer.answer.summary)

    def test_broad_opening_question_leads_with_personalized_shortlist(self):
        # Broad discovery leads with a persona-framed shortlist (3 picks with
        # fit reasons), while the full catalog size is still disclosed.
        answer, payload = self._ask("美国有什么账户我可以开", "full-1")
        self.assertEqual("discover_options", payload["dialogue_act"])
        self.assertEqual(6, len(payload["candidate_results"]))
        self.assertEqual(6, len(payload["conversation_state"]["presented_product_ids"]))
        blob = " ".join([answer.answer.summary, *answer.answer.steps])
        self.assertIn("6 款", blob)
        self.assertIn("HSBC Premier Checking——适合", blob)
        self.assertIn("适合", blob)
        # The supplementary Global Money (needs an existing Premier account as
        # funding source) must not lead the new-customer shortlist.
        self.assertNotIn("2. HSBC Global Money Account", blob)
        self.assertNotIn("有一个适合你的账户", blob)
        self.assertNotIn("。。", blob)

    def test_product_relationship_changes_the_explanation(self):
        answer, payload = self._ask("美国 Global Money 呢", "r1")
        self.assertEqual("focus_product", payload["dialogue_act"])
        self.assertIn("补充多币种账户", " ".join(answer.answer.steps))

    def test_state_is_stored_per_conversation_not_in_profile(self):
        self._ask("美国 Premier Checking 呢", "s1")
        row = self.store.one(
            "SELECT version, state_json FROM domain_conversation_state "
            "WHERE session_id=? AND domain='bank_account'", (self.conversation,))
        self.assertIsNotNone(row)
        self.assertGreaterEqual(row["version"], 1)
        profile = self.service.get_profile(self.user, self.profile.profile_id)
        self.assertIsNone(profile.confirmed("account_product"))

    def test_session_scope_fields_cannot_be_written_through_profile_api(self):
        with self.assertRaises(ServiceError) as caught:
            self.service.patch_profile(
                self.user, self.profile.profile_id, expected_version=1,
                set_fields={"account_region": "US"})
        self.assertEqual("INVALID_INPUT", caught.exception.code)
        self.assertEqual(1, self.service.get_profile(
            self.user, self.profile.profile_id).version)

    def test_market_branches_survive_switching_between_regions(self):
        self._ask("美国 Premier Checking 呢", "market-1")
        self._ask("香港 HSBC One 呢", "market-2")
        _answer, payload = self._ask("回到美国，这个怎么开", "market-3")
        state = payload["conversation_state"]
        self.assertEqual("US", state["active_market"])
        self.assertEqual({"US", "CN-HK"}, set(state["markets"]))
        self.assertEqual("hsbc_us_premier_checking",
                         state["markets"]["US"]["focused_product_id"])
        self.assertEqual("hsbc_hk_one", state["markets"]["CN-HK"]["focused_product_id"])
        self.assertEqual(["hsbc_us_premier_checking"], [
            item["product"]["account_id"] for item in payload["candidate_results"]])
        scoped = self.service.list_advice(self.user, self.profile.profile_id)["scoped_advice"]
        self.assertEqual({"US", "CN-HK"}, set(scoped["account"]))

    def test_explicit_product_choice_creates_confirmable_relationship(self):
        self._ask("美国 Premier Checking 呢", "rel-1")
        answer, _payload = self._ask("那就这个", "rel-2")
        self.assertEqual(1, len(answer.bank_relationship_proposals))
        proposal = answer.bank_relationship_proposals[0]
        self.assertEqual("US", proposal.market)
        self.assertEqual("selected", proposal.proposed_status)
        self.assertEqual([], self.service.list_bank_relationships(
            self.user, self.profile.profile_id)["relationships"])

        confirmed = self.service.confirm_bank_relationship_proposal(
            self.user, proposal.proposal_id, expected_version=self.profile.version,
            proposed_status="open")
        relationship = confirmed["relationship"]
        self.assertEqual("hsbc_us_premier_checking", relationship.product_id)
        self.assertEqual("open", relationship.status)
        self.assertEqual(1, len(self.service.list_bank_relationships(
            self.user, self.profile.profile_id)["relationships"]))

    def test_confirmed_accounts_in_two_markets_are_kept_as_separate_records(self):
        self._ask("美国 Premier Checking 呢", "multi-rel-1")
        us_answer, _ = self._ask("那就这个", "multi-rel-2")
        us_result = self.service.confirm_bank_relationship_proposal(
            self.user, us_answer.bank_relationship_proposals[0].proposal_id,
            expected_version=1)

        self._ask("香港 HSBC One 呢", "multi-rel-3")
        hk_answer, _ = self._ask("那就这个", "multi-rel-4")
        self.service.confirm_bank_relationship_proposal(
            self.user, hk_answer.bank_relationship_proposals[0].proposal_id,
            expected_version=us_result["profile_version"])

        relationships = self.service.list_bank_relationships(
            self.user, self.profile.profile_id)["relationships"]
        self.assertEqual({("US", "hsbc_us_premier_checking"), ("CN-HK", "hsbc_hk_one")},
                         {(item.market, item.product_id) for item in relationships})
        profile = self.service.get_profile(self.user, self.profile.profile_id)
        self.assertIsNone(profile.confirmed("account_region"))
        self.assertIsNone(profile.confirmed("account_product"))

    def test_profile_changes_from_one_message_are_confirmed_atomically(self):
        answer, _ = self._ask("我20岁，有香港身份证，想开香港账户", "batch-1")
        proposals = answer.profile_change_proposals
        self.assertEqual({"applicant_age", "has_hk_identity_card"},
                         {item.field_code for item in proposals})
        self.assertNotIn("destination_country", {item.field_code for item in proposals})

        updated = self.service.confirm_proposals(
            self.user, [item.proposal_id for item in proposals], expected_version=1)
        self.assertEqual(2, updated.version)
        self.assertEqual(20, updated.confirmed("applicant_age"))
        self.assertTrue(updated.confirmed("has_hk_identity_card"))
        self.assertEqual([], self.service.list_proposals(
            self.user, self.profile.profile_id))


if __name__ == "__main__":
    unittest.main()
