from __future__ import annotations

import asyncio

import pytest

from app.ingest.queue import ParseQueue, ParseTask


@pytest.mark.asyncio
async def test_parse_queue_runs_configured_workers_concurrently() -> None:
    started: list[str] = []
    both_started = asyncio.Event()
    release_workers = asyncio.Event()

    async def worker(task: ParseTask) -> None:
        started.append(task.message_id)
        if len(started) == 2:
            both_started.set()
        await release_workers.wait()

    queue = ParseQueue(worker, worker_count=2)
    await queue.start()
    try:
        await queue.enqueue(ParseTask(message_id="msg_one"))
        await queue.enqueue(ParseTask(message_id="msg_two"))
        await asyncio.wait_for(both_started.wait(), timeout=2)
        release_workers.set()
        await asyncio.wait_for(queue.drain(), timeout=2)
    finally:
        release_workers.set()
        await queue.stop()

    assert set(started) == {"msg_one", "msg_two"}


@pytest.mark.asyncio
async def test_parse_queue_waits_only_for_matching_active_tasks() -> None:
    started_old = asyncio.Event()
    release_old = asyncio.Event()

    async def worker(task: ParseTask) -> None:
        if task.message_id == "msg_old":
            started_old.set()
            await release_old.wait()

    queue = ParseQueue(worker, worker_count=2)
    await queue.start()
    try:
        await queue.enqueue(ParseTask(message_id="msg_old"))
        await asyncio.wait_for(started_old.wait(), timeout=2)

        waiter = asyncio.create_task(queue.wait_until_not_active(lambda message_id: message_id == "msg_old"))
        await asyncio.sleep(0)
        assert waiter.done() is False

        release_old.set()
        await asyncio.wait_for(waiter, timeout=2)
        await asyncio.wait_for(queue.drain(), timeout=2)
    finally:
        release_old.set()
        await queue.stop()


@pytest.mark.asyncio
async def test_parse_queue_stop_can_discard_pending_tasks() -> None:
    started: list[str] = []
    worker_started = asyncio.Event()

    async def worker(task: ParseTask) -> None:
        started.append(task.message_id)
        worker_started.set()
        await asyncio.sleep(60)

    queue = ParseQueue(worker, worker_count=1)
    await queue.start()
    await queue.enqueue(ParseTask(message_id="msg_active"))
    await queue.enqueue(ParseTask(message_id="msg_pending"))
    await asyncio.wait_for(worker_started.wait(), timeout=2)

    await queue.stop(discard_pending=True, timeout=0.01)

    assert started == ["msg_active"]
    assert queue.is_running is False
