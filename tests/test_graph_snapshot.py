import os
import shutil
import sqlite3
import tempfile
import unittest

import _path  # noqa: F401

import graph_snapshot
from graph_snapshot import CONTAINER_PROFILE, SnapshotError, Snapshotter


def make_store(case, stamp="hoo3", wal=True):
    """A kindex-shaped source store: a meta table and a nodes table.

    With wal=True a connection is left open with autocheckpoint off, so the
    newest rows live only in kindex.db-wal, which is the state a live host
    store is in. Returns (src_dir, conn); conn is None when wal=False.
    """
    src = tempfile.mkdtemp()
    case.addCleanup(shutil.rmtree, src, ignore_errors=True)
    db = os.path.join(src, "kindex.db")
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("CREATE TABLE nodes (title TEXT)")
    if stamp is not None:
        conn.execute("INSERT INTO meta VALUES ('kin_profile', ?)", (stamp,))
    conn.commit()
    if not wal:
        # A close() under journal_mode=WAL leaves the -wal file behind (it is
        # only truncated, not deleted) unless something forces a checkpoint
        # first. Switching back to DELETE mode does that, which is what a
        # fully checkpointed, WAL-free store on disk actually looks like.
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.close()
        return src, None
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("INSERT INTO nodes VALUES ('only-in-the-wal')")
    conn.commit()
    case.addCleanup(conn.close)
    return src, conn


def titles(dst):
    conn = sqlite3.connect(os.path.join(dst, "kindex.db"))
    try:
        return [r[0] for r in conn.execute("SELECT title FROM nodes ORDER BY rowid")]
    finally:
        conn.close()


def stamp_of(dst):
    conn = sqlite3.connect(os.path.join(dst, "kindex.db"))
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='kin_profile'").fetchone()
        return row[0] if row else None
    finally:
        conn.close()


class Harness:
    def __init__(self, case, src, **kw):
        self.root = tempfile.mkdtemp()
        case.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.dst = os.path.join(self.root, "kindex")
        self.opened = []
        self.copies = []

        def copy(a, b):
            self.copies.append(a)
            shutil.copyfile(a, b)

        kw.setdefault("opener", self.opened.append)
        kw.setdefault("copy", copy)
        self.snap = Snapshotter(src, self.dst, python="/nonexistent/python",
                                sleep=lambda s: None, **kw)


