#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import default_settings  # noqa: E402
from app.db.connection import connect_database, initialize_database  # noqa: E402


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%SZ")


def _resolve_database_path(args: argparse.Namespace) -> Path:
    base_dir = args.base_dir.expanduser().resolve(strict=False)
    if args.database is None:
        return default_settings(base_dir).database_path.resolve(strict=False)

    database_path = args.database.expanduser()
    if not database_path.is_absolute():
        database_path = base_dir / database_path
    return database_path.resolve(strict=False)


def _backup_path_for(database_path: Path) -> Path:
    return database_path.with_name(f"{database_path.name}.backup-{_utc_stamp()}")


def _backup_database(database_path: Path) -> Path:
    backup_path = _backup_path_for(database_path)
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    source_uri = database_path.as_uri()
    with sqlite3.connect(f"{source_uri}?mode=ro", uri=True) as source:
        with sqlite3.connect(backup_path) as target:
            source.backup(target)
    try:
        backup_path.chmod(0o600)
    except OSError:
        pass
    return backup_path


def _run_checks(database_path: Path) -> None:
    with connect_database(database_path) as connection:
        integrity_rows = connection.execute("PRAGMA integrity_check;").fetchall()
        integrity_errors = [str(row[0]) for row in integrity_rows if str(row[0]).lower() != "ok"]
        if integrity_errors:
            raise RuntimeError("SQLite integrity_check failed: " + "; ".join(integrity_errors))

        foreign_key_errors = connection.execute("PRAGMA foreign_key_check;").fetchall()
        if foreign_key_errors:
            samples = [
                f"{row[0]} rowid={row[1]} parent={row[2]} fkid={row[3]}"
                for row in foreign_key_errors[:5]
            ]
            suffix = "" if len(foreign_key_errors) <= 5 else f" ... 共 {len(foreign_key_errors)} 条"
            raise RuntimeError("SQLite foreign_key_check failed: " + "; ".join(samples) + suffix)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="对 Rapid Inbox SQLite 数据库执行一次性 schema 迁移。")
    parser.add_argument(
        "--base-dir",
        type=Path,
        default=Path.cwd(),
        help="读取 .env 和解析相对路径时使用的目录，默认当前工作目录。",
    )
    parser.add_argument(
        "--database",
        type=Path,
        help="SQLite 数据库路径；不填时使用 .env/环境变量中的 DATABASE_PATH。",
    )
    parser.add_argument(
        "--allow-create",
        action="store_true",
        help="允许数据库文件不存在时创建新库；默认会拒绝，避免线上路径写错。",
    )
    parser.add_argument(
        "--skip-backup",
        action="store_true",
        help="跳过迁移前备份；只建议在已经手动备份后使用。",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    database_path = _resolve_database_path(args)

    if not database_path.exists() and not args.allow_create:
        print(
            f"数据库不存在，已停止: {database_path}\n"
            "如果确认要创建新库，请加 --allow-create。",
            file=sys.stderr,
        )
        return 2

    backup_path: Path | None = None
    if database_path.exists() and not args.skip_backup:
        backup_path = _backup_database(database_path)
        print(f"已备份数据库: {backup_path}")

    print(f"开始迁移数据库: {database_path}")
    initialize_database(database_path)
    _run_checks(database_path)
    print("迁移完成，完整性检查通过。")
    if backup_path is not None:
        print(f"如需回滚，可先停服务再恢复备份: {backup_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
