from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from app.config import Settings


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def path_date_parts(timestamp: str) -> tuple[str, str, str]:
    dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return (f"{dt.year:04d}", f"{dt.month:02d}", f"{dt.day:02d}")


def safe_filename(filename: str | None) -> str:
    base_name = filename or "attachment.bin"
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", base_name).strip("._")
    return cleaned or "attachment.bin"


class FileStorage:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def storage_root(self) -> Path:
        return self._settings.storage_root

    def raw_message_path(self, message_id: str, received_at: str) -> str:
        return self._dated_path("raw", message_id, ".eml", received_at)

    def write_raw_message(self, message_id: str, received_at: str, content: bytes) -> tuple[str, str, int]:
        relative_path = self.raw_message_path(message_id, received_at)
        self._write_bytes(relative_path, content)
        digest = hashlib.sha256(content).hexdigest()
        return relative_path, digest, len(content)

    def write_text_body(self, message_id: str, received_at: str, content: str) -> str:
        relative_path = self._dated_path("text", message_id, ".txt", received_at)
        self._write_bytes(relative_path, content.encode("utf-8"))
        return relative_path

    def write_html_body(self, message_id: str, received_at: str, content: str) -> str:
        relative_path = self._dated_path("html", message_id, ".html", received_at)
        self._write_bytes(relative_path, content.encode("utf-8"))
        return relative_path

    def write_attachment(self, message_id: str, attachment_id: str, filename: str | None, content: bytes) -> tuple[str, str]:
        safe_name = safe_filename(filename)
        relative_path = str(Path("attachments") / message_id / f"{attachment_id}-{safe_name}")
        self._write_bytes(relative_path, content)
        return relative_path, safe_name

    def write_manifest(self, message_id: str, received_at: str, payload: dict[str, object]) -> str:
        relative_path = self._dated_path("manifests", message_id, ".json", received_at)
        content = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        self._write_bytes(relative_path, content)
        return relative_path

    def read_bytes(self, relative_path: str) -> bytes:
        return self.resolve(relative_path).read_bytes()

    def read_text(self, relative_path: str | None) -> str | None:
        if not relative_path:
            return None
        return self.resolve(relative_path).read_text(encoding="utf-8")

    def resolve(self, relative_path: str) -> Path:
        return self.storage_root / relative_path

    def cleanup_stale_parts(self) -> None:
        # Legacy visible temp files are safe to clean in fixed-extension stores.
        for category in (self._settings.raw_dir, self._settings.text_dir, self._settings.html_dir, self._settings.manifests_dir):
            for part_file in category.rglob("*.part"):
                part_file.unlink(missing_ok=True)

        # Hidden temp files are the current write-ahead artifact naming scheme.
        for part_file in self.storage_root.rglob(".*.part"):
            part_file.unlink(missing_ok=True)

    def clear_mail_data(self) -> None:
        for directory in (
            self._settings.raw_dir,
            self._settings.text_dir,
            self._settings.html_dir,
            self._settings.attachments_dir,
            self._settings.manifests_dir,
            self._settings.tmp_dir,
        ):
            shutil.rmtree(directory, ignore_errors=True)
            directory.mkdir(parents=True, exist_ok=True)

    def _dated_path(self, category: str, message_id: str, suffix: str, received_at: str) -> str:
        year, month, day = path_date_parts(received_at)
        return str(Path(category) / year / month / day / f"{message_id}{suffix}")

    def _write_bytes(self, relative_path: str, content: bytes) -> None:
        final_path = self.resolve(relative_path)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        # Keep write-ahead temp files hidden so final filenames can safely end in ".part".
        part_path = final_path.with_name(f".{final_path.name}.part")
        with part_path.open("wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(part_path, final_path)
        self._fsync_directory_chain(final_path.parent)

    def _fsync_directory_chain(self, directory: Path) -> None:
        current = directory
        while True:
            self._fsync_directory(current)
            if current.parent == current:
                break
            current = current.parent

    def _fsync_directory(self, directory: Path) -> None:
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        try:
            directory_fd = os.open(directory, flags)
        except OSError:
            return
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
