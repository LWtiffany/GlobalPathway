"""Consolidate duplicate Agent2 identity/profile tables into userinformation.

The migration is atomic.  Canonical user/profile/conversation/message rows live
in ``userinformation``; ``agent2`` keeps only audit and lifecycle extensions.
"""
from agent1 import config
import psycopg
from psycopg.rows import dict_row


FK_STATEMENTS = (
    "ALTER TABLE agent2.auth_sessions ADD CONSTRAINT auth_sessions_user_id_fkey FOREIGN KEY (user_id) REFERENCES userinformation.users(user_id)",
    "ALTER TABLE agent2.profile_field_values ADD CONSTRAINT profile_field_values_profile_id_fkey FOREIGN KEY (profile_id) REFERENCES userinformation.profiles(profile_id) ON DELETE CASCADE",
    "ALTER TABLE agent2.profile_field_events ADD CONSTRAINT profile_field_events_profile_id_fkey FOREIGN KEY (profile_id) REFERENCES userinformation.profiles(profile_id) ON DELETE CASCADE",
    "ALTER TABLE agent2.profile_change_proposals ADD CONSTRAINT profile_change_proposals_profile_id_fkey FOREIGN KEY (profile_id) REFERENCES userinformation.profiles(profile_id) ON DELETE CASCADE",
    "ALTER TABLE agent2.bank_relationships ADD CONSTRAINT bank_relationships_profile_id_fkey FOREIGN KEY (profile_id) REFERENCES userinformation.profiles(profile_id) ON DELETE CASCADE",
    "ALTER TABLE agent2.bank_relationship_proposals ADD CONSTRAINT bank_relationship_proposals_profile_id_fkey FOREIGN KEY (profile_id) REFERENCES userinformation.profiles(profile_id) ON DELETE CASCADE",
    "ALTER TABLE agent2.domain_conversation_state ADD CONSTRAINT domain_conversation_state_session_id_fkey FOREIGN KEY (session_id) REFERENCES userinformation.conversations(conversation_id) ON DELETE CASCADE",
    "ALTER TABLE agent2.answer_runs ADD CONSTRAINT answer_runs_message_id_fkey FOREIGN KEY (message_id) REFERENCES userinformation.message_index(message_id)",
    "ALTER TABLE agent2.answer_citations ADD CONSTRAINT answer_citations_message_id_fkey FOREIGN KEY (message_id) REFERENCES userinformation.message_index(message_id)",
    "ALTER TABLE agent2.advice_current ADD CONSTRAINT advice_current_profile_id_fkey FOREIGN KEY (profile_id) REFERENCES userinformation.profiles(profile_id) ON DELETE CASCADE",
    "ALTER TABLE agent2.advice_scoped_current ADD CONSTRAINT advice_scoped_current_profile_id_fkey FOREIGN KEY (profile_id) REFERENCES userinformation.profiles(profile_id) ON DELETE CASCADE",
    "ALTER TABLE agent2.advice_refresh_jobs ADD CONSTRAINT advice_refresh_jobs_profile_id_fkey FOREIGN KEY (profile_id) REFERENCES userinformation.profiles(profile_id) ON DELETE CASCADE",
    "ALTER TABLE agent2.message_reactions ADD CONSTRAINT message_reactions_message_id_fkey FOREIGN KEY (message_id) REFERENCES userinformation.message_index(message_id)",
    "ALTER TABLE agent2.message_reactions ADD CONSTRAINT message_reactions_user_id_fkey FOREIGN KEY (user_id) REFERENCES userinformation.users(user_id)",
    "ALTER TABLE agent2.feedback_tickets ADD CONSTRAINT feedback_tickets_message_id_fkey FOREIGN KEY (message_id) REFERENCES userinformation.message_index(message_id)",
    "ALTER TABLE agent2.feedback_tickets ADD CONSTRAINT feedback_tickets_user_id_fkey FOREIGN KEY (user_id) REFERENCES userinformation.users(user_id)",
    "ALTER TABLE agent2.plan_items ADD CONSTRAINT plan_items_profile_id_fkey FOREIGN KEY (profile_id) REFERENCES userinformation.profiles(profile_id) ON DELETE CASCADE",
)


