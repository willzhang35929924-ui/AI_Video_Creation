"""Meaningful safety/integrity tests; no Docker daemon or model is invoked."""
from __future__ import annotations

from contextlib import closing
import io
import os
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest

import volume_archive as archive


class VolumeArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = self.make_roots("source")
        self.target = self.make_roots("target")
        with closing(sqlite3.connect(self.source["data"] / archive.DB_NAME)) as connection:
            connection.execute("CREATE TABLE generation_jobs (id TEXT, status TEXT)")
            connection.execute("INSERT INTO generation_jobs VALUES ('job-test', 'completed')")
            connection.commit()
        (self.source["data"] / ".secrets").mkdir()
        self.key = os.urandom(32)
        (self.source["data"] / ".secrets" / "master.key").write_bytes(self.key)
        (self.source["data"] / "media").mkdir()
        self.media_bytes = bytes(range(256)) + b"\x00\r\n\xff"
        (self.source["data"] / "media" / "binary-fixture.bin").write_bytes(self.media_bytes)
        (self.source["logs"] / "application.log").write_text("safe test fixture", encoding="utf-8")
        (self.source["reports"] / "report.txt").write_text("safe test report", encoding="utf-8")

    def make_roots(self, name):
        roots = {key: self.base / name / key for key in archive.ROOTS}
        for root in roots.values():
            root.mkdir(parents=True)
        return roots

    def backup_bytes(self):
        stream = io.BytesIO()
        archive.create_backup(self.source, stream)
        return stream.getvalue()

    def assert_target_untouched(self):
        self.assertFalse((self.target["data"] / archive.DB_NAME).exists())
        self.assertFalse((self.target["data"] / "media").exists())
        self.assertFalse((self.target["data"] / "RESTORE_INCOMPLETE").exists())

    def test_round_trip_preserves_database_key_binary_media_and_all_three_volumes(self):
        archive.check_idle(self.source)
        archive.restore_backup(self.target, io.BytesIO(self.backup_bytes()))
        self.assertEqual((self.target["data"] / ".secrets" / "master.key").read_bytes(), self.key)
        self.assertEqual((self.target["data"] / "media" / "binary-fixture.bin").read_bytes(), self.media_bytes)
        self.assertEqual((self.target["logs"] / "application.log").read_text(), "safe test fixture")
        self.assertTrue((self.target["reports"] / "report.txt").is_file())
        with closing(sqlite3.connect(self.target["data"] / archive.DB_NAME)) as connection:
            self.assertEqual(connection.execute("SELECT status FROM generation_jobs").fetchone()[0], "completed")

    def test_busy_jobs_refuse_backup_quiesce(self):
        with closing(sqlite3.connect(self.source["data"] / archive.DB_NAME)) as connection:
            connection.execute("UPDATE generation_jobs SET status='running'")
            connection.commit()
        with self.assertRaisesRegex(RuntimeError, "Active or queued work"):
            archive.check_idle(self.source)

    def test_existing_hardlinks_are_backed_up_as_independent_regular_files(self):
        original = self.source["data"] / "media" / "binary-fixture.bin"
        linked = self.source["data"] / "media" / "hardlinked.bin"
        os.link(original, linked)
        saved = self.backup_bytes()
        with tarfile.open(fileobj=io.BytesIO(saved), mode="r:gz") as contents:
            self.assertTrue(all(member.isfile() for member in contents))
        archive.restore_backup(self.target, io.BytesIO(saved))
        self.assertEqual((self.target["data"] / "media" / "hardlinked.bin").read_bytes(), self.media_bytes)

    def test_restore_never_overwrites_existing_files(self):
        existing = self.target["data"] / "important.txt"
        existing.write_text("keep")
        with self.assertRaisesRegex(RuntimeError, "refuses to overwrite"):
            archive.restore_backup(self.target, io.BytesIO(self.backup_bytes()))
        self.assertEqual(existing.read_text(), "keep")
        self.assert_target_untouched()

    def test_tampered_file_fails_before_any_target_file_is_written(self):
        changed = io.BytesIO()
        with tarfile.open(fileobj=io.BytesIO(self.backup_bytes()), mode="r:gz") as source, tarfile.open(fileobj=changed, mode="w:gz") as destination:
            for member in source:
                content = source.extractfile(member).read()
                if member.name.endswith("binary-fixture.bin"):
                    content = bytes([content[0] ^ 1]) + content[1:]
                destination.addfile(member, io.BytesIO(content))
        with self.assertRaisesRegex(RuntimeError, "checksums"):
            archive.restore_backup(self.target, io.BytesIO(changed.getvalue()))
        self.assert_target_untouched()

    def test_path_traversal_and_links_are_rejected(self):
        for name, kind in (("../escape", tarfile.REGTYPE), ("data/media/link", tarfile.SYMTYPE)):
            with self.subTest(name=name):
                malicious = io.BytesIO()
                with tarfile.open(fileobj=malicious, mode="w:gz") as target:
                    member = tarfile.TarInfo(name)
                    member.type = kind
                    member.linkname = "/etc/passwd" if kind == tarfile.SYMTYPE else ""
                    target.addfile(member, io.BytesIO(b""))
                with self.assertRaises(RuntimeError):
                    archive.restore_backup(self.target, io.BytesIO(malicious.getvalue()))
                self.assert_target_untouched()
                self.assertFalse((self.base / "escape").exists())

    def test_missing_master_key_is_not_reported_as_complete_backup(self):
        (self.source["data"] / ".secrets" / "master.key").unlink()
        with self.assertRaisesRegex(RuntimeError, "master key is missing"):
            self.backup_bytes()


if __name__ == "__main__":
    unittest.main()
