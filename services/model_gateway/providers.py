"""Provider adapters behind the gateway. Services never import these directly."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any

import httpx
from llm_client.types import LlmRequest, ProviderReply

LocalHandler = Callable[[LlmRequest], Any]


def _approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


class DeterministicLocalProvider:
    """In-process deterministic 'models' (grammars, mappers, templates). No network, no
    retention, fully reproducible. Used where an LLM is unnecessary and as a safe fallback."""

    def __init__(self) -> None:
        self.handlers: dict[str, LocalHandler] = {}

    def register(self, model: str, fn: LocalHandler) -> None:
        self.handlers[model] = fn

    def complete(
        self,
        *,
        model: str,
        system: str,
        user: str,
        max_tokens: int,
        output_schema: dict[str, Any] | None,
        request: LlmRequest,
    ) -> ProviderReply:
        if model not in self.handlers:
            raise RuntimeError(f"no local handler for {model}")
        out = self.handlers[model](request)
        text = out if isinstance(out, str) else json.dumps(out)
        return ProviderReply(text, _approx_tokens(user), _approx_tokens(text))


class OpenAICompatibleProvider:
    """An OpenAI-compatible chat-completions endpoint.

    The same adapter serves self-hosted vLLM and explicitly configured prototype providers.
    Provider policy (data class, region and retention) remains in ``ModelGateway``; this class
    only handles the HTTP contract.
    """

    def __init__(
        self,
        base_url: str,
        client: httpx.Client | None = None,
        api_key_env: str = "VLLM_API_KEY",
        *,
        require_api_key: bool = False,
        strict_json_schema: bool = True,
    ) -> None:
        self.client = client or httpx.Client(base_url=base_url, timeout=60.0)
        self.api_key_env = api_key_env
        self.api_key = os.environ.get(api_key_env, "")
        self.require_api_key = require_api_key
        self.strict_json_schema = strict_json_schema

    def complete(
        self,
        *,
        model: str,
        system: str,
        user: str,
        max_tokens: int,
        output_schema: dict[str, Any] | None,
        request: LlmRequest,
    ) -> ProviderReply:
        if self.require_api_key and not self.api_key:
            raise RuntimeError(f"{self.api_key_env} is not set")
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": 0,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if output_schema:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "output",
                    "schema": output_schema,
                    "strict": self.strict_json_schema,
                },
            }
        r = self.client.post(
            "/chat/completions",
            json=body,
            headers={"Authorization": f"Bearer {self.api_key}"} if self.api_key else {},
        )
        r.raise_for_status()
        data = r.json()
        usage = data.get("usage", {})
        return ProviderReply(
            data["choices"][0]["message"]["content"],
            int(usage.get("prompt_tokens", 0)),
            int(usage.get("completion_tokens", 0)),
        )


class AnthropicMessagesProvider:
    """Frontier model via an approved private, zero-retention endpoint (Messages API shape)."""

    def __init__(
        self,
        base_url: str,
        client: httpx.Client | None = None,
        api_key_env: str = "ANTHROPIC_PRIVATE_API_KEY",
    ) -> None:
        self.client = client or httpx.Client(base_url=base_url, timeout=120.0)
        self.api_key = os.environ.get(api_key_env, "")

    def complete(
        self,
        *,
        model: str,
        system: str,
        user: str,
        max_tokens: int,
        output_schema: dict[str, Any] | None,
        request: LlmRequest,
    ) -> ProviderReply:
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        if output_schema:
            body["output_config"] = {"format": {"type": "json_schema", "schema": output_schema}}
        r = self.client.post(
            "/v1/messages",
            json=body,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )
        r.raise_for_status()
        data = r.json()
        text = "".join(
            b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
        )
        usage = data.get("usage", {})
        return ProviderReply(
            text, int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))
        )


class ScriptedProvider:
    """Test double: returns queued replies and records every prompt it saw."""

    def __init__(self, replies: list[str] | None = None, fail: bool = False) -> None:
        self.replies = list(replies or [])
        self.fail = fail
        self.seen: list[tuple[str, str, str]] = []

    def complete(
        self,
        *,
        model: str,
        system: str,
        user: str,
        max_tokens: int,
        output_schema: dict[str, Any] | None,
        request: LlmRequest,
    ) -> ProviderReply:
        self.seen.append((model, system, user))
        if self.fail:
            raise RuntimeError("scripted provider failure")
        text = self.replies.pop(0) if self.replies else "{}"
        return ProviderReply(text, _approx_tokens(user), _approx_tokens(text))
