from pathlib import Path

import pytest

from scripts.deployment.release_scope import (
    ReleaseScopeError,
    select_release_apps,
    verify_release_apps,
)


ROOT = "/srv/release16"


def _ecosystem():
    pinned = {"LRC_RELEASE_SHA": "new"}
    return [
        {"name": "trading-system", "cwd": ROOT, "script": "taskpolicy", "env": pinned},
        {"name": "dashboard", "cwd": ROOT, "script": "taskpolicy", "env": pinned},
        {"name": "stock-runner", "cwd": ROOT, "script": "taskpolicy", "env": pinned},
        {"name": "metrics", "cwd": ROOT, "script": "metrics.py", "env": {}},
        {"name": "foreign", "cwd": "/srv/other", "script": "worker.py", "env": pinned},
    ]


def _pm2():
    def row(name, sha, cwd=ROOT):
        return {"name": name, "pm2_env": {"pm_cwd": cwd, "LRC_RELEASE_SHA": sha}}

    return [
        row("trading-system", "base"),
        row("dashboard", "base"),
        # Same cwd and an LRC pin are insufficient: this is a different lineage.
        row("stock-runner", "stock-base"),
        row("metrics", "base"),
        row("foreign", "base", "/srv/other"),
    ]


def test_selector_includes_every_same_release_pinned_app_and_excludes_other_lineages():
    selected = select_release_apps(_ecosystem(), _pm2(), ROOT, "trading-system")

    assert selected == ["trading-system", "dashboard"]
    assert "stock-runner" not in selected
    assert "metrics" not in selected
    assert "foreign" not in selected


def test_sha_mismatch_fails_closed_and_names_the_app():
    rows = _pm2()
    rows[1]["pm2_env"]["LRC_RELEASE_SHA"] = "stale"

    with pytest.raises(ReleaseScopeError, match="dashboard"):
        verify_release_apps(rows, ["trading-system", "dashboard"], "base")


def test_missing_scoped_app_also_fails_closed_and_names_the_app():
    with pytest.raises(ReleaseScopeError, match="dashboard"):
        verify_release_apps(_pm2()[:1], ["trading-system", "dashboard"], "base")


def test_deploy_script_forbids_restart_and_update_env_and_has_aux_stage():
    text = (Path(__file__).parents[2] / "scripts" / "deployment" /
            "deploy_release.sh").read_text(encoding="utf-8")

    executable = "\n".join(
        line for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    assert "pm2 restart" not in executable
    assert "--update-env" not in executable
    assert "stage REPIN_AUX" in executable
