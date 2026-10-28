"""Configuration-driven construction of model providers.

Keeping construction here prevents services and agents from importing provider SDKs or learning
where credentials live. The free Groq prototype uses the existing OpenAI-compatible HTTP adapter;
no Groq-specific package is required.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from model_gateway.providers import DeterministicLocalProvider, OpenAICompatibleProvider

PROTOTYPE_ROUTES_PATH = Path(__file__).parent / "config" / "routes.prototype.yaml"


def load_routes(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("model route configuration must be an object")
    return data


def build_providers(config: dict[str, Any]) -> dict[str, Any]:
    providers: dict[str, Any] = {}
    for name, spec in config.get("providers", {}).items():
        kind = spec.get("kind")
        if kind == "deterministic_local":
            providers[name] = DeterministicLocalProvider()
        elif kind == "openai_compatible":
            providers[name] = OpenAICompatibleProvider(
                str(spec["base_url"]),
                api_key_env=str(spec.get("api_key_env", "VLLM_API_KEY")),
                require_api_key=bool(spec.get("require_api_key", False)),
                strict_json_schema=bool(spec.get("strict_json_schema", True)),
            )
        else:
            raise ValueError(f"unsupported provider kind {kind!r} for {name}")
    return providers
