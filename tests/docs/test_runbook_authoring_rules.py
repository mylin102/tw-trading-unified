import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUNBOOK_GLOB = "docs/operations/**/*RUNBOOK*.md"
ARTIFACT_WORDS = re.compile(r"快照|snapshot|capture|evidence|hash", re.I)
ACTION_WORDS = re.compile(
    r"執行|確認|檢查|停止|啟動|部署|切換|回滾|驗證|run|check|stop|start|deploy|verify|restart",
    re.I,
)


def _stage_bodies(text):
    matches = list(re.finditer(r"^##\s+階段\s*\d+[^\n]*\n", text, re.M))
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        yield text[match.end():end]


def _violations(paths):
    errors = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for body in _stage_bodies(text):
            if ARTIFACT_WORDS.search(body) and not ACTION_WORDS.search(body):
                errors.append(f"{path}: artifact-only stage")
        if ARTIFACT_WORDS.search(text) and "〈產物〉" not in text:
            errors.append(f"{path}: missing 〈產物〉 section")
    return errors


def test_runbook_template_exists():
    assert list(ROOT.glob(RUNBOOK_GLOB)), "deploy runbook template is required"


def test_no_artifact_only_stage_in_runbooks():
    paths = list(ROOT.glob(RUNBOOK_GLOB))
    assert not _violations(paths), _violations(paths)


def test_snapshot_mentions_require_artifact_section():
    paths = list(ROOT.glob(RUNBOOK_GLOB))
    for path in paths:
        text = path.read_text(encoding="utf-8")
        if ARTIFACT_WORDS.search(text):
            assert "〈產物〉" in text, path


def test_mutation_artifact_as_stage_fails(tmp_path):
    mutated = tmp_path / "MUTATED_RUNBOOK.md"
    mutated.write_text(
        "# Bad runbook\n\n## 階段 1：抓 snapshot\n\n保存 evidence。\n",
        encoding="utf-8",
    )
    assert _violations([mutated]), "mutation must be rejected"
