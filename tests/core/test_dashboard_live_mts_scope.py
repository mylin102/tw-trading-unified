import ast
from pathlib import Path


def _load_helper():
    source = (Path(__file__).parents[2] / "ui" / "dashboard.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_quick_mts_summary_presentation"
    )
    namespace = {}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "<helper>", "exec"), namespace)
    return namespace["_quick_mts_summary_presentation"]


def test_live_quick_summary_is_not_reported_as_state_unavailable():
    presentation = _load_helper()(True, None, "LIVE_BROKER_SCOPE", "—")

    assert presentation["level"] == "info"
    assert "券商" in presentation["message"]
    assert "狀態不可用" not in presentation["message"]
    assert "legacy/tmp" not in presentation["message"]


def test_paper_missing_state_remains_fail_closed_error():
    presentation = _load_helper()(False, None, "STATE_UNAVAILABLE", "/runtime/state.json")

    assert presentation["level"] == "error"
    assert "STATE_UNAVAILABLE" in presentation["message"]
    assert "不使用 legacy/tmp fallback" in presentation["message"]
