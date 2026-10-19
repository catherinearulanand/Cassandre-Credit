"""The only LLM client services and agents may use. It talks to the model gateway.

In-process here; in shared environments it calls ``POST /v1/llm`` on the gateway with the
caller's workload identity. Provider SDKs are banned outside ``services/model_gateway``
(enforced by ``tests/architecture/test_boundaries.py``).
"""

from __future__ import annotations

from typing import Any, Protocol

from llm_client.types import DataBlock, LlmRequest


class GatewayPort(Protocol):
    def complete(self, req: Any) -> Any: ...


class LlmClient:
    def __init__(self, gateway: GatewayPort, *, agent_id: str, human_principal: str) -> None:
        if not human_principal:
            raise ValueError("an LLM client is always bound to a human principal")
        self._gw = gateway
        self.agent_id = agent_id
        self.human_principal = human_principal

    def structured(
        self,
        *,
        use_case: str,
        instruction: str,
        data: list[tuple[str, str]],
        schema: dict[str, Any],
        data_class: str,
        person_names: list[str] | None = None,
        context: dict[str, Any] | None = None,
        max_tokens: int = 2000,
    ) -> Any:
        req = LlmRequest(
            use_case=use_case,
            agent_id=self.agent_id,
            human_principal=self.human_principal,
            data_class=data_class,  # type: ignore[arg-type]
            instruction=instruction,
            data=[DataBlock(i, t) for i, t in data],
            output_schema=schema,
            person_names=person_names or [],
            max_tokens=max_tokens,
            context=context or {},
        )
        return self._gw.complete(req)
