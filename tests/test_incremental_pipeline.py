"""Offline integration coverage for page checks, section reuse and domain routing."""
import io
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError

from agent1 import candidate_compare, formal_extract, formal_pipeline, gate, incremental, storage
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

    def test_numeric_evidence_does_not_match_part_of_a_larger_number(self):
        self.assertFalse(gate.number_in_quote(28, "Funds must be held for 128 days."))
        self.assertTrue(gate.number_in_quote(28, "Funds must be held for 28 days."))

    def test_duration_unit_conversion_is_normalisation_not_an_error(self):
        quote = "You'll usually get a decision within 3 weeks."
        markdown = "Apply from outside the UK. " + quote
        # weeks -> days is deterministic normalisation for duration fields
        self.assertTrue(gate.number_in_quote(21, quote, field="processing_days"))
        self.assertTrue(gate.number_in_quote(56, "within 8 weeks.", field="processing_days"))
        # a non-duration field must not silently accept the conversion
        self.assertFalse(gate.number_in_quote(21, quote, field="visa_fee"))
        patch = VisaPatch.model_validate({"policies": [{
            "policy": {"processing_days": 21, "applies_to_city": "Outside the UK"},
            "evidence": [
                {"field": "processing_days", "evidence_quote": quote},
                {"field": "applies_to_city", "evidence_quote": "Apply from outside the UK."}]}]})
        report = gate.run_gate_visa(patch, markdown, {}, {"excerpt_chars": len(markdown)})
        self.assertEqual([p for p in report.problems if p["severity"] == "error"], [])
        warnings = [p for p in report.problems if p["severity"] == "warning"
                    and p["field"] == "processing_days"]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["scope"], "Outside the UK")

    def test_spelled_out_unit_conversion_is_normalisation(self):
        # 香港入境处实测：引用写 "It normally takes six weeks ..."，取值 42
        quote = ("It normally takes six weeks to process a visa/entry permit "
                 "application for study upon receipt of all the required documents.")
        self.assertTrue(gate.number_in_quote(42, quote, field="processing_days"))
        self.assertTrue(gate.number_in_quote(7, "within a week.", field="processing_days"))
        note = gate.number_conversion_note(42, quote, field="processing_days")
        self.assertIn("six weeks", note)
        # 非时长字段不享受折算
        self.assertFalse(gate.number_in_quote(42, quote, field="visa_fee"))

    def test_multiple_durations_in_one_quote_are_ambiguous(self):
        # 引用含多个时长量级时不予自动折算，交人工判断
        quote = "Money must be held for 28 days, ending within 31 days of applying."
        self.assertFalse(gate.number_in_quote(21, quote, field="processing_days"))
        self.assertIsNone(gate.number_conversion_note(28, quote, field="processing_days"))
        # 字面命中不受影响（28 就在引用里）
        self.assertTrue(gate.number_in_quote(28, quote, field="funds_holding_days"))

    def test_any_supporting_quote_is_enough_when_a_field_has_several(self):
        markdown = ("£776 per year for students. If you're applying from inside the UK, "
                    "£388 for students, their dependants or under-18 applications")
        patch = VisaPatch.model_validate({"policies": [{
            "policy": {"health_surcharge": 776},
            "evidence": [
                {"field": "health_surcharge", "evidence_quote":
                 "£388 for students, their dependants or under-18 applications"},
                {"field": "health_surcharge", "evidence_quote": "£776 per year for students."}]}]})
        report = gate.run_gate_visa(patch, markdown, {}, {"excerpt_chars": len(markdown)})
        self.assertEqual(report.errors, [])
        patch.policies[0].evidence.pop()
        report = gate.run_gate_visa(patch, markdown, {}, {"excerpt_chars": len(markdown)})
        self.assertEqual([p["field"] for p in report.errors], ["health_surcharge"])

    def test_conditional_document_without_condition_is_flagged(self):
        quote = "If you have a student loan, provide evidence from the lender."
        patch = VisaPatch.model_validate({"policies": [], "documents": [{
            "code": "student_loan_evidence", "name_zh": "贷款证明", "required": True,
            "evidence_quote": quote}]})
        result = gate.run_gate_visa(patch, quote, {}, {"excerpt_chars": len(quote)})
        self.assertTrue(any(p["check"] == "applicability" for p in result.warnings))


class FormalScanTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory(prefix="agent1-incremental-suite-")
        self.addCleanup(self.directory.cleanup)
        self.paths = paths_for(Path(self.directory.name), "development")
        storage.initialize(self.paths)
        self.source_file = Path(self.directory.name) / "sources.json"
        self.source_file.write_text(json.dumps([{
            "id": "official_visa", "name": "Official visa", "url": "https://example.org/visa",
            "allowed_host": "example.org", "region": "UK", "category": "visa",
            "authority": "official", "review_status": "approved", "enabled": True,
            "owner": "reviewer", "access_basis": "public", "schema_type": "visa_policy",
            "check_interval_minutes": 1, "freshness_sla_hours": 168,
            "content_selector": "main", "dynamic": False,
        }]), encoding="utf-8")
        self.settings = {"model": "offline-test-model", "base_url": "https://example.invalid/v1",
                        "extra_body": None, "api_key": "not-used", "timeout": 1, "max_retries": 0,
                        "excerpt_chars": 2500}

    def _rewind_due(self):
        state_path = self.paths.data / "check_state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["official_visa"]["next_check_at"] = "2000-01-01T00:00:00+00:00"
        state_path.write_text(json.dumps(state), encoding="utf-8")

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
                "policy": {"funds_living_monthly": amount, "funds_living_currency": "GBP",
                           "applies_to_city": "London"},
                "evidence": [
                    {"field": "funds_living_monthly", "evidence_quote":
                     f"You need GBP {amount} per month for courses in London."},
                    {"field": "funds_living_currency", "evidence_quote":
                     f"You need GBP {amount} per month for courses in London."},
                    {"field": "applies_to_city", "evidence_quote":
                     f"You need GBP {amount} per month for courses in London."}]}]})
        return VisaPatch(policies=[], documents=[])

    def test_first_insert_then_unchanged_then_changed_then_dedup(self):
        current = {"markdown": self._page(1000)}
        calls = []

        async def fake_crawl(_source):
            return SimpleNamespace(success=True, url="https://example.org/visa",
                                   html="<main>offline fixture</main>", markdown=current["markdown"])

        def fake_extract(section, source, client, settings):
            calls.append(section.key)
            return self._patch(section), (10, 10)

        with patch("agent1.config.SOURCES_PATH", self.source_file), \
             patch("agent1.formal_pipeline.config.llm_settings", return_value=self.settings), \
             patch("agent1.formal_extract.legacy_extract._client", return_value=(object(), self.settings)), \
             patch("agent1.formal_pipeline.crawl.fetch_page", side_effect=fake_crawl), \
             patch("agent1.formal_pipeline.formal_extract.extract_section", side_effect=fake_extract), \
             patch("agent1.formal_pipeline.incremental.conditional_probe",
                   return_value=incremental.Probe("NOT_MODIFIED", etag='"stable"')) as probe:
            # 1. cold start: gate passes -> direct INSERT into VISA_POLICY
            first = formal_pipeline.scan("official_visa", paths=self.paths)
            self.assertEqual(first["result"], "applied")
            self.assertGreaterEqual(first["model_calls"], 3)
            london = [p for p in first["policies"] if p["scope"] == "london"][0]
            self.assertEqual(london["result"], "inserted")
            with storage.session(self.paths) as connection:
                row = connection.execute("SELECT funds_living_monthly FROM VISA_POLICY "
                                         "WHERE applies_to_city='London'").fetchone()
                self.assertEqual(row[0], 1000)

            # 2. due rewound, page unchanged -> not_modified, no model calls
            #    (the conditional probe runs on every scan since the ETag fix)
            self._rewind_due()
            before = len(calls)
            second = formal_pipeline.scan("official_visa", paths=self.paths)
            self.assertEqual(second["result"], "not_modified")
            self.assertEqual(len(calls), before)
            self.assertEqual(probe.call_count, 2)

            # 3. page changed 1000 -> 1100: only the changed section re-extracts,
            #    the amount change is queued for human review, not auto-applied
            current["markdown"] = self._page(1100)
            self._rewind_due()
            with patch("agent1.formal_pipeline.incremental.conditional_probe",
                       return_value=incremental.Probe("CHANGED", etag='"new"')):
                third = formal_pipeline.scan("official_visa", paths=self.paths)
            self.assertEqual(third["model_calls"], 1)
            self.assertGreaterEqual(third["reused_sections"], 2)
            london = [p for p in third["policies"] if p["scope"] == "london"][0]
            self.assertEqual(london["result"], "review")
            review_id = london["review_id"]
            with storage.session(self.paths) as connection:
                self.assertEqual(connection.execute(
                    "SELECT funds_living_monthly FROM VISA_POLICY "
                    "WHERE applies_to_city='London'").fetchone()[0], 1000)
                self.assertEqual(connection.execute(
                    "SELECT count(*) FROM admin.REVIEW_QUEUE").fetchone()[0], 1)

            # 4. full re-extract of the same content dedups the open review
            fourth = formal_pipeline.scan("official_visa", paths=self.paths, full=True)
            london = [p for p in fourth["policies"] if p["scope"] == "london"][0]
            self.assertEqual(london["result"], "review")
            self.assertEqual(london["review_id"], review_id)
            with storage.session(self.paths) as connection:
                self.assertEqual(connection.execute(
                    "SELECT count(*) FROM admin.REVIEW_QUEUE").fetchone()[0], 1)

            # 5. approving the review applies the new amount
            with storage.session(self.paths, readonly=False) as connection:
                result = formal_pipeline.apply_review(connection, review_id,
                                                      decided_by="tester", note="test apply")
                self.assertEqual(result["policy_id"], london.get("policy_id") or result["policy_id"])
            with storage.session(self.paths) as connection:
                self.assertEqual(connection.execute(
                    "SELECT funds_living_monthly FROM VISA_POLICY "
                    "WHERE applies_to_city='London'").fetchone()[0], 1100)
                self.assertEqual(connection.execute(
                    "SELECT count(*) FROM admin.REVIEW_QUEUE").fetchone()[0], 0)
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            snapshots = list((self.paths.snapshots / "official_visa").glob("*.md"))
            self.assertEqual(len(snapshots), 3)

    def test_scan_persists_gate_issues_from_real_gate(self):
        page = ("# Student visa\nThis official page explains student visa money "
                "requirements in detail. " + "General guidance. " * 20
                + "\n## London costs\nYou need £1,529 per month for courses in London.\n")

        async def fake_crawl(_source):
            return SimpleNamespace(success=True, url="https://example.org/visa",
                                   html="<main>x</main>", markdown=page)

        def fake_extract(section, source, client, settings):
            if section.heading == "London costs":
                quote = "You need £1,529 per month for courses in London."
                return VisaPatch.model_validate({"policies": [{
                    "policy": {"funds_living_monthly": 1529, "funds_living_currency": "GBP",
                               "applies_to_city": "London"},
                    "evidence": [{"field": f, "evidence_quote": quote}
                                 for f in ("funds_living_monthly", "funds_living_currency",
                                           "applies_to_city")]}]}), (5, 5)
            return VisaPatch(policies=[], documents=[]), (5, 5)

        with patch("agent1.config.SOURCES_PATH", self.source_file), \
             patch("agent1.formal_pipeline.config.llm_settings", return_value=self.settings), \
             patch("agent1.formal_extract.legacy_extract._client", return_value=(object(), self.settings)), \
             patch("agent1.formal_pipeline.crawl.fetch_page", side_effect=fake_crawl), \
             patch("agent1.formal_pipeline.formal_extract.extract_section", side_effect=fake_extract), \
             patch("agent1.formal_pipeline.incremental.conditional_probe",
                   return_value=incremental.Probe("CHANGED", etag='"x"')):
            result = formal_pipeline.scan("official_visa", paths=self.paths)
        self.assertEqual(result["result"], "applied")
        self.assertTrue(result["gate_issue_ids"])
        with storage.session(self.paths) as connection:
            rows = [dict(r) for r in connection.execute("SELECT * FROM GATE_ISSUE")]
        self.assertTrue(any(r["check_name"] == "applicability" and r["severity"] == "warning"
                            and r["field"] == "funds_living_currency" for r in rows))
        self.assertTrue(all(r["source_id"] == "official_visa" for r in rows))

    def test_gate_problems_are_persisted_and_auto_resolved(self):
        report = gate.GateReport()
        report.problem("applicability", "error",
                       "[Outside the UK] 字段 processing_days 的取值未出现在其引用中",
                       "值必须由引用直接支撑", field="processing_days", value="21",
                       quote="within 3 weeks.", scope="Outside the UK")
        report.problem("applicability", "warning", "币种由货币符号推断，引用中无代码原文",
                       "可接受为归一化", field="funds_living_currency", value="GBP")
        with storage.session(self.paths, readonly=False) as connection:
            ids = formal_pipeline._persist_gate_issues(
                connection, {"id": "official_visa"}, "snap-1", report)
        self.assertEqual(len(ids), 2)
        with storage.session(self.paths) as connection:
            rows = [dict(r) for r in connection.execute("SELECT * FROM GATE_ISSUE")]
        self.assertEqual({r["scope"] for r in rows}, {"Outside the UK", None})
        self.assertTrue(all(r["status"] == "OPEN" for r in rows))

        # 2nd run reports only the warning: error auto-resolves, warning is reused
        report2 = gate.GateReport()
        report2.problem("applicability", "warning", "币种由货币符号推断，引用中无代码原文",
                        "可接受为归一化", field="funds_living_currency", value="GBP")
        with storage.session(self.paths, readonly=False) as connection:
            ids2 = formal_pipeline._persist_gate_issues(
                connection, {"id": "official_visa"}, "snap-2", report2)
        self.assertEqual(ids2, [ids[1]])
        with storage.session(self.paths) as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM GATE_ISSUE WHERE status='OPEN'").fetchone()[0], 1)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM GATE_ISSUE WHERE status='RESOLVED'").fetchone()[0], 1)

    def test_review_reason_lists_only_its_own_scope_errors(self):
        page = VisaPatch.model_validate({"policies": [
            {"policy": {"funds_living_monthly": 1529, "applies_to_city": "London"}},
            {"policy": {"funds_living_monthly": 1171, "applies_to_city": "Outside London"}},
        ]})
        london = formal_pipeline._policy_id("UK", "london")
        outside = formal_pipeline._policy_id("UK", "outside_london")
        report = gate.GateReport()
        report.problem("applicability", "error",
                       "[Outside London] 字段 funds_living_monthly 的取值未出现在其引用中",
                       "值必须由引用直接支撑", field="funds_living_monthly", value="1171",
                       scope="Outside London")
        clean, blocked = formal_pipeline.split_blocked(page, report, "official_visa")
        merged = formal_extract.merge_sections([clean], conflicts=[], region="UK")
        with storage.session(self.paths, readonly=False) as connection:
            connection.execute("""INSERT INTO VISA_POLICY
                (policy_id, country, policy_type, title, applies_to_city,
                 funds_living_monthly, funds_living_currency)
                VALUES(?, 'UK', 'student', 't', 'London', 1400, 'GBP')""", (london,))
            results = formal_pipeline._apply_region(connection, "UK", merged, blocked)
        with storage.session(self.paths) as connection:
            reason = connection.execute(
                "SELECT reason FROM admin.REVIEW_QUEUE WHERE record_key=?", (london,)).fetchone()[0]
            outside_review = connection.execute(
                "SELECT action, reason FROM admin.REVIEW_QUEUE WHERE record_key=?", (outside,)).fetchone()
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM VISA_POLICY WHERE policy_id=?", (outside,)).fetchone())
        # the London review must not carry the Outside London error
        self.assertNotIn("Outside London", reason)
        self.assertNotIn("门控", reason)
        self.assertIn("字段变更", reason)
        # the blocked Outside London amount waits for review instead of being written
        self.assertEqual(outside_review["action"], "CREATE")
        self.assertIn("Outside London", outside_review["reason"])
        self.assertEqual({r["scope"]: r["result"] for r in results},
                         {"london": "review", "outside_london": "review"})


