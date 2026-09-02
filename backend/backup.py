"""妙舆数据备份/恢复工具。

默认备份 settings.db、tasks/raw/reports 文件；恢复必须显式传 --yes，避免误覆盖。
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from config import DATA_DIR, SETTINGS_DB


def _safe_member(name: str) -> bool:
    path = Path(name)
    return not path.is_absolute() and ".." not in path.parts and path.name not in {"", "."}


def create_backup(output: str | Path | None = None, *, data_dir: Path = DATA_DIR,
                  db_path: Path = SETTINGS_DB) -> Path:
    target = Path(output) if output else data_dir / "backups" / f"miaoyu-{datetime.now():%Y%m%d_%H%M%S}.zip"
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".db", dir=target.parent, delete=False) as temp:
        snapshot = Path(temp.name)
    try:
        source = sqlite3.connect(str(db_path))
        destination = sqlite3.connect(str(snapshot))
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(snapshot, "settings.db")
            for folder in ("tasks", "raw", "reports"):
                root = data_dir / folder
                if not root.exists():
                    continue
                for path in root.rglob("*"):
                    if path.is_file():
                        archive.write(path, str(Path(folder) / path.relative_to(root)))
        return target
    finally:
        snapshot.unlink(missing_ok=True)


def restore_backup(archive_path: str | Path, *, data_dir: Path = DATA_DIR,
                   db_path: Path = SETTINGS_DB, confirm: bool = False) -> Path:
    if not confirm:
        raise ValueError("恢复会覆盖现有数据，必须显式 confirm=True/--yes")
    archive = Path(archive_path)
    if not archive.exists():
        raise FileNotFoundError(archive)
    with tempfile.TemporaryDirectory(prefix="miaoyu-restore-") as tmp:
        staging = Path(tmp)
        with zipfile.ZipFile(archive) as source:
            bad = [name for name in source.namelist() if not _safe_member(name)]
            if bad:
                raise ValueError("备份包含不安全路径")
            source.extractall(staging)
        staged_db = staging / "settings.db"
        if not staged_db.exists():
            raise ValueError("备份缺少 settings.db")
        check = sqlite3.connect(str(staged_db))
        try:
            result = check.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            check.close()
        if result != "ok":
            raise ValueError(f"SQLite 完整性检查失败: {result}")
        data_dir.mkdir(parents=True, exist_ok=True)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(staged_db, db_path)
        for folder in ("tasks", "raw", "reports"):
            staged = staging / folder
            if not staged.exists():
                continue
            destination = data_dir / folder
            destination.mkdir(parents=True, exist_ok=True)
            for path in staged.rglob("*"):
                if path.is_file():
                    target = destination / path.relative_to(staged)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, target)
    return db_path


def main() -> None:
    parser = argparse.ArgumentParser(description="妙舆数据备份/恢复")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("output", nargs="?")
    restore = sub.add_parser("restore")
    restore.add_argument("archive")
    restore.add_argument("--yes", action="store_true", help="确认覆盖现有数据")
    args = parser.parse_args()
    if args.command == "create":
        print(create_backup(args.output))
    else:
        print(restore_backup(args.archive, confirm=args.yes))


if __name__ == "__main__":
    main()
