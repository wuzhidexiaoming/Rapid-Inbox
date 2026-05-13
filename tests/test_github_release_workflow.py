from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release-ingestd.yml"


def test_release_workflow_builds_and_publishes_ingestd_binary() -> None:
    content = WORKFLOW.read_text(encoding="utf-8")

    assert "actions/checkout@v4" in content
    assert "cmake -S cpp/ingestd -B cpp/ingestd/build" in content
    assert "ctest --test-dir cpp/ingestd/build --output-on-failure" in content
    assert "rapid-inbox-ingestd-linux-x86_64.tar.gz" in content
    assert "actions/upload-artifact@v4" in content
    assert "softprops/action-gh-release@v2" in content
    assert "startsWith(github.ref, 'refs/tags/')" in content
