from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from llm_client import LlmClient
from llm_client.types import DataBlock, LlmRequest
from model_gateway.bootstrap import PROTOTYPE_ROUTES_PATH, build_providers, load_routes
from model_gateway.gateway import GatewayError, ModelGateway, PolicyViolation
from model_gateway.providers import OpenAICompatibleProvider, ScriptedProvider
from security.audit import AuditLog

from ocr.pipeline.covenant_llm import COVENANT_SCHEMA, extract_synthetic_covenants
from ocr.pipeline.engine import Word


def _prototype_config() -> dict:  # type: ignore[type-arg]
    return load_routes(PROTOTYPE_ROUTES_PATH)


def _request(*, synthetic: bool, data_class: str = "public") -> LlmRequest:
    return LlmRequest(
        use_case="prototype.covenant_extract",
        agent_id="sanction_compiler",
        human_principal="prototype-user",
        data_class=data_class,  # type: ignore[arg-type]
        instruction="Extract candidate covenants.",
        data=[DataBlock("doc:p1:r1", "DSCR shall be at least 1.25x quarterly")],
        output_schema=COVENANT_SCHEMA,
        max_tokens=1000,
        context={"synthetic": synthetic},
    )


def test_groq_http_contract_uses_key_and_best_effort_schema(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("GROQ_API_KEY", "test-only-key")
    seen: dict = {}  # type: ignore[type-arg]

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"candidates":[],"unresolved":[]}'}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 4},
            },
        )

    client = httpx.Client(
        base_url="https://api.groq.com/openai/v1", transport=httpx.MockTransport(handler)
    )
    provider = OpenAICompatibleProvider(
        "https://api.groq.com/openai/v1",
        client=client,
        api_key_env="GROQ_API_KEY",
        require_api_key=True,
        strict_json_schema=False,
    )
    reply = provider.complete(
        model="openai/gpt-oss-20b",
        system="system",
        user="user",
        max_tokens=100,
        output_schema=COVENANT_SCHEMA,
        request=_request(synthetic=True),
    )
    assert reply.tokens_in == 12 and reply.tokens_out == 4
    assert seen["authorization"] == "Bearer test-only-key"
    assert seen["body"]["response_format"]["json_schema"]["strict"] is False


def test_groq_provider_requires_key(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    providers = build_providers(_prototype_config())
    with pytest.raises(RuntimeError, match="GROQ_API_KEY is not set"):
        providers["groq_free"].complete(
            model="openai/gpt-oss-20b",
            system="system",
            user="user",
            max_tokens=100,
            output_schema=COVENANT_SCHEMA,
            request=_request(synthetic=True),
        )


def test_cross_border_route_requires_public_synthetic_context(clock) -> None:  # type: ignore[no-untyped-def]
    reply = json.dumps({"candidates": [], "unresolved": []})
    gateway = ModelGateway(
        clock,
        AuditLog(clock),
        {"groq_free": ScriptedProvider([reply, reply])},
        _prototype_config(),
    )
    assert gateway.complete(_request(synthetic=True)).provider == "groq_free"
    with pytest.raises(GatewayError, match="outside India"):
        gateway.complete(_request(synthetic=False))
    with pytest.raises(PolicyViolation, match="not permitted"):
        gateway.complete(_request(synthetic=True, data_class="confidential"))


def test_ocr_rows_become_cited_human_review_proposals(clock) -> None:  # type: ignore[no-untyped-def]
    output = {
        "candidates": [
            {
                "name": "Debt Service Coverage Ratio",
                "kind": "financial",
                "operator": ">=",
                "threshold": "1.25x",
                "test_frequency": "quarterly",
                "cure_period_days": 15,
                "source_ids": ["SYN-1:p1:r1"],
                "confidence": 0.91,
            }
        ],
        "unresolved": [],
    }
    gateway = ModelGateway(
        clock,
        AuditLog(clock),
        {"groq_free": ScriptedProvider([json.dumps(output)])},
        _prototype_config(),
    )
    llm = LlmClient(gateway, agent_id="sanction_compiler", human_principal="prototype-user")
    words = [
        Word("DSCR", 10, 20, 55, 35, 0.98),
        Word("shall", 60, 20, 95, 35, 0.97),
        Word("be", 100, 20, 115, 35, 0.97),
        Word("at", 120, 20, 135, 35, 0.96),
        Word("least", 140, 20, 175, 35, 0.96),
        Word("1.25x", 180, 20, 220, 35, 0.99),
        Word("quarterly", 225, 20, 300, 35, 0.97),
    ]
    result = extract_synthetic_covenants(
        words,
        page_width=400,
        page_height=600,
        document_id="SYN-1",
        llm=llm,
        synthetic=True,
    )
    candidate = result.candidates[0]
    assert candidate.threshold == "1.25x"
    assert candidate.requires_human_review
    assert candidate.citations[0].document_id == "SYN-1"
    assert candidate.citations[0].page == 1
    assert candidate.citations[0].text_span.startswith("DSCR shall")


def test_prototype_extractor_refuses_non_synthetic_input() -> None:
    with pytest.raises(ValueError, match="synthetic-only"):
        extract_synthetic_covenants(
            [Word("DSCR", 0, 0, 10, 10, 1.0)],
            page_width=100,
            page_height=100,
            document_id="REAL-1",
            llm=None,  # type: ignore[arg-type]
            synthetic=False,
        )


def test_prompt_file_exists() -> None:
    assert Path("agents/sanction_compiler/prompts/prototype_extract.md").exists()
