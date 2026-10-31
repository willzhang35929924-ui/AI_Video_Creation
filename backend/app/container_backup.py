"""Offline backup and empty-volume restore for the Docker deployment.

Archives contain private credentials: keep them off GitHub and protect them.
Stop the application before either operation so media and database agree.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import sys
import tarfile
import tempfile

DATA_ROOT = Path('/app/data')
DATABASE = 'video_factory.db'
SECRET = '.secrets/master.key'
EXCLUDED_ROOTS = {'backups', 'tmp', 'home'}


class BackupError(Exception):
    pass


def _root(root: Path) -> Path:
    if root.is_symlink() or not root.is_dir():
        raise BackupError('data root must be an existing directory, not a symlink')
    return root.resolve(strict=True)


def _entries(root: Path):
    """Enumerate without following symlinks; reject unknown special files."""
    def scan(directory: Path):
        for entry in sorted(os.scandir(directory), key=lambda item: item.name):
            path = Path(entry.path)
            relative = path.relative_to(root)
            if len(relative.parts) == 1 and relative.name in EXCLUDED_ROOTS:
                continue
            if len(relative.parts) == 1 and relative.name in {DATABASE + '-wal', DATABASE + '-shm', DATABASE + '-journal'}:
                continue
            metadata = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode):
                raise BackupError(f'symlink found in data, backup stopped: {relative}')
            if stat.S_ISDIR(metadata.st_mode):
                yield path, relative
                yield from scan(path)
            elif stat.S_ISREG(metadata.st_mode):
                yield path, relative
            else:
                raise BackupError(f'unsupported special file in data: {relative}')
    yield from scan(root)


def _database_snapshot(source: Path, destination: Path):
    # mode=ro prevents a missing database silently creating an empty one.
    connection = sqlite3.connect(source.resolve(strict=True).as_uri() + '?mode=ro', uri=True)
    try:
        output = sqlite3.connect(destination)
        try:
            connection.backup(output)
            check = output.execute('PRAGMA quick_check').fetchone()
            if not check or check[0] != 'ok':
                raise BackupError('database snapshot failed SQLite integrity check')
        finally:
            output.close()
    finally:
        connection.close()


def backup(root: Path, archive: Path):
    root = _root(root)
    database = root / DATABASE
    secret = root / SECRET
    if not database.is_file() or database.is_symlink() or not secret.is_file() or secret.is_symlink():
        raise BackupError('database and private master key must both exist before backup')
    archive = archive.absolute()
    # Never put the output among data being scanned.
    if archive.is_relative_to(root) and not archive.is_relative_to(root / 'backups'):
        raise BackupError('backup inside the data volume must be placed in its backups directory')
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='vf-db-') as temporary:
        snapshot = Path(temporary) / DATABASE
        _database_snapshot(database, snapshot)
        entries = list(_entries(root))
        try:
            fd = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as error:
            raise BackupError('backup already exists; choose a new filename') from error
        try:
            # Data files may share an inode. Store each as a regular file so a
            # valid backup never relies on hard-link extraction during restore.
            with os.fdopen(fd, 'wb') as stream, tarfile.open(fileobj=stream, mode='w:gz', dereference=True) as tar:
                for path, relative in entries:
                    tar.add(snapshot if relative.as_posix() == DATABASE else path,
                            arcname=relative.as_posix(), recursive=False)
        except BaseException:
            archive.unlink(missing_ok=True)
            raise


def _members(tar: tarfile.TarFile):
    seen = set()
    members = tar.getmembers()
    for member in members:
        name = member.name
        path = PurePosixPath(name)
        if (not name or '\\' in name or name.startswith('/') or
                any(part in {'', '.', '..'} for part in name.split('/')) or
                path.parts[0] in EXCLUDED_ROOTS or path.parts[0].startswith('.restore-') or
                ':' in path.parts[0]):
            raise BackupError(f'unsafe archive path: {name!r}')
        if name in seen:
            raise BackupError(f'duplicate archive path: {name!r}')
        seen.add(name)
        if not (member.isfile() or member.isdir()):
            raise BackupError(f'archive contains a link or special file: {name!r}')
    if DATABASE not in seen or SECRET not in seen:
        raise BackupError('archive must include both the SQLite database and its master key')
    return members


def _assert_empty(root: Path):
    # Docker initializes the volume with empty media/.secrets directories.
    for entry in root.iterdir():
        if entry.is_symlink() or not entry.is_dir():
            raise BackupError('restore target must be an empty volume')
        for _, _, files in os.walk(entry, followlinks=False):
            if files:
                raise BackupError('restore target must be an empty volume')
        for directory, names, _ in os.walk(entry, followlinks=False):
            if any((Path(directory) / name).is_symlink() for name in names):
                raise BackupError('restore target must not contain symlinks')


def restore(root: Path, archive: Path):
    root = _root(root)
    if archive.is_symlink() or not archive.is_file():
        raise BackupError('backup archive must be an existing regular file')
    archive = archive.resolve()
    if archive.is_relative_to(root):
        raise BackupError('put the backup archive outside the target volume before restore')
    _assert_empty(root)
    # Stage on the target filesystem: /tmp in the container is a small tmpfs.
    with tempfile.TemporaryDirectory(prefix='.restore-', dir=root) as temporary:
        stage = Path(temporary)
        with tarfile.open(archive, mode='r:gz') as tar:
            members = _members(tar)
            for member in members:
                target = stage.joinpath(*PurePosixPath(member.name).parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True, mode=0o700)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                src = tar.extractfile(member)
                if src is None:
                    raise BackupError('archive file is unreadable')
                with src, target.open('xb') as dst:
                    shutil.copyfileobj(src, dst, length=1024 * 1024)
                os.chmod(target, 0o600)
        try:
            conn = sqlite3.connect((stage / DATABASE).as_uri() + '?mode=ro', uri=True)
            try:
                check = conn.execute('PRAGMA quick_check').fetchone()
                if not check or check[0] != 'ok':
                    raise BackupError('backup SQLite integrity check failed')
            finally:
                conn.close()
        except sqlite3.Error as error:
            raise BackupError('backup does not contain a valid SQLite database') from error
        if not (stage / SECRET).is_file():
            raise BackupError('backup master key missing or not a file')
        # No target file is overwritten. An error rolls back newly created files.
        created = []
        try:
            for source in sorted(stage.rglob('*')):
                relative = source.relative_to(stage)
                target = root / relative
                if source.is_dir():
                    target.mkdir(parents=True, exist_ok=True, mode=0o700)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    if target.exists() or target.is_symlink():
                        raise BackupError(f'restore would overwrite {relative}')
                    source.replace(target)
                    created.append(target)
            os.chmod(root / '.secrets', 0o700)
            os.chmod(root / SECRET, 0o600)
        except BaseException:
            for item in reversed(created):
                item.unlink(missing_ok=True)
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description='Offline backup / empty-volume restore; archives contain private credentials.')
    parser.add_argument('operation', choices=['backup', 'restore'])
    parser.add_argument('archive', type=Path)
    parser.add_argument('--data-root', type=Path, default=DATA_ROOT, help='mounted data volume (default /app/data)')
    args = parser.parse_args(argv)
    try:
        (backup if args.operation == 'backup' else restore)(args.data_root, args.archive)
    except (BackupError, OSError, sqlite3.Error, tarfile.TarError) as error:
        print(f'Backup/restore failed: {error}', file=sys.stderr)
        return 1
    print(f'{args.operation} complete: {args.archive}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
