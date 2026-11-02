"""Seed reviewed, market-scoped credit-card knowledge into shared RDS.

Usage:
  python scripts/seed_credit_card_knowledge.py          # validate only
  python scripts/seed_credit_card_knowledge.py --apply  # create/upsert RDS rows
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent1 import storage  # noqa: E402

MIGRATION = ROOT / "agent1" / "migrations" / "020_credit_card_knowledge.sql"
MANIFEST = ROOT / "agent1" / "migrations" / "sources" / "credit_card_knowledge_hk.json"


def entries() -> list[dict]:
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    required = {"knowledge_id", "market", "topic", "title", "customer_answer",
                "source_url", "verified_at"}
    for entry in payload["entries"]:
        missing = required - entry.keys()
        if missing:
            raise ValueError(f"{entry.get('knowledge_id', '<unknown>')}: missing {sorted(missing)}")
    return payload["entries"]


def apply(rows: list[dict]) -> None:
    connection = storage._connect()
    try:
        connection.execute(MIGRATION.read_text(encoding="utf-8"))
        for row in rows:
            connection.execute(
                """INSERT INTO card_knowledge_entry
                   (knowledge_id, market, topic, title, customer_answer, source_url, verified_at, status)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,'ACTIVE')
                   ON CONFLICT (knowledge_id) DO UPDATE SET
                     market=EXCLUDED.market, topic=EXCLUDED.topic, title=EXCLUDED.title,
                     customer_answer=EXCLUDED.customer_answer, source_url=EXCLUDED.source_url,
                     verified_at=EXCLUDED.verified_at, status='ACTIVE'""",
                (row["knowledge_id"], row["market"], row["topic"], row["title"],
                 row["customer_answer"], row["source_url"], row["verified_at"]),
            )
        connection.commit()
    finally:
        connection.close()


def main() -> None:
    rows = entries()
    for row in rows:
        print(f"{row['knowledge_id']}: {row['market']} / {row['topic']} / {row['source_url']}")
    if "--apply" in sys.argv:
        apply(rows)
        print(f"applied {len(rows)} credit-card knowledge entries to RDS")
    else:
        print("dry run (pass --apply to write RDS)")


if __name__ == "__main__":
    main()
