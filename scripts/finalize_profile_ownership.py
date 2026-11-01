"""Make userinformation the only durable owner of Profile state.

This migration is intentionally idempotent.  It moves the field-level current
projection next to the canonical Profile, removes the misleading Agent2 Profile
compatibility view, and enforces User -> Profile (1:N) and Profile -> Conversation
(1:1) with database constraints.
"""

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from agent2 import config


def main():
    with psycopg.connect(**config.connection_kwargs(), row_factory=dict_row) as db:
        db.execute("SELECT pg_advisory_xact_lock(hashtext('profile-ownership-finalization'))")

        kind = db.execute("""SELECT c.relkind FROM pg_class c
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='agent2' AND c.relname='profile_field_values'""").fetchone()
        canonical_fields = db.execute("""SELECT c.relkind FROM pg_class c
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='userinformation' AND c.relname='profile_field_values'""").fetchone()
        if kind and kind["relkind"] == "r" and not canonical_fields:
            db.execute("ALTER TABLE agent2.profile_field_values SET SCHEMA userinformation")
        elif kind and canonical_fields:
            raise RuntimeError("两个 schema 同时存在 profile_field_values，拒绝自动合并")

        db.execute("DROP VIEW IF EXISTS agent2.profiles")

        # Reassert cardinality contracts using canonical composite ownership.
        db.execute("""ALTER TABLE userinformation.profiles
            DROP CONSTRAINT IF EXISTS profiles_user_id_fkey""")
        db.execute("""ALTER TABLE userinformation.profiles
            ADD CONSTRAINT profiles_user_id_fkey FOREIGN KEY (user_id)
            REFERENCES userinformation.users(user_id) ON DELETE CASCADE""")
        db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS uq_conversations_profile
            ON userinformation.conversations(profile_id)""")
        db.execute("""ALTER TABLE userinformation.conversations
            DROP CONSTRAINT IF EXISTS conversations_profile_id_user_id_fkey""")
        db.execute("""ALTER TABLE userinformation.conversations
            ADD CONSTRAINT conversations_profile_id_user_id_fkey
            FOREIGN KEY (profile_id,user_id)
            REFERENCES userinformation.profiles(profile_id,user_id) ON DELETE CASCADE""")

        duplicates = db.execute("""SELECT profile_id,count(*) AS n
            FROM userinformation.conversations GROUP BY profile_id HAVING count(*)>1""").fetchall()
        orphans = db.execute("""SELECT count(*) AS n FROM userinformation.profiles p
            LEFT JOIN userinformation.users u ON u.user_id=p.user_id
            WHERE u.user_id IS NULL""").fetchone()["n"]
        if duplicates or orphans:
            raise RuntimeError(f"关系校验失败: duplicates={duplicates}, orphan_profiles={orphans}")

        # Clean up only abandoned integration-test schemas created by Store.temporary().
        test_schemas = [row["schema_name"] for row in db.execute("""SELECT schema_name
            FROM information_schema.schemata WHERE schema_name LIKE 'agent2_test_%'""").fetchall()]
        for schema_name in test_schemas:
            if not schema_name.startswith("agent2_test_") or len(schema_name) <= len("agent2_test_"):
                raise RuntimeError(f"拒绝删除未验证 schema: {schema_name}")
            db.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema_name)))

        db.commit()
        print({
            "profiles": db.execute("SELECT count(*) AS n FROM userinformation.profiles").fetchone()["n"],
            "conversations": db.execute("SELECT count(*) AS n FROM userinformation.conversations").fetchone()["n"],
            "multi_conversation_profiles": len(duplicates),
            "orphan_profiles": orphans,
            "dropped_test_schemas": test_schemas,
        })


if __name__ == "__main__":
    main()
