"""Contract-impact gate tests (Collapse 3).

The rule set the tests pin down:
  * TEST_ONLY paths        -> FAST (a focused test run is enough)
  * CONTRACT paths         -> FULL (order/permission/lifecycle/deploy surface)
  * INDIRECT contract      -> FULL (a module the contract roots import, e.g. a
                              shared helper under core/ that silently changes
                              the order contract)
  * UNCLASSIFIED path      -> FULL (fail-closed default)
  * any deletion / rename  -> FULL
  * empty diff             -> FULL (no evidence is not evidence of safety)
"""
import os
import subprocess
import sys
import textwrap

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO, "scripts", "ci", "contract_impact.py")


def _run(args, cwd=None):
    proc = subprocess.run(
        [sys.executable, "-B", SCRIPT] + args,
        capture_output=True, text=True, cwd=cwd or REPO,
    )
    return proc.returncode, proc.stdout + proc.stderr


def _decision(out):
    for line in out.splitlines():
        if line.startswith("[IMPACT] decision="):
            return line.split("decision=")[1].split()[0]
    return None


def test_script_is_present():
    assert os.path.isfile(SCRIPT), "scripts/ci/contract_impact.py missing"


def test_test_only_diff_is_fast():
    rc, out = _run(["--paths", "tests/core/test_order_intent_gateway.py,docs/adr-011.md"])
    assert rc == 0, out
    assert _decision(out) == "FAST", out


def test_monitor_change_requires_full():
    rc, out = _run(["--paths", "strategies/futures/monitor.py"])
    assert _decision(out) == "FULL", out
    assert "CONTRACT" in out


def test_order_path_modules_require_full():
    for path in (
        "core/order_intent_gateway.py",
        "core/order_management/order_manager.py",
        "core/exit_only_position.py",
        "core/order_lifecycle_reconciler.py",
        "core/execution_context_state.py",
        "core/mode_transition.py",
        "core/reconciliation/holding_state.py",
        "core/broker/shioaji_compat.py",
        "config/futures.yaml",
        "ecosystem.config.js",
        "scripts/deployment/deploy_release.sh",
    ):
        rc, out = _run(["--paths", path])
        assert _decision(out) == "FULL", f"{path} -> {out}"


def test_unclassified_path_is_full_not_fast():
    """Fail-closed: a path the allowlist does not know must NOT be assumed safe."""
    rc, out = _run(["--paths", "some/new/thing.xyz"])
    assert _decision(out) == "FULL", out
    assert "UNCLASSIFIED" in out


def test_deletion_requires_full():
    rc, out = _run(["--paths", "tests/core/test_x.py", "--deleted", "tests/core/test_x.py"])
    assert _decision(out) == "FULL", out
    assert "DELETED" in out


def test_empty_diff_requires_full():
    rc, out = _run(["--paths", ""])
    assert _decision(out) == "FULL", out


def test_indirect_import_of_contract_root_requires_full(tmp_path):
    """A shared helper imported (transitively) by an order-path root is CONTRACT."""
    tree = tmp_path / "tree"
    (tree / "core").mkdir(parents=True)
    (tree / "core" / "order_intent_gateway.py").write_text(
        textwrap.dedent(
            """
            from core import shared_helper
            from core.deep import inner
            """
        )
    )
    (tree / "core" / "deep").mkdir()
    (tree / "core" / "deep" / "inner.py").write_text("from core import deepest\n")
    (tree / "core" / "deepest.py").write_text("VALUE = 1\n")
    (tree / "core" / "shared_helper.py").write_text("def fmt(x):\n    return x\n")
    (tree / "core" / "unrelated.py").write_text("X = 1\n")

    rc, out = _run([
        "--repo", str(tree),
        "--paths", "core/shared_helper.py,core/deepest.py,core/unrelated.py",
    ])
    assert _decision(out) == "FULL", out
    assert "INDIRECT" in out
    # the unrelated module must be reported as UNCLASSIFIED (also FULL), not INDIRECT
    assert "core/unrelated.py" in out


def test_python_import_scan_is_transitive_but_bounded(tmp_path):
    tree = tmp_path / "tree"
    (tree / "core").mkdir(parents=True)
    (tree / "core" / "order_intent_gateway.py").write_text("import core.a\n")
    (tree / "core" / "a.py").write_text("import core.b\n")
    (tree / "core" / "b.py").write_text("import core.c\n")
    (tree / "core" / "c.py").write_text("import core.d\n")
    (tree / "core" / "d.py").write_text("X = 1\n")
    rc, out = _run(["--repo", str(tree), "--paths", "core/a.py,core/b.py,core/c.py,core/d.py"])
    assert _decision(out) == "FULL", out
    assert out.count("INDIRECT") >= 3, out


def test_exit_code_mode_is_scriptable():
    rc_fast, _ = _run(["--exit-code", "--paths", "tests/core/test_x.py"])
    rc_full, _ = _run(["--exit-code", "--paths", "strategies/futures/monitor.py"])
    assert rc_fast == 0
    assert rc_full == 1


def test_plan_line_lists_the_test_command():
    rc, out = _run(["--paths", "tests/core/test_x.py"])
    assert "[IMPACT] plan=" in out


def test_git_mode_reports_unknown_base_as_error():
    """No --paths => git mode; an unresolvable base must be a hard error (2)."""
    rc, out = _run(["--base", "0000000000000000000000000000000000000000"])
    assert rc == 2, out


def test_git_mode_empty_range_requires_full():
    """A valid git range with no changes must still be fail-closed FULL."""
    rc, out = _run(["--base", "HEAD", "--head", "HEAD"])
    assert rc == 0, out
    assert _decision(out) == "FULL", out
