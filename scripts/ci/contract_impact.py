#!/usr/bin/env python3
"""Contract-Impact Gate (Collapse 3) — decide FAST vs FULL verification for a diff.

Why this exists
---------------
Choosing the verification scope by *which files changed* misses the dangerous
case: a shared module that looks unrelated but is imported by the order path
(a formatter, a config reader, a state helper) and therefore changes the order
contract indirectly.  This gate replaces filename intuition with an explicit
allowlist plus a static import closure, and it is fail-closed: anything it
cannot classify runs the FULL gate.

Rules (in order)
  1. any deleted / renamed path  -> FULL   (removal is never "test only")
  2. no changed paths at all     -> FULL   (no evidence is not evidence)
  3. path in CONTRACT set        -> FULL
  4. path imported (transitively, <=3 hops) by a CONTRACT root -> FULL (INDIRECT)
  5. path in TEST_ONLY set       -> FAST
  6. anything else               -> FULL   (UNCLASSIFIED, fail-closed)
  Decision = FAST only when every path is TEST_ONLY and no rule 1/2 fired.

Usage
  python3 scripts/ci/contract_impact.py --base <sha> [--head <sha|HEAD>] [--json]
  python3 scripts/ci/contract_impact.py --paths a.py,b.py [--deleted c.py]
      [--repo DIR] [--exit-code]

Exit codes
  0 = classification ok (default); with --exit-code: 0 = FAST, 1 = FULL, 2 = error

Enforcement point (audit 2026-09-20): this repository has no CI runner (no
.github/workflows, and the suite needs broker/SDK deps), so the gate is NOT
enforced by CI.  It is enforced by the verification protocol: whoever verifies
a change runs this gate first and then runs the scope it names (FULL = the
whole suite plus every gate).  An operator who skips the gate is not blocked
by tooling — that is a known, deliberate limitation, not an oversight.
"""
import argparse
import ast
import fnmatch
import json
import os
import subprocess
import sys

CONTRACT_GLOBS = (
    "main.py",
    "strategies/**",
    "core/order_management/**",
    "core/order_intent_gateway.py",
    "core/order_lifecycle_reconciler.py",
    "core/exit_only_position.py",
    "core/execution_context_state.py",
    "core/mode_transition.py",
    "core/deployment_safety_gate.py",
    "core/live_broker_preflight.py",
    "core/live_route_certificate.py",
    "core/reconciliation/**",
    "core/broker/**",
    "core/runtime_paths.py",
    "config/**",
    "ecosystem.config.js",
    "scripts/deployment/**",
    "scripts/restart_live.sh",
    ".github/workflows/**",   # CI that can deploy/restart is contract surface
)

TEST_ONLY_GLOBS = (
    "tests/**",
    "docs/**",
    "*.md",
    "**/*.md",
    ".github/**",
)

CLOSURE_ROOTS = (
    "core/order_intent_gateway.py",
    "core/order_management/order_manager.py",
    "core/exit_only_position.py",
    "core/order_lifecycle_reconciler.py",
    "strategies/futures/monitor.py",
)
MAX_HOPS = 3
SCAN_DIRS = ("core", "strategies")


def _match(path, globs):
    return any(fnmatch.fnmatch(path, g) for g in globs)


def _module_name(rel_path):
    if not rel_path.endswith(".py"):
        return None
    mod = rel_path[:-3].replace(os.sep, "/").replace("/", ".")
    return mod


def _module_to_path(mod, repo):
    rel = mod.replace(".", "/") + ".py"
    cand = os.path.join(repo, rel)
    if os.path.isfile(cand):
        return rel
    pkg = os.path.join(repo, mod.replace(".", "/"), "__init__.py")
    if os.path.isfile(pkg):
        return os.path.join(mod.replace(".", "/"), "__init__.py")
    return None


def _parse_imports(path):
    """Return the module names imported by a python file (best effort)."""
    try:
        tree = ast.parse(open(path, encoding="utf-8", errors="replace").read())
    except Exception:
        return []
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            if node.module:
                out.append(node.module)
                for alias in node.names:
                    out.append(f"{node.module}.{alias.name}")
    return out


def _build_edges(repo):
    """Map repo-relative module file -> set of repo-relative module files it imports."""
    edges = {}
    for scan in SCAN_DIRS:
        base = os.path.join(repo, scan)
        if not os.path.isdir(base):
            continue
        for root, _dirs, files in os.walk(base):
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, repo)
                targets = set()
                for mod in _parse_imports(full):
                    tgt = _module_to_path(mod, repo)
                    if tgt:
                        targets.add(tgt)
                edges[rel] = targets
    return edges


