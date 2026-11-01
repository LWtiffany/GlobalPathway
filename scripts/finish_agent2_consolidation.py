"""Finish the agent2 -> userinformation consolidation.

Codex's migration stopped halfway: the runtime already resolves every
unqualified table to ``userinformation`` (search_path order), but the real
audit rows still sat in the old ``agent2`` tables — so answer_runs, citations,
profile events, proposals, advice, relationship and reaction history were all
invisible to the running code, and a stray empty ``userinformation.sessions``
table (created by the durable DDL) shadowed the agent2 sessions view.

This script, in ONE transaction:
  1. snapshots every leftover agent2 table into agent2_backup_20261031;
  2. copies the rows into their userinformation counterparts (ON CONFLICT DO NOTHING);
  3. replaces the stray userinformation.sessions TABLE with a view over
     userinformation.conversations (same shape as the old agent2.sessions view);
  4. drops the leftover agent2 tables and compatibility views;
  5. drops abandoned agent2_test_* integration schemas.
"""
import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from agent2 import config

BACKUP_SCHEMA = "agent2_backup_20261031"

# agent2 leftovers whose real data must move into userinformation.
MIGRATE_TABLES = (
    "answer_runs",
    "answer_citations",
    "profile_field_events",
    "profile_change_proposals",
    "profile_field_definitions",
    "advice_current",
    "advice_history",
    "advice_refresh_jobs",
    "advice_scoped_current",
    "domain_conversation_state",
    "bank_relationships",
    "bank_relationship_events",
    "bank_relationship_proposals",
    "message_reactions",
    "plan_items",
    "feedback_tickets",
    "auth_sessions",
)

# Compat views created by the earlier consolidation step; the runtime resolves
# these names in userinformation now, so the views are unreachable duplicates.
DROP_VIEWS = ("agent2.users", "agent2.sessions", "agent2.message_index")


def main():
    with psycopg.connect(**config.connection_kwargs(), row_factory=dict_row) as db:
        db.execute("SELECT pg_advisory_xact_lock(hashtext('agent2-consolidation-finish'))")

        existing = db.execute("""SELECT schema_name FROM information_schema.schemata
            WHERE schema_name = %s""", (BACKUP_SCHEMA,)).fetchone()
        if existing:
            raise RuntimeError(f"{BACKUP_SCHEMA} 已存在，请先人工核对后再重跑")
        db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(BACKUP_SCHEMA)))

        # Swap the stray sessions TABLE for a view over conversations first:
        # the copied rows must satisfy FKs that used to point at the stray table.
        stray = db.execute("""SELECT c.relkind FROM pg_class c
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='userinformation' AND c.relname='sessions'""").fetchone()
        if stray and stray["relkind"] == "r":
            db.execute("""ALTER TABLE userinformation.domain_conversation_state
                DROP CONSTRAINT IF EXISTS domain_conversation_state_session_id_fkey""")
            db.execute("DROP TABLE userinformation.sessions")
            db.execute("""ALTER TABLE userinformation.domain_conversation_state
                ADD CONSTRAINT domain_conversation_state_session_id_fkey
                FOREIGN KEY (session_id)
                REFERENCES userinformation.conversations(conversation_id) ON DELETE CASCADE""")
        db.execute("""CREATE OR REPLACE VIEW userinformation.sessions AS
            SELECT conversation_id AS session_id, user_id, profile_id,
                   profile_version, created_at
            FROM userinformation.conversations""")

        before = {}
        for table in MIGRATE_TABLES:
            count = db.execute(
                sql.SQL("SELECT count(*) AS n FROM agent2.{}").format(
                    sql.Identifier(table))).fetchone()["n"]
            db.execute(sql.SQL("CREATE TABLE {}.{} AS SELECT * FROM agent2.{}").format(
                sql.Identifier(BACKUP_SCHEMA), sql.Identifier(table),
                sql.Identifier(table)))
            db.execute(sql.SQL("""INSERT INTO userinformation.{}
                SELECT * FROM agent2.{} ON CONFLICT DO NOTHING""").format(
                sql.Identifier(table), sql.Identifier(table)))
            after = db.execute(sql.SQL("SELECT count(*) AS n FROM userinformation.{}").format(
                sql.Identifier(table))).fetchone()["n"]
            before[table] = {"agent2": count, "userinformation_after": after}
            if after < count:
                raise RuntimeError(f"{table}: 迁移后行数 {after} < 来源 {count}")

        session_rows = db.execute(
            "SELECT count(*) AS n FROM userinformation.sessions").fetchone()["n"]
        conversation_rows = db.execute(
            "SELECT count(*) AS n FROM userinformation.conversations").fetchone()["n"]
        if session_rows != conversation_rows:
            raise RuntimeError(
                f"sessions 视图行数 {session_rows} != conversations {conversation_rows}")

        for view in DROP_VIEWS:
            db.execute(f"DROP VIEW IF EXISTS {view}")
        for table in (*MIGRATE_TABLES, "schema_migrations"):
            db.execute(sql.SQL("DROP TABLE IF EXISTS agent2.{} CASCADE").format(
                sql.Identifier(table)))

        leftovers = [row["table_name"] for row in db.execute("""SELECT table_name
            FROM information_schema.tables WHERE table_schema='agent2'""").fetchall()]
        if leftovers:
            raise RuntimeError(f"agent2 schema 仍有残留对象: {leftovers}")

        dropped_test_schemas = []
        for row in db.execute("""SELECT schema_name FROM information_schema.schemata
                WHERE schema_name LIKE 'agent2_test_%'""").fetchall():
            name = row["schema_name"]
            if len(name) <= len("agent2_test_"):
                raise RuntimeError(f"拒绝删除未验证 schema: {name}")
            db.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))
            dropped_test_schemas.append(name)

        db.commit()
        print({"migrated": before,
               "sessions_view_rows": session_rows,
               "dropped_test_schemas": dropped_test_schemas})


if __name__ == "__main__":
    main()
