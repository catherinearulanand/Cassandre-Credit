"""Hardened agent runtime (P3-01).

An agent is a bounded caller of registered tools on behalf of one named human. The runtime
enforces, in code and not in the prompt:

* **deny by default**: a tool must be in the agent's allow-list and must not match a denied pattern;
* **no write tools**: a tool is ``read``, ``compute`` or ``propose``; proposing creates a pending
  proposal that a human decides. The runtime stamps the agent identity and the human it acts for
  onto it, whatever the tool's label, and an agent cannot pass those fields;
* **proposal kinds**: a policy lists the kinds an agent may propose (``propose_kinds``);
* **scope**: every call is authorised against the resource it names (case, obligor), resolved by the
  application, not against one fixed scope;
* **arguments**: only parameters the tool declares are accepted;
* **budgets**: failed and denied attempts count;
* **numbers come from tools**: drafted prose is checked by ``verify_numbers``: every digit run must be
  a fact from the pack, with its unit, or the draft is replaced by a deterministic template;
* **audit**: every call, including denied and failed ones, is recorded and emitted once as
  ``agent.run.completed``, even when the run aborts.
"""

from __future__ import annotations

import fnmatch
import inspect
import re
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

import yaml
from authz import Permission, Principal, ResourceScope, authorize
from events.catalogue import AgentRunCompleted, Payload
from platform_core.canonical import content_hash
from platform_core.errors import AuthorizationError, DomainError, ValidationFailure
from platform_core.ids import new_id

ToolKind = Literal["read", "compute", "propose"]
Emit = Callable[[str, Payload], None]
ScopeResolver = Callable[[str, dict[str, Any]], ResourceScope]


class AgentDenied(DomainError):
    code = "agent_denied"
    http_status = 403


class AgentBudgetExceeded(DomainError):
    code = "agent_budget_exceeded"
    http_status = 429


@dataclass(frozen=True)
class ToolSpec:
    name: str
    fn: Callable[..., Any]
    kind: ToolKind
    permission: Permission | None = None  # right the agent's human must hold to use this tool


@dataclass(frozen=True)
class AgentSpec:
    agent_id: str
    version: int
    owner: str
    risk_tier: int
    tools_allowed: tuple[str, ...]
    tools_denied: tuple[str, ...]
    llm_use_cases: tuple[str, ...]
    propose_kinds: tuple[str, ...] = ()
    max_tool_calls: int = 25

    @classmethod
    def load(cls, path: Path) -> AgentSpec:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls(
            agent_id=raw["agent_id"],
            version=int(raw["version"]),
            owner=raw["owner"],
            risk_tier=int(raw["risk_tier"]),
            tools_allowed=tuple(raw["tools_allowed"]),
            tools_denied=tuple(raw.get("tools_denied_examples", [])),
            llm_use_cases=tuple(raw.get("llm_use_cases", [])),
            propose_kinds=tuple(raw.get("propose_kinds", [])),
            max_tool_calls=int(raw.get("max_tool_calls", 25)),
        )


_STAMPED = ("proposed_by", "proposer_kind", "on_behalf_of", "required_permission")


