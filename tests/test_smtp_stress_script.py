from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "smtp_stress_test.py"


def _load_script_module():
    spec = importlib.util.spec_from_file_location("smtp_stress_test", SCRIPT)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_smtp_stress_script_has_help_output() -> None:
    result = subprocess.run(
        ["python3", str(SCRIPT), "--help"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "--count" in result.stdout
    assert "--concurrency" in result.stdout
    assert "--json-output" in result.stdout
    assert "用法:" in result.stdout
    assert "选项:" in result.stdout
    assert "显示帮助信息并退出" in result.stdout


def test_build_verification_message_contains_unique_code_and_run_id() -> None:
    module = _load_script_module()

    payload = module.build_verification_message(
        sender="stress@example.test",
        recipient="code@adb.com",
        index=42,
        run_id="ri-stress-20260513",
        code="654321",
    )

    text = payload.decode("utf-8")
    assert "\r\n" in text
    assert "To: code@adb.com" in text
    assert "Message-ID: <ri-stress-20260513-42@rapid-inbox.local>" in text
    assert "Subject: Rapid Inbox verification code 654321" in text
    assert "Your verification code is 654321." in text


def test_dot_stuff_data_lines_for_smtp() -> None:
    module = _load_script_module()

    stuffed = module.dot_stuff(b"first\r\n.second\r\n..\r\nlast\r\n")

    assert stuffed == b"first\r\n..second\r\n...\r\nlast\r\n"


def test_summarize_process_samples_reports_peak_and_average() -> None:
    module = _load_script_module()

    summary = module.summarize_process_samples(
        [
            module.ProcessSample(label="ingestd", pid=10, cpu_percent=50.0, rss_bytes=1000),
            module.ProcessSample(label="ingestd", pid=10, cpu_percent=150.0, rss_bytes=3000),
            module.ProcessSample(label="http", pid=20, cpu_percent=10.0, rss_bytes=2000),
        ]
    )

    assert summary["ingestd"]["pid"] == 10
    assert summary["ingestd"]["samples"] == 2
    assert summary["ingestd"]["cpu_percent_avg"] == 100.0
    assert summary["ingestd"]["cpu_percent_peak"] == 150.0
    assert summary["ingestd"]["rss_bytes_peak"] == 3000
    assert summary["http"]["samples"] == 1


def test_print_summary_uses_chinese_labels(capsys) -> None:
    module = _load_script_module()

    module.print_summary(
        {
            "run_id": "ri-stress-20260513",
            "recipient": "code@adb.com",
            "send": {
                "attempted": 2,
                "succeeded": 2,
                "failed": 0,
                "elapsed_seconds": 1.25,
                "throughput_per_second": 1.6,
                "latency_ms_avg": 12.5,
                "latency_ms_p50": 12.0,
                "latency_ms_p95": 15.0,
                "latency_ms_p99": 15.0,
                "first_errors": [],
            },
            "database": {
                "messages": 2,
                "parsed": 2,
                "with_verification_code": 2,
                "first_received_at": "2026-05-13T04:11:53Z",
                "last_received_at": "2026-05-13T04:11:54Z",
            },
            "processes": {
                "ingestd": {
                    "pid": 10,
                    "samples": 2,
                    "cpu_percent_avg": 100.0,
                    "cpu_percent_peak": 150.0,
                    "rss_bytes_peak": 3000,
                }
            },
        }
    )

    text = capsys.readouterr().out
    assert "压测结果" in text
    assert "运行ID" in text
    assert "收件人" in text
    assert "投递" in text
    assert "延迟" in text
    assert "数据库" in text
    assert "进程采样" in text
