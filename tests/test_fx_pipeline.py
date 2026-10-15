"""Agent1 外汇（留学购汇）管道：门控、合并、FX_POLICY 入库、冲突审核、证据与审核批准。"""
import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from agent1 import formal_pipeline, fx_extract, fx_pipeline, incremental, storage
from agent1.environment import paths_for
from agent1.models import FxPatch

INTRO = "国家外汇管理局发布个人外汇业务政策问答，以下内容供境内个人办理留学购汇时参考。" * 4
QUOTA = "个人结汇和境内个人购汇实行年度总额管理。年度总额分别为每人每年等值 5万美元。"
OVER = "境内个人经常项目项下非经营性购汇超过年度总额的，凭本人有效身份证件和有交易额的相关证明材料在银行办理。"
DAILY = "当日累计等值5万美元以下（含）的，凭本人有效身份证件在银行办理。"
PROXY_SAFE = "境内个人购汇可由其直系亲属代为办理，直系亲属包括父母、子女、配偶。"
PROXY_BRANCH = "近亲属可以在本人年度便利化额度内代为办理购汇，包括兄弟姐妹、祖父母。"
ID_DOC = "年度总额内购汇，凭本人有效身份证件在银行办理。"
TUITION_HEADING = "超过年度总额的留学学费购汇"
ADMISSION = "须提供境外学校出具的录取通知书和学费通知单。"


def safe_page():
    return f"# 个人外汇管理\n{INTRO}\n## 年度总额\n{QUOTA}\n{OVER}\n{DAILY}\n## 代办\n{PROXY_SAFE}\n{ID_DOC}\n" \
           f"## {TUITION_HEADING}\n{ADMISSION}\n"


def branch_page():
    return f"# 分局问答\n{INTRO}\n## 代办\n{PROXY_BRANCH}\n"


def fact(field, value, quote, purpose=None):
    return {"field": field, "value": value, "evidence_quote": quote, "purpose": purpose}


def safe_extraction(section):
    if section.heading == "年度总额":
        return FxPatch.model_validate({"facts": [
            fact("annual_quota_amount", 50000, "年度总额分别为每人每年等值5万美元"),
            fact("annual_quota_currency", "USD", "年度总额分别为每人每年等值5万美元"),
            fact("quota_reset", "calendar_year", "年度总额分别为每人每年等值5万美元"),
            fact("over_quota_allowed", True, OVER),
            fact("annual_quota_amount", 50000, DAILY)]})
    if section.heading == "代办":
        return FxPatch.model_validate({"facts": [fact("remitter_relation", "immediate_family", PROXY_SAFE)],
                                       "documents": [{"code": "id_document", "name_zh": "本人有效身份证件",
                                                      "required": True, "evidence_quote": ID_DOC}]})
    if section.heading == TUITION_HEADING:
        return FxPatch.model_validate({"documents": [
            {"code": "admission_letter", "name_zh": "录取通知书", "required": True, "purpose": "tuition",
             "condition": {"over_quota": True}, "condition_quote": TUITION_HEADING,
             "evidence_quote": ADMISSION}]})
    return FxPatch()


def branch_extraction(section):
    if section.heading == "代办":
        return FxPatch.model_validate({"facts": [fact("remitter_relation", "close_relatives", PROXY_BRANCH)]})
    return FxPatch()


