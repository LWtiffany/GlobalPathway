#!/usr/bin/env python3
"""Deterministic Oxbridge fee-table ingest into SCHOOL / PROGRAM / TUITION.

Oxford's official rates page and Cambridge's international UG fee-group table are
structured Markdown tables. Loading them directly (same ids / is_estimate=0 path
as scan-tuition) is faster and more complete than section-by-section LLM extract
for ~500 Oxford courses.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent1 import config, storage, tuition_extract, tuition_pipeline as tp  # noqa: E402
from agent1 import formal_pipeline as fp  # noqa: E402

OXFORD_URL = "https://www.ox.ac.uk/students/fees-funding/fees/rates"
CAMBRIDGE_UG_URL = (
    "https://www.undergraduate.study.cam.ac.uk/international-students/international-fees-and-costs"
)

OXFORD_ROW = re.compile(
    r"^\|\s*(.+?)\s*\|\s*([A-Za-z][A-Za-z./-]*)\s*\(([^)]+)\)\s*\|\s*"
    r"(£[\d,]+|N/A)\s*\|\s*(£[\d,]+|N/A)\s*\|?\s*$",
    re.MULTILINE,
)
MONEY = re.compile(r"£([\d,]+)")
CAM_GROUP = re.compile(
    r"\|\s*Group\s*(\d)\s*\|\s*(?:Courses\s*)?(.+?)\s*\|\s*(?:Tuition Fee\s*)?£?([\d,]+)\s*\|",
    re.IGNORECASE,
)

UG_AWARDS = {
    "BA", "BFA", "BTh", "MBiochem", "MBiol", "MChem", "MCompPhil", "MCompSci",
    "MEarthSci", "MEng", "MMath", "MMathCompSci", "MMathPhil", "MPhys", "MPhysPhil",
}
PGR_AWARDS = {"DPhil", "EngD"}
# Everything else with a taught/research award token defaults to PGT.


def _money(cell: str) -> int | None:
    if not cell or cell.strip().upper() == "N/A":
        return None
    match = MONEY.search(cell)
    if not match:
        return None
    amount = int(match.group(1).replace(",", ""))
    # Oxford marks some CDT rows as £0 when no fee applies / not offered.
    return amount if amount > 0 else None


def _oxford_level(award: str) -> str:
    if award in UG_AWARDS:
        return "undergraduate"
    if award in PGR_AWARDS:
        return "postgraduate_research"
    return "postgraduate_taught"


def _oxford_mode(course: str) -> str:
    return "part_time" if re.search(r"\bPT\b|\(PT\)", course, re.I) else "full_time"


def parse_oxford(markdown: str, academic_year: str = "2027/28"):
    rows = []
    for match in OXFORD_ROW.finditer(markdown):
        course, award, sits, home, overseas = match.groups()
        course = re.sub(r"\s+", " ", course).strip(" *")
        name = f"{course} {award}".strip()
        level = _oxford_level(award)
        mode = _oxford_mode(course)
        key = tuition_extract.program_key_for(name)
        for status, cell in (("home", home), ("international", overseas)):
            amount = _money(cell)
            if amount is None:
                continue
            quote = f"| {course} | {award} ({sits}) | {home} | {overseas} |"
            rows.append({
                "program_key": key,
                "program_name": name,
                "level": level,
                "scope": "programme",
                "academic_year": academic_year,
                "fee_status": status,
                "fee_basis": "per_year",
                "study_mode": mode,
                "amount": amount,
                "currency": "GBP",
                "source_url": OXFORD_URL,
                "quote": quote,
            })
    return rows


def parse_cambridge_ug(markdown: str, academic_year: str = "2027/28"):
    rows = []
    for match in CAM_GROUP.finditer(markdown):
        group, courses_blob, fee = match.groups()
        amount = int(fee.replace(",", ""))
        courses_blob = re.sub(r"^Courses\s*", "", courses_blob.strip())
        courses = [c.strip() for c in courses_blob.split(";") if c.strip()]
        for course in courses:
            # Drop Cambridge's long parenthetical clarifier on Medicine/Vet.
            name = re.sub(r"\s*\(including for this purpose.*?\)\s*$", "", course, flags=re.I).strip()
            key = tuition_extract.program_key_for(name)
            quote = f"Group {group}: {course} — £{fee}"
            rows.append({
                "program_key": key,
                "program_name": name,
                "level": "undergraduate",
                "scope": "programme",
                "academic_year": academic_year,
                "fee_status": "international",
                "fee_basis": "per_year",
                "study_mode": "full_time",
                "amount": amount,
                "currency": "GBP",
                "source_url": CAMBRIDGE_UG_URL,
                "quote": quote,
            })
    return rows


def _load_markdown(path: Path | None, url: str) -> str:
    if path and path.exists():
        return path.read_text(encoding="utf-8")
    # Prefer the research probe / prior WebFetch cache when present.
    candidates = [
        ROOT / "data/_research/probe/oxford__0.md",
        Path("/Users/lilyyang/.cursor/projects/Users-lilyyang-GlobalPathway/agent-tools/"
             "32e8b366-daa3-4f2b-a5fe-2cd79c9502cb.txt"),
    ]
    if "cambridge" in url:
        candidates = []
    for candidate in candidates:
        if candidate.exists() and candidate.stat().st_size > 1000:
            text = candidate.read_text(encoding="utf-8")
            if "Advanced Computer Science" in text or "Overseas" in text:
                return text
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "GlobalPathwayTuitionBot/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8", errors="replace")


def upsert_rows(school_meta: dict, rows: list[dict]) -> dict:
    inserted = updated = unchanged = 0
    with storage.session(readonly=False) as connection:
        sid = tp.resolve_school_id(connection, school_meta)
        if not fp._row(connection, "SELECT school_id FROM SCHOOL WHERE school_id=%s", (sid,)):
            connection.execute(
                """INSERT INTO SCHOOL (school_id, name, country, city) VALUES (%s,%s,%s,%s)""",
                (sid, tp._canonical_school_name(school_meta["name"]) or school_meta["name"],
                 school_meta["country"], school_meta.get("city")),
            )
        school_key = school_meta["key"]
        for row in rows:
            tp._upsert_program(
                connection, sid, school_key, row["program_key"], row["program_name"],
                row["level"], row["scope"],
            )
            pid = tp.program_id(school_key, row["program_key"])
            tid = tp.tuition_id(
                pid, row["academic_year"], row["fee_status"], row["fee_basis"],
                row["study_mode"], row.get("rate_label"),
            )
            existing = fp._row(connection, "SELECT * FROM TUITION WHERE tuition_id=%s", (tid,))
            payload = {
                "tuition_id": tid,
                "program_id": pid,
                "academic_year": row["academic_year"],
                "fee_status": row["fee_status"],
                "fee_basis": row["fee_basis"],
                "study_mode": row["study_mode"],
                "rate_label": row.get("rate_label"),
                "amount": row["amount"],
                "currency": row["currency"],
                "remark": "官网费率表结构化入库（非估算）",
                "is_estimate": 0,
                "source_url": row["source_url"],
            }
            if existing is None:
                cols = list(payload)
                connection.execute(
                    f"INSERT INTO TUITION ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
                    [payload[c] for c in cols],
                )
                inserted += 1
            elif tp._same_amount(existing, row["amount"], row["currency"]):
                connection.execute(
                    "UPDATE TUITION SET is_estimate=0, source_url=%s, remark=%s, rate_label=%s "
                    "WHERE tuition_id=%s",
                    (row["source_url"], payload["remark"], row.get("rate_label"), tid),
                )
                unchanged += 1
            else:
                connection.execute(
                    "UPDATE TUITION SET amount=%s, currency=%s, is_estimate=0, source_url=%s, "
                    "remark=%s, rate_label=%s WHERE tuition_id=%s",
                    (row["amount"], row["currency"], row["source_url"], payload["remark"],
                     row.get("rate_label"), tid),
                )
                updated += 1
    return {"school": school_key, "rows": len(rows), "inserted": inserted,
            "updated": updated, "unchanged": unchanged}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--oxford-md", type=Path)
    parser.add_argument("--cambridge-md", type=Path)
    parser.add_argument("--skip-oxford", action="store_true")
    parser.add_argument("--skip-cambridge", action="store_true")
    args = parser.parse_args()
    results = []
    if not args.skip_oxford:
        md = _load_markdown(args.oxford_md, OXFORD_URL)
        rows = parse_oxford(md)
        if not rows:
            raise SystemExit("Oxford: parsed 0 fee rows")
        results.append(upsert_rows(
            {"key": "oxford", "name": "University of Oxford", "country": "UK", "city": "Oxford"},
            rows,
        ))
    if not args.skip_cambridge:
        md = _load_markdown(args.cambridge_md, CAMBRIDGE_UG_URL)
        if args.cambridge_md is None:
            # Always fetch Cambridge UG page (probe is Oxford-only).
            import urllib.request
            req = urllib.request.Request(
                CAMBRIDGE_UG_URL, headers={"User-Agent": "GlobalPathwayTuitionBot/1.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                html = resp.read().decode("utf-8", errors="replace")
            # Prefer plain text-ish content; regex works on the WebFetch markdown form too.
            md = html
            # Also try crawl4ai-style conversion if groups not found.
            if not CAM_GROUP.search(md):
                from agent1 import crawl
                capture = crawl.fetch_markdown(CAMBRIDGE_UG_URL) if hasattr(crawl, "fetch_markdown") else None
                if capture:
                    md = capture
        rows = parse_cambridge_ug(md)
        if not rows:
            # Fall back to the WebFetch markdown saved by the agent if HTML parse failed.
            saved = Path(
                "/Users/lilyyang/.cursor/projects/Users-lilyyang-GlobalPathway/agent-tools")
            for path in sorted(saved.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
                text = path.read_text(encoding="utf-8", errors="replace")
                if "Group 1" in text and "Anglo-Saxon" in text:
                    rows = parse_cambridge_ug(text)
                    if rows:
                        break
        if not rows:
            raise SystemExit("Cambridge UG: parsed 0 fee rows")
        results.append(upsert_rows(
            {"key": "cambridge", "name": "University of Cambridge", "country": "UK",
             "city": "Cambridge"},
            rows,
        ))
    import json
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
