#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from email.utils import formatdate
from pathlib import Path
from typing import Any, NamedTuple


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import default_settings  # noqa: E402
from app.db.connection import connect_database  # noqa: E402


DEFAULT_PROCESS_PATTERNS = {
    "ingestd": "rapid-inbox-ingestd",
    "http": "uvicorn app.main:app",
}


class SmtpError(RuntimeError):
    pass


class ProcessSample(NamedTuple):
    label: str
    pid: int
    cpu_percent: float
    rss_bytes: int


class ProcSnapshot(NamedTuple):
    total_ticks: int
    rss_bytes: int


class SendResult(NamedTuple):
    ok: bool
    latency_ms: float
    error: str | None = None


def code_for_index(index: int) -> str:
    return f"{100000 + (index % 900000):06d}"


def utc_run_id() -> str:
    return "ri-stress-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


def normalize_connect_host(host: str) -> str:
    if host in {"0.0.0.0", "::", ""}:
        return "127.0.0.1"
    return host


def build_verification_message(
    *,
    sender: str,
    recipient: str,
    index: int,
    run_id: str,
    code: str,
) -> bytes:
    body = (
        f"Your verification code is {code}.\r\n"
        f"Run: {run_id}\r\n"
        f"Index: {index}\r\n"
    )
    message = (
        f"From: {sender}\r\n"
        f"To: {recipient}\r\n"
        f"Subject: Rapid Inbox verification code {code}\r\n"
        f"Date: {formatdate(localtime=True)}\r\n"
        f"Message-ID: <{run_id}-{index}@rapid-inbox.local>\r\n"
        "MIME-Version: 1.0\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "Content-Transfer-Encoding: 7bit\r\n"
        "\r\n"
        f"{body}"
    )
    return message.encode("utf-8")


def dot_stuff(data: bytes) -> bytes:
    if data.startswith(b"."):
        data = b"." + data
    return data.replace(b"\r\n.", b"\r\n..")


async def read_smtp_response(reader: asyncio.StreamReader) -> tuple[int, str]:
    lines: list[str] = []
    while True:
        raw = await reader.readline()
        if not raw:
            raise SmtpError("SMTP connection closed")
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        lines.append(line)
        if len(line) >= 4 and line[:3].isdigit() and line[3] != "-":
            return int(line[:3]), "\n".join(lines)
        if len(line) < 4:
            raise SmtpError(f"invalid SMTP response: {line!r}")


async def expect_smtp(
    reader: asyncio.StreamReader,
    expected: set[int],
    context: str,
) -> tuple[int, str]:
    code, text = await read_smtp_response(reader)
    if code not in expected:
        raise SmtpError(f"{context}: expected {sorted(expected)}, got {code}: {text}")
    return code, text


async def send_command(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    command: str,
    expected: set[int],
    context: str,
) -> tuple[int, str]:
    writer.write(command.encode("utf-8") + b"\r\n")
    await writer.drain()
    return await expect_smtp(reader, expected, context)


async def open_smtp_session(
    host: str,
    port: int,
    timeout: float,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
    await asyncio.wait_for(expect_smtp(reader, {220}, "banner"), timeout=timeout)
    await asyncio.wait_for(
        send_command(reader, writer, "EHLO rapid-inbox-stress", {250}, "EHLO"),
        timeout=timeout,
    )
    return reader, writer


async def send_one_message(
    *,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    sender: str,
    recipient: str,
    message_bytes: bytes,
    timeout: float,
) -> None:
    await asyncio.wait_for(
        send_command(reader, writer, f"MAIL FROM:<{sender}>", {250}, "MAIL FROM"),
        timeout=timeout,
    )
    await asyncio.wait_for(
        send_command(reader, writer, f"RCPT TO:<{recipient}>", {250}, "RCPT TO"),
        timeout=timeout,
    )
    await asyncio.wait_for(
        send_command(reader, writer, "DATA", {354}, "DATA"),
        timeout=timeout,
    )
    payload = dot_stuff(message_bytes)
    if not payload.endswith(b"\r\n"):
        payload += b"\r\n"
    writer.write(payload + b".\r\n")
    await writer.drain()
    await asyncio.wait_for(expect_smtp(reader, {250}, "message body"), timeout=timeout)


async def close_smtp_session(writer: asyncio.StreamWriter | None) -> None:
    if writer is None:
        return
    try:
        writer.write(b"QUIT\r\n")
        await writer.drain()
    except OSError:
        pass
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass


async def smtp_worker(
    *,
    worker_id: int,
    queue: asyncio.Queue[int],
    host: str,
    port: int,
    timeout: float,
    sender: str,
    recipient: str,
    run_id: str,
    results: list[SendResult],
) -> None:
    reader: asyncio.StreamReader | None = None
    writer: asyncio.StreamWriter | None = None
    while True:
        try:
            index = queue.get_nowait()
        except asyncio.QueueEmpty:
            break

        started = time.perf_counter()
        try:
            if reader is None or writer is None or writer.is_closing():
                reader, writer = await open_smtp_session(host, port, timeout)
            code = code_for_index(index)
            message = build_verification_message(
                sender=sender,
                recipient=recipient,
                index=index,
                run_id=run_id,
                code=code,
            )
            await send_one_message(
                reader=reader,
                writer=writer,
                sender=sender,
                recipient=recipient,
                message_bytes=message,
                timeout=timeout,
            )
            elapsed_ms = (time.perf_counter() - started) * 1000
            results.append(SendResult(ok=True, latency_ms=elapsed_ms))
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - started) * 1000
            results.append(SendResult(ok=False, latency_ms=elapsed_ms, error=f"worker {worker_id}: {exc}"))
            await close_smtp_session(writer)
            reader = None
            writer = None
        finally:
            queue.task_done()

    await close_smtp_session(writer)


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = int(round((len(ordered) - 1) * pct))
    return ordered[index]


