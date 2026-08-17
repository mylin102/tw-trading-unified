"""restart_live.sh must make repo imports available to its Python steps."""

from pathlib import Path
import subprocess


def test_restart_live_exports_repo_pythonpath():
    script = Path(__file__).parents[2] / "scripts" / "restart_live.sh"
    text = script.read_text(encoding="utf-8")
    assert 'export PYTHONPATH="$REPO_DIR${PYTHONPATH:+:$PYTHONPATH}"' in text
    assert 'scripts/reconcile_pending_orders.py' in text
    assert subprocess.run(["bash", "-n", str(script)]).returncode == 0
