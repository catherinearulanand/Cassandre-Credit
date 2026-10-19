"""Structured JSON logging with PII scrubbing as a last line of defence.

Callers must not log PII. This filter additionally masks PAN, Aadhaar-like numbers,
long account numbers, emails and phone numbers if they slip through.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from contextvars import ContextVar
from typing import Any

correlation_id_var: ContextVar[str | None] = ContextVar("correlation_id", default=None)

_PII_PATTERNS = [
    (re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b", re.I), "[PAN]"),
    (re.compile(r"\b[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]\b", re.I), "[GSTIN]"),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b"), "[EMAIL]"),
    (re.compile(r"(?<![\w])(?:\+?91[\s.\-]?)?[6-9](?:[\s.\-]{0,2}\d){9}(?!\d)"), "[PHONE]"),
    (re.compile(r"\b[A-Z]{1,3}\d{8,16}\b", re.I), "[ACCT]"),
    (re.compile(r"(?<![\d,.])\d(?:[\s.\-]{0,2}\d){8,17}(?![\d,]|\.\d)"), "[ACCT]"),
]


def scrub(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    for pat, repl in _PII_PATTERNS:
        text = pat.sub(repl, text)
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": scrub(record.getMessage()),
            "correlation_id": correlation_id_var.get(),
        }
        for key in ("obligor_id", "cts_id", "case_id", "event_type"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exc"] = scrub(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