class RegionMergeTests(unittest.TestCase):
    """多个页面的事实合并到同一国家的政策行。"""

    def setUp(self):
        directory = TemporaryDirectory(prefix="agent1-region-")
        self.addCleanup(directory.cleanup)
        self.paths = paths_for(Path(directory.name), "development")
        storage.initialize(self.paths)

    def _ev(self, field, quote):
        return {"field": field, "evidence_quote": quote}

    def test_documents_page_sets_cas_required_on_every_city_scope(self):
        money_md = "You need £1,529 per month for courses in London. You need £1,171 per month for courses outside London."
        docs_md = ("When you apply for your Student visa you must provide: a Confirmation of "
                   "Acceptance for Studies (CAS) from your course provider")
        money = VisaPatch.model_validate({"policies": [
            {"policy": {"funds_living_monthly": 1529, "funds_living_currency": "GBP",
                        "applies_to_city": "London"},
             "evidence": [self._ev(f, "You need £1,529 per month for courses in London.")
                          for f in ("funds_living_monthly", "funds_living_currency", "applies_to_city")]},
            {"policy": {"funds_living_monthly": 1171, "funds_living_currency": "GBP",
                        "applies_to_city": "Outside London"},
             "evidence": [self._ev(f, "You need £1,171 per month for courses outside London.")
                          for f in ("funds_living_monthly", "funds_living_currency", "applies_to_city")]},
        ], "documents": [{"code": "cas", "name_zh": "录取确认函（CAS）", "required": False,
                          "evidence_quote": "You need £1,529 per month for courses in London."}]})
        docs = VisaPatch.model_validate({"policies": [], "documents": [
            {"code": "cas", "name_zh": "录取确认函（CAS）", "required": True,
             "evidence_quote": "a Confirmation of Acceptance for Studies (CAS) from your course provider"}]})
        docs = formal_extract.derive_document_flags(docs)
        self.assertTrue(docs.policies[0].policy.cas_required)

        clean = []
        for patch, markdown in ((money, money_md), (docs, docs_md)):
            report = gate.run_gate_visa(patch, markdown, {}, {"excerpt_chars": len(markdown)})
            self.assertEqual([p for p in report.problems if p["severity"] == "error"], [])
            clean.append(formal_pipeline.split_blocked(patch, report)[0])
        merged = formal_extract.merge_sections(clean, conflicts=[], region="UK")
        with storage.session(self.paths, readonly=False) as connection:
            formal_pipeline._apply_region(connection, "UK", merged, [])
        with storage.session(self.paths) as connection:
            rows = {r["applies_to_city"]: dict(r) for r in connection.execute(
                "SELECT * FROM VISA_POLICY")}
            cas_links = connection.execute("""SELECT pd.required FROM POLICY_DOCUMENT pd
                JOIN DOCUMENT d USING(doc_id) WHERE d.code='cas'""").fetchall()
        self.assertEqual(set(rows), {"London", "Outside London"})
        self.assertTrue(all(r["cas_required"] == 1 for r in rows.values()))
        self.assertTrue(all(r["country"] == "UK" for r in rows.values()))
        # "must provide" on one page beats "mentioned" on another
        self.assertEqual([r["required"] for r in cas_links], [1, 1])

    def test_destination_name_scope_is_the_universal_scope(self):
        first = VisaPatch.model_validate({"policies": [
            {"policy": {"processing_days": 42, "applies_to_city": "Hong Kong"}, "evidence": []}]})
        second = VisaPatch.model_validate({"policies": [
            {"policy": {"visa_fee": 330}, "evidence": []}]})
        merged = formal_extract.merge_sections([first, second], conflicts=[], region="CN-HK")
        self.assertEqual(len(merged.policies), 1)
        fields = merged.policies[0].policy.model_dump(exclude_none=True)
        self.assertEqual(fields, {"processing_days": 42, "visa_fee": 330})

    def test_new_page_fills_empty_column_but_changed_value_needs_review(self):
        pid = formal_pipeline._policy_id("US", "all")
        with storage.session(self.paths, readonly=False) as connection:
            connection.execute("""INSERT INTO VISA_POLICY (policy_id, country, policy_type, title,
                visa_fee) VALUES (?, 'US', 'student', 't', 160)""", (pid,))
            merged = VisaPatch.model_validate({"policies": [
                {"policy": {"visa_fee": 185, "i20_required": True, "apply_window_days": 365},
                 "evidence": []}]})
            result = formal_pipeline._apply_region(connection, "US", merged, [])[0]
        self.assertEqual(result["result"], "review")
        self.assertEqual(result["filled"], ["apply_window_days", "i20_required"])
        with storage.session(self.paths) as connection:
            row = dict(connection.execute("SELECT * FROM VISA_POLICY WHERE policy_id=?",
                                          (pid,)).fetchone())
            review = json.loads(connection.execute(
                "SELECT proposed_value FROM admin.REVIEW_QUEUE WHERE record_key=?", (pid,)).fetchone()[0])
        self.assertEqual((row["visa_fee"], row["i20_required"], row["apply_window_days"]), (160, 1, 365))
        self.assertEqual(review["policy"]["visa_fee"], 185)
        self.assertNotIn("i20_required", review["policy"])

    def test_cross_page_conflict_is_blocked_not_last_write_wins(self):
        a = VisaPatch.model_validate({"policies": [{"policy": {"visa_fee": 558}, "evidence": []}]})
        b = VisaPatch.model_validate({"policies": [{"policy": {"visa_fee": 524,
                                                               "processing_days": 21}, "evidence": []}]})
        conflicts = []
        merged = formal_extract.merge_sections([a, b], conflicts=conflicts, region="UK")
        self.assertEqual([c["field"] for c in conflicts], ["visa_fee"])
        with storage.session(self.paths, readonly=False) as connection:
            result = formal_pipeline._apply_region(connection, "UK", merged, [], conflicts)[0]
        with storage.session(self.paths) as connection:
            row = dict(connection.execute("SELECT * FROM VISA_POLICY").fetchone())
        self.assertIsNone(row["visa_fee"])
        self.assertEqual(row["processing_days"], 21)
        self.assertEqual(result["result"], "inserted")
        self.assertIn("review_id", result)


