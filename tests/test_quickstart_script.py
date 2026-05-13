from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "quickstart.sh"


def test_quickstart_script_is_shellcheckable_entrypoint() -> None:
    assert SCRIPT.exists()
    assert os.access(SCRIPT, os.X_OK)

    syntax = subprocess.run(
        ["bash", "-n", str(SCRIPT)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert syntax.returncode == 0, syntax.stderr


def test_quickstart_script_has_help_output() -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT), "--help"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "bash quickstart.sh" in result.stdout
    assert "Admin login" in result.stdout
    assert "0.0.0.0:8000" in result.stdout
    assert "0.0.0.0:25" in result.stdout
    assert "--build-local" in result.stdout
    assert "--binary-url URL" in result.stdout
    assert "prebuilt" in result.stdout


def test_quickstart_script_downloads_prebuilt_by_default() -> None:
    content = SCRIPT.read_text(encoding="utf-8")

    assert "BUILD_LOCAL_INGESTD=\"${BUILD_LOCAL_INGESTD:-0}\"" in content
    assert "download_cpp_ingestd" in content
    assert "https://github.com/${INGESTD_RELEASE_REPO}/releases/latest/download/${asset_name}" in content
    assert "--build-local)" in content
