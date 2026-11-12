"""Typed data contracts (Pydantic) shared by every stage. Invalid shapes fail loudly instead of silently."""
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class FieldValue(BaseModel):
    value: Any = None
    confidence: float = 0.0          # 0..1
    source: str = ""                 # rules / ai only / rules+ai agree / +arith check ...


class LineItem(BaseModel):
    item_code: Optional[str] = None
    description: str
    quantity: float
    unit_price: float
    amount: float
    confidence: float = 0.0


class ExtractedInvoice(BaseModel):
    fields: dict[str, FieldValue]
    line_items: list[LineItem] = Field(default_factory=list)
    method: str = ""                 # text-layer | ocr
    text_chars: int = 0
    ocr_mean_conf: Optional[float] = None
    ai_status: str = "disabled"
    merge_notes: list[str] = Field(default_factory=list)
    raw_text: str = ""

    def v(self, name):
        f = self.fields.get(name)
        return f.value if f else None

    def c(self, name):
        f = self.fields.get(name)
        return f.confidence if f else 0.0


Outcome = Literal["PASS", "WARN", "FAIL", "INFO", "SKIP"]   # SKIP = check could not run; never counts as a pass
Effect = Literal["NONE", "REVIEW", "REJECT", "DUPLICATE"]


class Check(BaseModel):
    code: str
    stage: str
    name: str
    outcome: Outcome
    effect: Effect = "NONE"
    message: str
    amount_at_risk: float = 0.0
    action: str = ""                 # recommended next step if this check fails


class StageResult(BaseModel):
    key: str
    name: str
    status: Literal["ok", "warn", "fail", "error", "skipped"] = "ok"
    duration_ms: float = 0
    summary: str = ""
    details: dict = Field(default_factory=dict)


class Decision(BaseModel):
    decision: Literal["APPROVE", "REVIEW", "REJECT", "DUPLICATE"]
    headline: str
    reasons: list[str] = Field(default_factory=list)
    next_action: str = ""
    exception_value: float = 0.0
    trace: dict = Field(default_factory=dict)   # structured, deterministic explanation of the decision