class AgentRun:
    def __init__(
        self,
        spec: AgentSpec,
        human: Principal,
        scope_resolver: ScopeResolver,
        tools: dict[str, ToolSpec],
        *,
        emit: Emit | None = None,
    ) -> None:
        self.spec, self.human = spec, human
        self._resolve_scope = scope_resolver
        self.principal = Principal.for_agent(spec.agent_id, human)
        self.run_id = new_id("RUN")
        self._tools = tools
        self._emit: Emit = emit or (lambda t, p: None)
        self.calls: list[dict[str, Any]] = []
        self.denied = 0
        self._event: AgentRunCompleted | None = None
        for name in spec.tools_allowed:
            t = tools.get(name)
            if t is None:
                continue
            if t.kind not in ("read", "compute", "propose"):
                raise ValidationFailure(f"tool {name} has an unsupported kind", code="bad_tool")
            if name.startswith("case.propose") != (t.kind == "propose"):
                raise ValidationFailure(
                    f"tool {name}: kind {t.kind} does not match its name", code="bad_tool"
                )

    @property
    def finished(self) -> bool:
        return self._event is not None

    def _deny(self, name: str, why: str) -> AgentDenied:
        self.denied += 1
        self.calls.append({"tool": name, "ok": False, "denied": why})
        return AgentDenied(f"{self.spec.agent_id} may not call {name}: {why}")

    def call(self, name: str, **kwargs: Any) -> Any:
        if self.finished:
            raise AgentDenied("run already finished")
        if len(self.calls) >= 4 * self.spec.max_tool_calls:
            raise AgentBudgetExceeded("too many attempted calls, including denied ones")
        if any(fnmatch.fnmatchcase(name, pat) for pat in self.spec.tools_denied):
            raise self._deny(name, "explicitly denied")
        if name not in self.spec.tools_allowed:
            raise self._deny(name, "not in allow-list")
        tool = self._tools.get(name)
        if tool is None:
            raise self._deny(name, "not registered")
        executed = sum(1 for c in self.calls if "denied" not in c)
        if executed >= self.spec.max_tool_calls:
            raise AgentBudgetExceeded(f"more than {self.spec.max_tool_calls} tool calls")
        clash = [k for k in _STAMPED if k in kwargs]
        if clash:  # whatever the tool's label: identity is the runtime's to set
            raise self._deny(name, f"agent cannot set {', '.join(clash)}")
        if tool.kind == "propose" and kwargs.get("kind") not in self.spec.propose_kinds:
            raise self._deny(name, f"proposal kind {kwargs.get('kind')!r} not permitted")
        try:
            scope = self._resolve_scope(name, kwargs)
            authorize(self.principal, tool.permission or Permission.READ_OBLIGOR, scope)
        except (AuthorizationError, KeyError, LookupError) as exc:
            raise self._deny(name, getattr(exc, "code", "unresolvable_scope")) from exc
        if tool.kind == "propose":
            kwargs.update(
                proposed_by=self.principal.id, proposer_kind="agent", on_behalf_of=self.human.id
            )
        try:
            inspect.signature(tool.fn).bind(**kwargs)
        except TypeError as exc:
            raise self._deny(name, "arguments do not match the tool's parameters") from exc
        entry: dict[str, Any] = {
            "tool": name,
            "kind": tool.kind,
            "args": content_hash({k: v for k, v in kwargs.items() if k not in _STAMPED})[:16],
        }
        try:
            result = tool.fn(**kwargs)
        except Exception as exc:
            entry.update(ok=False, error=type(exc).__name__)
            self.calls.append(entry)
            raise
        entry["ok"] = True
        self.calls.append(entry)
        return result

    def finish(self, output: Any) -> AgentRunCompleted:
        if self._event is not None:
            return self._event  # idempotent: one completion event per run
        self._event = AgentRunCompleted(
            run_id=self.run_id,
            agent_id=self.spec.agent_id,
            human_principal=self.human.id,
            tool_calls=list(self.calls),
            output_ref=content_hash(output)[:24],
            denied_calls=self.denied,
        )
        self._emit("agent.run.completed", self._event)
        return self._event

    def close(self) -> None:
        """Call in a ``finally``: an aborted run is still audited."""
        if not self.finished:
            self.finish({"aborted": True})


