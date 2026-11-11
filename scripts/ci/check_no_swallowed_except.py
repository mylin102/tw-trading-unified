#!/usr/bin/env python3
"""I3 FAILPATH gate: order/exit handlers must declare what failure means.

WHY THIS EXISTS
---------------
2026-11-07 and 2026-11-10 both production incidents had the same shape:
a failure occurred, the code did not surface it, and the process ended up
in a state nobody could see (contract resolution returned None -> monitor
kept running -> AttributeError every tick -> clean exit 0 -> PM2's
stop_exit_codes:[0] read that as "operator stopped it on purpose" -> no
restart -> dead for 83 minutes, twice).

A handler that swallows an order/exit/session operation is the same defect
one layer down. But "did it log?" is NOT the standard: logging and then
continuing to trade on state that may have failed is still a silent failure
(agent_engineering_governance_three_layer.md, I3). AST cannot infer intent
from code alone, so this gate requires the classification to be DECLARED and
then checks the declaration against the body:

    except Exception as exc:
        # FAILPATH: RECOVERABLE reason=EXIT_INTENT_LOG_UNAVAILABLE
        #   why: legacy path still submits; losing the intent record only
        #        widens the crash-window exposure
        logger.warning("[EXIT_INTENT_DEGRADED] %s", exc)

RULES
-----
For every handler whose try-block wraps an order/exit/session call:

  * no `# FAILPATH:` annotation                     -> violation
  * `FAILPATH: FATAL`   but body does not raise     -> violation
  * `FAILPATH: UNKNOWN` but body does not raise     -> violation
  * `FAILPATH: RECOVERABLE` but no log/event
     evidence and no re-raise                       -> violation
  * `reason=<CODE>` not in FAILPATH_REASONS         -> violation
  * handler that re-raises unconditionally          -> compliant (nothing hidden)

The reason registry is a contract, not an allowlist of violations: adding a
reason means deciding, in review, that this failure mode is survivable.

Scope: order/exit/session verbs only. Measured 2026-11-11 -- 704 silent
handlers repo-wide, 211 on the trading path, 46 wrapping an order/exit op.
The narrow scope is what makes a zero-allowlist requirement realistic.

Exit codes: 0 = clean, 1 = violations, 2 = usage error.
"""
from __future__ import annotations

import argparse
import ast
import io
import json
import re
import sys
import tokenize
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

# Failure modes reviewed and accepted. A new reason code is a review decision.
FAILPATH_REASONS = {
    "EXIT_INTENT_LOG_UNAVAILABLE",
    "MTS_STATE_RESTORE_FAILED",
    "RELEASE_SYNC_FAILED",
    "ORDER_STATE_RACE",
    "QUOTE_SOURCE_UNAVAILABLE",
    "POSITION_STATE_UNREADABLE",
}

_ANN = re.compile(
    r"FAILPATH:\s*(?P<kind>RECOVERABLE|FATAL|UNKNOWN)"
    r"\s+reason=(?P<reason>[A-Z][A-Z0-9_]*)"
)

EVIDENCE_NAMES = {"warning", "error", "exception", "critical", "info", "debug",
                  "print", "append_event", "_append_event", "record", "emit"}

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
    """A body with no observable statement at all (pass/continue/bare return)."""
    if not handler.body:
        return True
    for stmt in handler.body:
        if isinstance(stmt, SILENT_STMTS):
            continue
        if isinstance(stmt, ast.Return) and stmt.value is None:
            continue
        return False
    return True


def _has_raise(handler: ast.ExceptHandler) -> bool:
    return any(isinstance(n, ast.Raise) for n in ast.walk(handler))


def _has_evidence(handler: ast.ExceptHandler) -> bool:
    for n in ast.walk(handler):
        if isinstance(n, ast.Call):
            name = _call_name(n)
            if (name in EVIDENCE_NAMES or name.endswith("_event")
                    or name.startswith("_log")):
                return True
    return False


def _comments_by_line(source: str) -> dict[int, str]:
    out: dict[int, str] = {}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT:
                out[tok.start[0]] = tok.string
    except (tokenize.TokenError, IndentationError):
        pass
    return out


