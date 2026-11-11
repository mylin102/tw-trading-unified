#!/usr/bin/env python3
"""I1 EVENT-SCHEMA gate: one event name carries one field shape.

WHY THIS EXISTS
---------------
The runtime event log (mts_spread_events.jsonl / futures_events.jsonl) is both
the audit trail and the reconstruction source: dashboards, replay tooling and
post-trade review all parse it by event name. When the same name is emitted
with different fields depending on which code path produced it, every consumer
that assumes a shape breaks silently -- it reads a missing key as "no value"
rather than "this emitter never provided it".

That is the same defect class as the bugs this project keeps paying for: the
value looks absent, not wrong. `agent_engineering_governance_three_layer.md`
(item 1) requires the output format to be locked so a missing field is a
refusal, not an improvisation.

RULE
----
For every `*_append_event` / `_append_mts_event` call site:

  * the event name must be a string literal (otherwise no schema can be
    enforced at all);
  * the field set must be statically visible: explicit kwargs plus the static
    keys of `**{...}` dict literals are flattened; `**<non-literal>` is an
    unverifiable emitter and fails;
  * all call sites of one event name must agree on a single field set. A
    second shape is only allowed if it is DECLARED in EVENT_SCHEMA -- a review
    decision that the extra/missing fields are intentional, not drift.

Scope: production code only (tests are fixtures, not emitters).

Exit codes: 0 = clean, 1 = violations.
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import defaultdict
from pathlib import Path

SCOPE = ["core", "strategies", "main.py"]

SKIP_PARTS = ("__pycache__", "/.venv/", "node_modules", "/tests/", "/archive",
              "/archive_orphaned", "/data/", "/logs/")

EVENT_FUNCS = ("append_event",)

# Declared acceptable shape differences, keyed by event name. Each entry is a
# list of field-name sets; the gate requires every observed shape to appear
# here once. An empty dict means "no event is allowed to drift".
EVENT_SCHEMA: dict[str, list[list[str]]] = {}


# Writer signatures differ, so the event-name argument position does too.
#   _append_mts_event(<name>, **fields)                -> name at 0
#   _ilog.append_event(<intent_id>, <name>, **fields)  -> name at 1
#   lifecycle_provenance._append_event(<event_obj>)    -> not name-based
NAME_ARG_INDEX = {
    "_append_mts_event": 0,
    "_append_mts_event_checked": 0,
    "append_event": 1,
}
NOT_NAME_BASED = {"_append_event"}


def _writer_kind(node: ast.Call) -> str:
    fn = node.func
    name = fn.attr if isinstance(fn, ast.Attribute) else (
        fn.id if isinstance(fn, ast.Name) else "")
    return name


def _is_event_call(node: ast.Call) -> bool:
    name = _writer_kind(node)
    if name in NOT_NAME_BASED:
        return False
    return name in NAME_ARG_INDEX or "append_mts_event" in name


def _static_dict_keys(node: ast.AST) -> set[str] | None:
    """Keys of a `{...}` literal, or None if any key is dynamic."""
    if not isinstance(node, ast.Dict):
        return None
    keys: set[str] = set()
    for k in node.keys:
        if k is None:            # **splat inside the literal
            return None
        if isinstance(k, ast.Constant) and isinstance(k.value, str):
            keys.add(k.value)
        else:
            return None
    return keys


def _dict_literal_keys(node: ast.AST) -> set[str] | None:
    """Keys of a dict LITERAL, recursing through nested **splats that are
    themselves dict literals. Returns None if any splat is not literal."""
    if not isinstance(node, ast.Dict):
        return None
    keys: set[str] = set()
    for k, v in zip(node.keys, node.values):
        if k is None:
            inner = _dict_literal_keys(v)
            if inner is None:
                return None
            keys |= inner
        elif isinstance(k, ast.Constant) and isinstance(k.value, str):
            keys.add(k.value)
        else:
            return None
    return keys


def _helper_keys(tree: ast.AST, name: str) -> set[str] | None:
    """Approximate the keys a same-file helper produces: every dict literal
    bound inside it. One level, no cross-file."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            keys: set[str] = set()
            found = False
            for n in ast.walk(node):
                if isinstance(n, ast.Dict):
                    inner = _dict_literal_keys(n)
                    if inner:
                        keys |= inner
                        found = True
            return keys if found else None
    return None


