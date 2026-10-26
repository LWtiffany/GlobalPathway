"""Make userinformation.profiles.school a foreign key to public.school.

Steps:
  1. Add school_id, backfill from free-text school names
  2. Delete rows whose school text cannot be matched (FK conflicts)
  3. Drop the text column and rename school_id → school with FK

Usage:
  AGENT1_DB_PASSWORD=… .venv/bin/python scripts/migrate_profile_school_fk.py
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent1 import storage


def _normalize(name: str) -> str:
    text = (name or "").strip()
    if text.casefold().startswith("the "):
        text = text[4:].strip()
    return text


def _match_school_id(connection, name: str) -> str | None:
    if not name or not str(name).strip():
        return None
    raw = str(name).strip()
    row = connection.execute(
        "SELECT school_id FROM school WHERE name=%s ORDER BY school_id LIMIT 1",
        (raw,),
    ).fetchone()
    if row:
        return row["school_id"]
    norm = _normalize(raw)
    row = connection.execute(
        "SELECT school_id FROM school WHERE name=%s OR lower(name)=lower(%s) "
        "ORDER BY school_id LIMIT 1",
        (norm, norm),
    ).fetchone()
    if row:
        return row["school_id"]
    row = connection.execute(
        "SELECT school_id FROM school WHERE lower(name)=lower(%s) "
        "OR lower(regexp_replace(name, '^The\\s+', '', 'i'))=lower(%s) "
        "ORDER BY school_id LIMIT 1",
        (raw, norm),
    ).fetchone()
    return row["school_id"] if row else None


def migrate(*, dry_run: bool = False):
    connection = storage._connect()
    try:
        # Already migrated?
        cols = {
            r["column_name"]
            for r in connection.execute(
                """SELECT column_name FROM information_schema.columns
                   WHERE table_schema='userinformation' AND table_name='profiles'"""
            ).fetchall()
        }
        fk = connection.execute(
            """SELECT 1 FROM information_schema.table_constraints
               WHERE table_schema='userinformation' AND table_name='profiles'
                 AND constraint_type='FOREIGN KEY' AND constraint_name='profiles_school_fkey'"""
        ).fetchone()
        if fk and "school" in cols and "school_id" not in cols:
            # Detect whether school already holds school_id values
            sample = connection.execute(
                "SELECT school FROM userinformation.profiles WHERE school IS NOT NULL LIMIT 1"
            ).fetchone()
            if sample and connection.execute(
                "SELECT 1 FROM school WHERE school_id=%s", (sample["school"],)
            ).fetchone():
                return {"status": "already_migrated", "deleted": 0, "updated": 0}

        if "school_id" not in cols:
            connection.execute(
                "ALTER TABLE userinformation.profiles ADD COLUMN school_id TEXT"
            )

        rows = connection.execute(
            "SELECT profile_id, school, school_id FROM userinformation.profiles"
        ).fetchall()
        updated, conflicts = [], []
        for row in rows:
            if row.get("school_id"):
                continue
            school_text = row.get("school")
            if school_text is None or str(school_text).strip() == "":
                continue
            sid = _match_school_id(connection, school_text)
            if sid:
                connection.execute(
                    "UPDATE userinformation.profiles SET school_id=%s WHERE profile_id=%s",
                    (sid, row["profile_id"]),
                )
                updated.append(row["profile_id"])
            else:
                conflicts.append({"profile_id": row["profile_id"], "school": school_text})

        deleted = []
        for item in conflicts:
            connection.execute(
                "DELETE FROM userinformation.profiles WHERE profile_id=%s",
                (item["profile_id"],),
            )
            deleted.append(item)

        # Replace text school with FK column named school.
        connection.execute(
            "ALTER TABLE userinformation.profiles DROP COLUMN IF EXISTS school"
        )
        # school_id may already be the only column; rename to school
        cols_now = {
            r["column_name"]
            for r in connection.execute(
                """SELECT column_name FROM information_schema.columns
                   WHERE table_schema='userinformation' AND table_name='profiles'"""
            ).fetchall()
        }
        if "school_id" in cols_now and "school" not in cols_now:
            connection.execute(
                "ALTER TABLE userinformation.profiles RENAME COLUMN school_id TO school"
            )
        connection.execute(
            "ALTER TABLE userinformation.profiles DROP CONSTRAINT IF EXISTS profiles_school_fkey"
        )
        connection.execute(
            """ALTER TABLE userinformation.profiles
               ADD CONSTRAINT profiles_school_fkey
               FOREIGN KEY (school) REFERENCES public.school(school_id)
               ON DELETE SET NULL"""
        )

        if dry_run:
            connection.rollback()
        else:
            connection.commit()
        return {
            "status": "ok" if not dry_run else "dry_run",
            "updated": updated,
            "deleted": deleted,
            "deleted_count": len(deleted),
            "updated_count": len(updated),
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    print(migrate(dry_run=dry))
