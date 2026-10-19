"""Contract export and backward-compatibility check (``make contracts-check``).

Within a major version, a change is backward compatible iff:
- no property is removed,
- no new *required* property is added,
- no property changes type,
- no enum loses a value.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from events.catalogue import CATALOGUE

CONTRACTS_DIR = Path(__file__).resolve().parents[2] / "contracts" / "events"


def current_schemas() -> dict[str, dict[str, Any]]:
    return {f"{t}.v{v}.json": m.model_json_schema() for (t, v), m in CATALOGUE.items()}


def export(dir_: Path = CONTRACTS_DIR) -> list[Path]:
    dir_.mkdir(parents=True, exist_ok=True)
    out = []
    for name, schema in current_schemas().items():
        p = dir_ / name
        p.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        out.append(p)
    return out


def _resolve(schema: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    ref = node.get("$ref")
    if ref and ref.startswith("#/$defs/"):
        return schema.get("$defs", {})[ref.split("/")[-1]]  # type: ignore[no-any-return]
    return node


def _type_sig(schema: dict[str, Any], node: dict[str, Any]) -> str:
    node = _resolve(schema, node)
    if "anyOf" in node:
        return "|".join(sorted(_type_sig(schema, n) for n in node["anyOf"]))
    if "enum" in node:
        return "enum"
    if "const" in node:
        return "const"
    return str(node.get("type", "object"))


def breaking_changes(old: dict[str, Any], new: dict[str, Any], path: str = "") -> list[str]:
    problems: list[str] = []
    old_props: dict[str, Any] = old.get("properties", {})
    new_props: dict[str, Any] = new.get("properties", {})
    old_req, new_req = set(old.get("required", [])), set(new.get("required", []))
    for name, spec in old_props.items():
        if name not in new_props:
            problems.append(f"{path}{name}: removed")
            continue
        if _type_sig(old, spec) != _type_sig(new, new_props[name]):
            problems.append(f"{path}{name}: type changed")
        o, n = _resolve(old, spec), _resolve(new, new_props[name])
        if "enum" in o and "enum" in n and set(o["enum"]) - set(n["enum"]):
            problems.append(f"{path}{name}: enum values removed")
    for name in new_req - old_req:
        if name not in old_props:
            problems.append(f"{path}{name}: new required field")
        else:
            problems.append(f"{path}{name}: became required")
    return problems


def check(dir_: Path = CONTRACTS_DIR) -> list[str]:
    """Compare committed contracts with the code. Returns problems (empty = compatible)."""
    problems: list[str] = []
    current = current_schemas()
    for name, schema in current.items():
        p = dir_ / name
        if not p.exists():
            continue  # new event type: fine
        committed = json.loads(p.read_text(encoding="utf-8"))
        problems += [f"{name}: {x}" for x in breaking_changes(committed, schema)]
    for p in dir_.glob("*.json"):
        if p.name not in current:
            problems.append(f"{p.name}: contract file has no producer model (event type removed?)")
    return problems
