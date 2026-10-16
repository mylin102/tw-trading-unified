import ast
from pathlib import Path


def _writer_source() -> str:
    path = (
        Path(__file__).parents[2]
        / "strategies"
        / "plugins"
        / "futures"
        / "active"
        / "tmf_spread.py"
    )
    # The test is copied into the deployment repository, so prefer its local
    # repository root when running there.
    return path.read_text(encoding="utf-8")


def test_hvwap_release_intent_keeps_gate_reason_out_of_mode():
    tree = ast.parse(_writer_source())
    matches = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Name) or func.id != "_append_event":
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant):
            continue
        if node.args[0].value == "HVWAP_RELEASE_INTENT":
            matches.append(node)

    assert len(matches) == 1
    keywords = {keyword.arg for keyword in matches[0].keywords}
    assert "gate_reason" in keywords
    assert "mode" not in keywords
