from __future__ import annotations

import os
import subprocess
import sys
import textwrap
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


def test_quickstart_uses_dotenv_http_settings(tmp_path: Path) -> None:
    http_port = 36517
    script = tmp_path / "quickstart.sh"
    script.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    script.chmod(0o755)
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "HOST=127.0.0.1",
                f"PORT={http_port}",
                "SMTP_HOST=127.0.0.1",
                "SMTP_PORT=2525",
            ]
        ),
        encoding="utf-8",
    )

    bin_dir = tmp_path / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    _write_executable(
        bin_dir / "python",
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import sys
            from pathlib import Path

            if len(sys.argv) == 4 and sys.argv[1] == "-" and Path(sys.argv[2]).name == ".env":
                key = sys.argv[3]
                for raw_line in Path(sys.argv[2]).read_text(encoding="utf-8").splitlines():
                    line = raw_line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if line.startswith("export "):
                        line = line[7:].strip()
                    if "=" not in line:
                        continue
                    found_key, value = line.split("=", 1)
                    if found_key.strip() != key:
                        continue
                    value = value.strip()
                    if len(value) >= 2 and value[0] == value[-1] and value[0] in {{"'", '"'}}:
                        value = value[1:-1]
                    print(value)
                    raise SystemExit(0)
                raise SystemExit(1)

            if len(sys.argv) == 5 and sys.argv[1] == "-":
                raise SystemExit(0)

            raise SystemExit(f"unexpected fake python invocation: {{sys.argv!r}}")
            """
        ),
    )
    _write_executable(
        bin_dir / "rapid-inbox-http",
        textwrap.dedent(
            """\
            #!/bin/sh
            trap 'exit 0' INT TERM
            while :; do
                sleep 1
            done
            """
        ),
    )

    env = os.environ.copy()
    for key in ("HOST", "PORT", "HTTP_HOST", "HTTP_PORT", "SMTP_HOST", "SMTP_PORT"):
        env.pop(key, None)
    env["VENV_DIR"] = str(tmp_path / ".venv")

    result = subprocess.run(
        ["timeout", "6", "bash", str(script), "--python-smtp", "--no-install"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode in {0, 124}, result.stderr
    assert f"HTTP bound to: 127.0.0.1:{http_port}" in result.stdout
    assert f"Admin login: http://127.0.0.1:{http_port}/admin/login" in result.stdout
    assert "SMTP runner: Python embedded SMTP bound to 127.0.0.1:2525" in result.stdout


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)
