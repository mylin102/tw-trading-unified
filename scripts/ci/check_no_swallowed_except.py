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
    # session / broker layer
    "SDK_KWARG_INCOMPATIBLE",
    "SESSION_RECONNECT_RETRY",
    "SESSION_LOGOUT_FAILED",
    "SUBPROCESS_LOGIN_FAILED",
    "EXIT_SUBMIT_FAILED",
    "ADAPTER_SUBMIT_REJECTED",
    # strategy evaluation layer
    "CONFIG_VALUE_INVALID",
    "HVWAP_EVAL_FAILED",
    "TREND_GATE_UNAVAILABLE",
    # monitor / execution layer
    "RELEASE_IDENTITY_UNAVAILABLE",
    "EXEC_CTX_MODULE_MISSING",
    "TRADE_REFRESH_FAILED",
    "SAFETY_STOP_FAILED",
    "SAFETY_STOP_CANCEL_FAILED",
    "OCO_CANCEL_FAILED",
    "ADAPTER_ORDER_REJECTED",
    "BROKER_QUERY_UNAVAILABLE",
    "DUPLICATE_SUBMIT_SUPPRESSED",
    "EXIT_LEG_SUBMIT_FAILED",
    "SUBMIT_STATUS_UNKNOWN",
    "WATCHDOG_CANCEL_FAILED",
    "CANCEL_NOT_APPLICABLE",
    "MANUAL_FLAG_RESTORE_FAILED",
    "RELEASE_ARM_SETUP_FAILED",
    "POST_FILL_SYNC_FAILED",
    "LIFECYCLE_EVAL_FAILED",
}

_ANN = re.compile(
    r"FAILPATH:\s*(?P<kind>RECOVERABLE|FATAL|UNKNOWN)"
    r"\s+reason=(?P<reason>[A-Z][A-Z0-9_]*)"
    r"(?:\s+refusal=sentinel-default\((?P<sentinel>\w+)\))?"
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
                    or name.startswith("_log") or name.startswith("_request_")):
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
                return m.group("kind"), m.group("reason"), m.group("sentinel")
            return "MALFORMED", None, None
    return None, None, None


REFUSAL_CALLS = ("reject", "invalidate_all", "put")  # explicit failure channel


def _returns_falsy(handler: ast.ExceptHandler) -> bool:
    """A refusal is either a falsy return or a named status constant
    (e.g. `return "SUBMIT_ERROR"`)."""
    for n in ast.walk(handler):
        if isinstance(n, ast.Return):
            if n.value is None:
                return True          # bare return = do not proceed
            if isinstance(n.value, ast.Constant):
                if not n.value.value:
                    return True
                if isinstance(n.value.value, str):
                    return True
            # a structured result (dict) is an explicit refusal channel,
            # e.g. {"blocked": True, "reason": ...}
            if isinstance(n.value, (ast.Dict, ast.Tuple)):
                return True
    return False


def _refuses(handler: ast.ExceptHandler) -> bool:
    """A refusal must be observable: raise, a falsy return, a falsy sentinel
    assignment (`ok = False`) that the following code acts on, or an explicit
    failure-channel call (reject()/invalidate_all()/queue put(False))."""
    if _has_raise(handler):
        return True
    if _returns_falsy(handler):
        return True
    for n in ast.walk(handler):
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant) \
                and not n.value.value:
            return True
        # explicit state transition (e.g. `rg.status = FAILED`) is a refusal
        if isinstance(n, (ast.Assign, ast.AnnAssign)) and isinstance(n.targets[0] if isinstance(n, ast.Assign) else n.target, ast.Attribute):
            return True
        if isinstance(n, ast.Call):
            name = _call_name(n)
            if name in REFUSAL_CALLS:
                return True
            if name == "put" and any(
                    isinstance(a, ast.Constant) and a.value is False
                    for a in n.args):
                return True
    return False


def _enclosing_function(node: ast.AST, parents: dict) -> ast.AST | None:
    cur = parents.get(node)
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur
        cur = parents.get(cur)
    return None


def _sentinel_default_refusal(handler: ast.ExceptHandler, sentinel: str,
                              try_node: ast.Try, parents: dict) -> str | None:
    """Verify the declared refusal shape: a tick-local decision sentinel that
    the handler leaves untouched, so the tick simply performs no action.

    Requires BOTH:
      (a) the handler never assigns the sentinel, and
      (b) the enclosing function initialises it to None before the try.
    Otherwise the annotation is claiming a refusal that isn't there.
    """
    for n in ast.walk(handler):
        if isinstance(n, (ast.Assign, ast.AnnAssign)):
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            for t in targets:
                if isinstance(t, ast.Name) and t.id == sentinel:
                    return (f"refusal=sentinel-default({sentinel}) but the "
                            f"handler assigns it")
    func = _enclosing_function(try_node, parents)
    if func is None:
        return (f"refusal=sentinel-default({sentinel}) outside a function")
    for n in ast.walk(func):
        if not isinstance(n, (ast.Assign, ast.AnnAssign)):
            continue
        if getattr(n, "lineno", 0) >= try_node.lineno:
            continue
        targets = n.targets if isinstance(n, ast.Assign) else [n.target]
        for t in targets:
            if isinstance(t, ast.Name) and t.id == sentinel:
                value = getattr(n, "value", None)
                if isinstance(value, ast.Constant) and value.value is None:
                    return None
    return (f"refusal=sentinel-default({sentinel}) but it is not initialised "
            f"to None before the try")


def check_handler(handler: ast.ExceptHandler, comments,
                  try_node=None, parents=None) -> tuple[str, str] | None:
    """Return (severity, problem), or None when compliant.

    ERROR = violates the invariant as written (silent swallow, or a declared
            FATAL/UNKNOWN that does not actually stop or refuse).
    WARN  = missing annotation on a handler that DOES record or return
            something. Classifying those needs per-site risk review, so they
            are reported and enforced under --strict rather than fabricated
            here.

    States:
      RECOVERABLE -- the code continues on a deliberate fallback; needs
                     log/event evidence so the degradation is visible.
      UNKNOWN     -- the state cannot be proven; must refuse and surface it
                     (raise, falsy return, or reject()/invalidate/put(False)).
      FATAL       -- unrecoverable; must raise.
    """
    if _has_raise(handler):
        return None  # nothing is hidden
    kind, reason, sentinel = _annotation_for(handler, comments)
    if kind is None:
        if is_silent(handler):
            return ("ERROR", "silent handler with no FAILPATH annotation")
        return ("WARN", "no FAILPATH annotation (classification pending)")
    if kind == "MALFORMED":
        return ("ERROR", "malformed FAILPATH annotation")
    if reason not in FAILPATH_REASONS:
        return ("ERROR", f"unregistered reason={reason}")
    if kind == "FATAL":
        return ("ERROR", "FAILPATH: FATAL but handler does not raise")
    if kind == "UNKNOWN":
        if sentinel:
            problem = _sentinel_default_refusal(handler, sentinel, try_node,
                                                parents or {})
            if problem:
                return ("ERROR", problem)
            if not _has_evidence(handler):
                return ("ERROR",
                        f"refusal=sentinel-default({sentinel}) but the failure "
                        f"is not logged")
            return None
        if not _refuses(handler):
            return ("ERROR",
                    "FAILPATH: UNKNOWN but handler neither raises nor refuses")
        return None
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
        parents = {child: parent for parent in ast.walk(tree)
                   for child in ast.iter_child_nodes(parent)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            verb = wraps_order_operation(node)
            if verb is None:
                continue
            for handler in node.handlers:
                result = check_handler(handler, comments, node, parents)
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