class RefreshTest(unittest.TestCase):
    def test_first_refresh_copies_the_wal_too(self):
        src, _ = make_store(self)
        h = Harness(self, src)
        self.assertTrue(h.snap.refresh())
        self.assertEqual(titles(h.dst), ["only-in-the-wal"])

    def test_the_copy_is_restamped_and_the_source_is_not(self):
        src, conn = make_store(self, stamp="hoo3")
        h = Harness(self, src)
        h.snap.refresh()
        self.assertEqual(stamp_of(h.dst), CONTAINER_PROFILE)
        row = conn.execute("SELECT value FROM meta WHERE key='kin_profile'").fetchone()
        self.assertEqual(row[0], "hoo3")

    def test_an_unstamped_store_is_stamped(self):
        # Review Focus 1: a legacy or repo-local store carries no stamp.
        src, _ = make_store(self, stamp=None)
        h = Harness(self, src)
        h.snap.refresh()
        self.assertEqual(stamp_of(h.dst), CONTAINER_PROFILE)

    def test_a_store_with_no_wal_file_copies(self):
        # Review Focus 2: a fully checkpointed store has no -wal at all.
        src, _ = make_store(self, wal=False)
        self.assertFalse(os.path.exists(os.path.join(src, "kindex.db-wal")))
        h = Harness(self, src)
        self.assertTrue(h.snap.refresh())
        self.assertEqual(titles(h.dst), [])

    def test_the_opener_sees_the_staged_copy_not_the_live_one(self):
        src, _ = make_store(self)
        h = Harness(self, src)
        h.snap.refresh()
        self.assertEqual(h.opened, [h.dst + ".staging"])

    def test_an_unchanged_source_is_not_copied_again(self):
        src, _ = make_store(self)
        h = Harness(self, src)
        h.snap.refresh()
        before = len(h.copies)
        self.assertFalse(h.snap.refresh())
        self.assertEqual(len(h.copies), before)

    def test_a_changed_source_is_copied_again(self):
        src, conn = make_store(self)
        h = Harness(self, src)
        h.snap.refresh()
        conn.execute("INSERT INTO nodes VALUES ('added-later')")
        conn.commit()
        self.assertTrue(h.snap.refresh())
        self.assertEqual(titles(h.dst), ["only-in-the-wal", "added-later"])

    def test_a_source_that_moves_during_one_copy_is_retried(self):
        src, conn = make_store(self)
        moved = []

        def racing_copy(a, b):
            shutil.copyfile(a, b)
            if not moved:
                moved.append(a)
                conn.execute("INSERT INTO nodes VALUES ('raced')")
                conn.commit()

        h = Harness(self, src, copy=racing_copy)
        self.assertTrue(h.snap.refresh())
        self.assertEqual(titles(h.dst), ["only-in-the-wal", "raced"])

    def test_a_source_that_never_settles_keeps_the_previous_snapshot(self):
        src, conn = make_store(self)
        h = Harness(self, src)
        h.snap.refresh()

        def always_racing(a, b):
            shutil.copyfile(a, b)
            conn.execute("INSERT INTO nodes VALUES ('churn')")
            conn.commit()

        h.snap._copy = always_racing
        conn.execute("INSERT INTO nodes VALUES ('trigger')")
        conn.commit()
        with self.assertRaisesRegex(SnapshotError, "kept changing"):
            h.snap.refresh()
        self.assertEqual(titles(h.dst), ["only-in-the-wal"])
        self.assertFalse(os.path.exists(h.dst + ".staging"))

    def test_a_corrupt_source_is_refused_and_leaves_dst_alone(self):
        src, conn = make_store(self)
        h = Harness(self, src)
        h.snap.refresh()
        conn.close()
        for name in ("kindex.db-wal", "kindex.db-shm"):
            try:
                os.unlink(os.path.join(src, name))
            except FileNotFoundError:
                pass
        with open(os.path.join(src, "kindex.db"), "wb") as fh:
            fh.write(b"this is not a sqlite database" * 200)
        with self.assertRaisesRegex(SnapshotError, "not a readable kindex store"):
            h.snap.refresh()
        self.assertEqual(titles(h.dst), ["only-in-the-wal"])

    def test_an_opener_refusal_leaves_dst_alone(self):
        # Review Focus 5's lower half: the image's kindex refusing a newer
        # schema must not replace a snapshot that works.
        src, conn = make_store(self)
        h = Harness(self, src)
        h.snap.refresh()
        conn.execute("INSERT INTO nodes VALUES ('newer')")
        conn.commit()

        def refuse(path):
            raise SnapshotError("kindex refused the copy: schema 16 is newer than 15")

        h.snap._opener = refuse
        with self.assertRaisesRegex(SnapshotError, "schema 16"):
            h.snap.refresh()
        self.assertEqual(titles(h.dst), ["only-in-the-wal"])
        self.assertFalse(os.path.exists(h.dst + ".staging"))

    def test_a_missing_source_is_an_error(self):
        h = Harness(self, tempfile.mkdtemp())
        with self.assertRaisesRegex(SnapshotError, "kindex.db"):
            h.snap.refresh()


class WarmUpTest(unittest.TestCase):
    def test_a_zero_exit_is_accepted(self):
        graph_snapshot.warm_up("/usr/bin/true", tempfile.gettempdir())

    def test_a_non_zero_exit_names_the_last_stderr_line(self):
        script = tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False)
        script.write("#!/bin/sh\necho noise >&2\necho 'schema 16 is newer' >&2\nexit 1\n")
        script.close()
        os.chmod(script.name, 0o755)
        self.addCleanup(os.unlink, script.name)
        with self.assertRaisesRegex(SnapshotError, "schema 16 is newer"):
            graph_snapshot.warm_up(script.name, tempfile.gettempdir())

    def test_an_unrunnable_python_is_an_error(self):
        with self.assertRaisesRegex(SnapshotError, "could not run kindex"):
            graph_snapshot.warm_up("/nonexistent/python", tempfile.gettempdir())


if __name__ == "__main__":
    unittest.main()
