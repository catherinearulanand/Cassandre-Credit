"""Hardened agent runtime (P3-01).

An agent is a bounded caller of registered tools on behalf of one named human. The runtime
enforces, in code and not in the prompt:

* **deny by default**: a tool must be in the agent's allow-list and must not match a denied pattern;
* **no write tools**: a tool is ``read``, ``compute`` or ``propose``; proposing creates a pending
  proposal that a human decides, and the runtime stamps the agent identity and the human it acts
  for onto it, so the agent cannot choose them;
* **scope**: the agent inherits its human's region, portfolio and obligor scope;
* **budgets**: at most ``max_tool_calls`` calls per run;
* **numbers come from tools**: any prose an agent drafts is checked, and a number that is not in
  the fact pack is rejected (``verify_numbers``). The model may phrase; it may not invent a figure;
* **audit**: every call, including denied ones, is recorded and emitted as ``agent.run.completed``.
"""

from __future__ import annotations

import fnmatch
import re
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
    permission: Permission | None = None  # right the agent's human must hold to read this


@dataclass(frozen=True)
class AgentSpec:
    agent_id: str
    version: int
    owner: str
    risk_tier: int
    tools_allowed: tuple[str, ...]
    tools_denied: tuple[str, ...]
    llm_use_cases: tuple[str, ...]
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
            max_tool_calls=int(raw.get("max_tool_calls", 25)),
        )


_STAMPED = ("proposed_by", "proposer_kind", "on_behalf_of")


class AgentRun:
    def __init__(
        self,
        spec: AgentSpec,
        human: Principal,
        scope: ResourceScope,
        tools: dict[str, ToolSpec],
        *,
        emit: Emit | None = None,
    ) -> None:
        self.spec, self.human, self.scope = spec, human, scope
        self.principal = Principal.for_agent(spec.agent_id, human)
        self.run_id = new_id("RUN")
        self._tools = tools
        self._emit: Emit = emit or (lambda t, p: None)
        self.calls: list[dict[str, Any]] = []
        self.denied = 0
        self._finished = False
        for name in spec.tools_allowed:
            t = tools.get(name)
            if t is not None and t.kind not in ("read", "compute", "propose"):
                raise ValidationFailure(f"tool {name} has an unsupported kind", code="bad_tool")

    def _deny(self, name: str, why: str) -> AgentDenied:
        self.denied += 1
        self.calls.append({"tool": name, "ok": False, "denied": why})
        return AgentDenied(f"{self.spec.agent_id} may not call {name}: {why}")

    def call(self, name: str, **kwargs: Any) -> Any:
        if self._finished:
            raise AgentDenied("run already finished")
        if len(self.calls) >= 4 * self.spec.max_tool_calls:
            raise AgentBudgetExceeded("too many attempted calls, including denied ones")
        if any(fnmatch.fnmatch(name, pat) for pat in self.spec.tools_denied):
            raise self._deny(name, "explicitly denied")
        if name not in self.spec.tools_allowed:
            raise self._deny(name, "not in allow-list")
        tool = self._tools.get(name)
        if tool is None:
            raise self._deny(name, "not registered")
        if sum(1 for c in self.calls if c.get("ok")) >= self.spec.max_tool_calls:
            raise AgentBudgetExceeded(f"more than {self.spec.max_tool_calls} tool calls")
        if tool.permission is not None:
            try:
                authorize(self.principal, tool.permission, self.scope)
            except AuthorizationError as exc:
                raise self._deny(name, exc.code) from exc
        if tool.kind == "propose":
            clash = [k for k in _STAMPED if k in kwargs]
            if clash:
                raise self._deny(name, f"agent cannot set {', '.join(clash)}")
            kwargs.update(
                proposed_by=self.principal.id, proposer_kind="agent", on_behalf_of=self.human.id
            )
        result = tool.fn(**kwargs)
        self.calls.append(
            {
                "tool": name,
                "kind": tool.kind,
                "args": content_hash({k: v for k, v in kwargs.items() if k not in _STAMPED})[:16],
                "ok": True,
            }
        )
        return result

    def finish(self, output: Any) -> AgentRunCompleted:
        self._finished = True
        ev = AgentRunCompleted(
            run_id=self.run_id,
            agent_id=self.spec.agent_id,
            human_principal=self.human.id,
            tool_calls=list(self.calls),
            output_ref=content_hash(output)[:24],
            denied_calls=self.denied,
        )
        self._emit("agent.run.completed", ev)
        return ev


# ------------------------------------------------------------------ number binding ----------
_NUM = re.compile(r"(?<![\w.-])(?:₹|Rs\.?\s?)?\d[\d,]*(?:\.\d+)?(?![\w-])%?")
_ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_ID_LIKE = re.compile(r"\b[A-Z]{2,}[-_][A-Za-z0-9-]+\b")


def _canon(token: str) -> Decimal | None:
    t = token.replace("₹", "").replace("Rs.", "").replace("Rs", "").replace(",", "")
    t = t.replace("%", "").strip()
    try:
        return Decimal(t).normalize()
    except InvalidOperation:
        return None


def verify_numbers(text: str, allowed: Iterable[str], *, ignore: Iterable[str] = ()) -> list[str]:
    """Numbers in ``text`` that are not among ``allowed`` (values from tool results).

    Dates in ISO form and identifiers such as ``CT-123`` are not numbers. A number is accepted
    if it equals an allowed value after removing commas, currency marks and percent signs.
    """
    pool: set[Decimal] = set()
    for a in list(allowed) + list(ignore):
        for tok in _NUM.findall(str(a)):
            d = _canon(tok)
            if d is not None:
                pool.add(d)
    scrubbed = _ID_LIKE.sub(" ", _ISO_DATE.sub(" ", text))
    bad: list[str] = []
    for tok in _NUM.findall(scrubbed):
        d = _canon(tok)
        if d is not None and d not in pool:
            bad.append(tok)
    return bad


@dataclass(frozen=True)
class Draft:
    text: str
    source: Literal["llm", "template"]
    unsupported_numbers: tuple[str, ...] = ()
    fallback_reason: str | None = None
    call_id: str | None = None


_DRAFT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"text": {"type": "string", "maxLength": 6000}},
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
) -> Draft:
    """Prose from facts. The model may phrase, never compute: its text is rejected if it quotes a
    number that is not in ``facts``, and the deterministic template is used instead. Any gateway
    refusal (kill switch, budget, policy, injection) also falls back to the template."""
    fallback = template(facts)
    if llm is None:
        return Draft(fallback, "template", fallback_reason="no model configured")
    try:
        resp = llm.structured(
            use_case=use_case,
            instruction=instruction,
            data=[],
            schema=_DRAFT_SCHEMA,
            data_class=data_class,
            person_names=person_names,
            context={"facts": facts},
            max_tokens=1500,
        )
    except DomainError as exc:
        return Draft(fallback, "template", fallback_reason=f"gateway: {exc.code}")
    parsed = getattr(resp, "parsed", None)
    text = parsed.get("text") if isinstance(parsed, dict) else None
    if not isinstance(text, str) or not text.strip():
        return Draft(fallback, "template", fallback_reason="model returned no text")
    bad = verify_numbers(text, facts.values())
    if bad:
        return Draft(
            fallback,
            "template",
            unsupported_numbers=tuple(bad),
            fallback_reason="model quoted numbers that are not in the fact pack",
            call_id=getattr(resp, "call_id", None),
        )
    return Draft(text, "llm", call_id=getattr(resp, "call_id", None))


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
