"""Static regression: verify verify_runtime.sh contains no mutating commands."""
from pathlib import Path

SCRIPT = Path("scripts/verify_runtime.sh")
FORBIDDEN = [
    r"\bpm2 +(restart|start|stop|reload|delete)\b",
    r"\bkill\b",
    r"\btruncate\b",
    r"\bgit +(reset|checkout|rebase)\b",
    r"\brm\b",
]


def test_no_mutating_commands():
    assert SCRIPT.exists(), f"{SCRIPT} not found"
    content = SCRIPT.read_text()
    errors = []
    for pattern in FORBIDDEN:
        import re
        for lineno, line in enumerate(content.splitlines(), 1):
            if re.search(pattern, line):
                # Skip lines that are part of the static guard grep itself
                if "grep" in line and "verify_runtime.sh" in line:
                    continue
                if line.strip().startswith("#"):
                    continue
                errors.append(f"  Line {lineno}: {line.strip()}")
    assert not errors, f"Mutating commands found in {SCRIPT}:\n" + "\n".join(errors)


def test_static_guard_present():
    """Verify the script contains its own mutating-command static guard."""
    content = SCRIPT.read_text()
    assert "grep -En" in content
    assert "pm2 +(" in content or "pm2 +\\\\" in content or "pm2" in content
    assert "mutating command" in content
