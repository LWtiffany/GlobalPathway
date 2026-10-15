"""Agent1 发布面：证据入库、封存修订、发布/撤回、扫描后复核、Agent2 只读接口。"""
import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from agent1 import formal_extract, formal_pipeline, gate, incremental, publication, storage
from agent1.environment import paths_for
from agent1.models import VisaPatch

INTRO = "This official page explains student visa requirements in detail. " + "General guidance. " * 20
CAS = "You must provide a Confirmation of Acceptance for Studies (CAS) from your course provider."
TB_MENTION = "You may need tuberculosis test results if you are from a listed country."
TB_ITEMS = ("you're coming to the UK for 6 months or more",
            "you've lived in a listed country for 6 months or more")


def page(amount):
    return (f"# Student visa\n{INTRO}\n## London costs\nYou need GBP {amount} per month for courses in London.\n"
            f"## Documents\n{CAS}\n{TB_MENTION}\n### TB test\nYou need a TB test if:\n\n"
            f"- {TB_ITEMS[0]}\n- {TB_ITEMS[1]}\n")


def extraction(section):
    if section.heading == "London costs":
        amount = 1100 if "1100" in section.text else 1000
        quote = f"You need GBP {amount} per month for courses in London."
        return VisaPatch.model_validate({"policies": [{
            "policy": {"funds_living_monthly": amount, "funds_living_currency": "GBP",
                       "applies_to_city": "London"},
            "evidence": [{"field": f, "evidence_quote": quote}
                         for f in ("funds_living_monthly", "funds_living_currency", "applies_to_city")]}]})
    if section.heading == "Documents":
        return VisaPatch.model_validate({"policies": [], "documents": [
            {"code": "cas", "name_zh": "录取确认函（CAS）", "required": True, "evidence_quote": CAS},
            {"code": "tuberculosis_test_certificate", "name_zh": "肺结核检测证明", "required": True,
             "evidence_quote": TB_MENTION}]})
    if section.heading == "TB test":
        return VisaPatch.model_validate({"policies": [], "documents": [
            {"code": "tuberculosis_test_certificate", "name_zh": "肺结核检测证明", "required": True,
             "condition": {"stay_6_months_or_more": True, "lived_in_listed_country": True},
             "condition_quote": "; ".join(TB_ITEMS), "evidence_quote": "You need a TB test if:"}]})
    return VisaPatch(policies=[], documents=[])


class GateConditionTests(unittest.TestCase):
    def test_semicolon_joined_list_items_are_checked_one_by_one(self):
        markdown = page(1000)
        self.assertTrue(gate.condition_quote_in_markdown("; ".join(TB_ITEMS), markdown))
        self.assertFalse(gate.condition_quote_in_markdown(
            "you're coming to the UK for 6 months or more; you are a student from Mars", markdown))

    def test_unverifiable_condition_makes_the_document_unknown_not_unconditional(self):
        doc = {"code": "tuberculosis_test_certificate", "name_zh": "肺结核检测证明", "required": True,
               "condition": {"x": True}, "condition_quote": "a condition that is not on the page",
               "evidence_quote": "You need a TB test if:"}
        patch_ = VisaPatch.model_validate({"policies": [], "documents": [doc]})
        report = gate.verify_visa_evidence(patch_, page(1000))
        clean, _ = formal_pipeline.split_blocked(patch_, report)
        self.assertEqual(len(clean.documents), 1)
        self.assertIsNone(clean.documents[0].required)
        self.assertIsNone(clean.documents[0].condition)

    def test_mention_with_if_is_unknown_and_a_stated_condition_wins_the_merge(self):
        mention = formal_extract.mark_unstated_conditions(extraction(SimpleNamespace(heading="Documents")))
        tb = [d for d in mention.documents if d.code == "tuberculosis_test_certificate"][0]
        self.assertIsNone(tb.required)
        stated = extraction(SimpleNamespace(heading="TB test"))
        for order in ((mention, stated), (stated, mention)):
            merged = formal_extract.merge_sections(list(order), conflicts=[], region="UK")
            tb = [d for d in merged.documents if d.code == "tuberculosis_test_certificate"][0]
            self.assertIs(tb.required, True)
            self.assertEqual(set(tb.condition), {"stay_6_months_or_more", "lived_in_listed_country"})