class SectionMergeTests(unittest.TestCase):
    """merge_sections 对兜底文档码与通用/范围冲突的处理。"""

    def _patch(self, policies=None, documents=None):
        return VisaPatch.model_validate({"policies": policies or [], "documents": documents or []})

    def test_same_code_different_names_do_not_conflict(self):
        # other_document 是兜底码：香港页多种材料共用，同码不同名必须全部保留
        first = self._patch(documents=[
            {"code": "other_document", "name_zh": "录取通知书", "required": True,
             "evidence_quote": "A letter of acceptance from the institution"}])
        second = self._patch(documents=[
            {"code": "other_document", "name_zh": "旅行证件副本", "required": True,
             "evidence_quote": "Copy of the applicant's valid travel document"}])
        merged = formal_extract.merge_sections([first, second])
        names = sorted(d.name_zh for d in merged.documents)
        self.assertEqual(names, ["录取通知书", "旅行证件副本"])

    def test_same_code_same_name_conflicting_required_is_error(self):
        first = self._patch(documents=[
            {"code": "other_document", "name_zh": "监护人同意书", "required": True,
             "condition": {"applicant_under_18": True},
             "evidence_quote": "consent letter signed by the guardian"}])
        second = self._patch(documents=[
            {"code": "other_document", "name_zh": "监护人同意书", "required": False,
             "evidence_quote": "consent letter signed by the guardian"}])
        with self.assertRaises(Exception) as ctx:
            formal_extract.merge_sections([first, second])
        self.assertIn("必需性冲突", str(ctx.exception))

    def test_paraphrased_condition_keeps_the_more_specific_quote(self):
        # 同一要求的详略写法：短引用是长引用的子串、condition key 拼写不同
        # → 保留更具体的一条，不视为冲突（香港页实测场景）
        first = self._patch(documents=[
            {"code": "other_document", "name_zh": "住宿安排證明副本", "required": True,
             "condition": {"applicant_age_under_18": True},
             "evidence_quote": "Copy of proof of the applicant's accommodation arrangement"}])
        second = self._patch(documents=[
            {"code": "other_document", "name_zh": "住宿安排證明副本", "required": True,
             "condition": {"applicant_under_18": True},
             "evidence_quote": "Copy of proof of the applicant's accommodation arrangement "
                               "[if the applicant is under the age of 18]"}])
        merged = formal_extract.merge_sections([first, second])
        self.assertEqual(len(merged.documents), 1)
        self.assertEqual(merged.documents[0].condition, {"applicant_under_18": True})
        self.assertIn("under the age of 18", merged.documents[0].evidence_quote)

    def test_diverging_condition_without_quote_containment_is_error(self):
        first = self._patch(documents=[
            {"code": "proof_of_funds", "name_zh": "经济支持证明", "required": True,
             "condition": {"sponsor_is_educational_institution": True},
             "evidence_quote": "proof of financial support e.g. bank statements"}])
        second = self._patch(documents=[
            {"code": "proof_of_funds", "name_zh": "经济支持证明", "required": True,
             "condition": {"applicant_is_self_funded": True},
             "evidence_quote": "bank statements showing sufficient funds for the course"}])
        with self.assertRaises(Exception) as ctx:
            formal_extract.merge_sections([first, second])
        self.assertIn("条件冲突", str(ctx.exception))

    def test_lenient_merge_combines_diverging_conditions_with_both_quotes(self):
        markdown = ("If you are under 18, provide a consent letter. "
                    "If your parents pay, provide a consent letter.")
        docs = [self._patch(documents=[
            {"code": "parental_consent", "name_zh": "父母或监护人同意书", "required": True,
             "condition": condition, "condition_quote": quote, "evidence_quote": "provide a consent letter"}])
            for condition, quote in (({"applicant_under_18": True}, "If you are under 18"),
                                     ({"parents_fund": True}, "If your parents pay"))]
        conflicts = []
        merged = formal_extract.merge_sections(docs, conflicts=conflicts)
        self.assertEqual(conflicts, [])
        document = merged.documents[0]
        self.assertEqual(document.condition,
                         {"any_of": [{"applicant_under_18": True}, {"parents_fund": True}]})
        report = gate.run_gate_visa(merged, markdown, {}, {"excerpt_chars": len(markdown)})
        self.assertEqual(report.errors, [])

        unconditional = self._patch(documents=[
            {"code": "parental_consent", "name_zh": "父母或监护人同意书", "required": True,
             "evidence_quote": "provide a consent letter"}])
        merged = formal_extract.merge_sections(docs + [unconditional], conflicts=conflicts)
        self.assertIsNone(merged.documents[0].condition)

    def test_unsupported_section_value_is_dropped_before_page_merge(self):
        markdown = "| (b) | Visa / entry permit for study | HK$330 |"
        good = self._patch(policies=[{"policy": {"visa_fee": 330},
                                      "evidence": [{"field": "visa_fee", "evidence_quote":
                                                    "Visa / entry permit for study | HK$330"}]}])
        invented = self._patch(policies=[{"policy": {"visa_fee": 190},
                                          "evidence": [{"field": "visa_fee", "evidence_quote": "Single entry"}]}])
        capture = {"source": {"id": "hk"}, "markdown": markdown, "patches": [invented, good]}
        patch, report = formal_pipeline._page_patch(capture, "CN-HK")
        self.assertEqual(patch.policies[0].policy.visa_fee, 330)
        self.assertEqual(report.errors, [])
        self.assertTrue(any("已丢弃" in w["reason"] for w in report.warnings))

    def test_stated_requirement_beats_an_exemption_section_saying_false(self):
        required = self._patch(policies=[{"policy": {"funds_tuition_required": True},
                                          "evidence": [{"field": "funds_tuition_required",
                                                        "evidence_quote": "money to pay for your course"}]}])
        exempt = self._patch(policies=[{"policy": {"funds_tuition_required": False},
                                        "evidence": [{"field": "funds_tuition_required",
                                                      "evidence_quote": "You do not need to prove"}]}])
        for order in ((exempt, required), (required, exempt)):
            conflicts = []
            merged = formal_extract.merge_sections(list(order), conflicts=conflicts)
            self.assertEqual(conflicts, [])
            self.assertIs(merged.policies[0].policy.funds_tuition_required, True)
            self.assertEqual([ev.evidence_quote for ev in merged.policies[0].evidence],
                             ["money to pay for your course"])

    def test_historical_effective_from_is_rejected(self):
        markdown = "Effective July 31, 2013, SEVP no longer mails receipts."
        patch = self._patch(policies=[{"policy": {"effective_from": "2013-07-31"},
                                       "evidence": [{"field": "effective_from",
                                                     "evidence_quote": "Effective July 31, 2013"}]}])
        report = gate.run_gate_visa(patch, markdown, {}, {"excerpt_chars": len(markdown)})
        self.assertIn("effective_from", [p["field"] for p in report.errors])

    def test_markdown_link_title_with_escaped_parentheses_is_plain_text(self):
        markdown = ('[F-1 student visas](https://travel.state.gov/study.html "\\(opens in a new window\\)") '
                    "can be issued up to 365 days in advance.")
        self.assertTrue(gate.quote_in_markdown(
            "F-1 student visas can be issued up to 365 days in advance.", markdown))

    def test_scope_specific_value_overrides_universal_with_traceable_note(self):
        # 页面同时有"例行 21 天"（范围）与"内地生 42 天"（未打范围标签落入通用桶）：
        # 范围特定值优先，被覆盖的通用值记入 remark，不得整页失败
        scoped = self._patch(policies=[{"policy": {"processing_days": 21,
                                                   "applies_to_city": "Hong Kong"},
                                        "evidence": []}])
        universal = self._patch(policies=[{"policy": {"processing_days": 42},
                                           "evidence": []}])
        merged = formal_extract.merge_sections([scoped, universal])
        self.assertEqual(len(merged.policies), 1)
        fields = merged.policies[0].policy.model_dump(exclude_none=True)
        self.assertEqual(fields["processing_days"], 21)
        self.assertIn("42", fields["remark"])
        self.assertIn("覆盖", fields["remark"])

    def test_universal_only_fields_propagate_to_every_scope(self):
        london = self._patch(policies=[{"policy": {"funds_living_monthly": 1529,
                                                   "applies_to_city": "London"},
                                        "evidence": []}])
        universal = self._patch(policies=[{"policy": {"apply_window_days": 28},
                                           "evidence": []}])
        merged = formal_extract.merge_sections([london, universal])
        fields = merged.policies[0].policy.model_dump(exclude_none=True)
        self.assertEqual(fields["apply_window_days"], 28)
        self.assertEqual(fields["funds_living_monthly"], 1529)

    def test_documents_with_shared_code_get_distinct_rows_in_db(self):
        directory = TemporaryDirectory(prefix="agent1-doc-rows-")
        self.addCleanup(directory.cleanup)
        paths = paths_for(Path(directory.name), "development")
        storage.initialize(paths)
        merged = self._patch(documents=[
            {"code": "other_document", "name_zh": "录取通知书", "required": True,
             "evidence_quote": "A letter of acceptance from the institution"},
            {"code": "other_document", "name_zh": "旅行证件副本", "required": True,
             "evidence_quote": "Copy of the applicant's valid travel document"}])
        with storage.session(paths, readonly=False) as connection:
            formal_pipeline._apply_documents(connection, "policy-1",
                                             [d.model_dump() for d in merged.documents])
        with storage.session(paths) as connection:
            docs = connection.execute(
                "SELECT doc_id, name_zh FROM DOCUMENT ORDER BY name_zh").fetchall()
            self.assertEqual([d["name_zh"] for d in docs], ["录取通知书", "旅行证件副本"])
            self.assertEqual(len({d["doc_id"] for d in docs}), 2)
            links = connection.execute(
                "SELECT COUNT(*) AS n FROM POLICY_DOCUMENT WHERE policy_id='policy-1'"
            ).fetchone()["n"]
            self.assertEqual(links, 2)


if __name__ == "__main__":
    unittest.main()
