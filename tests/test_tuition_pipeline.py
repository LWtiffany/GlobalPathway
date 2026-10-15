"""Agent1 学费管道：门控、合并、SCHOOL/PROGRAM/TUITION 入库、冲突与变更审核、证据。"""
import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from agent1 import formal_pipeline, incremental, storage, tuition_extract, tuition_pipeline
from agent1.environment import paths_for
from agent1.models import TuitionPatch

INTRO = "The University publishes tuition fees for each cohort. Fees are payable per annum. " * 4
LOCAL = "For Local Hong Kong Students Tuition Fees HKD47,000 Per annum, 2026-2027 cohort"
NON_STEM = "For Non-local Hong Kong Students Admitted to Non-STEM Faculties Tuition Fees HKD224,000"
STEM = "For Non-local Students Admitted to the STEM Faculties and Schools Tuition Fees HKD249,000"
COHORT = "Per annum, 2026-2027 cohort"
NEXT_YEAR = "Annual Tuition Fees for Non-Local Students in 2027/28 Academic Year"
NEXT_STEM = "For STEM Faculties and Schools | HK$ 280,000"

UCL_PAGE = """#### Computer Science, MSc (TMSCOMSING01)
**UK**
_Full-time:_
    £21,500
**Overseas**
_Full-time:_
    £42,700
* * *
#### Data Science and Machine Learning, MSc (TMSDATSMLE01)
**UK**
_Full-time:_
    £21,500
**Overseas**
_Full-time:_
    £42,700
* * *
#### Machine Learning, MSc (TMSCOMSMCL01)
**Overseas**
_Full-time:_
    £43,900
"""

HKU = {"key": "hku", "name": "The University of Hong Kong", "country": "CN-HK", "city": "Hong Kong"}
HKU_PROGRAMS = [
    {"key": "ug_stem", "name": "UG STEM", "level": "undergraduate", "aliases": ["STEM Faculties"],
     "exclude": ["Non-STEM"]},
    {"key": "ug_non_stem", "name": "UG Non-STEM", "level": "undergraduate", "aliases": ["Non-STEM"]},
]
UCL_SOURCE = {
    "id": "ucl_fees", "url": "https://www.ucl.ac.uk/fees", "school": {"key": "ucl", "name": "UCL", "country": "UK"},
    "programs": [
        {"key": "cs", "name": "Computer Science MSc", "aliases": ["Computer Science, MSc"]},
        {"key": "ml", "name": "Machine Learning MSc", "aliases": ["Machine Learning, MSc"],
         "exclude": ["Data Science and Machine Learning"]}],
    "defaults": {"academic_year": "2026/27", "fee_basis": "per_year"},
    "defaults_note": "URL year=2026"}


US_PAGE = """# Tuition and Fees, 2026-2027
## Undergraduate
Undergraduate tuition for the 2026-2027 academic year: $68,400 per year
## Graduate School of Arts and Sciences
Full-time tuition, 2026-2027: $63,200 per academic year
## School of Law (J.D.)
Law tuition and fees, 2026-2027: $79,500 per year
Law tuition, 2026-2027: $76,000 per year
"""
PUBLIC_PAGE = """# Undergraduate Tuition 2026-27
California residents: $14,950 per year
Nonresidents: $50,400 per year (includes Nonresident Supplemental Tuition)
"""
CATALOG_SOURCE = {"id": "uni_fees", "url": "https://uni.example.edu/tuition", "catalog": True,
                  "school": {"key": "uni", "name": "Example University", "country": "US"}}


def catalog_row(scope, label, name, level, amount, quote, context, kind="tuition", status=None,
                year="2026/27", basis="per_year"):
    return {"program_scope": scope, "program_label": label, "program_name": name, "level": level,
            "amount_kind": kind, "academic_year": year, "fee_status": status, "fee_basis": basis,
            "amount": amount, "currency": "USD", "evidence_quote": quote, "context_quotes": context}


US_ROWS = [
    catalog_row("school_wide", "Undergraduate", None, "undergraduate", 68400, "$68,400 per year",
                ["Undergraduate tuition for the 2026-2027 academic year"]),
    catalog_row("division", "Graduate School of Arts and Sciences", "Graduate School of Arts and Sciences",
                "graduate", 63200, "$63,200 per academic year", ["Full-time tuition, 2026-2027"]),
    catalog_row("division", "School of Law (J.D.)", "School of Law JD", "professional", 79500,
                "$79,500 per year", ["Law tuition and fees, 2026-2027"], kind="tuition_and_fees"),
    catalog_row("division", "School of Law (J.D.)", "School of Law JD", "professional", 76000,
                "$76,000 per year", ["Law tuition, 2026-2027"]),
]


