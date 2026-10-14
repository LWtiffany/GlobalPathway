"""业务 Schema：签证政策（多适用范围）与通用事实。模型输出仅作为候选知识。"""
from pydantic import BaseModel, ConfigDict, Field

VISA_FIELDS = {
    "effective_from", "effective_to", "cas_required", "i20_required",
    "funds_tuition_unpaid", "funds_tuition_currency", "funds_living_monthly",
    "funds_tuition_required",
    "funds_living_currency", "funds_living_months", "funds_holding_days",
    "funds_doc_max_age_days", "funds_owner_allowed", "english_level",
    "tb_test_required", "health_surcharge", "apply_window_days",
    "processing_days", "visa_fee", "applies_to_city", "conditions", "remark",
}


class Fact(BaseModel):
    """通用事实（适用于尚未定义专用 Schema 的页面）。"""
    topic: str = Field(description="Short name of a factual claim")
    value: str = Field(description="Claim exactly as supported by the source")
    evidence_quote: str = Field(description="Verbatim short quote from source")
    applicability: str | None = Field(default=None, description="Who/where/when this applies")


class Extraction(BaseModel):
    facts: list[Fact]


class VisaEvidence(BaseModel):
    field: str
    evidence_quote: str
    note: str | None = None
    # 由管道在页面门控后写入（模型不输出）；跨页面合并后仍能追溯到快照
    source_id: str | None = None
    snapshot_id: str | None = None


class DocumentSupport(BaseModel):
    """一条材料要求在某个页面快照上的逐字支撑。"""
    source_id: str
    snapshot_id: str
    evidence_quote: str
    condition_quote: str | None = None


class VisaDocument(BaseModel):
    code: str
    name_zh: str
    # True 必需 / False 仅提及 / None 原文带条件但条件未能确认（不得当作无条件必需）
    required: bool | None = None
    condition: dict | None = None
    evidence_quote: str
    # condition 必须附带逐字引用，避免把模型推断当成原文条件（门控强制）
    condition_quote: str | None = None
    support: list[DocumentSupport] = []


class VisaPolicyData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    effective_from: str | None = None
    effective_to: str | None = None
    cas_required: bool | None = None
    i20_required: bool | None = None
    funds_tuition_required: bool | None = None
    funds_tuition_unpaid: float | None = None
    funds_tuition_currency: str | None = None
    funds_living_monthly: float | None = None
    funds_living_currency: str | None = None
    funds_living_months: int | None = None
    funds_holding_days: int | None = None
    funds_doc_max_age_days: int | None = None
    funds_owner_allowed: str | None = None
    english_level: str | None = None
    tb_test_required: bool | None = None
    health_surcharge: float | None = None
    apply_window_days: int | None = None
    processing_days: int | None = None
    visa_fee: float | None = None
    applies_to_city: str | None = None
    conditions: list[dict] | None = None
    remark: str | None = None


class VisaPolicyEntry(BaseModel):
    """一个适用范围（如 London / Outside London）下的政策字段与字段级证据。"""
    policy: VisaPolicyData
    evidence: list[VisaEvidence] = []


class VisaPatch(BaseModel):
    policies: list[VisaPolicyEntry]
    documents: list[VisaDocument] = []


class GateProblem(BaseModel):
    """兼容管道门控问题，写入当前环境 legacy/gate/problems.json。"""
    problem_id: str
    at: str
    run_id: str
    source_id: str
    snapshot_hash: str
    check: str            # structure / applicability / evidence / change / conflict
    severity: str         # error（隔离）/ warning（转人工）
    field: str | None = None
    value: str | None = None
    quote: str | None = None
    reason: str
    suggested_action: str
    version_id: str | None = None
    status: str = "open"  # open / resolved
    resolution: str | None = None


class SourceConfig(BaseModel):
    """来源登记条目（设计 2.4 / A1.1 / A1.6）。"""
    source_id: str
    name: str
    url: str
    allowed_host: str
    category: str = "school"
    region: str = ""
    authority: str = "official"        # official / partner / third_party
    language: str = "en"
    access_basis: str = ""             # 授权方式 / 访问依据
    owner: str = ""                    # 负责人
    review_status: str = "approved"    # pending / approved / rejected
    enabled: bool = True
    check_interval_minutes: int = 1440
    freshness_sla_hours: int | None = None
    content_selector: str | None = None
    dynamic: bool = False
    schema_type: str = "generic_facts"  # visa_policy / generic_facts
    max_retries: int = 2
    notes: str = ""
