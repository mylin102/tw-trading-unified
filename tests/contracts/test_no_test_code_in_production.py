"""Structural guard against test-only behavior leaking into production."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PROD_DIRS = ("core", "strategies", "ui", "deployment", "infra")
FORBIDDEN_MARKERS = ("PYTEST_CURRENT_TEST", '"pytest" in', "'pytest' in")


def production_files():
    for dirname in PROD_DIRS:
        directory = ROOT / dirname
        if directory.exists():
            for path in sorted(directory.rglob("*.py")):
                # Ignore generated patch artifacts, not importable production
                # modules; these are handled by their source-of-truth tests.
                if "_fixed_" in path.name:
                    continue
                yield path


def violations(text: str, filename: str = "source.py") -> list[str]:
    problems: list[str] = []
    if any(marker in text for marker in FORBIDDEN_MARKERS):
        problems.append(f"{filename}: runtime pytest detection")
    tree = ast.parse(text, filename=filename)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [alias.name for alias in node.names]
            if any(name == "tests" or name.startswith("tests.") for name in names):
                problems.append(f"{filename}:{node.lineno}: imports tests")
        if isinstance(node, ast.If) and isinstance(node.test, ast.Constant) and node.test.value is True:
            line = text.splitlines()[node.lineno - 1].lower()
            if "test" in line or "testing" in line:
                problems.append(f"{filename}:{node.lineno}: test switch")
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("/tmp/"):
            # Production may not encode state/coordination paths in /tmp.
            if any(token in node.value.lower() for token in ("state", "coord", "safety", "health", "status")):
                problems.append(f"{filename}:{node.lineno}: /tmp state/coord path")
    return problems


def test_production_has_no_test_runtime_detection_or_tmp_state():
    found = [p for path in production_files() for p in violations(path.read_text(), str(path))]
    assert not found, "\n".join(found)


def test_mutation_pytest_detection_is_rejected():
    mutated = "def f():\n    if 'pytest' in sys.modules:\n        return 1\n"
    assert violations(mutated)  # M1: removing the rule must fail the check


def test_mutation_tmp_state_path_is_rejected():
    mutated = 'MTS_POSITION_STATE_PATH = Path("/tmp/MUTATED_state.json")\n'
    assert any("/tmp state/coord path" in item for item in violations(mutated))  # M2


def test_mutation_true_testing_switch_is_rejected():
    mutated = "if True:  # testing only\n    pass\n"
    assert any("test switch" in item for item in violations(mutated))


def test_seam_register_exists_and_covers_known_high_risk_seams():
    register = ROOT / "docs/contracts/TEST_SEAMS.md"
    text = register.read_text()
    for seam in ("S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8"):
        assert f"{seam}：" in text
        assert "(a)" in text and "(b)" in text


def test_mts_default_is_runtime_authority():
    source = (ROOT / "strategies/futures/monitor.py").read_text()
    assert "MTS_POSITION_STATE_PATH = Path(mts_position_state_path())" in source
    assert 'Path("/tmp/mts_position_state.json")' not in source
