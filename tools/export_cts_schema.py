"""Export the CTS JSON Schema to contracts/cts/cts.v1.schema.json."""

from __future__ import annotations

import json
from pathlib import Path

from domain.cts import CreditTermSheet

OUT = Path(__file__).resolve().parents[1] / "contracts" / "cts" / "cts.v1.schema.json"


def export_cts() -> Path:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(CreditTermSheet.model_json_schema(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return OUT
