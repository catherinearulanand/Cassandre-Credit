"""Task runner (Windows has no make). Mirrors the Makefile targets in Build Guide §5.2.

    python -m tools.cli <target>

Targets: lint typecheck test test-unit test-integration test-golden contracts-check
contracts-export rules-test replay-test simulate demo-p0 all
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable


def run(*args: str) -> int:
    print("$", " ".join(args), flush=True)
    return subprocess.call(list(args), cwd=ROOT)


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    t = argv[0]
    rest = argv[1:]
    if t == "lint":
        return run(
            PY,
            "-m",
            "ruff",
            "check",
            "libs",
            "services",
            "simulators",
            "ocr",
            "ml",
            "agents",
            "sim",
            "tools",
            "tests",
        )
    if t == "typecheck":
        return run(PY, "-m", "mypy", "libs", "services", "simulators", "ocr", "ml", "agents", "sim")
    if t == "test":
        return run(PY, "-m", "pytest", "tests", "-m", "not slow", *rest)
    if t == "test-unit":
        return run(PY, "-m", "pytest", "tests/unit", "tests/architecture", *rest)
    if t == "test-integration":
        return run(PY, "-m", "pytest", "tests/integration", *rest)
    if t == "test-golden":
        return run(PY, "-m", "ocr.eval.harness", "--gate", *rest)
    if t == "contracts-export":
        from events.compat import export

        for p in export():
            print("wrote", p.relative_to(ROOT))
        from tools.export_cts_schema import export_cts

        print("wrote", export_cts().relative_to(ROOT))
        return 0
    if t == "contracts-check":
        from events.compat import check

        problems = check()
        for p in problems:
            print("BREAKING:", p)
        return 1 if problems else 0
    if t == "rules-test":
        from rules_engine import default_registry

        fails = default_registry().run_embedded_tests()
        for f in fails:
            print("FAIL:", f)
        print(f"rule pack tests: {'FAILED' if fails else 'passed'}")
        return 1 if fails else 0
    if t == "replay-test":
        return run(PY, "-m", "pytest", "tests", "-k", "replay", *rest)
    if t == "simulate":
        return run(PY, "-m", "sim.run_simulation", *rest)
    if t == "demo-p0":
        return run(PY, "-m", "pytest", "tests/integration/test_finacle_roundtrip.py", "-v")
    if t == "all":
        for step in ("lint", "typecheck", "contracts-check", "rules-test", "test"):
            rc = main([step])
            if rc:
                return rc
        return 0
    print(f"unknown target {t}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