def _assigned_dict_keys(tree: ast.AST, name: str) -> set[str] | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    inner = _dict_literal_keys(node.value)
                    if inner:
                        return inner
    return None


def _splat_keys(value: ast.AST, tree: ast.AST) -> set[str] | None:
    literal = _dict_literal_keys(value)
    if literal is not None:
        return literal
    if isinstance(value, ast.Name):
        return _assigned_dict_keys(tree, value.id)
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
        return _helper_keys(tree, value.func.id)
    return None


def _fields(node: ast.Call, tree: ast.AST) -> tuple[set[str], list[str]]:
    """Return (fields, unverifiable reasons) for one call site."""
    fields: set[str] = set()
    problems: list[str] = []
    for kw in node.keywords:
        if kw.arg is not None:
            fields.add(kw.arg)
            continue
        keys = _splat_keys(kw.value, tree)
        if keys is None:
            problems.append(ast.dump(kw.value)[:40])
        else:
            fields |= keys
    return fields, problems


def collect(root: Path) -> tuple[dict, list[dict]]:
    by_event: dict[str, list[dict]] = defaultdict(list)
    unnamed: list[dict] = []
    for entry in SCOPE:
        target = root / entry
        files = [target] if target.is_file() else (
            list(target.rglob("*.py")) if target.is_dir() else [])
        for path in files:
            if any(part in str(path) for part in SKIP_PARTS):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8",
                                               errors="replace"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not _is_event_call(node):
                    continue
                idx = NAME_ARG_INDEX.get(_writer_kind(node), 0)
                if len(node.args) <= idx:
                    continue
                first = node.args[idx]
                if not (isinstance(first, ast.Constant)
                        and isinstance(first.value, str)):
                    unnamed.append({"file": str(path), "line": node.lineno})
                    continue
                fields, problems = _fields(node, tree)
                by_event[first.value].append({
                    "file": str(path), "line": node.lineno,
                    "fields": sorted(fields), "unverifiable": problems,
                })
    return by_event, unnamed


def check(root: Path) -> list[dict]:
    by_event, unnamed = collect(root)
    violations: list[dict] = []

    for item in unnamed:
        violations.append({
            "kind": "NON_LITERAL_EVENT_NAME",
            "event": "?",
            **item,
        })

    for event, sites in sorted(by_event.items()):
        for site in sites:
            if site["unverifiable"]:
                violations.append({
                    "kind": "UNVERIFIABLE_FIELDS",
                    "event": event,
                    "file": site["file"],
                    "line": site["line"],
                    "detail": site["unverifiable"],
                })

        shapes = defaultdict(list)
        for site in sites:
            if not site["unverifiable"]:
                shapes[tuple(site["fields"])].append(site)
        if len(shapes) <= 1:
            continue
        declared = {tuple(sorted(s)) for s in EVENT_SCHEMA.get(event, [])}
        for shape, members in sorted(shapes.items(), key=lambda kv: -len(kv[1])):
            if shape in declared:
                continue
            violations.append({
                "kind": "SCHEMA_DRIFT",
                "event": event,
                "shape": list(shape),
                "count": len(members),
                "sites": [f"{m['file']}:{m['line']}" for m in members[:4]],
            })
    return violations


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="I1 event-schema gate")
    ap.add_argument("--root", default=".")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    violations = check(Path(args.root))
    by_kind = defaultdict(int)
    for v in violations:
        by_kind[v["kind"]] += 1

    if args.json:
        print(json.dumps({"violations": violations,
                          "by_kind": dict(by_kind)}, indent=2))
    elif violations:
        print(f"I1 EVENT-SCHEMA: {len(violations)} violation(s) "
              f"{dict(by_kind)}\n")
        for v in violations:
            if v["kind"] == "NON_LITERAL_EVENT_NAME":
                print(f"  [NON_LITERAL] {v['file']}:{v['line']}")
            elif v["kind"] == "UNVERIFIABLE_FIELDS":
                print(f"  [UNVERIFIABLE] {v['file']}:{v['line']} {v['event']}"
                      f"  **{v['detail']}")
            else:
                print(f"  [DRIFT] {v['event']} shape={v['shape']} "
                      f"count={v['count']}")
                for s in v["sites"]:
                    print(f"          {s}")
        print("\nOne event name = one field shape. Either unify the emitters,")
        print("make the fields static, or declare the variant in EVENT_SCHEMA.")
    else:
        print("I1 EVENT-SCHEMA: OK (one event name, one field shape)")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
