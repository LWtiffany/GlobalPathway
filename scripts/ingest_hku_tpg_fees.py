#!/usr/bin/env python3
"""Load HKU taught-postgraduate composition fees from the official handbook PDF text.

Source: https://handbook.hku.hk/tpg/pdf/2026/2026_PG_FEES_COMPOSITION%20FEES_final%202.pdf
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent1 import tuition_extract  # noqa: E402
from scripts.ingest_oxbridge_tuition import upsert_rows  # noqa: E402

SOURCE_URL = "https://handbook.hku.hk/tpg/pdf/2026/2026_PG_FEES_COMPOSITION%20FEES_final%202.pdf"
ACADEMIC_YEAR = "2026/27"

# Lines / fragments that introduce a curriculum name.
SKIP = re.compile(
    r"^(Architecture|Arts|Business and Economics|Dentistry|Education|Engineering|"
    r"School of Computing and Data Science|School of Innovation|Law|Medicine|Science|"
    r"Social Sciences|School of Future Media|School of Governance and Policy|"
    r"Curriculum|Full-time|Part-time|COMPOSITION|FEES|For |According|All of|"
    r"RMB:|STUDENT ACTIVITY|A\.|B\.|specialist strand|otherspecialist|Admission suspended|"
    r"payable|instalments|Subject to|Renminbi|Local students|Non-local)",
    re.I,
)
MONEY = re.compile(
    r"\$\s*([\d,]+)\s*(?:p\.a\.|per programme|per semester|for the entire programme)?",
    re.I,
)
LOCAL = re.compile(r"local students", re.I)
NONLOCAL = re.compile(r"non-local students", re.I)
PER_SEM = re.compile(r"per semester", re.I)
PER_PROG = re.compile(r"per programme|for the entire programme|payable", re.I)
PER_YEAR = re.compile(r"p\.a\.", re.I)


def _clean_name(name: str) -> str:
    name = re.sub(r"\s+", " ", name).strip(" -–—|")
    name = re.sub(r"\*+$", "", name).strip()
    return name


def _parse_blob(name: str, blob: str) -> list[dict]:
    name = _clean_name(name)
    if len(name) < 3 or SKIP.match(name):
        return []
    if "Admission suspended" in blob:
        return []
    rows = []
    # local / non-local pairs on same blob
    pairs = re.findall(
        r"\$\s*([\d,]+)[^\n]{0,40}?\((?:for )?local students\)|"
        r"(?:for )?local students\)?\s*\$\s*([\d,]+)",
        blob, re.I,
    )
    # Simpler: find all (amount, nearby context) windows
    # Strategy: if both local and non-local mentioned with amounts, emit two rows.
    local_amts = re.findall(
        r"\$\s*([\d,]+)[^\n]{0,60}?local students|local students[^\n]{0,40}?\$\s*([\d,]+)",
        blob, re.I,
    )
    non_amts = re.findall(
        r"\$\s*([\d,]+)[^\n]{0,60}?non-local students|non-local students[^\n]{0,40}?\$\s*([\d,]+)",
        blob, re.I,
    )

    def first_amt(matches):
        for a, b in matches:
            raw = a or b
            if raw:
                return int(raw.replace(",", ""))
        return None

    la, na = first_amt(local_amts), first_amt(non_amts)
    basis = "per_term" if PER_SEM.search(blob) else (
        "total" if PER_PROG.search(blob) and not PER_YEAR.search(blob) else "per_year"
    )
    # semester fees: store as stated (per_term); caller may annualize later if needed
    if la and na and la != na:
        for status, amount in (("local", la), ("non_local", na)):
            rows.append(_row(name, amount, status, basis, blob))
        return rows
    if la and not na:
        return [_row(name, la, "local", basis, blob)]
    if na and not la:
        return [_row(name, na, "non_local", basis, blob)]

    # single / dual mode amounts without status
    amounts = [int(x.replace(",", "")) for x in MONEY.findall(blob)]
    amounts = [a for a in amounts if a >= 10000]
    if not amounts:
        return []
    # Prefer full-time first amount; if two large amounts and "Part-time" in blob after first,
    # treat as FT/PT without fee_status.
    if len(amounts) >= 2 and re.search(r"Part-time|part-time", blob):
        rows.append(_row(name, amounts[0], None, basis, blob, study_mode="full_time"))
        rows.append(_row(name, amounts[1], None, basis, blob, study_mode="part_time"))
        return rows
    rows.append(_row(name, amounts[0], None, basis, blob))
    return rows


def _row(name, amount, fee_status, fee_basis, blob, study_mode=None):
    if study_mode is None:
        if re.search(r"Part-time only|--\s*$", blob) and not re.search(r"Full-time", blob):
            # can't reliably infer
            study_mode = None
        elif re.search(r"\(Full-time\)|Full-time", name + blob) and not re.search(r"Part-time", blob):
            study_mode = "full_time"
        elif re.search(r"\(Part-time\)", name) or (
            re.search(r"Part-time", blob) and not re.search(r"Full-time", blob)
        ):
            study_mode = "part_time"
    level = "postgraduate_taught"
    if re.search(r"\bEdD\b|\bDNurs\b|\bDBA\b|\bPsyD|\bDPA\b|Doctor", name):
        level = "professional" if re.search(r"DBA|JD|MDS|EMBA", name) else "postgraduate_research"
    if re.search(r"\bJD\b|Juris Doctor|MDS\(|PCLL|EMBA|MBA\b", name):
        level = "professional"
    return {
        "program_key": tuition_extract.program_key_for(name),
        "program_name": name,
        "level": level if level in {
            "undergraduate", "postgraduate_taught", "postgraduate_research",
            "graduate", "professional",
        } else "postgraduate_taught",
        "scope": "programme",
        "academic_year": ACADEMIC_YEAR,
        "fee_status": fee_status,
        "fee_basis": fee_basis,
        "study_mode": study_mode,
        "amount": amount,
        "currency": "HKD",
        "source_url": SOURCE_URL,
        "quote": blob[:240],
    }


def parse_handbook_text(text: str) -> list[dict]:
    # Normalize PDF extraction noise
    text = text.replace("\u00a0", " ")
    # Cut to composition section B onwards if present
    start = text.find("B. For other curricula")
    if start < 0:
        start = text.find("COMPOSITION FEES")
    text = text[start:] if start >= 0 else text

    # Heuristic: curriculum codes / names followed by fee language on same or next lines.
    # Match known programme-like tokens at line starts.
    line_re = re.compile(
        r"^(?P<name>(?:M[A-Z][A-Za-z(&)/\- ]{1,80}|MA |MSc|MEd|MArch|MBA|EMBA|JD |"
        r"LLM|MCL|PCLL|PGDE|PDip|PsyD|DNurs|DBA|EdD|AdvDip|MDS|MAP |MBio|"
        r"MCHC|MChinMed|MCRPD|MMedSc|MNurs|MPH |MPsyMed|MRes|MIPA|MPA |"
        r"MExpArtsTh|MChDS|MCGRM|MTP&P|MACAIFM|MJ |MGP |MUS&HM|MDUM|MLA |"
        r"MUrbanDesign|PDLA|MBC |MBuddhStud|MFA |MAIB |MAcct|MAA |MEcon|"
        r"MFin |MFFinTech|MFWM |MGM |MSAF |MWM |MArch\(Design\))"
        r"[^\n]{0,120})",
        re.M,
    )
    rows: list[dict] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        m = line_re.match(line)
        if not m:
            i += 1
            continue
        name = m.group("name")
        # Gather following fee context (up to next curriculum-looking line or 6 lines)
        blob_lines = [line]
        j = i + 1
        while j < len(lines) and j <= i + 8:
            nxt = lines[j].strip()
            if line_re.match(nxt) and "$" not in nxt:
                break
            blob_lines.append(nxt)
            if nxt.count("$") >= 2 and j > i + 1:
                j += 1
                break
            j += 1
        blob = " ".join(blob_lines)
        if "$" not in blob:
            i += 1
            continue
        # Trim name to before first $
        name = re.split(r"\$", name, 1)[0].strip(" -–—|")
        name = re.sub(r"\s+", " ", name)
        parsed = _parse_blob(name, blob)
        rows.extend(parsed)
        i = max(j, i + 1)

    # Deduplicate by key dimensions keeping first
    seen = set()
    out = []
    for r in rows:
        key = (r["program_key"], r["academic_year"], r["fee_status"], r["fee_basis"],
               r["study_mode"], r.get("rate_label"))
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def main():
    # Prefer agent-tools fetch of the PDF (markdown), else re-fetch via urllib is not needed—
    # caller passes path or we read the last WebFetch content from stdin/file.
    candidates = sorted(
        Path("/Users/lilyyang/.cursor/projects/Users-lilyyang-GlobalPathway/agent-tools").glob("*.txt"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    text = None
    for path in candidates[:30]:
        raw = path.read_text(encoding="utf-8", errors="replace")
        if "COMPOSITION FEES" in raw and "MSc(CompSc)" in raw:
            text = raw
            break
    if not text:
        raise SystemExit("HKU TPg composition fee text not found in agent-tools cache")
    rows = parse_handbook_text(text)
    print(json.dumps({"parsed": len(rows), "sample": rows[:5]}, ensure_ascii=False, indent=2))
    result = upsert_rows(
        {"key": "hku", "name": "The University of Hong Kong (HKU)",
         "country": "CN-HK", "city": "Hong Kong"},
        rows,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
