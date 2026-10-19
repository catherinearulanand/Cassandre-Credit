"""Canonical JSON (sorted keys, no whitespace, decimals as strings) and content hashing.

Used for CTS ``content_hash``, event hash chains and signatures. Two semantically equal
documents always produce the same bytes.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel


def _default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return format(o, "f")
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, date):
        return o.isoformat()
    if isinstance(o, Enum):
        return o.value
    if isinstance(o, BaseModel):
        return o.model_dump(mode="python")
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    if isinstance(o, tuple):
        return list(o)
    if isinstance(o, float):
        raise TypeError("floats are not allowed in canonical documents")
    raise TypeError(f"not canonicalisable: {type(o).__name__}")


def _normalise(o: Any) -> Any:
    if isinstance(o, BaseModel):
        return _normalise(o.model_dump(mode="python"))
    if isinstance(o, dict):
        return {str(k): _normalise(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_normalise(v) for v in o]
    if isinstance(o, float):
        # Citations carry normalised bbox floats; canonicalise them to fixed precision strings.
        return format(Decimal(repr(o)).quantize(Decimal("0.000001")), "f")
    return o


def canonical_json(obj: Any) -> bytes:
    return json.dumps(
        _normalise(obj), sort_keys=True, separators=(",", ":"), default=_default, ensure_ascii=False
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_hash(obj: Any) -> str:
    return sha256_hex(canonical_json(obj))