class FxGateTests(unittest.TestCase):
    def check(self, field, value, quote, markdown=None):
        report = fx_extract.gate.GateReport()
        patch_ = FxPatch.model_validate({"facts": [fact(field, value, quote)]})
        clean, _ = fx_extract.verify(patch_, markdown or safe_page(), report=report)
        return clean.facts, report

    def test_chinese_wan_amount_and_page_spacing_are_accepted(self):
        facts, report = self.check("annual_quota_amount", 50000, "年度总额分别为每人每年等值5万美元")
        self.assertEqual(facts[0].value, 50000)
        self.assertTrue(report.ok)

    def test_daily_limit_is_not_an_annual_quota(self):
        facts, report = self.check("annual_quota_amount", 50000, DAILY)
        self.assertEqual(facts, [])
        self.assertIn("年度", report.errors[0]["reason"])

    def test_amount_and_currency_must_appear_in_the_quote(self):
        self.assertEqual(self.check("annual_quota_amount", 60000, "年度总额分别为每人每年等值5万美元")[0], [])
        self.assertEqual(self.check("annual_quota_currency", "EUR", "年度总额分别为每人每年等值5万美元")[0], [])

    def test_quote_missing_from_the_page_is_rejected(self):
        facts, report = self.check("over_quota_allowed", True, "超过年度总额的可以随意购汇，无需任何材料")
        self.assertEqual(facts, [])
        self.assertEqual(report.errors[0]["check"], "evidence")

    def test_invalid_values_are_rejected(self):
        self.assertEqual(self.check("remitter_relation", "friends", PROXY_SAFE)[0], [])
        self.assertEqual(self.check("effective_from", "2007/02/01", QUOTA)[0], [])

    def test_unknown_condition_key_makes_the_document_unknown(self):
        patch_ = FxPatch.model_validate({"documents": [
            {"code": "admission_letter", "name_zh": "录取通知书", "required": True,
             "condition": {"has_scholarship": True}, "condition_quote": TUITION_HEADING,
             "evidence_quote": ADMISSION}]})
        clean, report = fx_extract.verify(patch_, safe_page())
        self.assertIsNone(clean.documents[0].required)
        self.assertIsNone(clean.documents[0].condition)
        self.assertTrue(report.ok)


class FxExtractTests(unittest.TestCase):
    def test_output_cleanup_drops_unknown_fields_and_canonicalizes_names(self):
        cleaned = fx_extract._clean_output(json.dumps({
            "facts": [fact("visa_fee", 1, "x"), fact("quota_reset", "calendar_year", "每人每年", "general")],
            "documents": [{"code": "Admission_Letter", "name_zh": "入学通知书", "evidence_quote": "q"},
                          {"code": "made_up", "name_zh": "保证金证明", "evidence_quote": "q"}]}))
        self.assertEqual([f["field"] for f in cleaned["facts"]], ["quota_reset"])
        self.assertIsNone(cleaned["facts"][0]["purpose"])
        self.assertEqual([(d["code"], d["name_zh"]) for d in cleaned["documents"]],
                         [("admission_letter", "境外学校录取通知书"), ("other_document", "保证金证明")])

    def test_condition_simplification(self):
        self.assertEqual(fx_extract.simplify_condition({"any_of": [
            {"over_quota": True}, {"over_quota": True, "study_year": "first"}]}), {"over_quota": True})
        self.assertIsNone(fx_extract.simplify_condition({"any_of": [
            {"over_quota": True}, {"over_quota": False}, {"by_proxy": True}]}))
        self.assertEqual(fx_extract.simplify_condition({"any_of": [{"over_quota": True}, {"by_proxy": True}]}),
                         {"any_of": [{"over_quota": True}, {"by_proxy": True}]})

    def test_purpose_value_overrides_universal_and_same_level_values_conflict(self):
        patches = [FxPatch.model_validate({"facts": [
            fact("over_quota_allowed", 1, OVER), fact("over_quota_allowed", 0, OVER, "living"),
            fact("remitter_relation", "immediate_family", PROXY_SAFE),
            fact("remitter_relation", "close_relatives", PROXY_BRANCH)]})]
        merged, conflicts, notes = fx_extract.merge(patches)
        self.assertEqual(merged["tuition"]["fields"]["over_quota_allowed"], 1)
        self.assertEqual(merged["living"]["fields"]["over_quota_allowed"], 0)
        self.assertTrue(notes)
        self.assertEqual([c["field"] for c in conflicts], ["remitter_relation"])
        self.assertIn("remitter_relation", merged["tuition"]["conflicted"])


class FxPipelineTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(prefix="agent1-fx-")
        self.addCleanup(directory.cleanup)
        self.paths = paths_for(Path(directory.name), "development")
        storage.initialize(self.paths)
        base = {"category": "fx", "region": "CN", "authority": "official", "review_status": "approved",
                "enabled": True, "owner": "reviewer", "access_basis": "public", "schema_type": "fx_policy",
                "check_interval_minutes": 1, "freshness_sla_hours": 24, "dynamic": False}
        self.sources = [{"id": "safe_rules", "name": "SAFE", "url": "https://safe.example.org/rules",
                         "allowed_host": "safe.example.org", **base},
                        {"id": "branch_qa", "name": "Branch", "url": "https://safe.example.org/qa",
                         "allowed_host": "safe.example.org", **base}]
        source_file = Path(directory.name) / "sources.json"
        source_file.write_text(json.dumps(self.sources), encoding="utf-8")
        self.pages = {"https://safe.example.org/rules": safe_page(),
                      "https://safe.example.org/qa": branch_page()}
        self.extractions = {"safe_rules": safe_extraction, "branch_qa": branch_extraction}
        settings = {"model": "offline", "base_url": "https://example.invalid/v1", "extra_body": None,
                    "api_key": "x", "timeout": 1, "max_retries": 0, "excerpt_chars": 2500}

        async def fake_crawl(source):
            return SimpleNamespace(success=True, url=source["url"], html="<main>x</main>",
                                   markdown=self.pages[source["url"]])

        for target in (
                patch("agent1.config.SOURCES_PATH", source_file),
                patch("agent1.config.llm_settings", return_value=settings),
                patch("agent1.formal_extract.legacy_extract._client", return_value=(object(), settings)),
                patch("agent1.formal_pipeline.crawl.fetch_page", side_effect=fake_crawl),
                patch("agent1.fx_extract.extract_section",
                      side_effect=lambda section, source, *a: (self.extractions[source["id"]](section), (1, 1))),
                patch("agent1.formal_pipeline.incremental.conditional_probe",
                      side_effect=lambda *a: incremental.Probe("CHANGED", etag='"1"'))):
            target.start()
            self.addCleanup(target.stop)

    def db(self):
        connection = sqlite3.connect(self.paths.database)
        connection.row_factory = sqlite3.Row
        connection.execute("ATTACH DATABASE ? AS admin", (str(self.paths.admin_database),))
        self.addCleanup(connection.close)
        return connection

    def test_scan_writes_one_row_per_purpose_with_documents_and_evidence(self):
        outcome = fx_pipeline.scan_all("CN", paths=self.paths)
        self.assertEqual(outcome["result"], "applied")
        db = self.db()
        rows = {r["purpose_code"]: dict(r) for r in db.execute("SELECT * FROM FX_POLICY")}
        self.assertEqual(set(rows), {"tuition", "living"})
        for row in rows.values():
            self.assertEqual((row["annual_quota_amount"], row["annual_quota_currency"]), (50000, "USD"))
            self.assertEqual((row["quota_reset"], row["over_quota_allowed"]), ("calendar_year", 1))
            self.assertIsNone(row["remitter_relation"])  # conflicted -> review
        docs = {(r["purpose_code"], r["code"]): r["condition"] for r in db.execute("""
            SELECT p.purpose_code, d.code, pd."condition" FROM POLICY_DOCUMENT pd
            JOIN DOCUMENT d USING (doc_id) JOIN FX_POLICY p USING (policy_id)
            WHERE pd.policy_domain='fx_policy'""")}
        self.assertEqual(docs, {("tuition", "id_document"): None, ("living", "id_document"): None,
                                ("tuition", "admission_letter"): '{"over_quota": true}'})
        quotes = [r["quote"] for r in db.execute(
            "SELECT quote FROM FX_EVIDENCE WHERE target='field:annual_quota_amount'")]
        self.assertTrue(quotes)
        self.assertNotIn(DAILY, quotes)
        conditions = db.execute("SELECT quote FROM FX_EVIDENCE WHERE kind='condition'").fetchall()
        self.assertEqual([r["quote"] for r in conditions], [TUITION_HEADING])
        self.assertEqual(db.execute("SELECT COUNT(*) FROM POLICY_PUBLICATION").fetchone()[0], 0)

    def test_cross_page_conflict_goes_to_review_and_approval_applies_it(self):
        fx_pipeline.scan_all("CN", paths=self.paths)
        reviews = self.db().execute(
            "SELECT * FROM admin.REVIEW_QUEUE WHERE domain_table='FX_POLICY'").fetchall()
        self.assertEqual(len(reviews), 2)
        self.assertIn("remitter_relation", reviews[0]["reason"])
        issue = self.db().execute(
            "SELECT * FROM GATE_ISSUE WHERE source_id='region:fx:CN' AND status='OPEN'").fetchone()
        self.assertEqual(issue["check_name"], "conflict")
        review = next(r for r in reviews if "tuition" in r["proposed_value"])
        with storage.session(self.paths, readonly=False) as connection:
            result = formal_pipeline.apply_review(connection, review["review_id"], decided_by="reviewer")
        self.assertEqual(result["applied_fields"], ["remitter_relation"])
        db = self.db()
        row = db.execute("SELECT remitter_relation FROM FX_POLICY WHERE purpose_code='tuition'").fetchone()
        self.assertEqual(row["remitter_relation"], "immediate_family")
        evidence = db.execute("SELECT quote FROM FX_EVIDENCE WHERE target='field:remitter_relation'").fetchall()
        self.assertEqual([r["quote"] for r in evidence], [PROXY_SAFE])

    def test_rescan_is_idempotent_and_a_changed_value_is_queued_not_overwritten(self):
        fx_pipeline.scan_all("CN", paths=self.paths)
        again = fx_pipeline.scan_all("CN", paths=self.paths, reapply=True)
        self.assertEqual({p["result"] for p in again["policies"]}, {"review"})
        self.assertEqual(self.db().execute(
            "SELECT COUNT(*) FROM admin.REVIEW_QUEUE WHERE domain_table='FX_POLICY'").fetchone()[0], 2)

        changed_quota = QUOTA.replace("5万", "6万")
        self.pages["https://safe.example.org/rules"] = safe_page().replace(QUOTA, changed_quota)
        original = self.extractions["safe_rules"]

        def raised(section):
            patch_ = original(section)
            return patch_.model_copy(update={"facts": [
                f.model_copy(update={"value": 60000, "evidence_quote": "年度总额分别为每人每年等值6万美元"})
                if f.field == "annual_quota_amount" and "每人每年" in f.evidence_quote else
                f.model_copy(update={"evidence_quote": "年度总额分别为每人每年等值6万美元"})
                if "每人每年" in f.evidence_quote else f
                for f in patch_.facts]})

        self.extractions["safe_rules"] = raised
        fx_pipeline.scan_all("CN", paths=self.paths, full=True)
        db = self.db()
        self.assertEqual(db.execute("SELECT annual_quota_amount FROM FX_POLICY WHERE purpose_code='tuition'")
                         .fetchone()[0], 50000)
        reason = db.execute("""SELECT reason FROM admin.REVIEW_QUEUE WHERE domain_table='FX_POLICY'
            AND proposed_value LIKE '%tuition%'""").fetchone()["reason"]
        self.assertIn("annual_quota_amount: 50000 → 60000.0", reason)

    def test_formal_scan_dispatches_fx_sources(self):
        outcome = formal_pipeline.scan("safe_rules", paths=self.paths)
        self.assertEqual(outcome["source_id"], "safe_rules")
        self.assertEqual(outcome["country"], "CN")
        self.assertEqual(formal_pipeline.register_sources(), 2)


if __name__ == "__main__":
    unittest.main()
