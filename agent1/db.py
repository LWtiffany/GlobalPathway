"""SQLite 存储：来源登记、运行记录、租约、快照、知识版本、审核、审计与事件。

设计依据：设计文档 3.3/4.2/4.5/7.4。权威存储在本地 SQLite；
大型原文快照按内容指纹存本地文件，数据库只保存路径与哈希。
"""
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config
from .models import SourceConfig

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS source_registry (
  source_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  url TEXT NOT NULL,
  allowed_host TEXT NOT NULL,
  category TEXT NOT NULL,
  region TEXT,
  authority TEXT NOT NULL DEFAULT 'official',
  language TEXT,
  access_basis TEXT,
  owner TEXT,
  review_status TEXT NOT NULL DEFAULT 'approved',
  enabled INTEGER NOT NULL DEFAULT 1,
  check_interval_minutes INTEGER NOT NULL DEFAULT 1440,
  freshness_sla_hours INTEGER,
  content_selector TEXT,
  dynamic INTEGER NOT NULL DEFAULT 0,
  schema_type TEXT NOT NULL DEFAULT 'generic_facts',
  max_retries INTEGER NOT NULL DEFAULT 2,
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_runs (
  run_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL,
  trigger TEXT NOT NULL,
  status TEXT NOT NULL,
  attempt INTEGER NOT NULL DEFAULT 1,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  duration_ms INTEGER,
  error TEXT,
  error_kind TEXT,
  retry_after TEXT,
  snapshot_hash TEXT,
  extraction_method TEXT,
  model TEXT,
  schema_version TEXT,
  prompt_version TEXT,
  excerpt_chars INTEGER,
  prompt_tokens INTEGER,
  completion_tokens INTEGER,
  gate_result TEXT,
  version_id TEXT,
  review_id TEXT,
  detail_json TEXT
);
CREATE TABLE IF NOT EXISTS run_leases (
  source_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  leased_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
  hash TEXT PRIMARY KEY,
  source_id TEXT NOT NULL,
  url TEXT NOT NULL,
  captured_at TEXT NOT NULL,
  html_path TEXT NOT NULL,
  markdown_path TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_versions (
  version_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL,
  schema_type TEXT NOT NULL,
  snapshot_hash TEXT NOT NULL,
  business_hash TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  version_no INTEGER NOT NULL,
  supersedes TEXT,
  payload_json TEXT NOT NULL,
  policy_keys_json TEXT NOT NULL,
  risk_level TEXT NOT NULL,
  change_type TEXT,
  changed_fields_json TEXT,
  key_change INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL,
  verified_at TEXT,
  model TEXT,
  schema_version TEXT,
  prompt_version TEXT,
  run_id TEXT,
  conflicts_json TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(source_id, version_no)
);
CREATE TABLE IF NOT EXISTS knowledge_index (
  policy_key TEXT PRIMARY KEY,
  source_id TEXT NOT NULL,
  schema_type TEXT NOT NULL,
  applies_to_city TEXT,
  version_id TEXT NOT NULL,
  verified_at TEXT,
  availability TEXT NOT NULL DEFAULT 'available',
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS published_knowledge (
  source_id TEXT PRIMARY KEY,
  version_id TEXT NOT NULL,
  published_at TEXT NOT NULL,
  published_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_queue (
  review_id TEXT PRIMARY KEY,
  version_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  risk_level TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'OPEN',
  decision TEXT,
  decided_by TEXT,
  decided_at TEXT,
  decision_note TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  entity TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  detail_json TEXT
);
CREATE TABLE IF NOT EXISTS knowledge_events (
  event_id TEXT PRIMARY KEY,
  event_type TEXT NOT NULL,
  source_id TEXT NOT NULL,
  version_id TEXT,
  occurred_at TEXT NOT NULL,
  detail_json TEXT
);
"""


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def parse_ts(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def new_id(prefix):
    return f"{prefix}-{uuid.uuid4().hex}"


@contextmanager
def session():
    # Compatibility store; never add old tables to the new formal database.
    config.require_legacy_environment()
    config.ensure_dirs()
    db = sqlite3.connect(config.LEGACY_DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=30000")
    db.executescript(SCHEMA_SQL)
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def rows_to_dicts(rows):
    return [dict(row) for row in rows]


# ---------- 来源登记 ----------

def source_from_json(entry):
    category = entry.get("category", "school")
    defaults = config.CATEGORY_DEFAULTS.get(category, config.CATEGORY_DEFAULTS["school"])
    return SourceConfig(
        source_id=entry["id"],
        name=entry.get("name", entry["id"]),
        url=entry["url"],
        allowed_host=entry["allowed_host"],
        category=category,
        region=entry.get("region", ""),
        authority=entry.get("authority", "official"),
        language=entry.get("language", "en"),
        access_basis=entry.get("access_basis", ""),
        owner=entry.get("owner", ""),
        review_status="approved" if entry.get("approved_for_demo", True) else entry.get("review_status", "pending"),
        enabled=bool(entry.get("enabled", True)),
        check_interval_minutes=int(entry.get("check_interval_minutes", defaults["check_interval_minutes"])),
        freshness_sla_hours=entry.get("freshness_sla_hours", defaults["freshness_sla_hours"]),
        content_selector=entry.get("content_selector"),
        dynamic=bool(entry.get("dynamic", False)),
        schema_type=entry.get("schema_type", "generic_facts"),
        max_retries=int(entry.get("max_retries", 2)),
        notes=entry.get("notes", ""),
    )


def upsert_source(db, source: SourceConfig):
    existing = db.execute("SELECT created_at FROM source_registry WHERE source_id=?", (source.source_id,)).fetchone()
    created = existing["created_at"] if existing else now_iso()
    columns = ["source_id", "name", "url", "allowed_host", "category", "region", "authority",
               "language", "access_basis", "owner", "review_status", "enabled",
               "check_interval_minutes", "freshness_sla_hours", "content_selector", "dynamic",
               "schema_type", "max_retries", "notes", "created_at", "updated_at"]
    values = [source.source_id, source.name, source.url, source.allowed_host, source.category,
              source.region, source.authority, source.language, source.access_basis, source.owner,
              source.review_status, 1 if source.enabled else 0, source.check_interval_minutes,
              source.freshness_sla_hours, source.content_selector, 1 if source.dynamic else 0,
              source.schema_type, source.max_retries, source.notes, created, now_iso()]
    db.execute(f"INSERT OR REPLACE INTO source_registry ({','.join(columns)}) "
               f"VALUES ({','.join('?' for _ in columns)})", values)


def sync_registry(db, path=None):
    """从 sources.json 加载来源登记（幂等）。"""
    path = Path(path or config.SOURCES_PATH)
    entries = json.loads(path.read_text(encoding="utf-8"))
    sources = []
    for entry in entries:
        source = source_from_json(entry)
        upsert_source(db, source)
        sources.append(source)
    return sources


def get_source(db, source_id):
    row = db.execute("SELECT * FROM source_registry WHERE source_id=?", (source_id,)).fetchone()
    return dict(row) if row else None


def list_sources(db, enabled_only=False):
    sql = "SELECT * FROM source_registry"
    if enabled_only:
        sql += " WHERE enabled=1"
    return rows_to_dicts(db.execute(sql + " ORDER BY source_id"))


def update_source(db, source_id, **fields):
    allowed = {"check_interval_minutes", "freshness_sla_hours", "enabled", "owner",
               "access_basis", "review_status", "max_retries", "notes"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return False
    sets["updated_at"] = now_iso()
    db.execute(f"UPDATE source_registry SET {', '.join(f'{k}=?' for k in sets)} WHERE source_id=?",
               [*sets.values(), source_id])
    return True


# ---------- 运行记录与租约 ----------

def create_run(db, source_id, trigger, attempt=1):
    run_id = new_id("run")
    db.execute(
        "INSERT INTO agent_runs(run_id,source_id,trigger,status,attempt,started_at) VALUES(?,?,?,?,?,?)",
        (run_id, source_id, trigger, "RUNNING", attempt, now_iso()),
    )
    return run_id


def finish_run(db, run_id, status, *, duration_ms=None, error=None, error_kind=None, retry_after=None,
               snapshot_hash=None, extraction_method=None, model=None, schema_version=None,
               prompt_version=None, excerpt_chars=None, prompt_tokens=None, completion_tokens=None,
               gate_result=None, version_id=None, review_id=None, detail=None):
    fields = {
        "status": status,
        "finished_at": now_iso(),
        "duration_ms": duration_ms,
        "error": error,
        "error_kind": error_kind,
        "retry_after": retry_after,
        "snapshot_hash": snapshot_hash,
        "extraction_method": extraction_method,
        "model": model,
        "schema_version": schema_version,
        "prompt_version": prompt_version,
        "excerpt_chars": excerpt_chars,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "gate_result": gate_result,
        "version_id": version_id,
        "review_id": review_id,
        "detail_json": json.dumps(detail, ensure_ascii=False) if detail is not None else None,
    }
    fields = {k: v for k, v in fields.items() if v is not None}
    db.execute(f"UPDATE agent_runs SET {', '.join(f'{k}=?' for k in fields)} WHERE run_id=?",
               [*fields.values(), run_id])


def last_run(db, source_id):
    row = db.execute("SELECT * FROM agent_runs WHERE source_id=? ORDER BY started_at DESC LIMIT 1",
                     (source_id,)).fetchone()
    return dict(row) if row else None


def recent_runs(db, limit=20, source_id=None):
    if source_id:
        rows = db.execute("SELECT * FROM agent_runs WHERE source_id=? ORDER BY started_at DESC LIMIT ?",
                          (source_id, limit))
    else:
        rows = db.execute("SELECT * FROM agent_runs ORDER BY started_at DESC LIMIT ?", (limit,))
    return rows_to_dicts(rows)


def acquire_lease(db, source_id, run_id, minutes=None):
    minutes = minutes or config.LEASE_MINUTES
    now = datetime.now(timezone.utc)
    row = db.execute("SELECT * FROM run_leases WHERE source_id=?", (source_id,)).fetchone()
    if row and parse_ts(row["expires_at"]) and parse_ts(row["expires_at"]) > now:
        return False
    db.execute(
        "INSERT OR REPLACE INTO run_leases VALUES(?,?,?,?)",
        (source_id, run_id, now.isoformat(), (now + timedelta(minutes=minutes)).isoformat()),
    )
    return True


def release_lease(db, source_id, run_id=None):
    if run_id:
        db.execute("DELETE FROM run_leases WHERE source_id=? AND run_id=?", (source_id, run_id))
    else:
        db.execute("DELETE FROM run_leases WHERE source_id=?", (source_id,))


def active_lease(db, source_id, now=None):
    now = now or datetime.now(timezone.utc)
    row = db.execute("SELECT * FROM run_leases WHERE source_id=?", (source_id,)).fetchone()
    if row and parse_ts(row["expires_at"]) and parse_ts(row["expires_at"]) > now:
        return dict(row)
    return None


# ---------- 快照 ----------

def insert_snapshot(db, page_hash, source_id, url, html_path, markdown_path):
    existing = db.execute("SELECT captured_at FROM snapshots WHERE hash=?", (page_hash,)).fetchone()
    if existing:
        return False
    db.execute("INSERT OR IGNORE INTO snapshots VALUES(?,?,?,?,?,?)",
               (page_hash, source_id, url, now_iso(), str(html_path), str(markdown_path)))
    return True


def latest_snapshot(db, source_id):
    row = db.execute("SELECT * FROM snapshots WHERE source_id=? ORDER BY captured_at DESC LIMIT 1",
                     (source_id,)).fetchone()
    return dict(row) if row else None


# ---------- 知识版本 ----------

def latest_version(db, source_id):
    row = db.execute(
        "SELECT * FROM knowledge_versions WHERE source_id=? AND status NOT IN ('REJECTED','WITHDRAWN') "
        "ORDER BY version_no DESC LIMIT 1", (source_id,)).fetchone()
    return dict(row) if row else None


def next_version_no(db, source_id):
    row = db.execute("SELECT MAX(version_no) AS n FROM knowledge_versions WHERE source_id=?", (source_id,)).fetchone()
    return (row["n"] or 0) + 1


def insert_version(db, **fields):
    fields.setdefault("version_id", new_id("ver"))
    fields.setdefault("created_at", now_iso())
    columns = ["version_id", "source_id", "schema_type", "snapshot_hash", "business_hash", "content_hash",
               "version_no", "supersedes", "payload_json", "policy_keys_json", "risk_level", "change_type",
               "changed_fields_json", "key_change", "status", "verified_at", "model", "schema_version",
               "prompt_version", "run_id", "conflicts_json", "created_at"]
    values = [fields.get(c) for c in columns]
    db.execute(f"INSERT INTO knowledge_versions ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", values)
    return fields["version_id"]


def get_version(db, version_id):
    row = db.execute("SELECT * FROM knowledge_versions WHERE version_id=?", (version_id,)).fetchone()
    return dict(row) if row else None


def list_versions(db, source_id=None, limit=50):
    if source_id:
        rows = db.execute("SELECT * FROM knowledge_versions WHERE source_id=? ORDER BY version_no DESC LIMIT ?",
                          (source_id, limit))
    else:
        rows = db.execute("SELECT * FROM knowledge_versions ORDER BY created_at DESC LIMIT ?", (limit,))
    return rows_to_dicts(rows)


def set_version_status(db, version_id, status):
    db.execute("UPDATE knowledge_versions SET status=? WHERE version_id=?", (status, version_id))


def update_version_in_place(db, version_id, *, snapshot_hash, business_hash, content_hash,
                            payload_json, policy_keys_json, changed_fields_json, run_id):
    """原地更新一个版本的业务内容，不新建版本（适用于低频微调场景：每日抓取后
    字段值小幅变化直接合并进当前版本，而非堆叠新版本）。保留 version_id/version_no/
    状态/发布时间不变，仅更新内容与指纹，核验时间由调用方通过 touch_verified 刷新。"""
    db.execute(
        "UPDATE knowledge_versions SET snapshot_hash=?, business_hash=?, content_hash=?, "
        "payload_json=?, policy_keys_json=?, changed_fields_json=?, run_id=? "
        "WHERE version_id=?",
        (snapshot_hash, business_hash, content_hash, payload_json,
         policy_keys_json, changed_fields_json, run_id, version_id),
    )


def touch_verified(db, version_id, at=None):
    """刷新最近成功核验时间（仅在抽取+门控通过或内容未变化时调用；失败不得刷新）。"""
    at = at or now_iso()
    db.execute("UPDATE knowledge_versions SET verified_at=? WHERE version_id=?", (at, version_id))
    db.execute("UPDATE knowledge_index SET verified_at=?, updated_at=? WHERE version_id=?",
               (at, now_iso(), version_id))


def set_published_pointer(db, source_id, version_id, actor):
    db.execute("INSERT OR REPLACE INTO published_knowledge VALUES(?,?,?,?)",
               (source_id, version_id, now_iso(), actor))


def clear_published_pointer(db, source_id):
    db.execute("DELETE FROM published_knowledge WHERE source_id=?", (source_id,))


def replace_index_rows(db, source_id, schema_type, version_id, policy_keys, verified_at, availability="available"):
    db.execute("DELETE FROM knowledge_index WHERE source_id=?", (source_id,))
    for key in policy_keys:
        db.execute(
            "INSERT OR REPLACE INTO knowledge_index VALUES(?,?,?,?,?,?,?,?)",
            (key["policy_key"], source_id, schema_type, key.get("applies_to_city"),
             version_id, verified_at, availability, now_iso()),
        )


def set_index_availability(db, source_id, availability):
    db.execute("UPDATE knowledge_index SET availability=?, updated_at=? WHERE source_id=?",
               (availability, now_iso(), source_id))


def published_row(db, source_id):
    row = db.execute("SELECT * FROM published_knowledge WHERE source_id=?", (source_id,)).fetchone()
    return dict(row) if row else None


# ---------- 审核 ----------

def add_review(db, version_id, source_id, reason, risk_level):
    review_id = new_id("rev")
    db.execute(
        "INSERT INTO review_queue(review_id,version_id,source_id,reason,risk_level,created_at) VALUES(?,?,?,?,?,?)",
        (review_id, version_id, source_id, reason, risk_level, now_iso()),
    )
    return review_id


def list_reviews(db, status="OPEN"):
    rows = db.execute("SELECT * FROM review_queue WHERE status=? ORDER BY created_at DESC", (status,))
    return rows_to_dicts(rows)


def get_review(db, review_id):
    row = db.execute("SELECT * FROM review_queue WHERE review_id=?", (review_id,)).fetchone()
    return dict(row) if row else None


def decide_review(db, review_id, decision, actor, note):
    db.execute(
        "UPDATE review_queue SET status=?, decision=?, decided_by=?, decided_at=?, decision_note=? WHERE review_id=?",
        ("DECIDED", decision, actor, now_iso(), note, review_id),
    )


# ---------- 审计与事件 ----------

def audit(db, actor, action, entity, entity_id, detail=None):
    db.execute("INSERT INTO audit_log(at,actor,action,entity,entity_id,detail_json) VALUES(?,?,?,?,?,?)",
               (now_iso(), actor, action, entity, entity_id,
                json.dumps(detail, ensure_ascii=False) if detail is not None else None))


def add_event(db, event_type, source_id, version_id=None, detail=None):
    db.execute("INSERT INTO knowledge_events VALUES(?,?,?,?,?,?)",
               (new_id("evt"), event_type, source_id, version_id, now_iso(),
                json.dumps(detail, ensure_ascii=False) if detail is not None else None))
