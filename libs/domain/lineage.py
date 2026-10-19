"""Lineage primitives: every extracted or computed number traces back to source pixels."""

from __future__ import annotations

from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

T = TypeVar("T")


class Citation(BaseModel):
    model_config = ConfigDict(frozen=True)

    document_id: str
    page: int = Field(ge=1)
    bbox: tuple[float, float, float, float]  # x0, y0, x1, y1 normalised 0..1
    text_span: str
    confidence: float = Field(ge=0, le=1)

    @field_validator("bbox")
    @classmethod
    def _bbox(cls, v: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
        x0, y0, x1, y1 = v
        if not (0 <= x0 <= x1 <= 1 and 0 <= y0 <= y1 <= 1):
            raise ValueError("bbox must be normalised with x0<=x1, y0<=y1 in [0,1]")
        return v


class HumanEntry(BaseModel):
    """Replaces citations for values a human typed. Carries the approval that allowed it."""

    model_config = ConfigDict(frozen=True)

    entered_by: str
    approval_id: str
    reason: str


ReviewStatus = Literal["auto", "needs_review", "human_corrected", "human_confirmed"]


class Extracted(BaseModel, Generic[T]):
    value: T | None
    raw_text: str | None = None
    citations: list[Citation] = []
    confidence: float = Field(ge=0, le=1)
    validation: list[str] = []
    review_status: ReviewStatus = "auto"
    human_entry: HumanEntry | None = None

    def has_lineage(self) -> bool:
        return bool(self.citations) or self.human_entry is not None


class LineageRef(BaseModel):
    """A pointer used in computed results: ``spread:SPR-1:line:pbt`` + its citations."""

    model_config = ConfigDict(frozen=True)

    ref: str
    citations: tuple[Citation, ...] = ()