def main():
    with psycopg.connect(
        host=config.DB_HOST, port=config.DB_PORT, dbname=config.DB_NAME,
        user=config.DB_USER, password=config.DB_PASSWORD, row_factory=dict_row,
    ) as db:
        db.execute("SELECT pg_advisory_xact_lock(hashtext('agent2-userinformation-consolidation'))")
        before = {
            name: db.execute(f"SELECT count(*) AS n FROM agent2.{name}").fetchone()["n"]
            for name in ("users", "profiles", "sessions", "message_index")
        }

        db.execute("ALTER TABLE userinformation.users ADD COLUMN IF NOT EXISTS display_name TEXT")
        db.execute("ALTER TABLE userinformation.users ADD COLUMN IF NOT EXISTS synthetic INTEGER NOT NULL DEFAULT 0")
        db.execute("UPDATE userinformation.users SET display_name=username WHERE display_name IS NULL")
        db.execute("ALTER TABLE userinformation.users ALTER COLUMN display_name SET NOT NULL")
        db.execute("ALTER TABLE userinformation.users ALTER COLUMN username SET DEFAULT ('agent2_' || gen_random_uuid()::text)")
        db.execute("ALTER TABLE userinformation.users ALTER COLUMN password_hash SET DEFAULT '!agent2-disabled!'")
        for column in ("password_updated_at", "created_at", "updated_at"):
            db.execute(f"ALTER TABLE userinformation.users ALTER COLUMN {column} SET DEFAULT now()")

        db.execute("ALTER TABLE userinformation.profiles ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active'")
        db.execute("ALTER TABLE userinformation.profiles ADD COLUMN IF NOT EXISTS synthetic INTEGER NOT NULL DEFAULT 0")
        db.execute("ALTER TABLE userinformation.conversations ADD COLUMN IF NOT EXISTS profile_version INTEGER NOT NULL DEFAULT 1")

        db.execute("ALTER TABLE userinformation.message_index DROP CONSTRAINT IF EXISTS message_index_storage_backend_check")
        db.execute("ALTER TABLE userinformation.message_index DROP CONSTRAINT IF EXISTS message_index_status_check")
        db.execute("ALTER TABLE userinformation.message_index DROP CONSTRAINT IF EXISTS message_index_check")
        db.execute("""ALTER TABLE userinformation.message_index ADD CONSTRAINT message_index_storage_backend_check
            CHECK (storage_backend IN ('sqlite','oss','rds'))""")
        db.execute("""ALTER TABLE userinformation.message_index ADD CONSTRAINT message_index_status_check
            CHECK (status IN ('pending','processing','complete','failed','received','delivered'))""")
        db.execute("""ALTER TABLE userinformation.message_index ADD CONSTRAINT message_index_check
            CHECK ((storage_backend IN ('sqlite','rds') AND body_json IS NOT NULL AND object_key IS NULL)
                OR (storage_backend='oss' AND body_json IS NULL AND object_key IS NOT NULL AND sha256 IS NOT NULL))""")
        db.execute("ALTER TABLE userinformation.message_index ALTER COLUMN storage_backend SET DEFAULT 'rds'")

        db.execute("""INSERT INTO userinformation.users
            (user_id,username,password_hash,external_subject,status,password_updated_at,created_at,
             updated_at,failed_attempts,display_name,synthetic)
            SELECT user_id,'agent2_' || user_id,'!agent2-disabled!',external_subject,status,
                   now(),now(),now(),0,display_name,synthetic
            FROM agent2.users ON CONFLICT DO NOTHING""")

        db.execute("""INSERT INTO userinformation.profiles
            (profile_id,user_id,version,student_name,profile_data_json,
             remittance_advice_json,account_advice_json,visa_advice_json,card_advice_json,
             created_at,updated_at,status,synthetic)
            SELECT p.profile_id,p.user_id,p.version,p.display_name,
                   COALESCE((SELECT jsonb_object_agg(f.field_code,f.value_json::jsonb)
                     FROM agent2.profile_field_values f
                     WHERE f.profile_id=p.profile_id AND f.value_json IS NOT NULL),'{}'::jsonb),
                   (SELECT payload_json::jsonb FROM agent2.advice_current a WHERE a.profile_id=p.profile_id AND a.topic='remittance'),
                   (SELECT payload_json::jsonb FROM agent2.advice_current a WHERE a.profile_id=p.profile_id AND a.topic='account'),
                   (SELECT payload_json::jsonb FROM agent2.advice_current a WHERE a.profile_id=p.profile_id AND a.topic='visa'),
                   (SELECT payload_json::jsonb FROM agent2.advice_current a WHERE a.profile_id=p.profile_id AND a.topic='credit_card'),
                   p.created_at::timestamptz,p.updated_at::timestamptz,p.status,p.synthetic
            FROM agent2.profiles p ON CONFLICT DO NOTHING""")

        db.execute("""INSERT INTO userinformation.conversations
            (conversation_id,user_id,profile_id,created_at,profile_version)
            SELECT session_id,user_id,profile_id,created_at::timestamptz,profile_version
            FROM agent2.sessions ON CONFLICT DO NOTHING""")
        db.execute("""INSERT INTO userinformation.message_index
            (message_id,conversation_id,seq,role,client_message_id,reply_to,body_json,
             storage_backend,status,created_at)
            SELECT message_id,session_id,seq,role,client_message_id,reply_to,body_json::jsonb,
                   'rds',status,created_at::timestamptz
            FROM agent2.message_index ON CONFLICT DO NOTHING""")

        # Import the canonical profile's first-class columns into the audited
        # field projection only when Agent2 has no newer value for that field.
        db.execute("""ALTER TABLE agent2.profile_field_values
            DROP CONSTRAINT IF EXISTS profile_field_values_profile_id_fkey""")
        db.execute("""INSERT INTO agent2.profile_field_values
            (profile_id,field_code,value_json,value_type,status,source_type,confirmed_at,
             field_version,sensitivity,updated_at)
            SELECT profile_id,field_code,to_jsonb(value)::text,'string','confirmed','document_import',
                   updated_at::text,1,'personal',updated_at::text
            FROM (
              SELECT profile_id,'destination_country' field_code,
                CASE destination WHEN 'Hong Kong' THEN 'CN-HK' ELSE destination END value,updated_at
                FROM userinformation.profiles WHERE destination IS NOT NULL
              UNION ALL SELECT profile_id,'course',course,updated_at FROM userinformation.profiles WHERE course IS NOT NULL
              UNION ALL SELECT profile_id,'offer_status',offer_status,updated_at FROM userinformation.profiles WHERE offer_status IS NOT NULL
              UNION ALL SELECT profile_id,'planned_start_date',enrollment_date::text,updated_at FROM userinformation.profiles WHERE enrollment_date IS NOT NULL
              UNION ALL SELECT profile_id,'visa_category',
                CASE WHEN lower(visa_category) LIKE '%student%' OR lower(visa_category)='f-1'
                     THEN 'student_visa' ELSE visa_category END,updated_at
                FROM userinformation.profiles WHERE visa_category IS NOT NULL
              UNION ALL SELECT profile_id,'school',school,updated_at FROM userinformation.profiles WHERE school IS NOT NULL
            ) imported
            ON CONFLICT(profile_id,field_code) DO NOTHING""")
        db.execute("""INSERT INTO agent2.profile_field_values
            (profile_id,field_code,value_json,value_type,status,source_type,confirmed_at,
             field_version,sensitivity,updated_at)
            SELECT p.profile_id,j.key,j.value::text,
                   CASE jsonb_typeof(j.value) WHEN 'array' THEN 'multi_enum'
                     WHEN 'number' THEN 'number' WHEN 'boolean' THEN 'boolean'
                     WHEN 'object' THEN 'object' ELSE 'string' END,
                   'confirmed','document_import',p.updated_at::text,1,'standard',p.updated_at::text
            FROM userinformation.profiles p CROSS JOIN LATERAL jsonb_each(p.profile_data_json) j
            JOIN agent2.profile_field_definitions d ON d.field_code=j.key
            ON CONFLICT(profile_id,field_code) DO NOTHING""")

        migrated = {
            "users": db.execute("SELECT count(*) AS n FROM userinformation.users u JOIN agent2.users a USING(user_id)").fetchone()["n"],
            "profiles": db.execute("SELECT count(*) AS n FROM userinformation.profiles u JOIN agent2.profiles a USING(profile_id)").fetchone()["n"],
            "sessions": db.execute("SELECT count(*) AS n FROM userinformation.conversations u JOIN agent2.sessions a ON u.conversation_id=a.session_id").fetchone()["n"],
            "message_index": db.execute("SELECT count(*) AS n FROM userinformation.message_index u JOIN agent2.message_index a USING(message_id)").fetchone()["n"],
        }
        if migrated != before:
            raise RuntimeError(f"canonical row verification failed: before={before}, migrated={migrated}")

        for table in ("message_index", "sessions", "profiles", "users"):
            db.execute(f"DROP TABLE agent2.{table} CASCADE")

        db.execute("""CREATE VIEW agent2.users AS
            SELECT user_id,external_subject,display_name,synthetic,status
            FROM userinformation.users""")
        db.execute("""CREATE VIEW agent2.profiles AS
            SELECT profile_id,user_id,student_name AS display_name,version,status,synthetic,
                   created_at,updated_at FROM userinformation.profiles""")
        db.execute("""CREATE VIEW agent2.sessions AS
            SELECT conversation_id AS session_id,user_id,profile_id,profile_version,created_at
            FROM userinformation.conversations""")
        db.execute("""CREATE VIEW agent2.message_index AS
            SELECT message_id,conversation_id AS session_id,seq,role,client_message_id,reply_to,
                   body_json,status,created_at FROM userinformation.message_index""")

        for statement in FK_STATEMENTS:
            db.execute(statement)
        db.commit()
        print({"migrated": migrated, "canonical": {
            "users": db.execute("SELECT count(*) AS n FROM userinformation.users").fetchone()["n"],
            "profiles": db.execute("SELECT count(*) AS n FROM userinformation.profiles").fetchone()["n"],
            "conversations": db.execute("SELECT count(*) AS n FROM userinformation.conversations").fetchone()["n"],
            "messages": db.execute("SELECT count(*) AS n FROM userinformation.message_index").fetchone()["n"],
        }})


if __name__ == "__main__":
    main()
