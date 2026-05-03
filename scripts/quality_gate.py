#!/usr/bin/env python3
"""Quality gate: function length check + LOC trend tracking.

Usage:
  python scripts/quality_gate.py --snapshot       Generate baseline (LOC + function whitelist)
  python scripts/quality_gate.py --check-functions Check function length against whitelist
  python scripts/quality_gate.py --ci             Full CI check (functions + LOC trend)
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
METRICS_DIR = ROOT / ".metrics"
LOC_FILE = METRICS_DIR / "loc.json"
WHITELIST_FILE = METRICS_DIR / "func_whitelist.json"
MAX_FUNC_LINES = 50
LOC_GROWTH_WARN_PCT = 5

PY_DIRS = ["agents", "app", "cli", "core", "integrations", "pages", "scripts", "tools", "utils"]
TS_DIRS = ["web/apps/web/src", "web/apps/api/src", "web/packages/shared/src"]


# -- function scanning --


def _count_func_lines(node: ast.AST) -> int:
    lines = set()
    for child in ast.walk(node):
        if hasattr(child, "lineno") and hasattr(child, "end_lineno"):
            lines.update(range(child.lineno, child.end_lineno + 1))
    return len(lines)


def scan_py_functions(dirs: list[str]) -> list[tuple[str, str, int]]:
    results = []
    for d in dirs:
        for path in sorted((ROOT / d).rglob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    n = _count_func_lines(node)
                    if n > MAX_FUNC_LINES:
                        rel = str(path.relative_to(ROOT))
                        results.append((rel, node.name, n))
    return results


TS_FUNC_RE = re.compile(
    r"(?:^|\s)(?:export\s+)?(?:async\s+)?function\s+(\w+)|"
    r"(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\(",
    re.MULTILINE,
)


def scan_ts_functions(dirs: list[str]) -> list[tuple[str, str, int]]:
    results = []
    for d in dirs:
        dp = ROOT / d
        if not dp.exists():
            continue
        for path in sorted(dp.rglob("*.ts")) + sorted(dp.rglob("*.tsx")):
            lines = path.read_text(encoding="utf-8").splitlines()
            i = 0
            while i < len(lines):
                m = TS_FUNC_RE.search(lines[i])
                if m:
                    name = m.group(1) or m.group(2) or "anonymous"
                    depth, start = 0, i
                    found_open = False
                    for j in range(i, len(lines)):
                        depth += lines[j].count("{") - lines[j].count("}")
                        if "{" in lines[j]:
                            found_open = True
                        if found_open and depth <= 0:
                            length = j - start + 1
                            if length > MAX_FUNC_LINES:
                                rel = str(path.relative_to(ROOT))
                                results.append((rel, name, length))
                            i = j
                            break
                    else:
                        break
                i += 1
    return results


# -- LOC counting --


def count_loc(dirs: list[str], extensions: list[str]) -> dict[str, int]:
    result = {}
    for d in dirs:
        dp = ROOT / d
        if not dp.exists():
            continue
        total = 0
        for ext in extensions:
            for path in dp.rglob(f"*{ext}"):
                total += sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
        result[d] = total
    return result


def generate_loc_metrics() -> dict:
    py = count_loc(PY_DIRS, [".py"])
    ts = count_loc(TS_DIRS, [".ts", ".tsx"])
    return {
        "total_python_loc": sum(py.values()),
        "total_ts_loc": sum(ts.values()),
        "by_module": {**py, **ts},
        "generated_at": __import__("datetime").date.today().isoformat(),
    }


# -- whitelist management --


def _make_key(filepath: str, funcname: str) -> str:
    return f"{filepath}::{funcname}"


def load_whitelist() -> dict[str, int]:
    if not WHITELIST_FILE.exists():
        return {}
    return json.loads(WHITELIST_FILE.read_text(encoding="utf-8"))


def save_whitelist(wl: dict[str, int]) -> None:
    METRICS_DIR.mkdir(parents=True, exist_ok=True)
    WHITELIST_FILE.write_text(json.dumps(wl, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# -- commands --


def cmd_snapshot() -> int:
    METRICS_DIR.mkdir(parents=True, exist_ok=True)
    metrics = generate_loc_metrics()
    LOC_FILE.write_text(json.dumps(metrics, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    all_violations = scan_py_functions(PY_DIRS) + scan_ts_functions(TS_DIRS)
    wl = {_make_key(f, fn): n for f, fn, n in all_violations}
    save_whitelist(wl)

    print(f"LOC baseline: Python={metrics['total_python_loc']}  TS={metrics['total_ts_loc']}")
    print(f"Function whitelist: {len(wl)} legacy functions recorded")
    return 0


def cmd_check_functions() -> int:
    wl = load_whitelist()
    all_v = scan_py_functions(PY_DIRS) + scan_ts_functions(TS_DIRS)

    new_violations = []
    worsened = []
    for filepath, funcname, lines in all_v:
        key = _make_key(filepath, funcname)
        if key not in wl:
            new_violations.append((filepath, funcname, lines))
        elif lines > wl[key]:
            worsened.append((filepath, funcname, wl[key], lines))

    if new_violations or worsened:
        if new_violations:
            print(f"FAIL: {len(new_violations)} NEW functions exceed {MAX_FUNC_LINES}-line limit:")
            for f, fn, n in new_violations:
                print(f"  {f}  {fn}()  {n} lines")
        if worsened:
            print(f"FAIL: {len(worsened)} whitelisted functions got LONGER:")
            for f, fn, old, new in worsened:
                print(f"  {f}  {fn}()  {old} -> {new} lines")
        return 1

    print(f"OK: No new functions exceed {MAX_FUNC_LINES}-line limit. ({len(wl)} legacy whitelisted)")
    return 0


def cmd_ci() -> int:
    exit_code = cmd_check_functions()

    metrics = generate_loc_metrics()
    print(f"\nLOC: Python={metrics['total_python_loc']}  TS={metrics['total_ts_loc']}")
    if LOC_FILE.exists():
        baseline = json.loads(LOC_FILE.read_text(encoding="utf-8"))
        for key in ("total_python_loc", "total_ts_loc"):
            base_val = baseline.get(key, 0)
            if base_val == 0:
                continue
            growth = (metrics[key] - base_val) / base_val * 100
            if growth > LOC_GROWTH_WARN_PCT:
                print(f"  WARNING: {key} grew {growth:.1f}% ({base_val} -> {metrics[key]})")

    return exit_code


def main() -> int:
    args = set(sys.argv[1:])
    if "--snapshot" in args:
        return cmd_snapshot()
    if "--ci" in args:
        return cmd_ci()
    if "--check-functions" in args:
        return cmd_check_functions()
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