class FootnoteAndBracketTests(unittest.TestCase):
    TABLE = ("## Checklist\n| Documents |\n| --- |\n| Copy of the sponsor's Hong Kong identity card #  |\n"
             "# Submission of the documents is not required if the sponsor is the educational institution\n"
             "| Proof of the applicant's financial support [if the sponsor is the educational institution] |\n"
             "| Copy of the applicant's valid travel document and details of any re-entry visa (if applicable) |\n"
             "\n### Next steps\nApply online.\n")

    def test_single_hash_line_glued_to_a_table_is_a_footnote_not_a_heading(self):
        sections = incremental.split_sections(self.TABLE)
        self.assertEqual([s.heading for s in sections], ["Checklist", "Next steps"])
        self.assertIn("# Submission of the documents", sections[0].text)
        glued = incremental.split_sections("| a |\n| --- |\n## Real heading\ntext\n")
        self.assertEqual([s.heading for s in glued], ["preamble", "Real heading"])

    def test_quote_that_cuts_off_a_bracketed_condition_is_unstated(self):
        self.assertTrue(gate.omits_bracketed_condition("Proof of the applicant's financial support", self.TABLE))
        self.assertFalse(gate.omits_bracketed_condition(
            "Proof of the applicant's financial support [if the sponsor is the educational institution]",
            self.TABLE))
        self.assertFalse(gate.omits_bracketed_condition("Copy of the applicant's valid travel document",
                                                        self.TABLE))
        cut = VisaPatch.model_validate({"policies": [], "documents": [
            {"code": "proof_of_funds", "name_zh": "资金证明", "required": True,
             "evidence_quote": "Proof of the applicant's financial support"}]})
        self.assertIsNone(formal_extract.mark_unstated_conditions(cut, self.TABLE).documents[0].required)
        self.assertTrue(formal_extract.mark_unstated_conditions(cut).documents[0].required)


class PublicationFlowTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(prefix="agent1-publication-")
        self.addCleanup(directory.cleanup)
        self.paths = paths_for(Path(directory.name), "development")
        storage.initialize(self.paths)
        self.source = {
            "id": "official_visa", "name": "Official visa", "url": "https://example.org/visa",
            "allowed_host": "example.org", "region": "UK", "category": "visa",
            "authority": "official", "review_status": "approved", "enabled": True,
            "owner": "reviewer", "access_basis": "public", "schema_type": "visa_policy",
            "check_interval_minutes": 1, "freshness_sla_hours": 168, "dynamic": False}
        self.sources = {"official_visa": self.source}
        self.source_file = Path(directory.name) / "sources.json"
        self.source_file.write_text(json.dumps([self.source]), encoding="utf-8")
        self.markdown = page(1000)
        settings = {"model": "offline", "base_url": "https://example.invalid/v1", "extra_body": None,
                    "api_key": "x", "timeout": 1, "max_retries": 0, "excerpt_chars": 2500}

        async def fake_crawl(_source):
            return SimpleNamespace(success=True, url="https://example.org/visa",
                                   html="<main>x</main>", markdown=self.markdown)

        self.probe = incremental.Probe("CHANGED", etag='"1"')
        for target in (
                patch("agent1.config.SOURCES_PATH", self.source_file),
                patch("agent1.formal_pipeline.config.llm_settings", return_value=settings),
                patch("agent1.formal_extract.legacy_extract._client", return_value=(object(), settings)),
                patch("agent1.formal_pipeline.crawl.fetch_page", side_effect=fake_crawl),
                patch("agent1.formal_pipeline.formal_extract.extract_section",
                      side_effect=lambda section, *a: (extraction(section), (1, 1))),
                patch("agent1.formal_pipeline.incremental.conditional_probe",
                      side_effect=lambda *a: self.probe)):
            target.start()
            self.addCleanup(target.stop)

    def scan(self):
        state_path = self.paths.data / "check_state.json"
        if state_path.is_file():
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["official_visa"]["next_check_at"] = "2000-01-01T00:00:00+00:00"
            state_path.write_text(json.dumps(state), encoding="utf-8")
        return formal_pipeline.scan("official_visa", paths=self.paths)

    def publish(self, **kwargs):
        return publication.publish("UK", actor="reviewer", paths=self.paths, sources=self.sources,
                                   **kwargs)

    def test_scan_records_traceable_evidence_and_tb_stays_conditional(self):
        self.scan()
        with storage.session(self.paths) as connection:
            evidence = [dict(r) for r in connection.execute("SELECT * FROM POLICY_EVIDENCE")]
            tb = dict(connection.execute("""SELECT pd.required, pd."condition" FROM POLICY_DOCUMENT pd
                JOIN DOCUMENT d USING (doc_id) WHERE d.code='tuberculosis_test_certificate'""").fetchone())
        self.assertTrue(evidence)
        self.assertTrue(all(e["source_id"] == "official_visa" and e["snapshot_id"].startswith("snap-")
                            for e in evidence))
        self.assertEqual({e["target"] for e in evidence if e["target"].startswith("field:")},
                         {"field:funds_living_monthly", "field:funds_living_currency",
                          "field:applies_to_city", "field:cas_required"})
        self.assertEqual(tb["required"], 1)
        self.assertIn("lived_in_listed_country", tb["condition"])
        self.assertTrue(any(e["kind"] == "condition" for e in evidence))

    def test_publish_read_withdraw_republish_and_integrity(self):
        self.scan()
        preview = publication.preview("UK", paths=self.paths, sources=self.sources)[0]
        self.assertEqual(preview["problems"], [])
        self.assertIn("tuberculosis_test_certificate 肺结核检测证明：条件 ", "\n".join(preview["documents"]))

        first = self.publish(reason="首次发布")[0]
        self.assertEqual(first["result"], "published")
        records = publication.search("UK", paths=self.paths)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["availability"], "available")
        self.assertFalse(record["synthetic"])
        self.assertEqual(record["payload"]["fields"]["funds_living_monthly"], 1000)
        self.assertTrue(all(ev["source_url"] == "https://example.org/visa" and ev["quote"]
                            for ev in record["evidence"]))

        with storage.session(self.paths, readonly=False) as connection, \
                self.assertRaises(sqlite3.IntegrityError):
            connection.execute("UPDATE POLICY_REVISION SET payload_json='{}'")

        self.assertEqual(self.publish()[0]["result"], "unchanged")

        publication.withdraw(record["record_id"], actor="reviewer", reason="官方页面更正",
                             paths=self.paths)
        self.assertEqual(publication.search("UK", paths=self.paths), [])
        withdrawn = publication.get(record["record_id"], record["revision_id"], paths=self.paths)
        self.assertEqual(withdrawn["availability"], "withdrawn")

        second = self.publish()[0]
        self.assertEqual((second["result"], second["revision_no"]), ("published", 2))
        old = publication.get(record["record_id"], record["revision_id"], paths=self.paths)
        self.assertEqual(old["availability"], "superseded")

        later = datetime.now(timezone.utc) + timedelta(hours=169)
        self.assertEqual(publication.search("UK", paths=self.paths, now=later)[0]["availability"], "stale")

        snapshot = next((self.paths.snapshots / "official_visa").glob("*.md"))
        snapshot.write_text(snapshot.read_text(encoding="utf-8") + "\ntampered", encoding="utf-8")
        self.assertEqual(publication.search("UK", paths=self.paths)[0]["availability"], "missing_evidence")

    def test_open_warning_needs_acknowledgement_and_open_review_blocks(self):
        self.scan()
        with storage.session(self.paths, readonly=False) as connection:
            report = gate.GateReport()
            report.problem("applicability", "warning", "币种由货币符号推断", "核对",
                           field="funds_living_currency")
            formal_pipeline._persist_gate_issues(connection, self.source, None, report)
        refused = self.publish()[0]
        self.assertEqual(refused["result"], "refused")
        self.assertIn("--ack-warnings", refused["problems"][-1])
        self.assertEqual(self.publish(ack_warnings=True)[0]["result"], "published")

        self.markdown = page(1100)
        result = self.scan()
        self.assertEqual(result["policies"][0]["result"], "review")
        self.assertEqual(result["publications"][0]["result"], "not_reverified")
        refused = self.publish(ack_warnings=True)[0]
        self.assertTrue(any("审核项" in p for p in refused["problems"]))

    def open_reviews(self):
        with storage.session(self.paths) as connection:
            return [dict(r) for r in connection.execute(
                "SELECT * FROM admin.REVIEW_QUEUE")]

    def rescan_without_cas(self, cas_quote=None):
        """Re-extract every section; CAS is omitted (or given an unverifiable quote)."""
        original = extraction

        def variant(section):
            patch_ = original(section)
            documents = [d for d in patch_.documents if d.code != "cas"]
            if cas_quote and section.heading == "Documents":
                documents.append(patch_.documents[0].model_copy(update={"evidence_quote": cas_quote}))
            return patch_.model_copy(update={"documents": documents})

        with patch(f"{__name__}.extraction", side_effect=variant):
            return formal_pipeline.scan("official_visa", paths=self.paths, full=True)

    def test_vanished_document_is_removed_only_after_review(self):
        self.scan()
        self.publish()
        self.markdown = page(1000).replace(CAS + "\n", "")
        result = self.rescan_without_cas()
        self.assertEqual(result["policies"][0]["result"], "review")
        review = self.open_reviews()[0]
        removed = json.loads(review["proposed_value"])["removed_documents"]
        self.assertEqual([(d["code"], d["still_quoted"]) for d in removed], [("cas", False)])
        self.assertIn("页面不再列出的材料", review["reason"])
        self.assertTrue(any("审核项" in p for p in self.publish()[0]["problems"]))

        with storage.session(self.paths, readonly=False) as connection:
            formal_pipeline.apply_review(connection, review["review_id"], decided_by="reviewer")
            left = {r[0] for r in connection.execute(
                "SELECT d.code FROM POLICY_DOCUMENT JOIN DOCUMENT d USING (doc_id)")}
            orphan = connection.execute("""SELECT COUNT(*) FROM POLICY_EVIDENCE e WHERE e.target LIKE 'document:%'
                AND substr(e.target, 10) NOT IN (SELECT doc_id FROM POLICY_DOCUMENT)""").fetchone()[0]
        self.assertNotIn("cas", left)
        self.assertEqual(orphan, 0)
        republished = self.publish(ack_warnings=True)[0]
        self.assertEqual((republished["result"], republished["revision_no"]), ("published", 2))
        codes = {d["code"] for d in publication.search("UK", paths=self.paths)[0]["payload"]["documents"]}
        self.assertNotIn("cas", codes)

    def test_missed_extraction_is_flagged_and_unverifiable_quote_keeps_the_link(self):
        self.scan()
        self.rescan_without_cas(cas_quote="You must provide a CAS letter signed in blue ink by the dean.")
        self.assertFalse(any(json.loads(r["proposed_value"]).get("removed_documents")
                             for r in self.open_reviews()))

        self.rescan_without_cas()
        review = self.open_reviews()[0]
        removed = json.loads(review["proposed_value"])["removed_documents"]
        self.assertEqual([(d["code"], d["still_quoted"]) for d in removed], [("cas", True)])
        self.assertIn("请先核对是否漏抽", review["reason"])

    def test_unchanged_scan_refreshes_verified_at(self):
        self.scan()
        published = self.publish()[0]
        self.probe = incremental.Probe("NOT_MODIFIED", etag='"1"')
        result = self.scan()
        self.assertEqual(result["publications"][0]["result"], "reverified")
        self.assertGreater(result["publications"][0]["verified_at"], published["verified_at"])
        with storage.session(self.paths) as connection:
            actions = [r[0] for r in connection.execute(
                "SELECT action FROM PUBLICATION_EVENT ORDER BY at")]
        self.assertEqual(actions, ["PUBLISH", "REVERIFY"])


if __name__ == "__main__":
    unittest.main()
