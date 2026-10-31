"""Private, verified backups of the three application volumes (stdlib only).

Host scripts stop ingress, check for active work and stop the backend before
calling this helper in an ephemeral, network-isolated container. It never
executes application startup, migrations, model downloads or provider requests.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import sys
import tarfile
import tempfile
from typing import BinaryIO


ROOTS = {name: Path(f"/app/{name}") for name in ("data", "logs", "reports")}
DB_NAME = "video_factory.db"
MANIFEST = "backup-manifest.json"
WORK_DIRS = {".backup_work", ".restore_work"}
FORMAT = "video-factory-three-volume-backup"
ACTIVE_STATES = {"queued", "pending", "running", "submitting", "submitted", "dispatching", "processing"}


def database_connection(path: Path) -> sqlite3.Connection:
    # as_uri correctly quotes spaces, # and ? in paths without changing them.
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=15)


def check_idle(roots: dict[str, Path]) -> None:
    database = roots["data"] / DB_NAME
    if not database.is_file():
        raise RuntimeError("No application database found. Start and initialize the application before backing it up.")
    with closing(database_connection(database)) as connection:
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        active = []
        for table in tables:
            if not (table.endswith(("_jobs", "_renders", "_tasks")) or table == "runtime_jobs"):
                continue
            # Names originate in sqlite_master, but still quote identifiers.
            identifier = '"' + table.replace('"', '""') + '"'
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({identifier})")}
            if "status" not in columns:
                continue
            placeholders = ",".join("?" for _ in ACTIVE_STATES)
            count = connection.execute(
                f"SELECT COUNT(*) FROM {identifier} WHERE lower(status) IN ({placeholders})",
                sorted(ACTIVE_STATES),
            ).fetchone()[0]
            if count:
                active.append(f"{table}: {count}")
        if active:
            raise RuntimeError("Active or queued work exists; let it finish or cancel it in the application, then retry: " + "; ".join(active))


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def add_file(archive: tarfile.TarFile, source: Path, name: str, inventory: dict) -> None:
    if source.is_symlink() or not source.is_file():
        raise RuntimeError(f"Only regular files can be backed up: {name}")
    # Backend is stopped by the host script, so filesystem and DB cannot race.
    inventory[name] = {"sha256": hash_file(source), "size": source.stat().st_size}
    archive.add(source, arcname=name, recursive=False)


def create_backup(roots: dict[str, Path], output: BinaryIO) -> None:
    database = roots["data"] / DB_NAME
    if not database.is_file():
        raise RuntimeError("Application database is missing; refusing an incomplete backup.")
    work_parent = roots["data"] / ".backup_work"
    work_parent.mkdir(mode=0o700, exist_ok=True)
    inventory: dict[str, dict] = {}
    with tempfile.TemporaryDirectory(prefix="snapshot-", dir=work_parent) as work:
        snapshot = Path(work) / DB_NAME
        with closing(database_connection(database)) as source, closing(sqlite3.connect(snapshot)) as target:
            source.backup(target)
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("SQLite backup failed its integrity check.")
        os.chmod(snapshot, 0o600)
        # Always store hard-linked files as independent regular entries so the
        # strict restore path never needs to create archive-supplied links.
        with tarfile.open(fileobj=output, mode="w|gz", dereference=True) as archive:
            for name, root in roots.items():
                if not root.is_dir():
                    raise RuntimeError(f"Required persisted volume is missing: {name}")
                for path in sorted(root.rglob("*")):
                    relative = path.relative_to(root)
                    if relative.parts[0] in WORK_DIRS:
                        continue
                    if path.is_symlink():
                        raise RuntimeError(f"External or symbolic paths are not archived: {name}/{relative.as_posix()}")
                    if path.is_dir():
                        continue
                    if name == "data" and relative.as_posix() in {DB_NAME, DB_NAME + "-wal", DB_NAME + "-shm"}:
                        continue
                    add_file(archive, path, f"{name}/{relative.as_posix()}", inventory)
            add_file(archive, snapshot, f"data/{DB_NAME}", inventory)
            manifest = {
                "format": FORMAT,
                "format_version": 1,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "application_version": os.environ.get("VIDEO_FACTORY_VERSION", "unknown"),
                "application_revision": os.environ.get("VIDEO_FACTORY_REVISION", "unknown"),
                "contains_private_data_and_encryption_key": True,
                "files": inventory,
            }
            if "data/.secrets/master.key" not in inventory:
                raise RuntimeError("The persistent encryption master key is missing; backup cannot safely restore cloud credentials.")
            encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8")
            member = tarfile.TarInfo(MANIFEST)
            member.size, member.mode = len(encoded), 0o600
            archive.addfile(member, io.BytesIO(encoded))
    print(f"Backed up {len(inventory)} files including database, master key, media, logs and reports.", file=sys.stderr)


def require_empty(roots: dict[str, Path]) -> None:
    for name, root in roots.items():
        if not root.is_dir():
            raise RuntimeError(f"Required mounted volume is missing: {name}")
        for path in root.rglob("*"):
            if path.relative_to(root).parts[0] in WORK_DIRS:
                continue
            if path.is_symlink() or path.is_file():
                raise RuntimeError(
                    f"Restore refuses to overwrite existing {name} data. Choose a NEW unused COMPOSE_PROJECT_NAME and empty volumes."
                )


def safe_member_name(name: str) -> PurePosixPath:
    if "\\" in name or ":" in name or "\0" in name:
        raise RuntimeError("Backup contains an unsafe path.")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise RuntimeError("Backup contains an unsafe path.")
    if name != MANIFEST and (len(path.parts) < 2 or path.parts[0] not in ROOTS):
        raise RuntimeError("Backup contains an unexpected volume/path.")
    if name != MANIFEST and path.parts[1] in WORK_DIRS:
        raise RuntimeError("Backup contains a reserved temporary path.")
    return path


def restore_backup(roots: dict[str, Path], source: BinaryIO) -> None:
    require_empty(roots)
    work_parent = roots["data"] / ".restore_work"
    work_parent.mkdir(mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="restore-", dir=work_parent) as work:
        staging = Path(work)
        inventory: dict[str, dict] = {}
        manifest = None
        seen: set[str] = set()
        # Do not use extractall: reject special files, links, aliases and traversal.
        with tarfile.open(fileobj=source, mode="r|gz") as archive:
            for member in archive:
                relative = safe_member_name(member.name)
                name = relative.as_posix()
                if name in seen or name != member.name or not member.isfile():
                    raise RuntimeError("Backup contains duplicate, noncanonical or nonregular entries.")
                seen.add(name)
                if member.size > shutil.disk_usage(staging).free - 16 * 1024 * 1024:
                    raise RuntimeError("Not enough free space to safely stage this backup.")
                contents = archive.extractfile(member)
                if contents is None:
                    raise RuntimeError("Backup entry has no readable content.")
                if name == MANIFEST:
                    if member.size > 16 * 1024 * 1024:
                        raise RuntimeError("Backup manifest is too large.")
                    manifest = json.load(contents)
                    continue
                target = staging.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                digest, copied = hashlib.sha256(), 0
                with target.open("xb") as destination:
                    for block in iter(lambda: contents.read(1024 * 1024), b""):
                        destination.write(block)
                        digest.update(block)
                        copied += len(block)
                if copied != member.size:
                    raise RuntimeError("Backup entry is truncated.")
                os.chmod(target, member.mode & 0o777 & ~0o022)
                inventory[name] = {"sha256": digest.hexdigest(), "size": copied}
        if not isinstance(manifest, dict) or manifest.get("format") != FORMAT or manifest.get("format_version") != 1:
            raise RuntimeError("Not a supported complete Video Factory backup.")
        if inventory != manifest.get("files"):
            raise RuntimeError("Backup file inventory/checksums do not match; nothing has been restored.")
        if f"data/{DB_NAME}" not in inventory or "data/.secrets/master.key" not in inventory:
            raise RuntimeError("Backup is missing the paired database or encryption key.")
        with closing(database_connection(staging / "data" / DB_NAME)) as connection:
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Restored database failed its integrity check.")
        require_empty(roots)
        # Mark partial copies so an interruption is never mistaken for success.
        marker = roots["data"] / "RESTORE_INCOMPLETE"
        marker.write_text("Restore is incomplete. Do not start the application. Restore into new empty volumes.\n", encoding="utf-8")
        for name in inventory:
            relative = PurePosixPath(name)
            target = roots[relative.parts[0]].joinpath(*relative.parts[1:])
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
            with (staging.joinpath(*relative.parts)).open("rb") as reader, target.open("xb") as writer:
                shutil.copyfileobj(reader, writer, length=1024 * 1024)
            os.chmod(target, 0o600 if name == "data/.secrets/master.key" else 0o640)
        marker.unlink()
    print(f"Restored and verified {len(inventory)} files. Start the application normally to run any required migrations.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check-idle", "check-empty", "backup", "restore"))
    arguments = parser.parse_args()
    try:
        if arguments.command == "check-idle":
            check_idle(ROOTS)
        elif arguments.command == "check-empty":
            require_empty(ROOTS)
        elif arguments.command == "backup":
            create_backup(ROOTS, sys.stdout.buffer)
        else:
            restore_backup(ROOTS, sys.stdin.buffer)
    except Exception as error:
        print(f"Backup/restore failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