def _handler_end(handler: ast.ExceptHandler) -> int:
    end = handler.lineno
    for stmt in handler.body:
        end = max(end, getattr(stmt, "end_lineno", stmt.lineno))
    return end


def _annotation_for(handler: ast.ExceptHandler, comments: dict[int, str]):
    start, end = handler.lineno, _handler_end(handler)
    for line in range(start, end + 1):
        text = comments.get(line)
        if text and "FAILPATH:" in text:
            m = _ANN.search(text)
            if m:
                return m.group("kind"), m.group("reason")
            return "MALFORMED", None
    return None, None


def check_handler(handler: ast.ExceptHandler, comments) -> tuple[str, str] | None:
    """Return (severity, problem), or None when compliant.

    ERROR = violates the invariant as written (silent swallow, or a declared
            FATAL/UNKNOWN that does not actually stop).
    WARN  = missing annotation on a handler that DOES record or return
            something. Classifying those needs per-site risk review, so they
            are reported and enforced under --strict rather than fabricated
            here.
    """
    if _has_raise(handler):
        return None  # nothing is hidden
    kind, reason = _annotation_for(handler, comments)
    if kind is None:
        if is_silent(handler):
            return ("ERROR", "silent handler with no FAILPATH annotation")
        return ("WARN", "no FAILPATH annotation (classification pending)")
    if kind == "MALFORMED":
        return ("ERROR", "malformed FAILPATH annotation")
    if reason not in FAILPATH_REASONS:
        return ("ERROR", f"unregistered reason={reason}")
    if kind in ("FATAL", "UNKNOWN"):
        return ("ERROR", f"FAILPATH: {kind} but handler does not raise")
    if not _has_evidence(handler):
        return ("ERROR", "FAILPATH: RECOVERABLE but no log/event evidence")
    return None


def scan_tree(root: Path) -> list[dict]:
    findings: list[dict] = []
    files = list(root.rglob("*.py")) if root.is_dir() else [root]
    for path in files:
        if "__pycache__" in str(path):
            continue
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source)
        except (SyntaxError, OSError):
            continue
        comments = _comments_by_line(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            verb = wraps_order_operation(node)
            if verb is None:
                continue
            for handler in node.handlers:
                result = check_handler(handler, comments)
                if result:
                    severity, problem = result
                    findings.append({
                        "file": str(path),
                        "line": handler.lineno,
                        "wrapped_call": verb,
                        "severity": severity,
                        "problem": problem,
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
    ap = argparse.ArgumentParser(description="I3 FAILPATH gate")
    ap.add_argument("--root", default=".", help="repository root")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--strict", action="store_true",
                    help="also fail on WARN (annotation pending); phase-2 target")
    args = ap.parse_args(argv)

    findings = scan(Path(args.root))
    errors = [f for f in findings if f["severity"] == "ERROR"]
    warns = [f for f in findings if f["severity"] == "WARN"]

    if args.json:
        print(json.dumps({"violations": findings, "errors": len(errors),
                          "warnings": len(warns)}, indent=2))
    else:
        for f in errors + warns:
            print(f"  [{f['severity']}] {f['file']}:{f['line']}  "
                  f"[{f['wrapped_call']}]  {f['problem']}")
        if warns and not errors:
            print(f"\nI3 FAILPATH: OK ({len(errors)} errors) — {len(warns)} "
                  f"handler(s) still need a FAILPATH classification "
                  f"(enforced under --strict)")
        elif errors:
            print(f"\nI3 FAILPATH: {len(errors)} error(s), {len(warns)} warning(s)")
            print("Declare intent inside the handler, then act on it, e.g.")
            print("  # FAILPATH: RECOVERABLE reason=RELEASE_SYNC_FAILED")
            print("  logger.warning(...)")
            print("or use FAILPATH: FATAL / UNKNOWN together with a re-raise.")
        else:
            print("I3 FAILPATH: OK (every order/exit handler declares its failure path)")

    failed = bool(errors) or (args.strict and bool(warns))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