def overview_page():
    return f"# Fees and Scholarships\n{INTRO}\n## Local\n{LOCAL}\n## Non-local\n{NON_STEM}\n{COHORT}\n{STEM}\n{COHORT}\n"


def admissions_page():
    return f"# Fees\n{INTRO}\n## Next year\n{NEXT_YEAR}\n{NEXT_STEM}\n"


def row(program, amount, quote, context, year="2026/27", status="non_local", basis="per_year", currency="HKD"):
    return {"program": program, "academic_year": year, "fee_status": status, "fee_basis": basis,
            "amount": amount, "currency": currency, "evidence_quote": quote, "context_quotes": context}


def overview_extraction(section):
    rows = []
    if LOCAL in section.text:
        rows.append(row("all", 47000, "HKD47,000", [LOCAL], status="local"))
    if NON_STEM in section.text:
        rows += [row("ug_non_stem", 224000, "HKD224,000", [NON_STEM, COHORT]),
                 row("ug_stem", 249000, "HKD249,000", [STEM, COHORT])]
    return TuitionPatch.model_validate({"rows": rows})


def admissions_extraction(section):
    if NEXT_STEM in section.text:
        return TuitionPatch.model_validate({"rows": [
            row("ug_stem", 280000, "HK$ 280,000", [NEXT_YEAR, NEXT_STEM], year="2027/28")]})
    return TuitionPatch()


class TuitionGateTests(unittest.TestCase):
    def check(self, rows, markdown=UCL_PAGE, source=UCL_SOURCE):
        clean, report = tuition_extract.verify(TuitionPatch.model_validate({"rows": rows}), markdown,
                                               source, "snap-1")
        return clean.rows, report

    def test_supported_row_takes_year_and_basis_from_source_defaults(self):
        rows, report = self.check([row("cs", 42700, "£42,700", ["Computer Science, MSc (TMSCOMSING01)",
                                                                  "Overseas", "Full-time"],
                                       year=None, status="international", basis=None, currency=None)
                                   | {"study_mode": "full_time"}])
        self.assertTrue(report.ok, report.problems)
        self.assertEqual((rows[0].amount, rows[0].currency, rows[0].academic_year, rows[0].fee_basis,
                          rows[0].study_mode, rows[0].source_id), (42700, "GBP", "2026/27", "per_year",
                                                                    "full_time", "ucl_fees"))

    def test_programme_must_be_named_near_the_amount_and_excludes_apply(self):
        rows, report = self.check([row("ml", 42700, "£42,700",
                                       ["Data Science and Machine Learning, MSc (TMSDATSMLE01)", "Overseas"],
                                       status="international", currency="GBP")])
        self.assertEqual(rows, [])
        self.assertIn("项目", report.errors[0]["reason"])

    def test_amount_and_currency_must_match_the_quote(self):
        context = ["Computer Science, MSc (TMSCOMSING01)", "Overseas"]
        self.assertEqual(self.check([row("cs", 44400, "£42,700", context, status="international",
                                         currency="GBP")])[0], [])
        self.assertEqual(self.check([row("cs", 42700, "£42,700", context, status="international",
                                         currency="HKD")])[0], [])

    def test_year_must_be_quoted_when_not_declared(self):
        source = {**UCL_SOURCE, "id": "hku_overview", "programs": HKU_PROGRAMS, "defaults": {}}
        rows, _ = self.check([row("ug_stem", 249000, "HKD249,000", [STEM, COHORT], year="2026-2027")],
                             overview_page(), source)
        self.assertEqual(rows[0].academic_year, "2026/27")
        rows, report = self.check([row("ug_stem", 249000, "HKD249,000", [STEM], year="2025/26")],
                                  overview_page(), source)
        self.assertEqual(rows, [])
        self.assertIn("学年", report.errors[0]["reason"])

    def test_fee_status_must_be_supported(self):
        source = {**UCL_SOURCE, "programs": HKU_PROGRAMS, "defaults": {}}
        rows, report = self.check([row("ug_stem", 249000, "HKD249,000", [STEM, COHORT], status="local")],
                                  overview_page(), source)
        self.assertEqual(rows, [])
        self.assertIn("学生类别", report.errors[0]["reason"])

    def test_label_after_the_amount_does_not_claim_it(self):
        # hku.hk layout: the next block's label follows the previous amount
        page = ("For Non-local Hong Kong Students Admitted to Non-STEM Faculties#\nTuition Fees\n"
                "#### HKD224,000\nPer annum, 2026-2027 cohort\n#Faculties of Architecture, Arts\n"
                "For Non-local Students Admitted to the STEM Faculties and Schools (inclusive of STEM Fee)^\n"
                "Tuition Fees\n#### HKD249,000\nPer annum, 2026-2027 cohort\n")
        source = {**UCL_SOURCE, "programs": HKU_PROGRAMS, "defaults": {}}
        stem_label = "For Non-local Students Admitted to the STEM Faculties and Schools (inclusive of STEM Fee)^"
        wrong, report = self.check([row("ug_stem", 224000, "HKD224,000", [COHORT, stem_label])], page, source)
        self.assertEqual(wrong, [])
        self.assertIn("项目标签", report.errors[0]["reason"])
        non_stem_label = "For Non-local Hong Kong Students Admitted to Non-STEM Faculties#"
        right, report = self.check([row("ug_non_stem", 224000, "HKD224,000", [non_stem_label, COHORT]),
                                    row("ug_stem", 249000, "HKD249,000", [stem_label, COHORT])], page, source)
        self.assertEqual([r.program for r in right], ["ug_non_stem", "ug_stem"])
        self.assertTrue(report.ok, report.problems)

    def test_distant_or_invented_context_is_ignored(self):
        rows, report = self.check([row("cs", 42700, "£42,700", ["Computer Science, MSc (TMSCOMSING01)",
                                                                  "Overseas", "Invented label"],
                                       status="international", currency="GBP")])
        self.assertEqual(rows[0].context_quotes, ["Computer Science, MSc (TMSCOMSING01)", "Overseas"])
        self.assertTrue(report.warnings)