def summarize_send_results(results: list[SendResult], elapsed_seconds: float) -> dict[str, Any]:
    successes = [result for result in results if result.ok]
    failures = [result for result in results if not result.ok]
    latencies = [result.latency_ms for result in successes]
    return {
        "attempted": len(results),
        "succeeded": len(successes),
        "failed": len(failures),
        "elapsed_seconds": round(elapsed_seconds, 3),
        "throughput_per_second": round(len(successes) / elapsed_seconds, 2) if elapsed_seconds > 0 else 0.0,
        "latency_ms_avg": round(sum(latencies) / len(latencies), 3) if latencies else 0.0,
        "latency_ms_p50": round(percentile(latencies, 0.50), 3),
        "latency_ms_p95": round(percentile(latencies, 0.95), 3),
        "latency_ms_p99": round(percentile(latencies, 0.99), 3),
        "first_errors": [result.error for result in failures[:5] if result.error],
    }


def read_proc_snapshot(pid: int, *, clock_ticks: int) -> ProcSnapshot | None:
    stat_path = Path("/proc") / str(pid) / "stat"
    status_path = Path("/proc") / str(pid) / "status"
    try:
        stat = stat_path.read_text(encoding="utf-8")
        after_comm = stat.rsplit(") ", 1)[1].split()
        utime = int(after_comm[11])
        stime = int(after_comm[12])
        rss_bytes = 0
        for line in status_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                rss_bytes = int(line.split()[1]) * 1024
                break
    except (FileNotFoundError, IndexError, ValueError):
        return None
    _ = clock_ticks
    return ProcSnapshot(total_ticks=utime + stime, rss_bytes=rss_bytes)


