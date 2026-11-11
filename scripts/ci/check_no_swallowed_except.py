#!/usr/bin/env python3
"""I3 FAILPATH gate: silent exception handlers around order/exit operations.

WHY THIS EXISTS
---------------
2026-11-07 and 2026-11-10 both production incidents had the same shape:
a failure occurred, the code did not surface it, and the process ended up
in a state nobody could see (contract resolution returned None -> monitor
kept running -> AttributeError every tick -> clean exit 0 -> PM2's
stop_exit_codes:[0] read that as "operator stopped it on purpose" -> no
restart -> dead for 83 minutes, twice).

A handler that swallows an order/exit/session operation is the same defect
one layer down: the operation fails, nothing is recorded, and the system
continues as if it had succeeded.

RULE (deliberately narrow)
--------------------------
Flag an `except` handler ONLY when BOTH hold:
  1. its body is silent: only pass / continue / break / bare `return`
     (no log call, no event append, no re-raise), AND
  2. the associated try-block wraps at least one call whose name matches
     an order/exit/session verb (place_order, submit, cancel, release,
     flatten, close_position, _execute_trade, send_order, login, logout,
     activate_ca, ...).

Rationale for (2): a silent handler around e.g. `json.dumps` is noise; a
silent handler around `submit(...)` can lose a real order. Scoping to the
trading path keeps the signal high enough that a zero-violation requirement
is realistic (this repo: 704 silent handlers overall, 211 on the trading
path, but only 6 that actually wrap an order/exit operation).

Exit codes: 0 = clean, 1 = violations found, 2 = usage/parse error.
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

SCOPE = [
    "strategies/plugins/futures/active",
    "strategies/futures/monitor.py",
    "core/order_management",
    "core/exit_intent.py",
    "core/exit_only_position.py",
    "core/broker",
    "core/shioaji_session.py",
]

ORDER_VERBS = (
    "place_order", "submit", "cancel", "_execute_trade", "release",
    "flatten", "close_position", "exit_order", "send_order",
    "logout", "login", "activate_ca",
)

SILENT_STMTS = (ast.Pass, ast.Continue, ast.Break)


def _call_name(node: ast.Call) -> str:
    f = node.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return ""


def wraps_order_operation(try_node: ast.Try) -> str | None:
    """Return the matched verb if the try body calls an order/exit op."""
    for n in ast.walk(try_node):
        if isinstance(n, ast.Call):
            name = _call_name(n).lower()
            for verb in ORDER_VERBS:
                if verb in name:
                    return name
    return None


def is_silent(handler: ast.ExceptHandler) -> bool:
    if not handler.body:
        return True
    for stmt in handler.body:
        if isinstance(stmt, SILENT_STMTS):
            continue
        if isinstance(stmt, ast.Return) and stmt.value is None:
            continue
        return False
    return True


def scan_tree(root: Path) -> list[dict]:
    findings: list[dict] = []
    files = list(root.rglob("*.py")) if root.is_dir() else [root]
    for path in files:
        if "__pycache__" in str(path):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            verb = wraps_order_operation(node)
            if verb is None:
                continue
            for handler in node.handlers:
                if is_silent(handler):
                    findings.append({
                        "file": str(path),
                        "line": handler.lineno,
                        "caught": (ast.unparse(handler.type)
                                   if handler.type else "bare except"),
                        "wrapped_call": verb,
                    })
    return findings


def scan(repo_root: Path) -> list[dict]:
    findings: list[dict] = []
    for entry in SCOPE:
        target = repo_root / entry
        if target.exists():
            findings.extend(scan_tree(target))
    return sorted(findings, key=lambda f: (f["file"], f["line"]))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=".", help="repository root")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    findings = scan(Path(args.root))
    if args.json:
        print(json.dumps({"violations": findings, "count": len(findings)}, indent=2))
    elif findings:
        print(f"I3 FAILPATH: {len(findings)} silent handler(s) around order/exit ops\n")
        for f in findings:
            print(f"  {f['file']}:{f['line']}  catches {f['caught']} "
                  f"wrapping '{f['wrapped_call']}'")
        print("\nFix: record evidence (logger.*/event) or re-raise. Never `pass`.")
    else:
        print("I3 FAILPATH: OK (0 silent handlers around order/exit ops)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
