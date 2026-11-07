"""Seed ACCOUNT_SOURCE_DOCUMENT / ACCOUNT_DOC_CHUNK and archive originals to OSS.

Usage:
  # Dry run (default): validate sources, print plan, no writes.
  python scripts/seed_account_source_docs.py

  # Apply to RDS (creates tables, upserts doc + chunk rows).
  python scripts/seed_account_source_docs.py --apply

  # Also upload archived markdown to OSS via ossutil (run on the team ECS,
  # where the instance role provides credentials automatically).
  python scripts/seed_account_source_docs.py --apply --upload-oss

Design:
- OSS holds the faithful archived page text (documents/hsbc/<doc_id>.md).
- RDS holds doc metadata (incl. oss_key + content sha256) and Chinese
  retrieval chunks; chunk numbers are kept verbatim from the source.
- account_key in the JSON is the logical id (e.g. hsbc_hk_premier); the
  script resolves the physical ACCOUNT_PRODUCT.account_id via RDS_ACCOUNT_IDS
  so rows align with the other ACCOUNT_* tables.
"""

import hashlib
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent1 import storage  # noqa: E402
from agent2.orchestration.domains.bank_account_repository import RDS_ACCOUNT_IDS  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "agent1" / "migrations" / "sources"
MIGRATION = ROOT / "agent1" / "migrations" / "019_account_source_documents.sql"
OSS_BUCKET = "ai-campaign-oss-team3"
OSS_PREFIX = "documents/hsbc"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_manifest() -> list[dict]:
    manifest = json.loads((SOURCES / "account_source_documents.json").read_text(encoding="utf-8"))
    docs = []
    for entry in manifest["documents"]:
        md_path = SOURCES / entry["source_file"]
        markdown = md_path.read_text(encoding="utf-8")
        docs.append({**entry, "markdown": markdown,
                     "oss_key": f"{OSS_PREFIX}/{entry['doc_id']}.md",
                     "content_sha256": sha256_text(markdown)})
    return docs


def resolve_physical_account_id(db, account_key: str) -> str | None:
    row = db.execute(
        "SELECT account_id FROM ACCOUNT_PRODUCT WHERE account_id=%s OR product_code=%s",
        (account_key, account_key.removeprefix("hsbc_")),
    ).fetchone()
    if row:
        return row["account_id"]
    for physical, logical in RDS_ACCOUNT_IDS.items():
        if logical == account_key:
            return physical
    return None


def apply(docs: list[dict]) -> None:
    connection = storage._connect()
    try:
        connection.execute(MIGRATION.read_text(encoding="utf-8"))
        for doc in docs:
            physical = resolve_physical_account_id(connection, doc["account_key"])
            if not physical:
                print(f"SKIP {doc['doc_id']}: no ACCOUNT_PRODUCT row for {doc['account_key']}")
                continue
            connection.execute(
                """INSERT INTO ACCOUNT_SOURCE_DOCUMENT
                   (doc_id, account_id, market, title, source_url, doc_type,
                    oss_bucket, oss_key, content_sha256, chunk_count, fetched_at, verified_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (doc_id) DO UPDATE SET
                     account_id=EXCLUDED.account_id, title=EXCLUDED.title,
                     source_url=EXCLUDED.source_url, oss_key=EXCLUDED.oss_key,
                     content_sha256=EXCLUDED.content_sha256,
                     chunk_count=EXCLUDED.chunk_count, verified_at=EXCLUDED.verified_at""",
                (doc["doc_id"], physical, doc["market"], doc["title"], doc["source_url"],
                 doc["doc_type"], OSS_BUCKET, doc["oss_key"], doc["content_sha256"],
                 len(doc["chunks"]), doc["fetched_at"], doc["verified_at"]))
            for chunk in doc["chunks"]:
                content = chunk["content"].strip()
                connection.execute(
                    """INSERT INTO ACCOUNT_DOC_CHUNK
                       (chunk_id, doc_id, account_id, seq, heading, content, content_sha256)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (chunk_id) DO UPDATE SET
                         heading=EXCLUDED.heading, content=EXCLUDED.content,
                         content_sha256=EXCLUDED.content_sha256""",
                    (f"{doc['doc_id']}#{chunk['seq']:03d}", doc["doc_id"], physical,
                     chunk["seq"], chunk["heading"], content, sha256_text(content)))
        connection.commit()
        print(f"applied {len(docs)} document(s) to RDS")
    finally:
        connection.close()


def upload_oss(docs: list[dict]) -> None:
    for doc in docs:
        local = SOURCES / doc["source_file"]
        target = f"oss://{OSS_BUCKET}/{doc['oss_key']}"
        result = subprocess.run(
            ["ossutil", "cp", str(local), target, "-f"],
            capture_output=True, text=True)
        if result.returncode != 0:
            print(f"OSS UPLOAD FAILED for {target}:\n{result.stderr}")
            sys.exit(1)
        print(f"uploaded {local.name} -> {target}")


def main() -> None:
    docs = load_manifest()
    for doc in docs:
        print(f"doc {doc['doc_id']}: {len(doc['chunks'])} chunks, "
              f"archive sha256={doc['content_sha256'][:12]}…, oss_key={doc['oss_key']}")
    if "--apply" in sys.argv:
        apply(docs)
    else:
        print("dry run (pass --apply to write RDS)")
    if "--upload-oss" in sys.argv:
        upload_oss(docs)


if __name__ == "__main__":
    main()
