"""T19: manifest integrity (design §7)."""
import json
import os
import tempfile

from scripts.research.exit_attribution.manifest import (
    build_manifest,
    sha256_file,
    verify_manifest,
)


def _tmp_file(content: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as fh:
        fh.write(content)
    return path


def test_t19_manifest_integrity():
    fills = _tmp_file("fill-a\nfill-b\n")
    ticks = _tmp_file("tick-1\ntick-2\n")
    manifest = build_manifest(
        run_id="run-1",
        input_paths={"fills": fills, "ticks": ticks},
        schema_version="exit_attribution.v1",
        repo_root=os.getcwd(),
    )
    assert manifest["schema_version"] == "exit_attribution.v1"
    assert manifest["input_sha256"]["fills"] == sha256_file(fills)
    assert verify_manifest(manifest, {"fills": fills, "ticks": ticks}) == {}

    # mutate the input -> manifest mismatch detected
    with open(fills, "a") as fh:
        fh.write("fill-c\n")
    mismatches = verify_manifest(manifest, {"fills": fills, "ticks": ticks})
    assert "fills" in mismatches
    assert "ticks" not in mismatches


def test_t19b_manifest_git_head_recorded():
    manifest = build_manifest(
        run_id="run-2",
        input_paths={},
        schema_version="exit_attribution.v1",
        repo_root=os.getcwd(),
    )
    assert "commit" in manifest["git"]
    assert "dirty" in manifest["git"]
    assert manifest["fee_source"]["effective_date"] == ""