class CatalogGateTests(unittest.TestCase):
    def check(self, rows, markdown=US_PAGE, source=CATALOG_SOURCE):
        clean, report = tuition_extract.verify(TuitionPatch.model_validate({"rows": rows}), markdown,
                                               source, "snap-1")
        return clean.rows, report

    def test_page_labels_name_programmes_divisions_and_school_wide_rates(self):
        rows, report = self.check(US_ROWS)
        self.assertTrue(report.ok, report.problems)
        self.assertEqual([(r.program, r.program_name, r.program_scope, r.level, r.amount_kind) for r in rows], [
            ("level:undergraduate", "Undergraduate (school-wide)", "school_wide", "undergraduate", "tuition"),
            ("name:graduate school of arts and sciences", "Graduate School of Arts and Sciences", "division",
             "graduate", "tuition"),
            ("name:school of law jd", "School of Law JD", "division", "professional", "tuition_and_fees"),
            ("name:school of law jd", "School of Law JD", "division", "professional", "tuition")])

    def test_label_must_be_on_the_page_and_nearest_before_the_amount(self):
        invented = catalog_row("division", "School of Medicine", "School of Medicine", "professional", 76000,
                               "$76,000 per year", ["Law tuition, 2026-2027"])
        rows, report = self.check([invented])
        self.assertEqual(rows, [])
        self.assertIn("项目标签", report.errors[0]["reason"])
        stolen = catalog_row("division", "Graduate School of Arts and Sciences", "GSAS", "graduate", 76000,
                             "$76,000 per year", ["Law tuition, 2026-2027"])
        rows, report = self.check([stolen, US_ROWS[3]])
        self.assertEqual([r.amount for r in rows], [76000])
        self.assertEqual(rows[0].program_name, "School of Law JD")
        self.assertIn("最近的项目标签", report.errors[0]["reason"])

    def test_ungrounded_name_falls_back_to_the_label_and_unsupported_level_is_dropped(self):
        row_ = catalog_row("division", "Graduate School of Arts and Sciences", "Harvard GSAS PhD", "undergraduate",
                           63200, "$63,200 per academic year", ["Full-time tuition, 2026-2027"])
        rows, report = self.check([row_])
        self.assertEqual((rows[0].program_name, rows[0].level), ("Graduate School of Arts and Sciences", None))
        self.assertTrue(report.warnings)

    def test_school_wide_rate_needs_a_level_on_the_page(self):
        row_ = catalog_row("school_wide", None, None, "postgraduate_research", 63200,
                           "$63,200 per academic year", ["Full-time tuition, 2026-2027"])
        rows, report = self.check([row_])
        self.assertEqual(rows, [])
        self.assertIn("层次", report.errors[0]["reason"])

    def test_us_residency_statuses(self):
        public = {**CATALOG_SOURCE, "school": {**CATALOG_SOURCE["school"], "control": "public"}}
        rows, report = self.check([
            catalog_row("school_wide", "Undergraduate Tuition 2026-27", None, "undergraduate", 14950,
                        "$14,950 per year", ["California residents"], status="in_state"),
            catalog_row("school_wide", "Undergraduate Tuition 2026-27", None, "undergraduate", 50400,
                        "$50,400 per year", ["Nonresidents"], status="out_of_state"),
            catalog_row("school_wide", "Undergraduate Tuition 2026-27", None, "undergraduate", 50400,
                        "$50,400 per year", ["Undergraduate Tuition 2026-27"], status="in_state")],
            PUBLIC_PAGE, public)
        self.assertEqual([(r.fee_status, r.amount) for r in rows], [("in_state", 14950), ("out_of_state", 50400)])
        self.assertIn("学生类别", report.errors[0]["reason"])
        private_page = "Undergraduate tuition 2026-27: $72,500 for residents of the residential colleges"
        rows, report = self.check([catalog_row("school_wide", None, None, "undergraduate", 72500, "$72,500",
                                               [private_page], status="in_state")], private_page)
        self.assertEqual(rows[0].fee_status, None)
        self.assertIn("非公立", report.warnings[0]["reason"])

    def test_label_that_is_only_a_rate_falls_back_to_the_page_level(self):
        page = "## Tuition 2026-27\n| Tuition for one course | $7,381 |\n"
        row_ = {**catalog_row("programme", "Tuition for one course", "Tuition for one course", None, 7381,
                              "| Tuition for one course | $7,381 |", ["Tuition 2026-27"], basis=None),
                "rate_label": "Tuition for one course"}
        rows, report = self.check([row_], page)
        self.assertEqual(rows, [])
        self.assertIn("费率标签", report.errors[0]["reason"])
        rows, _ = self.check([row_], page, {**CATALOG_SOURCE, "level": "graduate"})
        self.assertEqual((rows[0].program, rows[0].rate_label), ("level:graduate", "Tuition for one course"))

    def test_currency_from_table_header_or_source_default(self):
        page = ("# Tuition and Fees 2026-27 College\n| Degree Program | Per Quarter (US$) |\n| --- | --- |\n"
                "| College (Flat-Rate) | 25,320 |\n")
        row_ = catalog_row("division", "College (Flat-Rate)", "College", "undergraduate", 25320,
                           "| College (Flat-Rate) | 25,320 |", ["Per Quarter (US$)", "Tuition and Fees 2026-27"],
                           basis="per_term")
        rows, report = self.check([{**row_, "currency": None}], page)
        self.assertEqual((rows[0].currency, rows[0].fee_basis), ("USD", "per_term"))
        bare = page.replace(" (US$)", "")
        self.assertEqual(self.check([{**row_, "currency": None}], bare)[0], [])
        rows, _ = self.check([{**row_, "currency": None}], bare,
                             {**CATALOG_SOURCE, "defaults": {"currency": "USD"}})
        self.assertEqual(rows[0].currency, "USD")
        rows, report = self.check([{**row_, "currency": "GBP"}], page,
                                  {**CATALOG_SOURCE, "defaults": {"currency": "USD"}})
        self.assertEqual(rows, [])

    def test_source_level_supports_rates_of_a_single_level_page(self):
        page = "Tuition and Fees 2026-27\n| | Annual |\n| --- | --- |\n| Tuition | $68,574 |\n"
        row_ = catalog_row("school_wide", "Tuition", None, "graduate", 68574, "$68,574",
                           ["Tuition and Fees 2026-27", "Annual"], status="all")
        self.assertEqual(self.check([row_], page)[0], [])
        rows, report = self.check([row_], page, {**CATALOG_SOURCE, "level": "graduate"})
        self.assertEqual((rows[0].program, rows[0].fee_status, rows[0].fee_basis),
                         ("level:graduate", None, "per_year"))
        self.assertTrue(report.ok, report.problems)

    def test_rate_variants_are_separate_price_points(self):
        page = ("## University Rates & Costs: 2026-27\n| Enrollment Status | University Rate |\n| --- | --- |\n"
                "| Tuition - Regular Rate | 65,210 |\n| SHP Fee | 4,220 |\n"
                "| **DCE, In Absentia, Trailing Nondegree Students** | |\n| Tuition - Marginal Rate | 4,700 |\n")
        source = {**CATALOG_SOURCE, "level": "graduate", "defaults": {"currency": "USD"}}
        base = dict(scope="school_wide", label="University Rate", name=None, level="graduate",
                    context=["University Rates & Costs: 2026-27"], basis=None)
        rows, report = self.check([
            catalog_row(amount=65210, quote="| Tuition - Regular Rate | 65,210 |", **base),
            {**catalog_row(amount=4700, quote="| Tuition - Marginal Rate | 4,700 |", **base),
             "rate_label": "Marginal Rate"},
            {**catalog_row(amount=4700, quote="| Tuition - Marginal Rate | 4,700 |", **base),
             "rate_label": "Thesis rate"}], page, source)
        self.assertEqual([(r.amount, r.rate_label, r.academic_year) for r in rows],
                         [(65210, None, "2026/27"), (4700, "Marginal Rate", "2026/27")])
        self.assertIn("费率标签", report.errors[0]["reason"])
        merged, conflicts = tuition_extract.merge(rows, {})
        self.assertEqual((len(merged), conflicts), (2, []))

    def test_year_comes_from_the_nearest_year_heading_before_the_amount(self):
        page = ("Tuition and Fees 2026-27\n| Tuition | $68,574 |\n" + "x" * 200 +
                "\nTuition and Fees 2025-26\n| Tuition | $65,622 |\n")
        self.assertEqual(tuition_extract.years_before("$65,622", page), {"2025/26"})
        self.assertEqual(tuition_extract.years_before("$68,574", page), {"2026/27"})
        self.assertEqual(tuition_extract.years_before("Fall 2026: $1,000", "Fall 2026: $1,000"), set())
        self.assertEqual(tuition_extract.years_before("$1,000", "Fall 2026 term\n$1,000"), {"2026/27"})

    def test_sections_without_amounts_are_not_sent_to_the_model(self):
        section = incremental.Section("k", "Menu", "Tuition and Fees 2026-27\n* Billing\n* Contact", 0, 1, "h")
        patch_, usage = tuition_extract.extract_section(section, CATALOG_SOURCE, client=object(), settings={})
        self.assertEqual((patch_.rows, usage), ([], (0, 0)))

    def test_tuition_only_amount_wins_over_combined_figure(self):
        rows, _ = self.check(US_ROWS)
        merged, conflicts = tuition_extract.merge(rows, {})
        self.assertEqual(conflicts, [])
        law = merged[("name:school of law jd", "2026/27", None, "per_year", None, None)]
        self.assertEqual(law["amount"], 76000)


