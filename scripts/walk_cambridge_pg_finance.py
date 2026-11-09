#!/usr/bin/env python3
"""Walk Cambridge master/phd programs via gaobase course JSON → RDS tuition."""
from __future__ import annotations

import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent1 import formal_pipeline as fp  # noqa: E402
from agent1 import storage, tuition_pipeline as tp  # noqa: E402

SCHOOL = "University of Cambridge"
YEAR = "2026/27"
# 2027 entry cycle mirrors current prospectus widget year used on course pages.
GAOBASE = "https://2027.gaobase.admin.cam.ac.uk/api/courses/{code}"
HEADERS = {
    "User-Agent": "GlobalPathwayTuitionBot/1.0 (+research)",
    "Accept": "application/json",
}
CODE_RE = re.compile(r"/courses/directory/([A-Za-z0-9]+)(?:/|$)", re.I)


def course_code(source_url: str | None) -> str | None:
    if not source_url:
        return None
    m = CODE_RE.search(source_url)
    return m.group(1).upper() if m else None


def extract_ucf(data: dict) -> list[tuple[str, int]]:
    finance = data.get("finance") or {}
    ucfs = finance.get("ucfs") or []
    out: list[tuple[str, int]] = []
    seen: set[str] = set()
    for item in ucfs:
        ucf = (item or {}).get("ucf") or {}
        # DB check allows home|international (not overseas).
        for status, key in (("home", "home"), ("international", "overseas")):
            raw = ucf.get(key)
            if raw is None:
                continue
            try:
                amount = int(raw)
            except (TypeError, ValueError):
                continue
            if amount <= 0:
                continue
            tag = f"{status}:{amount}"
            if tag in seen:
                continue
            seen.add(tag)
            out.append((status, amount))
        if out:
            break  # first UCF band is the published composition fee
    return out


def fetch_one(client: httpx.Client, program: dict) -> dict:
    code = course_code(program.get("source_url"))
    result = {
        "program_id": program["program_id"],
        "name": program["name"],
        "level": program["level"],
        "code": code,
        "fees": [],
        "error": None,
        "source_url": None,
    }
    if not code:
        result["error"] = "no_course_code"
        return result
    url = GAOBASE.format(code=code)
    result["source_url"] = url
    try:
        r = client.get(url, follow_redirects=True, timeout=30.0)
        if r.status_code != 200:
            result["error"] = f"http_{r.status_code}"
            return result
        fees = extract_ucf(r.json())
        if not fees:
            result["error"] = "no_ucf"
            return result
        result["fees"] = fees
        return result
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)[:200]
        return result


def upsert(connection, program_id, amount, status, url, name):
    tid = tp.tuition_id(program_id, YEAR, status, "per_year", "full_time", None)
    row = {
        "tuition_id": tid, "program_id": program_id,
        "academic_year": YEAR, "fee_status": status, "fee_basis": "per_year",
        "study_mode": "full_time", "rate_label": None,
        "amount": amount, "currency": "GBP",
        "remark": f"Cambridge PG {status} University Composition Fee (gaobase {YEAR}) — {name}",
        "is_estimate": 0, "source_url": url,
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
        "UPDATE TUITION SET amount=%s, currency=%s, is_estimate=0, source_url=%s, remark=%s WHERE tuition_id=%s",
        (row["amount"], row["currency"], row["source_url"], row["remark"], tid))
    return "updated"


def main():
    summary = {"programs": 0, "fetched_ok": 0, "inserted": 0, "updated": 0,
               "unchanged": 0, "errors": 0, "error_samples": []}
    with storage.session(readonly=True) as connection:
        sid = connection.execute(
            "SELECT school_id FROM school WHERE name=%s", (SCHOOL,)).fetchone()["school_id"]
        programs = connection.execute(
            """SELECT program_id, name, level, source_url FROM program
               WHERE school_id=%s AND level IN ('master','phd','graduate','postgraduate_taught',
                 'postgraduate_research','professional')
               ORDER BY level, name""", (sid,)).fetchall()
        programs = [dict(p) for p in programs]
    summary["programs"] = len(programs)

    results = []
    with httpx.Client(headers=HEADERS, http2=False) as client:
        with ThreadPoolExecutor(max_workers=10) as pool:
            futs = [pool.submit(fetch_one, client, p) for p in programs]
            for i, fut in enumerate(as_completed(futs), 1):
                results.append(fut.result())
                if i % 50 == 0:
                    print(f"fetched {i}/{len(programs)}", flush=True)

    with storage.session(readonly=False) as connection:
        for res in results:
            if res["error"] or not res["fees"]:
                summary["errors"] += 1
                if len(summary["error_samples"]) < 20:
                    summary["error_samples"].append({
                        "name": res["name"], "error": res["error"], "code": res["code"]})
                continue
            summary["fetched_ok"] += 1
            for status, amount in res["fees"]:
                action = upsert(
                    connection, res["program_id"], amount, status, res["source_url"], res["name"])
                summary[action] += 1

    path = ROOT / "data/_research/tuition_scan_logs/cambridge_pg_finance_walk.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
