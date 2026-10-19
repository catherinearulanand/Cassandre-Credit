"""Request/response contracts shared by the LLM client and the gateway."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

DataClass = Literal["public", "internal", "confidential", "borrower_raw"]


@dataclass(frozen=True)
class DataBlock:
    """Untrusted content (document text, OCR, email). Always data, never instructions."""

    block_id: str
    text: str


@dataclass
class LlmRequest:
    use_case: str
    agent_id: str
    human_principal: str
    data_class: DataClass
    instruction: str  # trusted, from the versioned prompt file
    data: list[DataBlock] = field(default_factory=list)
    output_schema: dict[str, Any] | None = None
    person_names: list[str] = field(default_factory=list)
    max_tokens: int = 2000
    context: dict[str, Any] = field(default_factory=dict)  # structured inputs for local providers


@dataclass
class LlmResponse:
    call_id: str
    text: str
    parsed: Any
    provider: str
    model: str
    tokens_in: int
    tokens_out: int
    latency_ms: float
    used_fallback: bool
    redacted: bool
    injection: dict[str, Any]


@dataclass
class ProviderReply:
    text: str
    tokens_in: int
    tokens_out: int