# ------------------------------------------------------------------ number binding ----------
_TOKEN = re.compile(r"(\d[\d,]*(?:\.\d+)?|\.\d+)")
_UNIT_AFTER = re.compile(
    r"\s{0,2}(%|pct|percent|per\s?cent|bps|bp|basis\s+points?|crores?|cr|lakhs?|lacs?|"
    r"millions?|mn|billions?|bn|thousands?|k|x|times)(?![a-z])",
    re.IGNORECASE,
)
_CURRENCY_BEFORE = re.compile(r"(?:₹|rs\.?|inr|rupees?)\s{0,2}$", re.IGNORECASE)
_NUMBER_WORDS = re.compile(
    r"\b(zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|"
    r"fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|"
    r"eighty|ninety|hundred|thousand|lakh|lac|crore|million|billion|half|double|triple|twice|"
    r"thrice|first|second|third|fourth|fifth)\b",
    re.IGNORECASE,
)
_UNIT_CANON = {
    "%": "pct", "pct": "pct", "percent": "pct", "percentage": "pct",
    "bps": "bps", "bp": "bps", "crore": "cr", "crores": "cr", "cr": "cr",
    "lakh": "lakh", "lakhs": "lakh", "lac": "lakh", "lacs": "lakh",
    "million": "mn", "millions": "mn", "mn": "mn", "billion": "bn", "billions": "bn",
    "bn": "bn", "thousand": "k", "thousands": "k", "k": "k", "x": "x", "times": "x",
}  # fmt: skip


def _digits_to_ascii(text: str) -> str:
    out = []
    for ch in unicodedata.normalize("NFKC", text):
        out.append(str(unicodedata.digit(ch)) if unicodedata.category(ch) == "Nd" else ch)
    return "".join(out)


def _unit(after: str, before: str) -> str:
    m = _UNIT_AFTER.match(after)
    unit = ""
    if m:
        raw = re.sub(r"\s+", " ", m.group(1).lower())
        unit = "bps" if raw.startswith("basis") else "pct" if raw.startswith("per") else raw
        unit = _UNIT_CANON.get(unit, unit)
    if _CURRENCY_BEFORE.search(before):
        unit = "inr" + unit
    return unit


def _numbers(text: str) -> list[tuple[Decimal, str]]:
    """(value, unit) for every digit run. Nothing is exempt: ids, dates, ranges and glued
    suffixes (250bps, 5crore) are all numbers, and a sign counts."""
    t = _digits_to_ascii(text)
    out: list[tuple[Decimal, str]] = []
    for m in _TOKEN.finditer(t):
        raw = m.group(1).replace(",", "")
        try:
            val = Decimal(raw).normalize()
        except InvalidOperation:
            continue
        start = m.start()
        if start >= 1 and t[start - 1] == "-" and (start == 1 or not t[start - 2].isalnum()):
            val = -val
        out.append((val, _unit(t[m.end() :], t[:start].rstrip("-"))))
    return out


def verify_numbers(text: str, allowed: Iterable[str]) -> list[str]:
    """Numbers (or number words) in ``text`` that are not facts in ``allowed``.

    A number is accepted only if the same value *and unit* occurs in a fact (so 1.42 is not 1.42%,
    paise are not rupees, 5 is not 5 crore). Identifiers and dates are not exempt: they pass only
    when they appear verbatim in a fact. Number words and scale words pass only when the same word
    appears in a fact.
    """
    facts = [str(a) for a in allowed]
    pool = {n for f in facts for n in _numbers(f)}
    scrubbed = text
    for f in sorted({f.strip() for f in facts if len(f.strip()) >= 3}, key=len, reverse=True):
        if _VERBATIM.fullmatch(f):
            scrubbed = scrubbed.replace(f, " ")  # verbatim ids and dates that are themselves facts
    bad = [
        f"{v.normalize():f}{u}" if u else f"{v.normalize():f}"
        for v, u in _numbers(scrubbed)
        if (v, u) not in pool
    ]
    fact_words = {w.lower() for f in facts for w in _NUMBER_WORDS.findall(f)}
    bad += [w for w in _NUMBER_WORDS.findall(scrubbed) if w.lower() not in fact_words]
    return bad


_VERBATIM = re.compile(
    r"[A-Za-z]{2,8}[-_]\d[\w-]*|\d{4}-\d{2}-\d{2}(?:T[\d:.+Z-]+)?"
)  # ids and ISO dates
_FORBIDDEN_PROMISES = re.compile(
    r"\b(waiv\w*|forbear\w*|forgiv\w*|condon\w*|relax\w*|extend\w*|extension|grant\w* "
    r"(?:a |an )?(?:cure|grace)|no objection|withdraw\w*|release\w* (?:the )?(?:security|charge))\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Draft:
    text: str
    source: Literal["llm", "template"]
    unsupported_numbers: tuple[str, ...] = ()
    fallback_reason: str | None = None
    call_id: str | None = None
    provider: str | None = None


_DRAFT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"text": {"type": "string", "minLength": 40, "maxLength": 6000}},
    "required": ["text"],
    "additionalProperties": False,
}


