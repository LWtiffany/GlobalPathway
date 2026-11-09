#!/usr/bin/env python3
"""Walk every bachelor/master/phd program in RDS for a school and attach official tuition.

Rate rules come from each university's published fee tables (not CIP estimates).
CIP / directory names are mapped to the published bucket that covers that field.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent1 import formal_pipeline as fp  # noqa: E402
from agent1 import storage, tuition_pipeline as tp  # noqa: E402

YEAR = "2026/27"


def _add(rows, amount, basis, url, remark, rate=None, mode="full_time", status=None, year=YEAR):
    rows.append({
        "amount": amount, "fee_basis": basis, "source_url": url, "remark": remark,
        "rate_label": rate, "study_mode": mode, "fee_status": status, "academic_year": year,
        "currency": "USD",
    })
    return rows


def harvard(name: str, level: str) -> list[dict]:
    n = name.casefold()
    rows: list[dict] = []
    ug = "https://college.harvard.edu/financial-aid/how-aid-works"
    gsas = "https://gsas.harvard.edu/apply/cost-attendance-2026-2027"
    fas = "https://registrar.fas.harvard.edu/tuition-and-fees"
    if level in {"bachelor", "undergraduate"}:
        return _add(rows, 62226, "per_year", ug, "Harvard College tuition 2026-27 (official COA tuition line)")
    # SEAS special master's
    if level == "master" and (
        "computational science" in n or "data science" in n
        or "computer engineering" in n and "software" not in n
    ):
        _add(rows, 67504, "per_year", fas, "Harvard FAS/GSAS SEAS master's tuition (CSE / Data Science table)")
        _add(rows, 33752, "per_year", fas, "SEAS master's G2 / second-year reduced table rate", rate="G2 / second year")
        return rows
    if "law" in n or "legal research" in n:
        return _add(rows, 84400, "per_year", "https://hls.harvard.edu/sfs/prospective-and-admitted-students/budgeting-for-law-school/",
                    "Harvard Law School JD/LLM tuition 2026-27 ($84,400)")
    if level in {"master", "phd"}:
        _add(rows, 59048, "per_year", fas, "Harvard GSAS / FAS standard full tuition 2026-27")
        _add(rows, 15352, "per_year", gsas, "Harvard GSAS reduced tuition 2026-27", rate="Reduced Tuition")
        if level == "phd":
            _add(rows, 3906, "per_year", gsas, "Harvard GSAS continuation fee", rate="Continuation Fee")
        return rows
    return rows


def yale(name: str, level: str) -> list[dict]:
    n = name.casefold()
    rows: list[dict] = []
    url = "https://student-accounts.yale.edu/tuition-and-fees"
    if level in {"bachelor", "undergraduate"}:
        return _add(rows, 72500, "per_year", url, "Yale College tuition 2026-27")
    # Professional / school mapping by CIP keywords
    if any(k in n for k in ("architecture",)):
        return _add(rows, 66660, "per_year", url, "Yale School of Architecture full-time tuition 2026-27")
    if any(k in n for k in ("drama", "acting", "playwriting", "theater", "theatre")):
        return _add(rows, 40750, "per_year", url, "Yale Drama School full-time tuition 2026-27")
    if any(k in n for k in ("art history",)) and level == "master":
        return _add(rows, 52400, "per_year", url, "Yale GSAS full-time tuition 2026-27 (Art History is GSAS)")
    if re.search(r"\bart\b|fine arts|sculpture|painting|photography|design$", n) and "history" not in n:
        return _add(rows, 50198, "per_year", url, "Yale School of Art full-time tuition 2026-27")
    if any(k in n for k in ("divinity", "theology", "bible", "religion", "ministerial", "sacred music")):
        return _add(rows, 31192, "per_year", url, "Yale Divinity School full-time tuition 2026-27")
    if any(k in n for k in ("environment", "forestry", "environmental science", "environmental studies")):
        return _add(rows, 55960, "per_year", url, "Yale School of the Environment full-time tuition 2026-27")
    if any(k in n for k in ("law", "legal")):
        return _add(rows, 79318, "per_year", url, "Yale Law School JD/LLM/MSL full-time tuition 2026-27")
    if any(k in n for k in ("business", "management", "finance", "marketing", "accounting")):
        return _add(rows, 90900, "per_year", url, "Yale SOM Residential Masters tuition 2026-27")
    if any(k in n for k in ("medicine", "physician", "medical")):
        return _add(rows, 76700, "per_year", url, "Yale School of Medicine tuition 2026-27")
    if any(k in n for k in ("nursing",)):
        return _add(rows, 54335, "per_year", url, "Yale School of Nursing MSN full-time tuition 2026-27")
    if any(k in n for k in ("public health", "epidemiology", "biostatistics")):
        return _add(rows, 57256, "per_year", url, "Yale School of Public Health Two-Year MPH tuition 2026-27")
    if any(k in n for k in ("music",)):
        return _add(rows, 42000, "per_year", url, "Yale School of Music full-time tuition 2026-27")
    if any(k in n for k in ("physician associate", "physician assistant")):
        return _add(rows, 54147, "per_year", url, "Yale Physician Associate Program tuition 2026-27")
    if any(k in n for k in ("global affairs", "international relations", "international affairs")):
        return _add(rows, 65100, "per_year", url, "Yale Jackson School of Global Affairs tuition 2026-27")
    # Default GSAS
    if level in {"master", "phd"}:
        _add(rows, 52400, "per_year", url, "Yale Graduate School of Arts and Sciences full-time tuition 2026-27")
        if level == "phd":
            _add(rows, 1760, "per_year", url, "Yale GSAS continuing registration fee", rate="Continuing Registration")
        return rows
    return rows


def princeton(name: str, level: str) -> list[dict]:
    rows: list[dict] = []
    ug = "https://finaid.princeton.edu/how-aid-works/student-budget"
    gr = "https://gradschool.princeton.edu/financial-support/financial-support-model/funding-calendar-university-rates-costs"
    if level in {"bachelor", "undergraduate"}:
        return _add(rows, 68140, "per_year", ug, "Princeton undergraduate tuition 2026-27")
    if level in {"master", "phd", "graduate"}:
        _add(rows, 65210, "per_year", gr, "Princeton Graduate School regular tuition 2026-27")
        _add(rows, 4700, "per_year", gr, "Princeton Graduate School marginal rate (DCE / in absentia)", rate="Marginal Rate")
        return rows
    return rows


_ENGINEERING = re.compile(
    r"engineering|computer science|aeronautical|aerospace|bioengineering|biomedical engineering|"
    r"chemical engineering|civil engineering|electrical|materials|mechanical|petroleum|"
    r"environmental.*engineering|systems engineering|management science and engineering",
    re.I,
)


def stanford(name: str, level: str) -> list[dict]:
    n = name.casefold()
    rows: list[dict] = []
    ug = "https://studentservices.stanford.edu/tuition-rates/2026-2027-undergraduate-tuition-rates"
    gr = "https://studentservices.stanford.edu/tuition-rates/2026-2027-graduate-and-professional-tuition-rates"
    if level in {"bachelor", "undergraduate"}:
        _add(rows, 22577, "per_term", ug, "Stanford undergraduate full-time quarterly tuition 2026-27")
        _add(rows, 67731, "per_year", "https://financialaid.stanford.edu/undergrad/budget/",
             "Stanford undergraduate tuition in COA 2026-27")
        return rows
    if any(k in n for k in ("business administration", "management and operations")) and level == "master":
        _add(rows, 29729, "per_term", gr, "Stanford GSB MBA first-year quarterly tuition 2026-27", rate="First year (GSB)")
        _add(rows, 28585, "per_term", gr, "Stanford GSB MBA second-year quarterly tuition 2026-27", rate="Second year (GSB)")
        return rows
    if "law" in n or "legal research" in n:
        return _add(rows, 26593, "per_term", gr, "Stanford Law School quarterly tuition 2026-27")
    if any(k in n for k in ("medicine", "physician", "medical scientist", "md ")):
        return _add(rows, 24034, "per_term", gr, "Stanford MD regular enrollment quarterly tuition 2026-27")
    if level in {"master", "phd"} and _ENGINEERING.search(n):
        _add(rows, 23239, "per_term", gr, "Stanford Graduate School of Engineering 11-18 units quarterly 2026-27",
             rate="11-18 units")
        _add(rows, 15100, "per_term", gr, "Stanford GSE 8-10 units quarterly 2026-27", rate="8-10 units")
        return rows
    if level in {"master", "phd"}:
        _add(rows, 21815, "per_term", gr, "Stanford Graduate School 11-18 units quarterly tuition 2026-27",
             rate="11-18 units")
        _add(rows, 14180, "per_term", gr, "Stanford Graduate School 8-10 units quarterly 2026-27", rate="8-10 units")
        return rows
    return rows


def _hkd(rows):
    for r in rows:
        r["currency"] = "HKD"
    return rows


def hkust(name: str, level: str) -> list[dict]:
    rows: list[dict] = []
    ug = "https://registry.hkust.edu.hk/resource-library/tuition-rates-ug"
    rpg = "https://registry.hkust.edu.hk/resource-library/tuition-rates-research-pg"
    adm = "https://join.hkust.edu.hk/fees-and-scholarships"
    # Known taught MSc fees published on program pages (2025/26–2026/27 intake refs).
    tpg = {
        "msc in business analytics": (420000, "https://prog-crs.ust.hk/pgprog/2025-26/msc-ba"),
        "msc in big data technology (bdt)": (243000, "https://prog-crs.ust.hk/pgprog/2025-26/msc-bdt"),
        "msc in aeronautical engineering (ae)": (210000, "https://prog-crs.ust.hk/pgprog/2025-26/msc-ae"),
        "msc in biomolecular engineering and health informatics (behi)": (
            210000, "https://prog-crs.ust.hk/pgprog/2025-26/msc-behi"),
        "msc in chemical and energy engineering (cee)": (210000, "https://prog-crs.ust.hk/pgprog/2025-26/msc-cee"),
    }
    if level in {"bachelor", "undergraduate"}:
        _add(rows, 47000, "per_year", ug, "HKUST UG local tuition (Registry)", status="local", year="2026/27")
        _add(rows, 215000, "per_year", ug, "HKUST UG non-local 2026-27 cohort (Registry)", status="non_local", year="2026/27")
        _add(rows, 49500, "per_year", adm, "HKUST UG local 2027/28 (Admissions)", status="local", year="2027/28")
        _add(rows, 260000, "per_year", adm, "HKUST UG non-local 2027/28 (Admissions)", status="non_local", year="2027/28")
        return _hkd(rows)
    if level in {"phd", "postgraduate_research"} or name.casefold().startswith("mphil"):
        _add(rows, 47000, "per_year", rpg, "HKUST Research PG tuition 2026-27 (Registry)", mode="full_time")
        return _hkd(rows)
    if level == "master":
        key = name.casefold().strip()
        if key in tpg:
            amount, url = tpg[key]
            _add(rows, amount, "total", url, f"HKUST taught PG program fee for {name} (program catalog)")
            return _hkd(rows)
        return []
    return rows


def hku(name: str, level: str) -> list[dict]:
    rows: list[dict] = []
    ug = "https://www.admissions.hku.hk/fees-and-scholarships/fees"
    cal = "https://calendar.hku.hk/schedule-of-fees/"
    n = name.casefold()
    if level in {"bachelor", "undergraduate"}:
        if any(k in n for k in ("medicine", "surgery", "mbbs", "chinese medicine", "nursing", "dental")):
            # MBBS / health often STEM or special; admissions table uses STEM non-local or MBBS line
            _add(rows, 47000, "per_year", ug, "HKU UG local tuition", status="local", year="2026/27")
            _add(rows, 249000, "per_year", ug, "HKU UG non-local STEM / clinical band 2026/27", status="non_local", year="2026/27")
        elif any(k in n for k in ("engineering", "artificial intelligence", "data", "robotics", "science")):
            _add(rows, 47000, "per_year", ug, "HKU UG local tuition", status="local", year="2026/27")
            _add(rows, 249000, "per_year", ug, "HKU UG non-local STEM 2026/27", status="non_local", year="2026/27")
        else:
            _add(rows, 47000, "per_year", ug, "HKU UG local tuition", status="local", year="2026/27")
            _add(rows, 224000, "per_year", ug, "HKU UG non-local non-STEM 2026/27", status="non_local", year="2026/27")
        return _hkd(rows)
    if level in {"phd", "postgraduate_research"} or "mphil" in n:
        _add(rows, 47000, "per_year", cal, "HKU MPhil/PhD full-time composition (local rate on calendar; non-local same band for RPg UGC)", status="local")
        _add(rows, 47000, "per_year", cal, "HKU MPhil/PhD full-time composition fee", status="non_local")
        return _hkd(rows)
    return rows


def cuhk(name: str, level: str) -> list[dict]:
    rows: list[dict] = []
    res = "https://www.res.cuhk.edu.hk/general-information/tuition-and-other-fees/"
    gs = "https://www.gs.cuhk.edu.hk/admissions/scholarships-fees/fees"
    if level in {"bachelor", "undergraduate"}:
        _add(rows, 47000, "per_year", res, "CUHK UG local tuition 2026-27", status="local")
        _add(rows, 214000, "per_year", res, "CUHK UG non-local 2026-27 intake", status="non_local",
             rate="2026-27 intake")
        return _hkd(rows)
    if level in {"phd", "postgraduate_research"} or name.casefold().startswith("mphil"):
        _add(rows, 47000, "per_year", gs, "CUHK Research Postgraduate tuition 2026-27")
        return _hkd(rows)
    return rows


_CAM_G1 = re.compile(
    r"anglo-saxon|archaeology|asian and middle eastern|classics|economics|education|english|"
    r"history|human, social|law|linguistics|modern and medieval|philosophy|theology|"
    r"politics|land economy", re.I)
_CAM_G2 = re.compile(r"^mathematics$|mathematics,", re.I)
_CAM_G3 = re.compile(r"architecture|design|geography|music", re.I)
_CAM_G4 = re.compile(
    r"chemical engineering|computer science|engineering|management studies|"
    r"manufacturing|natural sciences|psychological|behavioural", re.I)
_CAM_G5 = re.compile(r"medicine|veterinary|medical", re.I)


def cambridge(name: str, level: str) -> list[dict]:
    rows: list[dict] = []
    url = "https://www.undergraduate.study.cam.ac.uk/international-students/international-fees-and-costs"
    if level not in {"bachelor", "undergraduate"}:
        return []  # PG fees scraped from each course /finance page in companion script
    n = name.casefold()
    if _CAM_G5.search(n):
        fee, group = 70554, 5
    elif _CAM_G4.search(n):
        fee, group = 46872, 4
    elif _CAM_G2.search(n) or n.strip() in {"mathematics"}:
        fee, group = 34356, 2
    elif _CAM_G3.search(n):
        fee, group = 40296, 3
    elif _CAM_G1.search(n):
        fee, group = 30798, 1
    else:
        return []
    _add(rows, fee, "per_year", url,
         f"Cambridge UG international tuition 2027 entry Fee Group {group}", status="international", year="2027/28")
    for r in rows:
        r["currency"] = "GBP"
    return rows


SCHOOLS = {
    "Harvard University": harvard,
    "Yale University": yale,
    "Princeton University": princeton,
    "Stanford University": stanford,
    "The Hong Kong University of Science and Technology": hkust,
    "The University of Hong Kong": hku,
    "The Chinese University of Hong Kong": cuhk,
    "University of Cambridge": cambridge,
}


def upsert(connection, program_id, price):
    tid = tp.tuition_id(
        program_id, price["academic_year"], price.get("fee_status"),
        price["fee_basis"], price.get("study_mode"), price.get("rate_label"),
    )
    row = {
        "tuition_id": tid, "program_id": program_id,
        "academic_year": price["academic_year"],
        "fee_status": price.get("fee_status"),
        "fee_basis": price["fee_basis"],
        "study_mode": price.get("study_mode"),
        "rate_label": price.get("rate_label"),
        "amount": price["amount"],
        "currency": price.get("currency") or "USD",
        "remark": price["remark"],
        "is_estimate": 0,
        "source_url": price["source_url"],
    }
    existing = fp._row(connection, "SELECT * FROM TUITION WHERE tuition_id=%s", (tid,))
    if existing is None:
        cols = list(row)
        connection.execute(
            f"INSERT INTO TUITION ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
            [row[c] for c in cols])
        return "inserted"
    if tp._same_amount(existing, row["amount"], row["currency"]):
        connection.execute(
            "UPDATE TUITION SET is_estimate=0, source_url=%s, remark=%s WHERE tuition_id=%s",
            (row["source_url"], row["remark"], tid))
        return "unchanged"
    connection.execute(
        "UPDATE TUITION SET amount=%s, currency=%s, is_estimate=0, source_url=%s, remark=%s, "
        "fee_basis=%s, study_mode=%s, rate_label=%s WHERE tuition_id=%s",
        (row["amount"], row["currency"], row["source_url"], row["remark"],
         row["fee_basis"], row["study_mode"], row["rate_label"], tid))
    return "updated"


def walk_school(school_name: str) -> dict:
    classify = SCHOOLS[school_name]
    summary = {"school": school_name, "programs": 0, "inserted": 0, "updated": 0,
               "unchanged": 0, "skipped": 0, "sample_special": []}
    with storage.session(readonly=False) as connection:
        sid = connection.execute(
            "SELECT school_id FROM school WHERE name=%s", (school_name,)).fetchone()["school_id"]
        programs = connection.execute(
            """SELECT program_id, name, level FROM program
               WHERE school_id=%s AND level IN
                 ('bachelor','undergraduate','master','phd','graduate','postgraduate_taught',
                  'postgraduate_research','professional')
               ORDER BY level, name""", (sid,)).fetchall()
        summary["programs"] = len(programs)
        for p in programs:
            prices = classify(p["name"], p["level"])
            if not prices:
                summary["skipped"] += 1
                continue
            for price in prices:
                action = upsert(connection, p["program_id"], price)
                summary[action] += 1
            # collect a few non-default amounts
            if any(pr["amount"] not in {62226, 59048, 15352, 3906, 72500, 52400, 1760, 68140, 65210, 4700,
                                        22577, 67731, 21815, 14180, 23239, 15100, 47000, 215000, 49500, 260000}
                   for pr in prices):
                if len(summary["sample_special"]) < 12:
                    summary["sample_special"].append({
                        "name": p["name"], "level": p["level"],
                        "amounts": [pr["amount"] for pr in prices],
                    })
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("schools", nargs="*", default=list(SCHOOLS))
    args = parser.parse_args()
    out = []
    for name in args.schools:
        if name not in SCHOOLS:
            raise SystemExit(f"unsupported school: {name}; choose from {list(SCHOOLS)}")
        out.append(walk_school(name))
    path = ROOT / "data/_research/tuition_scan_logs/walk_schools_summary.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
