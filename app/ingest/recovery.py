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
        policy_manifests: list[dict[str, object]] = []
        legacy_manifests: list[dict[str, object]] = []
        latest_policy_snapshots: dict[int, dict[str, object]] = {}
        for manifest_path in sorted(self.runtime.settings.manifests_dir.rglob("*.json")):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                self.runtime.validate_recovery_manifest(manifest)
            except (json.JSONDecodeError, ValueError):
                # Malformed manifests are skipped so one bad file cannot block startup recovery.
                continue
            if self._has_domain_policy(manifest):
                policy_manifests.append(manifest)
                self._record_latest_policy_snapshots(latest_policy_snapshots, manifest)
            else:
                legacy_manifests.append(manifest)

        for snapshot in self._sorted_snapshots(latest_policy_snapshots):
            try:
                await self.runtime.recover_domain_snapshot(snapshot)
            except ValueError:
                continue

        for manifest in policy_manifests + legacy_manifests:
            try:
                await self.runtime.recover_from_manifest(manifest)
            except ValueError:
                # Legacy manifests can remain unrecoverable if the matching domain never reappears.
                continue

        for message_id in await self.runtime.find_messages_for_reparse():
            await self.runtime.parse_queue.enqueue(ParseTask(message_id=message_id))

    def _has_domain_policy(self, manifest: dict[str, object]) -> bool:
        recipients = manifest.get("recipients")
        if not isinstance(recipients, list) or not recipients:
            return False
        return any(isinstance(recipient, dict) and recipient.get("domain_policy") is not None for recipient in recipients)

    def _record_latest_policy_snapshots(
        self,
        latest_policy_snapshots: dict[int, dict[str, object]],
        manifest: dict[str, object],
    ) -> None:
        received_at = str(manifest["received_at"])
        for recipient in manifest["recipients"]:
            if not isinstance(recipient, dict):
                continue
            domain_policy = recipient.get("domain_policy")
            if domain_policy is None:
                continue
            domain_id = int(recipient["domain_id"])
            snapshot = {
                "domain_id": domain_id,
                "root_domain_ascii": str(recipient["root_domain_ascii"]),
                "received_at": received_at,
                "domain_policy": domain_policy,
            }
            current = latest_policy_snapshots.get(domain_id)
            if current is None or str(current["received_at"]) <= received_at:
                latest_policy_snapshots[domain_id] = snapshot

    def _sorted_snapshots(self, latest_policy_snapshots: dict[int, dict[str, object]]) -> list[dict[str, object]]:
        return sorted(
            latest_policy_snapshots.values(),
            key=lambda snapshot: (str(snapshot["received_at"]), int(snapshot["domain_id"])),
        )