def contract_closure(repo):
    """Repo-relative files reachable from the CONTRACT roots within MAX_HOPS."""
    edges = _build_edges(repo)
    seen = set()
    frontier = [r for r in CLOSURE_ROOTS if os.path.isfile(os.path.join(repo, r))]
    hop = 0
    while frontier and hop < MAX_HOPS:
        hop += 1
        nxt = []
        for node in frontier:
            for tgt in edges.get(node, ()):
                if tgt not in seen and tgt not in CLOSURE_ROOTS:
                    seen.add(tgt)
                    nxt.append(tgt)
        frontier = nxt
    return seen


def classify(paths, deleted, repo):
    closure = contract_closure(repo)
    reasons = []
    verdicts = []
    for p in paths:
        if p in deleted:
            verdicts.append("FULL")
            reasons.append(("DELETED", p, "path removed -> FULL"))
            continue
        if _match(p, CONTRACT_GLOBS):
            verdicts.append("FULL")
            reasons.append(("CONTRACT", p, "explicit contract-impact allowlist"))
            continue
        if p in closure:
            verdicts.append("FULL")
            reasons.append(("INDIRECT", p, "imported by an order-path root (<=3 hops)"))
            continue
        if _match(p, TEST_ONLY_GLOBS):
            verdicts.append("FAST")
            reasons.append(("TEST_ONLY", p, "tests/docs only"))
            continue
        verdicts.append("FULL")
        reasons.append(("UNCLASSIFIED", p, "not in any allowlist -> fail-closed FULL"))
    no_evidence = not paths and not deleted
    if no_evidence:
        reasons.append(("NO_EVIDENCE", "", "empty diff -> fail-closed FULL"))
    for p in deleted:
        reasons.append(("DELETED", p, "path removed -> FULL"))
        verdicts.append("FULL")
    decision = "FULL" if (no_evidence or any(v == "FULL" for v in verdicts)) else "FAST"
    return decision, reasons


def git_changed_paths(repo, base, head):
    verify = subprocess.run(
        ["git", "-C", repo, "rev-parse", "--verify", f"{base}^{{commit}}"],
        capture_output=True, text=True,
    )
    sha = (verify.stdout or "").strip()
    if verify.returncode != 0 or not sha or set(sha) == {"0"}:
        return None, f"unknown base revision: {base}"
    proc = subprocess.run(
        ["git", "-C", repo, "diff", "--name-status", "--find-renames", f"{base}..{head}"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return None, proc.stderr.strip() or "git diff failed"
    changed, deleted = [], []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0]
        if status.startswith("R") or status.startswith("D"):
            for p in parts[1:]:
                deleted.append(p)
                changed.append(p)
            continue
        changed.append(parts[-1])
    return (changed, deleted), None


def main(argv=None):
    ap = argparse.ArgumentParser(description="Contract-impact gate (FAST vs FULL).")
    ap.add_argument("--base", help="git base sha")
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--paths", default=None, help="comma separated changed paths (offline mode)")
    ap.add_argument("--deleted", default="", help="comma separated deleted/renamed paths")
    ap.add_argument("--repo", default=None, help="repo root (defaults to this script's repo)")
    ap.add_argument("--exit-code", action="store_true", help="0=FAST 1=FULL 2=error")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    repo = args.repo or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    deleted = [p.strip() for p in (args.deleted or "").split(",") if p.strip()]

    if args.paths is not None:
        paths = [p.strip() for p in args.paths.split(",") if p.strip()]
    else:
        if not args.base:
            print("[IMPACT] error=missing --base or --paths")
            return 2
        got, err = git_changed_paths(repo, args.base, args.head)
        if got is None:
            print(f"[IMPACT] error={err}")
            return 2
        paths, deleted = got

    decision, reasons = classify(paths, deleted, repo)
    plan = ("pytest -q tests/core tests/strategies tests/contracts"
            if decision == "FULL" else "pytest -q <changed test files only>")
    if args.json:
        print(json.dumps({
            "decision": decision,
            "paths": paths,
            "deleted": deleted,
            "reasons": [{"rule": r, "path": p, "why": w} for r, p, w in reasons],
            "plan": plan,
        }))
    else:
        for rule, path, why in reasons:
            print(f"[IMPACT] {rule} {path} :: {why}")
        print(f"[IMPACT] decision={decision} paths={len(paths)} deleted={len(deleted)}")
        print(f"[IMPACT] plan={plan}")
    if args.exit_code:
        return 0 if decision == "FAST" else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
