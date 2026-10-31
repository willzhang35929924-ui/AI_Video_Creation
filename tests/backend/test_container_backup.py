import io
import os
from pathlib import Path
import sqlite3
import tarfile

import pytest

from app.container_backup import BackupError, backup, restore


def make_data(root):
    root.mkdir()
    (root / '.secrets').mkdir()
    (root / '.secrets' / 'master.key').write_bytes(b'secret-key\n')
    (root / 'media').mkdir()
    (root / 'media' / 'image.png').write_bytes(b'original media')
    (root / 'workflows').mkdir()
    (root / 'workflows' / 'user.json').write_bytes(b'{"workflow":"sample"}')
    (root / 'tmp').mkdir()
    (root / 'tmp' / 'omit').write_text('temporary')
    database = sqlite3.connect(root / 'video_factory.db')
    database.execute('CREATE TABLE records (value TEXT)')
    database.execute('INSERT INTO records VALUES (?)', ('example',))
    database.commit()
    database.close()


def test_roundtrip_preserves_database_media_workflow_and_key(tmp_path):
    source = tmp_path / 'source'
    make_data(source)
    archive = tmp_path / 'backup.tar.gz'
    backup(source, archive)
    target = tmp_path / 'target'
    target.mkdir()
    (target / 'media').mkdir()
    (target / '.secrets').mkdir()
    restore(target, archive)
    with sqlite3.connect(target / 'video_factory.db') as database:
        assert database.execute('SELECT value FROM records').fetchone() == ('example',)
    assert (target / 'media' / 'image.png').read_bytes() == b'original media'
    assert (target / 'workflows' / 'user.json').read_bytes() == b'{"workflow":"sample"}'
    assert (target / '.secrets' / 'master.key').read_bytes() == b'secret-key\n'
    assert not (target / 'tmp').exists()
    if os.name == 'posix':
        assert (target / '.secrets' / 'master.key').stat().st_mode & 0o777 == 0o600


def test_existing_backup_and_nonempty_target_are_not_overwritten(tmp_path):
    source = tmp_path / 'source'
    make_data(source)
    archive = tmp_path / 'backup.tar.gz'
    backup(source, archive)
    before = archive.read_bytes()
    with pytest.raises(BackupError, match='already exists'):
        backup(source, archive)
    assert archive.read_bytes() == before
    target = tmp_path / 'target'
    target.mkdir()
    (target / 'existing.txt').write_text('keep me')
    with pytest.raises(BackupError, match='empty volume'):
        restore(target, archive)
    assert (target / 'existing.txt').read_text() == 'keep me'


@pytest.mark.parametrize('malicious,kind', [('../escape', 'file'), ('/absolute', 'file'), ('media/link', 'symlink')])
def test_rejects_dangerous_archive_members_before_writing(tmp_path, malicious, kind):
    source = tmp_path / 'source'
    make_data(source)
    archive = tmp_path / 'backup.tar.gz'
    backup(source, archive)
    crafted = tmp_path / 'crafted.tar.gz'
    with tarfile.open(archive, 'r:gz') as original, tarfile.open(crafted, 'w:gz') as output:
        for member in original.getmembers():
            output.addfile(member, original.extractfile(member) if member.isfile() else None)
        member = tarfile.TarInfo(malicious)
        if kind == 'symlink':
            member.type = tarfile.SYMTYPE
            member.linkname = '../../outside'
            output.addfile(member)
        else:
            member.size = 1
            output.addfile(member, io.BytesIO(b'x'))
    target = tmp_path / 'target'
    target.mkdir()
    with pytest.raises(BackupError):
        restore(target, crafted)
    assert list(target.iterdir()) == []
    assert not (tmp_path / 'escape').exists()


def test_backup_captures_committed_wal_database(tmp_path):
    source = tmp_path / 'source'
    make_data(source)
    conn = sqlite3.connect(source / 'video_factory.db')
    try:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('INSERT INTO records VALUES (?)', ('in wal',))
        conn.commit()
        archive = tmp_path / 'backup.tar.gz'
        backup(source, archive)
        target = tmp_path / 'target'
        target.mkdir()
        restore(target, archive)
        with sqlite3.connect(target / 'video_factory.db') as restored:
            assert restored.execute('SELECT value FROM records ORDER BY rowid').fetchall() == [('example',), ('in wal',)]
    finally:
        conn.close()


def test_symlink_in_source_is_rejected(tmp_path):
    source = tmp_path / 'source'
    make_data(source)
    try:
        (source / 'media' / 'linked').symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip('symlink creation not available')
    archive = tmp_path / 'backup.tar.gz'
    with pytest.raises(BackupError, match='symlink'):
        backup(source, archive)
    assert not archive.exists()
