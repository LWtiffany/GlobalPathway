"""M07 Knowledge Read 适配器。

Agent 2 的唯一事实入口。S1 只有合成夹具实现（synthetic=true）；Agent 1 的审核发布与只读
接口完成后，以同一协议替换，编排与门控不变。本模块不连接 Agent 1 数据库。
"""
import json
from pathlib import Path
from typing import Protocol

from .models import KnowledgeRecord, ReadRecord

FIXTURES = Path(__file__).with_name("fixtures")
DEFAULT_FIXTURE = FIXTURES / "knowledge_uk_student_visa.json"

# Freshness SLA removed for answer generation: use published ACTIVE rows as-is.
# Effective dates / withdrawn / missing evidence still block definite answers.


class KnowledgeRead(Protocol):
    read_id: str

    def search(self, *, country, visa_category, topic, now) -> list[ReadRecord]: ...

    def get(self, record_id, revision_id, *, now) -> ReadRecord | None: ...


def availability(record: KnowledgeRecord, now):
    if record.status == "WITHDRAWN":
        return "withdrawn"
    if record.status != "ACTIVE":
        return "pending_review"
    today = now.date()
    if record.effective_from and today < record.effective_from:
        return "not_yet_effective"
    if record.effective_to and today > record.effective_to:
        return "expired"
    if record.snapshot is not None and any(
            not r.evidence_quote.strip() or r.evidence_quote not in record.snapshot
            for r in record.requirements):
        return "missing_evidence"
    return "available"


def read(record, now):
    reason = availability(record, now)
    return ReadRecord(record=record, available=reason == "available", availability_reason=reason)


class FixtureKnowledgeRead:
    """Synthetic fixture adapter. Refuses non-synthetic records so fixtures can never pose as real data."""

    def __init__(self, records, fixture_version):
        records = [r if isinstance(r, KnowledgeRecord) else KnowledgeRecord.model_validate(r) for r in records]
        real = [r.record_id for r in records if not r.synthetic]
        if real:
            raise ValueError(f"夹具适配器只接受 synthetic=true 的记录：{real}")
        self._records = {(r.record_id, r.revision_id): r for r in records}
        self.fixture_version = fixture_version
        self.read_id = f"fixture:{fixture_version}"

    @classmethod
    def from_file(cls, path=DEFAULT_FIXTURE):
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(data["records"], data["fixture_version"])

    def search(self, *, country, visa_category, topic, now):
        return [read(r, now) for r in self._records.values()
                if (r.country, r.visa_category, r.topic) == (country, visa_category, topic)]

    def get(self, record_id, revision_id, *, now):
        record = self._records.get((record_id, revision_id))
        return read(record, now) if record else None

    def withdraw(self, revision_id):
        for key, record in self._records.items():
            if record.revision_id == revision_id:
                self._records[key] = record.model_copy(update={"status": "WITHDRAWN"})

    def snapshot(self, revision_id):
        for record in self._records.values():
            if record.revision_id == revision_id:
                return {"record_id": record.record_id, "revision_id": revision_id,
                        "synthetic": record.synthetic, "source": record.source.model_dump(),
                        "snapshot": record.snapshot}
        return None