def draft_narrative(
    llm: Any | None,
    *,
    use_case: str,
    instruction: str,
    facts: dict[str, str],
    template: Callable[[dict[str, str]], str],
    data_class: str = "confidential",
    person_names: list[str] | None = None,
    required_phrases: tuple[str, ...] = (),
    allow_promises: bool = False,
) -> Draft:
    """Prose from facts. The model may phrase, never compute or concede.

    Facts reach the model as screened, redacted data blocks (never as an unscreened side channel).
    The reply is accepted only if every number is a fact (with its unit), it makes no promise of
    waiver, extension or release unless ``allow_promises``, and it contains each required phrase.
    Anything else, or any gateway refusal, yields the deterministic template."""
    fallback = template(facts)
    if llm is None:
        return Draft(fallback, "template", fallback_reason="no model configured")
    try:
        resp = llm.structured(
            use_case=use_case,
            instruction=instruction,
            data=[(f"fact:{k}", f"{k}: {v}") for k, v in sorted(facts.items())],
            schema=_DRAFT_SCHEMA,
            data_class=data_class,
            person_names=person_names,
            max_tokens=1500,
        )
    except DomainError as exc:
        return Draft(fallback, "template", fallback_reason=f"gateway: {exc.code}")
    parsed = getattr(resp, "parsed", None)
    text = parsed.get("text") if isinstance(parsed, dict) else None
    call_id, provider = getattr(resp, "call_id", None), getattr(resp, "provider", None)
    if not isinstance(text, str) or len(text.strip()) < 40:
        return Draft(fallback, "template", fallback_reason="model returned no usable text")
    bad = verify_numbers(text, facts.values())
    if bad:
        return Draft(
            fallback,
            "template",
            unsupported_numbers=tuple(bad),
            fallback_reason="model quoted numbers that are not in the fact pack",
            call_id=call_id,
            provider=provider,
        )
    if not allow_promises and _FORBIDDEN_PROMISES.search(text):
        return Draft(
            fallback,
            "template",
            fallback_reason="model made or implied a concession",
            call_id=call_id,
            provider=provider,
        )
    low = text.lower()
    missing = [p for p in required_phrases if p.lower() not in low]
    if missing:
        return Draft(
            fallback,
            "template",
            fallback_reason=f"model omitted required wording: {missing[0]}",
            call_id=call_id,
            provider=provider,
        )
    return Draft(text, "llm", call_id=call_id, provider=provider)


@dataclass
class AgentResult:
    run: AgentRun
    proposals: list[str] = field(default_factory=list)
    drafts: dict[str, Draft] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def load_prompt(path: Path) -> tuple[dict[str, Any], str]:
    """Versioned prompt file: YAML front matter between ``---`` lines, then the instruction."""
    text = path.read_text(encoding="utf-8")
    parts = text.split("---\n", 2)
    if len(parts) < 3 or parts[0].strip():
        raise ValidationFailure(f"{path.name}: prompt needs front matter", code="bad_prompt")
    meta = yaml.safe_load(parts[1])
    if not isinstance(meta, dict) or "prompt_id" not in meta or "version" not in meta:
        raise ValidationFailure(f"{path.name}: prompt front matter incomplete", code="bad_prompt")
    return meta, parts[2].strip()


def closing(fn: Callable[..., AgentResult]) -> Callable[..., AgentResult]:
    """Decorator for an agent's ``run``: the run is audited even if the body raises."""

    def wrapper(agent_run: AgentRun, *args: Any, **kwargs: Any) -> AgentResult:
        try:
            return fn(agent_run, *args, **kwargs)
        finally:
            agent_run.close()

    wrapper.__name__, wrapper.__doc__ = fn.__name__, fn.__doc__
    return wrapper
