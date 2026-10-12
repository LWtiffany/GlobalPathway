"""Offline integration coverage for page checks, section reuse and candidate routing."""
import json
import io
import unittest
from urllib.error import HTTPError
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from agent1 import candidate_compare, formal_pipeline, gate, incremental, storage
from agent1.models import VisaPatch
from agent1.environment import paths_for


class IncrementalRulesTests(unittest.TestCase):
    def test_conditional_request_returns_304_without_body_download(self):
        source = {"url": "https://example.org/visa", "allowed_host": "example.org", "dynamic": False}
        seen = []

        class Robots(io.BytesIO):
            def __init__(self):
                super().__init__(b"User-agent: *\nAllow: /\n")

        class Opener:
            def open(self, req, timeout):
                seen.append(req)
                if req.full_url.endswith("/robots.txt"):
                    return Robots()
                raise HTTPError(req.full_url, 304, "Not Modified", {}, None)

        with patch("agent1.incremental.request.build_opener", return_value=Opener()):
            result = incremental.conditional_probe(
                source, {"etag": '"v1"', "snapshot_id": "snapshot-1"})
        self.assertEqual(result.result, "NOT_MODIFIED")
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[1].get_header("If-none-match"), '"v1"')

    def test_all_characters_covered_by_section_splitting(self):
        markdown = "# Title\n" + "A " * 2400 + "\n## Terms\n" + "B " * 2200
        sections = incremental.split_sections(markdown, limit=1000)
        self.assertEqual(sections[-1].end, len(markdown))
        self.assertEqual(sections[0].start, 0)
        self.assertTrue(all(len(section.text) <= 1000 for section in sections))
        self.assertTrue(all(a.end >= b.start for a, b in zip(sections, sections[1:])))

    def test_only_changed_chapter_reprocesses(self):
        old = incremental.split_sections("# Visa\nSame content.\n## Fee\nGBP 1000 each month.")
        new = incremental.split_sections("# Visa\nSame content.\n## Fee\nGBP 1100 each month.")
        changed, removed = incremental.changed_sections(old, new)
        self.assertEqual(len(changed), 1)
        self.assertEqual(removed, set())

    def test_exception_chapter_invalidates_other_chapters(self):
        old = incremental.split_sections("# Fee\nGBP 1000 each month.\n## Exemptions\nIf you are eligible, waiver applies.")
        new = incremental.split_sections("# Fee\nGBP 1000 each month.\n## Exemptions\nIf you are eligible, a different waiver applies.")
        changed, _ = incremental.changed_sections(old, new)
        self.assertEqual(changed, {section.key for section in new})

    def test_missing_material_cannot_be_auto_deleted(self):
        result = candidate_compare.compare(
            {"funds_living_monthly": 1000, "applies_to_city": "London"},
            [{"code": "bank_statement", "required": True}],
            {"funds_living_monthly": 1000, "applies_to_city": "London"},
            [], complete=True)
        self.assertEqual(result.result, "INCOMPLETE")
        self.assertTrue(any("bank_statement" in item for item in result.issues))

    def test_remark_wording_does_not_create_change(self):
        result = candidate_compare.compare({"funds_living_monthly": 1000, "remark": "Old summary"}, [],
                                           {"funds_living_monthly": 1000, "remark": "New summary"}, [],
                                           complete=True)
        self.assertEqual(result.result, "NO_CHANGE")

    def test_empty_structured_conditions_equal_absent_conditions(self):
        result = candidate_compare.compare({"conditions": [], "funds_living_monthly": 1000}, [],
                                           {"funds_living_monthly": 1000}, [], complete=True)
        self.assertEqual(result.result, "NO_CHANGE")

    def test_numeric_evidence_does_not_match_part_of_a_larger_number(self):
        self.assertFalse(gate.number_in_quote(28, "Funds must be held for 128 days."))
        self.assertTrue(gate.number_in_quote(28, "Funds must be held for 28 days."))

    def test_conditional_document_without_condition_is_flagged(self):
        quote = "If you have a student loan, provide evidence from the lender."
        patch = VisaPatch.model_validate({"policies": [], "documents": [{
            "code": "student_loan_evidence", "name_zh": "贷款证明", "required": True,
            "evidence_quote": quote}]})
        result = gate.run_gate_visa(patch, quote, {}, {"excerpt_chars": len(quote)})
        self.assertTrue(any(p["check"] == "applicability" for p in result.warnings))

    def test_eligibility_in_remark_requires_structured_condition(self):
        result = candidate_compare.compare(None, [],
                                           {"funds_living_monthly": 1000,
                                            "remark": "If you lived here for 12 months, no proof."},
                                           [], complete=True)
        self.assertEqual(result.result, "INCOMPLETE")


class FormalScanTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory(prefix="agent1-incremental-suite-")
        self.addCleanup(self.directory.cleanup)
        self.paths = paths_for(Path(self.directory.name), "development")
        storage.initialize(self.paths)
        source_file = Path(self.directory.name) / "sources.json"
        source_file.write_text(json.dumps([{
            "id": "official_visa", "name": "Official visa", "url": "https://example.org/visa",
            "allowed_host": "example.org", "region": "UK", "category": "visa",
            "authority": "official", "review_status": "approved", "enabled": True,
            "owner": "reviewer", "access_basis": "public", "schema_type": "visa_policy",
            "check_interval_minutes": 1, "freshness_sla_hours": 168,
            "content_selector": "main", "dynamic": False,
        }]), encoding="utf-8")
        formal_pipeline.register_sources(source_file, self.paths)
        self.settings = {"model": "offline-test-model", "base_url": "https://example.invalid/v1",
                         "extra_body": None, "api_key": "not-used", "timeout": 1, "max_retries": 0,
                         "excerpt_chars": 2500}

    def _page(self, amount):
        return ("# Student visa\nThis official page explains student visa money requirements in detail. "
                + "General guidance. " * 20
                + "\n## Course fee\nYou need enough money to pay for your course for one academic year.\n"
                + f"## London costs\nYou need GBP {amount} per month for courses in London.\n")

    def _patch(self, section):
        if section.heading == "Course fee":
            return VisaPatch.model_validate({"policies": [{
                "policy": {"funds_tuition_required": True},
                "evidence": [{"field": "funds_tuition_required", "evidence_quote":
                              "You need enough money to pay for your course for one academic year."}]}]})
        if section.heading == "London costs":
            amount = 1100 if "1100" in section.text else 1000
            return VisaPatch.model_validate({"policies": [{
                "policy": {"funds_living_monthly": amount, "applies_to_city": "London"},
                "evidence": [
                    {"field": "funds_living_monthly", "evidence_quote":
                     f"You need GBP {amount} per month for courses in London."},
                    {"field": "applies_to_city", "evidence_quote":
                     f"You need GBP {amount} per month for courses in London."}]}]})
        return VisaPatch(policies=[], documents=[])

    def test_first_full_then_http_unchanged_then_one_changed_section(self):
        current = {"markdown": self._page(1000)}
        calls = []

        async def fake_crawl(_source):
            return SimpleNamespace(success=True, url="https://example.org/visa",
                                   html="<main>offline fixture</main>", markdown=current["markdown"])

        def fake_extract(section, source, client, settings):
            calls.append(section.key)
            return self._patch(section), (10, 10)

        with patch("agent1.formal_pipeline.config.llm_settings", return_value=self.settings), \
             patch("agent1.formal_extract.legacy_extract._client", return_value=(object(), self.settings)), \
             patch("agent1.formal_pipeline.crawl.fetch_page", side_effect=fake_crawl), \
             patch("agent1.formal_pipeline.formal_extract.extract_section", side_effect=fake_extract), \
             patch("agent1.formal_pipeline.incremental.conditional_probe",
                   return_value=incremental.Probe("NOT_MODIFIED", etag='"stable"')) as probe:
            first = formal_pipeline.scan("official_visa", paths=self.paths)
            self.assertGreaterEqual(first["model_calls"], 3)
            self.assertEqual(first["candidates"][0]["result"], "pending_review")
            with storage.session(self.paths, readonly=False) as connection:
                connection.execute("UPDATE source_check_state SET next_check_at='2000-01-01T00:00:00+00:00'")
            before = len(calls)
            second = formal_pipeline.scan("official_visa", paths=self.paths)
            self.assertEqual(second["result"], "not_modified")
            self.assertEqual(len(calls), before)
            self.assertEqual(probe.call_count, 1)
            current["markdown"] = self._page(1100)
            with storage.session(self.paths, readonly=False) as connection:
                connection.execute("UPDATE source_check_state SET next_check_at='2000-01-01T00:00:00+00:00'")
            with patch("agent1.formal_pipeline.incremental.conditional_probe",
                       return_value=incremental.Probe("CHANGED", etag='"new"')):
                third = formal_pipeline.scan("official_visa", paths=self.paths)
            self.assertEqual(third["model_calls"], 1)
            self.assertGreaterEqual(third["reused_sections"], 2)
            self.assertEqual(third["candidates"][0]["result"], "pending_review")
            fourth = formal_pipeline.scan("official_visa", paths=self.paths, full=True)
            self.assertEqual(fourth["candidates"][0]["result"], "reused_candidate")
            with storage.session(self.paths) as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM source_snapshots").fetchone()[0], 2)
                self.assertEqual(connection.execute("SELECT count(*) FROM change_candidates").fetchone()[0], 2)
                self.assertEqual(connection.execute("SELECT count(*) FROM policy_publication").fetchone()[0], 0)
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main()
