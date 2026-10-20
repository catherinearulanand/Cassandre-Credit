"""Prompt-injection screening for untrusted document text (borrower PDFs, OCR, emails).

Layered: (1) normalisation that defeats zero-width/homoglyph spacing tricks, (2) weighted
signature rules, (3) structural signals (role markers, tool-call syntax, hidden-instruction
phrasing). A score >= ``block_threshold`` blocks the LLM step and raises a security signal;
between ``review_threshold`` and block, the text is passed only to schema-constrained
extraction with tools disabled.

This is a screen, not a guarantee. The real defences are architectural: document text is
always data in a delimited block, outputs are schema-constrained, and agents have no tools
that act without human approval.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u2060-\u2064\ufeff]")

_RULES: list[tuple[str, float, re.Pattern[str]]] = [
    (
        "override_instructions",
        0.6,
        re.compile(
            r"(ignore|disregard|forget|override)\s+(all\s+|any\s+|the\s+)?(previous|prior|above|earlier|system)"
            r"\s+(instructions?|prompts?|rules?|messages?)",
            re.I,
        ),
    ),
    (
        "new_role",
        0.45,
        re.compile(
            r"\byou\s+are\s+(now|no\s+longer)\b|\bact\s+as\s+(an?\s+)?(admin|system|developer)",
            re.I,
        ),
    ),
    (
        "role_marker",
        0.45,
        re.compile(
            r"(^|\n)\s*(system|assistant|developer)\s*:|<\|?(system|im_start|im_end)\|?>|\[/?INST\]",
            re.I,
        ),
    ),
    (
        "exfiltrate",
        0.5,
        re.compile(
            r"(reveal|print|output|send|leak)\s+(the\s+)?(system\s+prompt|instructions|api\s*keys?|secrets?|credentials)",
            re.I,
        ),
    ),
    (
        "approve_override",
        0.6,
        re.compile(
            r"(mark|classify|set|report)\s+(this|the)?\s*(covenant|account|borrower|loan|document)?\s*(as\s+)?"
            r"(compliant|complied|approved|standard|passed|genuine)",
            re.I,
        ),
    ),
    (
        "tool_syntax",
        0.35,
        re.compile(r"\b(function_call|tool_use|\"tool\"\s*:|<tool>|call\s+the\s+\w+\s+tool)", re.I),
    ),
    (
        "hidden_instruction",
        0.3,
        re.compile(
            r"(do\s+not\s+(tell|inform|mention)|without\s+(telling|informing)|secretly)", re.I
        ),
    ),
    (
        "threshold_tamper",
        0.5,
        re.compile(
            r"(threshold|limit|drawing\s+power)\s+(should|must)\s+be\s+(treated|read|taken)\s+as",
            re.I,
        ),
    ),
    (
        "disregard_above",
        0.6,
        re.compile(
            r"(disregard|forget|ignore)\s+(everything|anything|all)\s+(above|before|prior)"
            r"|forget\s+your\s+(instructions|rules|guidelines)",
            re.I,
        ),
    ),
    (
        "treat_as_satisfied",
        0.6,
        re.compile(
            r"treat\s+(the\s+)?(covenant|ratio|test|condition|account)s?\s+as\s+"
            r"(satisfied|met|complied|compliant|passed)",
            re.I,
        ),
    ),
    ("encoded_payload", 0.25, re.compile(r"(base64|rot13|decode\s+the\s+following)", re.I)),
]


_LEET = str.maketrans("013457@$", "oieatsas")
_LEET_RULES = {"override_instructions", "new_role", "disregard_above", "treat_as_satisfied"}


@dataclass
class InjectionVerdict:
    score: float
    action: str  # allow | constrain | block
    hits: list[str] = field(default_factory=list)


def normalise(text: str) -> str:
    t = unicodedata.normalize("NFKC", text)
    t = _ZERO_WIDTH.sub("", t)
    return re.sub(r"[ \t]+", " ", t)


def screen(
    text: str, *, review_threshold: float = 0.3, block_threshold: float = 0.6
) -> InjectionVerdict:
    t = normalise(text)
    score = 0.0
    hits: list[str] = []
    for name, weight, pat in _RULES:
        if pat.search(t):
            hits.append(name)
            score += weight
    # spaced-out evasion: "i g n o r e  p r e v i o u s"
    collapsed = re.sub(r"(?<=\b\w) (?=\w\b)", "", t)
    if collapsed != t:
        for name, weight, pat in _RULES[:2]:
            if name not in hits and pat.search(collapsed):
                hits.append(f"{name}:spaced")
                score += weight
    leet = t.translate(_LEET)  # "1gn0re pr3v1ous" -> "ignore previous"
    if leet != t:
        for name, weight, pat in _RULES:
            if name in _LEET_RULES and name not in hits and pat.search(leet):
                hits.append(f"{name}:leet")
                score += weight
    score = min(score, 1.0)
    action = (
        "block"
        if score >= block_threshold
        else "constrain"
        if score >= review_threshold
        else "allow"
    )
    return InjectionVerdict(score=round(score, 3), action=action, hits=hits)
