from __future__ import annotations

import json
from typing import TYPE_CHECKING

from app.ingest.queue import ParseTask

if TYPE_CHECKING:
    from app.runtime import RapidInboxRuntime


class RecoveryScanner:
    def __init__(self, runtime: "RapidInboxRuntime") -> None:
        self.runtime = runtime

    async def run(self) -> None:
        self.runtime.storage.cleanup_stale_parts()
        for manifest_path in sorted(self.runtime.settings.manifests_dir.rglob("*.json")):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                self.runtime.validate_recovery_manifest(manifest)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                # Malformed manifests are skipped so one bad file cannot block startup recovery.
                continue
            await self.runtime.recover_from_manifest(manifest)
        for message_id in await self.runtime.find_messages_for_reparse():
            await self.runtime.parse_queue.enqueue(ParseTask(message_id=message_id))
