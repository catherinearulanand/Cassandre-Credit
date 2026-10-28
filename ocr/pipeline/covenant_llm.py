"""Prototype bridge from word-level PaddleOCR output to schema-constrained LLM proposals.

This module is intentionally synthetic-only. Real borrower text remains restricted to the bank's
self-hosted model route. Every proposed covenant cites the OCR row(s) from which it came and still
requires human review before it can enter a Credit Term Sheet.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from domain.lineage import Citation
from llm_client import LlmClient

from ocr.pipeline.engine import Word
from ocr.pipeline.structure import group_rows

PROMPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "agents"
    / "sanction_compiler"
    / "prompts"
    / "prototype_extract.md"
)

COVENANT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "kind": {
                        "type": "string",
                        "enum": ["financial", "information", "affirmative", "negative", "other"],
                    },
                    "operator": {"type": ["string", "null"]},
                    "threshold": {"type": ["string", "null"]},
                    "test_frequency": {"type": ["string", "null"]},
                    "cure_period_days": {"type": ["integer", "null"]},
                    "source_ids": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": [
                    "name",
                    "kind",
                    "operator",
                    "threshold",
                    "test_frequency",
                    "cure_period_days",
                    "source_ids",
                    "confidence",
                ],
                "additionalProperties": False,
            },
        },
        "unresolved": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["candidates", "unresolved"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class CovenantCandidate:
    name: str
    kind: str
    operator: str | None
    threshold: str | None
    test_frequency: str | None
    cure_period_days: int | None
    confidence: float
    citations: tuple[Citation, ...]
    requires_human_review: bool = True


@dataclass(frozen=True)
class CovenantProposalSet:
    candidates: tuple[CovenantCandidate, ...]
    unresolved: tuple[str, ...]
    provider: str
    model: str
    call_id: str


def _instruction() -> str:
    text = PROMPT_PATH.read_text(encoding="utf-8")
    return text.split("---", 2)[-1].strip()


def extract_synthetic_covenants(
    words: list[Word],
    *,
    page_width: float,
    page_height: float,
    document_id: str,
    llm: LlmClient,
    synthetic: bool,
) -> CovenantProposalSet:
    if not synthetic:
        raise ValueError("the free Groq route is synthetic-only; use a self-hosted model for borrower data")
    rows = [r for r in group_rows(words) if r.text.strip()]
    if not rows:
        raise ValueError("OCR returned no text rows")

    row_words: dict[str, list[Word]] = {}
    blocks: list[tuple[str, str]] = []
    for number, row in enumerate(rows, 1):
        source_id = f"{document_id}:p1:r{number}"
        row_words[source_id] = row.words
        blocks.append((source_id, row.text))

    response = llm.structured(
        use_case="prototype.covenant_extract",
        instruction=_instruction(),
        data=blocks,
        schema=COVENANT_SCHEMA,
        data_class="public",
        context={"synthetic": True},
        max_tokens=4000,
    )
    parsed = response.parsed
    candidates: list[CovenantCandidate] = []
    for raw in parsed["candidates"]:
        source_ids = raw["source_ids"]
        if not source_ids or any(s not in row_words for s in source_ids):
            raise ValueError("LLM returned a missing or unknown source row")
        citations = []
        for source_id in source_ids:
            ws = row_words[source_id]
            citations.append(
                Citation(
                    document_id=document_id,
                    page=1,
                    bbox=(
                        min(w.x0 for w in ws) / page_width,
                        min(w.y0 for w in ws) / page_height,
                        max(w.x1 for w in ws) / page_width,
                        max(w.y1 for w in ws) / page_height,
                    ),
                    text_span=" ".join(w.text for w in ws)[:120],
                    confidence=min(w.conf for w in ws),
                )
            )
        candidates.append(
            CovenantCandidate(
                name=raw["name"],
                kind=raw["kind"],
                operator=raw["operator"],
                threshold=raw["threshold"],
                test_frequency=raw["test_frequency"],
                cure_period_days=raw["cure_period_days"],
                confidence=float(raw["confidence"]),
                citations=tuple(citations),
            )
        )
    return CovenantProposalSet(
        tuple(candidates),
        tuple(parsed["unresolved"]),
        response.provider,
        response.model,
        response.call_id,
    )
