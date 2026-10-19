"""Architecture fitness functions (Build Guide §3, §6, §11, §14).

- services never import other services (except the finacle_adapter port, model_gateway types via libs)
- libs never import services
- provider SDKs / raw HTTP-to-LLM only inside services/model_gateway
- no float in money/ratio domain code; no eval/exec anywhere; no datetime.now() in domain code
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVICES = sorted(
    p.name for p in (ROOT / "services").iterdir() if p.is_dir() and not p.name.startswith("_")
)
LIBS = sorted(
    p.name for p in (ROOT / "libs").iterdir() if p.is_dir() and not p.name.startswith("_")
)
# Allowed cross-service imports: everyone may use the Finacle *port* types.
ALLOWED_CROSS = {("*", "finacle_adapter.ports")}


def _py(dir_: Path) -> list[Path]:
    return [p for p in dir_.rglob("*.py") if "__pycache__" not in p.parts]


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.append(node.module)
    return out


def test_services_do_not_import_each_other() -> None:
    bad = []
    for svc in SERVICES:
        for f in _py(ROOT / "services" / svc):
            for mod in _imports(f):
                top = mod.split(".")[0]
                allowed = any(mod.startswith(a) for _, a in ALLOWED_CROSS)
                if top in SERVICES and top != svc and not allowed:
                    bad.append(f"{f.relative_to(ROOT)} imports {mod}")
    assert bad == []


def test_libs_do_not_import_services() -> None:
    bad = [
        f"{f.relative_to(ROOT)} imports {m}"
        for lib in LIBS
        for f in _py(ROOT / "libs" / lib)
        for m in _imports(f)
        if m.split(".")[0] in SERVICES
    ]
    assert bad == []


def test_no_provider_sdks_outside_gateway() -> None:
    banned = ("anthropic", "openai", "google.generativeai", "mistralai", "cohere")
    bad = []
    for base in ("libs", "services", "agents", "ocr", "ml", "sim"):
        d = ROOT / base
        if not d.exists():
            continue
        for f in _py(d):
            if "model_gateway" in f.parts:
                continue
            for m in _imports(f):
                if m.split(".")[0] in banned:
                    bad.append(f"{f.relative_to(ROOT)} imports {m}")
    assert bad == []


def test_no_eval_exec() -> None:
    bad = []
    for base in ("libs", "services", "agents", "ocr", "ml", "sim", "simulators"):
        d = ROOT / base
        if not d.exists():
            continue
        for f in _py(d):
            for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id in ("eval", "exec")
                ):
                    bad.append(str(f.relative_to(ROOT)))
    assert bad == []


MONEY_DOMAIN = [
    ROOT / "libs" / "platform_core" / "money.py",
    ROOT / "libs" / "rules_engine" / "dsl.py",
]


def test_no_float_in_money_domain() -> None:
    domain_dirs = [p for p in (ROOT / "services").glob("*/domain.py")] + MONEY_DOMAIN
    bad = []
    for f in domain_dirs:
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, float):
                bad.append(f"{f.relative_to(ROOT)}:{node.lineno} float literal")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "float"
            ):
                bad.append(f"{f.relative_to(ROOT)}:{node.lineno} float()")
    assert bad == []


def test_domain_code_does_not_read_wall_clock() -> None:
    pat = re.compile(r"datetime\.now\(|date\.today\(|time\.time\(")
    bad = []
    for f in list((ROOT / "services").glob("*/domain.py")) + list(
        (ROOT / "libs" / "domain").rglob("*.py")
    ):
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if pat.search(line):
                bad.append(f"{f.relative_to(ROOT)}:{i}")
    assert bad == []


def test_only_adapter_talks_to_finacle_http() -> None:
    bad = []
    for f in _py(ROOT / "services"):
        if "finacle_adapter" in f.parts:
            continue
        txt = f.read_text(encoding="utf-8")
        if re.search(r"/v1/(limits|disbursements|covenant-records|accounts/)", txt):
            bad.append(str(f.relative_to(ROOT)))
    assert bad == []


_INVISIBLE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]")


def test_no_invisible_or_bidi_characters_in_source() -> None:
    """Trojan Source guard (CVE-2021-42574): reviewers must see exactly what executes."""
    bad = []
    for base in (
        "libs",
        "services",
        "simulators",
        "ocr",
        "ml",
        "agents",
        "sim",
        "tools",
        "tests",
        "rules",
    ):
        d = ROOT / base
        if not d.exists():
            continue
        for f in list(d.rglob("*.py")) + list(d.rglob("*.yaml")):
            if "__pycache__" in f.parts:
                continue
            if _INVISIBLE.search(f.read_text(encoding="utf-8")):
                bad.append(str(f.relative_to(ROOT)))
    assert bad == []


def test_no_control_characters_in_source() -> None:
    """A stray backspace in a regex once silently disabled a unit pattern (non-raw string)."""
    bad = []
    for base in ("libs", "services", "simulators", "ocr", "ml", "agents", "sim", "tools", "tests"):
        d = ROOT / base
        if not d.exists():
            continue
        for f in d.rglob("*.py"):
            if "__pycache__" in f.parts:
                continue
            if any(ord(c) < 32 and c not in "\n\r\t" for c in f.read_text(encoding="utf-8")):
                bad.append(str(f.relative_to(ROOT)))
    assert bad == []