def process_cmdline(pid: int) -> str:
    try:
        raw = (Path("/proc") / str(pid) / "cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()


def discover_processes(patterns: dict[str, str]) -> dict[str, int]:
    matches: dict[str, int] = {}
    for proc_dir in Path("/proc").iterdir():
        if not proc_dir.name.isdigit():
            continue
        pid = int(proc_dir.name)
        cmdline = process_cmdline(pid)
        if not cmdline:
            continue
        for label, pattern in patterns.items():
            if pattern in cmdline:
                matches[label] = max(pid, matches.get(label, 0))
    return matches


async def sample_processes(
    *,
    patterns: dict[str, str],
    interval: float,
    stop_event: asyncio.Event,
) -> list[ProcessSample]:
    samples: list[ProcessSample] = []
    previous: dict[tuple[str, int], tuple[float, int]] = {}
    clock_ticks = os.sysconf(os.sysconf_names["SC_CLK_TCK"])

    while not stop_event.is_set():
        now = time.monotonic()
        for label, pid in discover_processes(patterns).items():
            snapshot = read_proc_snapshot(pid, clock_ticks=clock_ticks)
            if snapshot is None:
                continue
            key = (label, pid)
            if key in previous:
                last_time, last_ticks = previous[key]
                elapsed = now - last_time
                tick_delta = snapshot.total_ticks - last_ticks
                if elapsed > 0 and tick_delta >= 0:
                    cpu_percent = (tick_delta / clock_ticks) / elapsed * 100
                    samples.append(
                        ProcessSample(
                            label=label,
                            pid=pid,
                            cpu_percent=round(cpu_percent, 3),
                            rss_bytes=snapshot.rss_bytes,
                        )
                    )
            previous[key] = (now, snapshot.total_ticks)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except TimeoutError:
            continue
    return samples


def summarize_process_samples(samples: list[ProcessSample]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[ProcessSample]] = defaultdict(list)
    for sample in samples:
        grouped[sample.label].append(sample)

    summary: dict[str, dict[str, Any]] = {}
    for label, label_samples in sorted(grouped.items()):
        cpu_values = [sample.cpu_percent for sample in label_samples]
        rss_values = [sample.rss_bytes for sample in label_samples]
        summary[label] = {
            "pid": label_samples[-1].pid,
            "samples": len(label_samples),
            "cpu_percent_avg": round(sum(cpu_values) / len(cpu_values), 3),
            "cpu_percent_peak": round(max(cpu_values), 3),
            "rss_bytes_avg": round(sum(rss_values) / len(rss_values)),
            "rss_bytes_peak": max(rss_values),
        }
    return summary


def load_default_recipient(local_part: str) -> str:
    settings = default_settings(PROJECT_ROOT)
    with connect_database(settings.database_path) as connection:
        row = connection.execute(
            """
            SELECT root_domain_ascii
            FROM domains
            WHERE is_active = 1
              AND accept_exact = 1
            ORDER BY
              CASE dns_status WHEN 'ok' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,
              id
            LIMIT 1
            """
        ).fetchone()
    if row is None:
        raise RuntimeError("No active exact-match domain found. Pass --to explicitly.")
    return f"{local_part}@{row['root_domain_ascii']}"


def query_database_summary(run_id: str) -> dict[str, Any]:
    settings = default_settings(PROJECT_ROOT)
    pattern = f"<{run_id}-%@rapid-inbox.local>"
    with sqlite3.connect(settings.database_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN parse_status = 'parsed' THEN 1 ELSE 0 END) AS parsed,
                SUM(CASE WHEN verification_code IS NOT NULL AND verification_code != '' THEN 1 ELSE 0 END) AS with_code,
                MIN(received_at) AS first_received_at,
                MAX(received_at) AS last_received_at
            FROM messages
            WHERE message_id_header LIKE ?
            """,
            (pattern,),
        ).fetchone()
    return {
        "messages": int(row["total"] or 0),
        "parsed": int(row["parsed"] or 0),
        "with_verification_code": int(row["with_code"] or 0),
        "first_received_at": row["first_received_at"],
        "last_received_at": row["last_received_at"],
    }


async def wait_for_database(
    *,
    run_id: str,
    expected: int,
    timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    summary = query_database_summary(run_id)
    while time.monotonic() < deadline:
        if summary["messages"] >= expected and summary["parsed"] >= expected:
            return summary
        await asyncio.sleep(0.25)
        summary = query_database_summary(run_id)
    return summary


def parse_process_patterns(values: list[str]) -> dict[str, str]:
    patterns = dict(DEFAULT_PROCESS_PATTERNS)
    for value in values:
        label, separator, pattern = value.partition(":")
        if not separator or not label.strip() or not pattern.strip():
            raise SystemExit("--process must use label:cmd-substring")
        patterns[label.strip()] = pattern.strip()
    return patterns


def parse_args() -> argparse.Namespace:
    settings = default_settings(PROJECT_ROOT)
    default_host = normalize_connect_host(settings.smtp_host)
    run_id = utc_run_id()

    parser = argparse.ArgumentParser(
        description="Run a high-concurrency Rapid Inbox SMTP verification-code delivery stress test.",
    )
    parser.add_argument("--host", default=default_host, help=f"SMTP host, default {default_host}")
    parser.add_argument("--port", type=int, default=settings.smtp_port, help=f"SMTP port, default {settings.smtp_port}")
    parser.add_argument("--to", help="Recipient address. If omitted, the first active exact-match domain is used.")
    parser.add_argument("--local-part", default=f"stress-{run_id}", help="Local part when --to is omitted.")
    parser.add_argument("--sender", default="stress@example.test", help="Envelope/header sender address.")
    parser.add_argument("--count", type=int, default=5000, help="Total messages to send.")
    parser.add_argument("--concurrency", type=int, default=100, help="Concurrent SMTP sessions.")
    parser.add_argument("--timeout", type=float, default=10.0, help="Per SMTP operation timeout in seconds.")
    parser.add_argument("--sample-interval", type=float, default=0.5, help="Process CPU/RSS sample interval in seconds.")
    parser.add_argument("--wait-db-timeout", type=float, default=30.0, help="Seconds to wait for DB parsed rows.")
    parser.add_argument("--no-db-check", action="store_true", help="Skip SQLite result polling after SMTP send.")
    parser.add_argument("--json-output", type=Path, help="Write full result JSON to this path.")
    parser.add_argument(
        "--process",
        action="append",
        default=[],
        help="Process sampler override/addition as label:cmd-substring. Can be repeated.",
    )
    parser.add_argument("--run-id", default=run_id, help="Run id embedded in Message-ID headers.")
    parser.add_argument("--quiet", action="store_true", help="Only print the final summary.")
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    if args.count < 1:
        raise SystemExit("--count must be >= 1")
    if args.concurrency < 1:
        raise SystemExit("--concurrency must be >= 1")
    if args.timeout <= 0:
        raise SystemExit("--timeout must be > 0")
    if args.sample_interval <= 0:
        raise SystemExit("--sample-interval must be > 0")
    if args.wait_db_timeout < 0:
        raise SystemExit("--wait-db-timeout must be >= 0")


async def run_stress(args: argparse.Namespace) -> dict[str, Any]:
    validate_args(args)
    recipient = args.to or load_default_recipient(args.local_part)
    patterns = parse_process_patterns(args.process)
    queue: asyncio.Queue[int] = asyncio.Queue()
    for index in range(1, args.count + 1):
        queue.put_nowait(index)

    results: list[SendResult] = []
    stop_sampler = asyncio.Event()
    sampler_task = asyncio.create_task(
        sample_processes(
            patterns=patterns,
            interval=args.sample_interval,
            stop_event=stop_sampler,
        )
    )

    if not args.quiet:
        print(f"Run id: {args.run_id}")
        print(f"Target: {args.host}:{args.port}")
        print(f"Recipient: {recipient}")
        print(f"Messages: {args.count}, concurrency: {args.concurrency}")

    started = time.perf_counter()
    workers = [
        asyncio.create_task(
            smtp_worker(
                worker_id=worker_id,
                queue=queue,
                host=args.host,
                port=args.port,
                timeout=args.timeout,
                sender=args.sender,
                recipient=recipient,
                run_id=args.run_id,
                results=results,
            )
        )
        for worker_id in range(1, min(args.concurrency, args.count) + 1)
    ]
    await asyncio.gather(*workers)
    elapsed = time.perf_counter() - started
    stop_sampler.set()
    process_samples = await sampler_task

    send_summary = summarize_send_results(results, elapsed)
    db_summary = None
    if not args.no_db_check:
        db_summary = await wait_for_database(
            run_id=args.run_id,
            expected=send_summary["succeeded"],
            timeout=args.wait_db_timeout,
        )

    return {
        "run_id": args.run_id,
        "target": {"host": args.host, "port": args.port},
        "recipient": recipient,
        "send": send_summary,
        "database": db_summary,
        "processes": summarize_process_samples(process_samples),
    }


def format_bytes(value: int) -> str:
    units = ["B", "KiB", "MiB", "GiB"]
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{value} B"


def print_summary(result: dict[str, Any]) -> None:
    send = result["send"]
    print("\nSMTP stress summary")
    print(f"Run id: {result['run_id']}")
    print(f"Recipient: {result['recipient']}")
    print(
        "Send: "
        f"{send['succeeded']}/{send['attempted']} ok, "
        f"{send['failed']} failed, "
        f"{send['elapsed_seconds']}s, "
        f"{send['throughput_per_second']} mail/s"
    )
    print(
        "Latency: "
        f"avg {send['latency_ms_avg']} ms, "
        f"p50 {send['latency_ms_p50']} ms, "
        f"p95 {send['latency_ms_p95']} ms, "
        f"p99 {send['latency_ms_p99']} ms"
    )
    if send["first_errors"]:
        print("First errors:")
        for error in send["first_errors"]:
            print(f"  - {error}")

    if result["database"] is not None:
        db = result["database"]
        print(
            "Database: "
            f"{db['messages']} rows, "
            f"{db['parsed']} parsed, "
            f"{db['with_verification_code']} codes"
        )

    if result["processes"]:
        print("Process samples:")
        for label, summary in result["processes"].items():
            print(
                f"  {label} pid={summary['pid']} "
                f"cpu avg/peak={summary['cpu_percent_avg']}%/{summary['cpu_percent_peak']}% "
                f"rss peak={format_bytes(summary['rss_bytes_peak'])}"
            )
    else:
        print("Process samples: none matched")


def main() -> int:
    args = parse_args()
    result = asyncio.run(run_stress(args))
    print_summary(result)
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON written: {args.json_output}")
    return 0 if result["send"]["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