class TuitionExtractTests(unittest.TestCase):
    def test_output_cleanup(self):
        cleaned = tuition_extract._clean_output(json.dumps({"rows": [
            row("cs", 1, "£1", "x", status="mature"), row("unknown", 1, "£1", []),
            {"program": "cs", "amount": "N/A", "evidence_quote": ""}]}), ["cs"])
        self.assertEqual(len(cleaned["rows"]), 1)
        self.assertIsNone(cleaned["rows"][0]["fee_status"])
        self.assertEqual(cleaned["rows"][0]["context_quotes"], [])

    def test_small_heading_sections_are_merged_up_to_the_limit(self):
        sections = tuition_extract.split_sections(UCL_PAGE)
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0].text, UCL_PAGE)
        long_page = "".join(f"## Part {i}\n{'x' * 1500}\n" for i in range(4))
        sections = tuition_extract.split_sections(long_page)
        self.assertEqual(len(sections), 2)
        self.assertEqual("".join(s.text for s in sections), long_page)

    def test_year_normalization(self):
        for value in ("2026-2027", "2026/27", "2026-27", 2026, "2026 / 2027"):
            self.assertEqual(tuition_extract.normalize_year(value), "2026/27")
        with self.assertRaises(ValueError):
            tuition_extract.normalize_year("2026-2028")

    def test_merge_expands_all_and_flags_conflicts(self):
        base = {"academic_year": "2026/27", "fee_status": "local", "fee_basis": "per_year",
                "currency": "HKD", "evidence_quote": "q"}
        rows = TuitionPatch.model_validate({"rows": [
            {**base, "program": "all", "amount": 47000, "source_id": "a"},
            {**base, "program": "ug_stem", "amount": 48000, "source_id": "a"},
            {**base, "program": "ug_non_stem", "amount": 47000, "source_id": "b"},
            {**base, "program": "ug_non_stem", "amount": 46000, "source_id": "b"}]}).rows
        merged, conflicts = tuition_extract.merge(rows, {"a": ["ug_stem", "ug_non_stem"], "b": ["ug_non_stem"]})
        stem = ("ug_stem", "2026/27", "local", "per_year", None, None)
        non_stem = ("ug_non_stem", "2026/27", "local", "per_year", None, None)
        self.assertEqual(merged[stem]["amount"], 48000)
        self.assertFalse(merged[stem]["conflict"])
        self.assertTrue(merged[non_stem]["conflict"])
        self.assertEqual([c["key"] for c in conflicts], [non_stem])


class TuitionPipelineTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(prefix="agent1-tuition-")
        self.addCleanup(directory.cleanup)
        self.paths = paths_for(Path(directory.name), "development")
        storage.initialize(self.paths)
        base = {"category": "school", "region": "CN-HK", "authority": "official", "review_status": "approved",
                "enabled": True, "owner": "reviewer", "access_basis": "public", "schema_type": "tuition",
                "check_interval_minutes": 1, "freshness_sla_hours": 168, "dynamic": False,
                "school": HKU, "programs": HKU_PROGRAMS}
        self.sources = [{"id": "hku_overview", "name": "HKU", "url": "https://hku.example.org/fees",
                         "allowed_host": "hku.example.org", **base},
                        {"id": "hku_admissions", "name": "HKU admissions", "url": "https://hku.example.org/adm",
                         "allowed_host": "hku.example.org", **base}]
        self.source_file = Path(directory.name) / "sources.json"
        self.source_file.write_text(json.dumps(self.sources), encoding="utf-8")
        self.pages = {"https://hku.example.org/fees": overview_page(),
                      "https://hku.example.org/adm": admissions_page()}
        self.extractions = {"hku_overview": overview_extraction, "hku_admissions": admissions_extraction}
        settings = {"model": "offline", "base_url": "https://example.invalid/v1", "extra_body": None,
                    "api_key": "x", "timeout": 1, "max_retries": 0, "excerpt_chars": 2500}

        async def fake_crawl(source):
            return SimpleNamespace(success=True, url=source["url"], html="<main>x</main>",
                                   markdown=self.pages[source["url"]])

        for target in (
                patch("agent1.config.SOURCES_PATH", self.source_file),
                patch("agent1.config.llm_settings", return_value=settings),
                patch("agent1.formal_extract.legacy_extract._client", return_value=(object(), settings)),
                patch("agent1.formal_pipeline.crawl.fetch_page", side_effect=fake_crawl),
                patch("agent1.tuition_extract.extract_section",
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

    def tuition(self):
        return {(r["program"], r["academic_year"], r["fee_status"]): (r["amount"], r["currency"])
                for r in self.db().execute("""SELECT p.name AS program, t.* FROM TUITION t
                    JOIN PROGRAM p USING (program_id)""")}

    def test_scan_writes_school_programmes_tuition_and_evidence(self):
        outcome = tuition_pipeline.scan_all("hku", paths=self.paths)
        self.assertEqual(outcome["result"], "applied")
        self.assertEqual({r["result"] for r in outcome["tuition"]}, {"inserted"})
        db = self.db()
        self.assertEqual([tuple(r) for r in db.execute("SELECT name, country FROM SCHOOL")],
                         [("The University of Hong Kong", "CN-HK")])
        self.assertEqual(db.execute("SELECT COUNT(*) FROM PROGRAM").fetchone()[0], 2)
        self.assertEqual(self.tuition(), {
            ("UG STEM", "2026/27", "local"): (47000, "HKD"),
            ("UG Non-STEM", "2026/27", "local"): (47000, "HKD"),
            ("UG Non-STEM", "2026/27", "non_local"): (224000, "HKD"),
            ("UG STEM", "2026/27", "non_local"): (249000, "HKD"),
            ("UG STEM", "2027/28", "non_local"): (280000, "HKD")})
        evidence = db.execute("""SELECT e.kind, e.quote FROM TUITION_EVIDENCE e JOIN TUITION t USING (tuition_id)
            WHERE t.amount=249000""").fetchall()
        self.assertEqual({tuple(r) for r in evidence},
                         {("amount", "HKD249,000"), ("context", STEM), ("context", COHORT)})
        self.assertEqual(db.execute("SELECT COUNT(*) FROM admin.REVIEW_QUEUE").fetchone()[0], 0)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM POLICY_PUBLICATION").fetchone()[0], 0)

    def test_rescan_is_idempotent_and_a_changed_amount_is_queued_then_approved(self):
        tuition_pipeline.scan_all("hku", paths=self.paths)
        again = tuition_pipeline.scan_all("hku", paths=self.paths, reapply=True)
        self.assertEqual({r["result"] for r in again["tuition"]}, {"no_change"})

        self.pages["https://hku.example.org/fees"] = overview_page().replace("HKD249,000", "HKD259,000")
        original = self.extractions["hku_overview"]

        def raised(section):
            patch_ = original(section)
            return patch_.model_copy(update={"rows": [
                r.model_copy(update={"amount": 259000, "evidence_quote": "HKD259,000",
                                     "context_quotes": [STEM.replace("249", "259"), COHORT]})
                if r.amount == 249000 else r for r in patch_.rows]})

        self.extractions["hku_overview"] = raised
        tuition_pipeline.scan_all("hku", paths=self.paths, full=True)
        self.assertEqual(self.tuition()[("UG STEM", "2026/27", "non_local")], (249000, "HKD"))
        review = self.db().execute("SELECT * FROM admin.REVIEW_QUEUE WHERE domain_table='TUITION'").fetchone()
        self.assertEqual(review["action"], "UPDATE")
        self.assertIn("249000 HKD → 259000 HKD", review["reason"])
        with storage.session(self.paths, readonly=False) as connection:
            formal_pipeline.apply_review(connection, review["review_id"], decided_by="reviewer")
        self.assertEqual(self.tuition()[("UG STEM", "2026/27", "non_local")], (259000, "HKD"))
        quotes = [r["quote"] for r in self.db().execute("""SELECT e.quote FROM TUITION_EVIDENCE e
            JOIN TUITION t USING (tuition_id) WHERE t.amount=259000 AND e.kind='amount'""")]
        self.assertEqual(quotes, ["HKD259,000"])

    def test_cross_page_conflict_goes_to_review_and_approval_inserts(self):
        conflicting = "Non-local STEM Faculties fee for 2026-2027: HK$ 250,000 per annum"
        self.pages["https://hku.example.org/adm"] = admissions_page() + f"## Correction\n{conflicting}\n"
        original = self.extractions["hku_admissions"]

        def with_conflict(section):
            patch_ = original(section)
            if conflicting in section.text:
                patch_.rows.append(TuitionPatch.model_validate({"rows": [
                    row("ug_stem", 250000, "HK$ 250,000", [conflicting])]}).rows[0])
            return patch_

        self.extractions["hku_admissions"] = with_conflict
        outcome = tuition_pipeline.scan_all("CN-HK", paths=self.paths)
        self.assertNotIn(("UG STEM", "2026/27", "non_local"), self.tuition())
        result = next(r for r in outcome["tuition"] if r["result"] == "review")
        db = self.db()
        review = db.execute("SELECT * FROM admin.REVIEW_QUEUE WHERE review_id=?", (result["review_id"],)).fetchone()
        self.assertEqual(review["action"], "CREATE")
        self.assertIn("249000 HKD / 250000 HKD", review["reason"])
        issue = db.execute("SELECT * FROM GATE_ISSUE WHERE source_id='school:hku' AND status='OPEN'").fetchone()
        self.assertEqual(issue["check_name"], "conflict")
        with storage.session(self.paths, readonly=False) as connection:
            formal_pipeline.apply_review(connection, review["review_id"], decided_by="reviewer")
        self.assertEqual(self.tuition()[("UG STEM", "2026/27", "non_local")], (249000, "HKD"))

    def test_formal_scan_dispatch_and_registry_validation(self):
        outcome = formal_pipeline.scan("hku_overview", paths=self.paths)
        self.assertEqual((outcome["source_id"], outcome["school"]), ("hku_overview", "hku"))
        self.assertEqual(formal_pipeline.register_sources(), 2)
        broken = [{**self.sources[0], "programs": [{"key": "all", "name": "x"}]}]
        self.source_file.write_text(json.dumps(broken), encoding="utf-8")
        with self.assertRaises(ValueError):
            formal_pipeline.register_sources()
        mixed = [{**self.sources[0], "catalog": True}]
        self.source_file.write_text(json.dumps(mixed), encoding="utf-8")
        with self.assertRaises(ValueError):
            formal_pipeline.register_sources()

    def test_catalog_scan_records_discovered_programmes_with_scope(self):
        catalog = {**{k: v for k, v in self.sources[0].items() if k != "programs"},
                   "id": "uni_fees", "url": "https://uni.example.edu/tuition", "allowed_host": "uni.example.edu",
                   "region": "US", "catalog": True, "school": CATALOG_SOURCE["school"]}
        self.source_file.write_text(json.dumps([*self.sources, catalog]), encoding="utf-8")
        self.pages[catalog["url"]] = US_PAGE
        self.extractions["uni_fees"] = lambda section: TuitionPatch.model_validate(
            {"rows": [r for r in US_ROWS if r["evidence_quote"].split()[0] in section.text]})
        self.assertEqual(formal_pipeline.register_sources(), 3)
        outcome = tuition_pipeline.scan_all("uni", paths=self.paths)
        self.assertEqual(outcome["result"], "applied")
        programs = {tuple(r) for r in self.db().execute("""SELECT p.name, p.level, p.scope FROM PROGRAM p
            JOIN SCHOOL s USING (school_id) WHERE s.name='Example University'""")}
        self.assertEqual(programs, {("Undergraduate (school-wide)", "undergraduate", "school_wide"),
                                    ("Graduate School of Arts and Sciences", "graduate", "division"),
                                    ("School of Law JD", "professional", "division")})
        self.assertEqual({k: v for k, v in self.tuition().items() if k[0] == "School of Law JD"},
                         {("School of Law JD", "2026/27", None): (76000, "USD")})
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM admin.REVIEW_QUEUE").fetchone()[0], 0)


class TuitionMigrationTests(unittest.TestCase):
    def test_007_keeps_tuition_rows_and_evidence(self):
        with TemporaryDirectory(prefix="agent1-mig-") as directory:
            old = Path(directory) / "migrations"
            old.mkdir()
            for file in sorted(storage.MIGRATIONS.glob("[0-9]*.sql")):
                if file.stem <= "006_tuition":
                    (old / file.name).write_text(file.read_text(encoding="utf-8"), encoding="utf-8")
            paths = paths_for(Path(directory), "development")
            with patch("agent1.storage.MIGRATIONS", old), \
                    patch("agent1.storage.REQUIRED_MIGRATIONS",
                          storage.REQUIRED_MIGRATIONS[:storage.REQUIRED_MIGRATIONS.index("006_tuition") + 1]):
                storage.initialize(paths)
            db = sqlite3.connect(paths.database)
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("INSERT INTO SCHOOL (school_id, name, country) VALUES ('s','S','UK')")
            db.execute("INSERT INTO PROGRAM (program_id, school_id, name) VALUES ('p','s','P')")
            db.execute("""INSERT INTO TUITION (tuition_id, program_id, academic_year, amount, currency, fee_status,
                fee_basis) VALUES ('t','p','2026/27',42700,'GBP','international','per_year')""")
            db.execute("""INSERT INTO TUITION_EVIDENCE (evidence_id, tuition_id, kind, source_id, snapshot_id, quote,
                recorded_at) VALUES ('e','t','amount','src','snap','£42,700','now')""")
            db.commit()
            db.close()
            storage.initialize(paths)
            db = sqlite3.connect(paths.database)
            db.execute("PRAGMA foreign_keys=ON")
            self.assertEqual(db.execute("SELECT amount, fee_status FROM TUITION").fetchall(),
                             [(42700, "international")])
            self.assertEqual(db.execute("SELECT quote FROM TUITION_EVIDENCE").fetchall(), [("£42,700",)])
            db.execute("UPDATE TUITION SET fee_status='in_state'")
            db.execute("DELETE FROM TUITION")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM TUITION_EVIDENCE").fetchone()[0], 0)
            db.close()


if __name__ == "__main__":
    unittest.main()
