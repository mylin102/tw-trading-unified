"""2026-11-05 Hermes Agent: requirements.txt and pyproject.toml had drifted.

pyproject declared only shioaji + ruamel.yaml, so an environment built from it
(`uv sync`) could not import pydantic/ruamel for the dashboard or schema tests --
collect-only reported 10 ModuleNotFoundError while the shared system Python
reported a different, larger set. Keep the two declarations equal; this test is
the mechanical gate.
"""
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _reqs() -> list[str]:
    out = []
    for line in (ROOT / "requirements.txt").read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line and not line.startswith("-"):
            out.append(line)
    return out


def _norm(spec: str) -> str:
    return re.split(r"[<>=!~;\[ ]", spec, 1)[0].strip().lower().replace("_", "-")


def test_pyproject_declares_every_requirement_with_the_same_pin():
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    by_name = {_norm(d): d for d in declared}
    missing, pin_diff = [], []
    for req in _reqs():
        key = _norm(req)
        if key not in by_name:
            missing.append(req)
        elif "==" in req and "==" in by_name[key] and req.replace(" ", "") != by_name[key].replace(" ", ""):
            pin_diff.append((req, by_name[key]))
    assert not missing, f"missing from pyproject.dependencies: {missing}"
    assert not pin_diff, f"pin mismatch: {pin_diff}"
