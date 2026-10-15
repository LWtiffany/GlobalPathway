-- 009_review_queue_to_admin.sql
-- REVIEW_QUEUE moves to the admin database (attached as `admin`, see admin_store).
-- Pending items are carried over; decided ones are not kept.

INSERT INTO admin.REVIEW_QUEUE
    (review_id, domain_table, record_key, action, field_path, proposed_value, evidence_json, reason, created_at)
SELECT review_id, domain_table, record_key, action, field_path, proposed_value, evidence_json, reason, created_at
FROM main.REVIEW_QUEUE WHERE status = 'OPEN';

DROP TABLE main.REVIEW_QUEUE;
