from __future__ import annotations

import json
from typing import TYPE_CHECKING

from app.db.connection import connect_database
from app.ingest.queue import ParseTask

if TYPE_CHECKING:
    from app.runtime import RapidInboxRuntime


class RecoveryScanner:
    def __init__(self, runtime: "RapidInboxRuntime") -> None:
        self.runtime = runtime

    async def run(self) -> None:
        if self._database_needs_manifest_recovery():
            await self._recover_manifests()

        await self._requeue_unparsed_messages()

    async def _recover_manifests(self) -> None:
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
                self._record_latest_policy_snapshots(latest_policy_snapshots, manifest_path, manifest)
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

    async def _requeue_unparsed_messages(self) -> None:
        for message_id in await self.runtime.find_messages_for_reparse():
            await self.runtime.parse_queue.enqueue(ParseTask(message_id=message_id))

    def _database_needs_manifest_recovery(self) -> bool:
        with connect_database(self.runtime.settings.database_path) as connection:
            messages_count = int(
                connection.execute("SELECT COUNT(*) AS count FROM messages").fetchone()["count"]
            )
            if messages_count == 0:
                return True

            domains_count = int(
                connection.execute("SELECT COUNT(*) AS count FROM domains").fetchone()["count"]
            )
            if domains_count == 0:
                return True

            messages_without_deliveries = int(
                connection.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM messages AS m
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM message_deliveries AS d
                        WHERE d.message_id = m.id
                    )
                    """
                ).fetchone()["count"]
            )
        return messages_without_deliveries > 0

    def _has_domain_policy(self, manifest: dict[str, object]) -> bool:
        recipients = manifest.get("recipients")
        if not isinstance(recipients, list) or not recipients:
            return False
        return any(isinstance(recipient, dict) and recipient.get("domain_policy") is not None for recipient in recipients)

    def _record_latest_policy_snapshots(
        self,
        latest_policy_snapshots: dict[int, dict[str, object]],
        manifest_path,
        manifest: dict[str, object],
    ) -> None:
        recovery_order = self._manifest_recovery_order(manifest_path, manifest)
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
                "received_at": str(manifest["received_at"]),
                "domain_policy": domain_policy,
                "_recovery_order": recovery_order,
            }
            current = latest_policy_snapshots.get(domain_id)
            if current is None or self._recovery_order_key(current) <= recovery_order:
                latest_policy_snapshots[domain_id] = snapshot

    def _sorted_snapshots(self, latest_policy_snapshots: dict[int, dict[str, object]]) -> list[dict[str, object]]:
        return sorted(
            latest_policy_snapshots.values(),
            key=lambda snapshot: (*self._recovery_order_key(snapshot), int(snapshot["domain_id"])),
        )

    def _recovery_order_key(self, snapshot: dict[str, object]) -> tuple[int, int]:
        order = snapshot["_recovery_order"]
        if not isinstance(order, tuple) or len(order) != 2:
            return (0, 0)
        return (int(order[0]), int(order[1]))

    def _manifest_recovery_order(self, manifest_path, manifest: dict[str, object]) -> tuple[int, int]:
        try:
            mtime_ns = manifest_path.stat().st_mtime_ns
        except OSError as exc:
            raise ValueError("invalid recovery manifest") from exc

        recovery_order_ns = manifest.get("recovery_order_ns")
        if recovery_order_ns is None:
            return (mtime_ns, mtime_ns)
        if not isinstance(recovery_order_ns, int) or isinstance(recovery_order_ns, bool) or recovery_order_ns < 0:
            raise ValueError("invalid recovery manifest")
        return (recovery_order_ns, mtime_ns)
