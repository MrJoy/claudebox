"""A private copy of the host's kindex store, refreshed between cycles.

The launcher mounts the data dir kindex resolves for the reviewed repo at
KINDEX_SRC, read-only. It is never opened in place: kindex opens every store
read-write, forces WAL and may migrate it, and a live WAL database read
through a Docker Desktop bind mount is unsafe even for readers, because WAL
coordinates through the memory-mapped -shm index and that does not cross the
VM. So the supervisor copies kindex.db and kindex.db-wal into KINDEX_DATA_DIR
and kin-mcp runs against the copy. Nothing a reviewer does reaches the host
graph.

A copy is accepted only when neither source file's size or mtime moved while
it was being taken. A checkpoint on the host rewrites kindex.db, so a copy
that raced one is retried rather than passed on torn. SQLite rebuilds the -shm
index from the copied WAL when the copy is first opened, locally.

The copy is restamped to CONTAINER_PROFILE before kindex sees it. kindex
stamps a store with the profile that created it and refuses to open it under
any other name, and the container uses one fixed profile whatever the host's
was called. Then the image's own kindex opens it once (warm_up), which
migrates an older schema in the copy and refuses a newer one -- the host
upgraded kindex and the image did not -- before anything is swapped in.

Stdlib only: kindex lives in its own venv and is reached by subprocess.
"""

import os
import shutil
import sqlite3
import subprocess
import time
from typing import Callable, Optional, Tuple

CONTAINER_PROFILE = "claudebox"
DB = "kindex.db"
WAL = "kindex.db-wal"

Stat = Optional[Tuple[int, int]]
Fingerprint = Tuple[Stat, Stat]


class SnapshotError(Exception):
    """The snapshot could not be taken or refreshed. The message says why."""


def _stat(path: str) -> Stat:
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SnapshotError(f"could not stat {path}: {exc}")
    return (st.st_size, st.st_mtime_ns)


def fingerprint(src: str) -> Fingerprint:
    """(size, mtime_ns) of the source DB and its WAL, None for a missing file."""
    return (_stat(os.path.join(src, DB)), _stat(os.path.join(src, WAL)))


# Run by the image's kindex, never imported here. An explicit profile and
# data_dir, so neither the cwd's .kin/config nor a profile root can pick a
# different store.
_WARM_UP = (
    "import sys\n"
    "from kindex.config import load_config\n"
    "from kindex.store import Store\n"
    "Store(load_config(profile=sys.argv[1], data_dir=sys.argv[2])).conn\n"
)


def warm_up(python: str, data_dir: str) -> None:
    """Open the copy once with the image's kindex, or raise SnapshotError."""
    try:
        done = subprocess.run(
            [python, "-c", _WARM_UP, CONTAINER_PROFILE, data_dir],
            cwd=data_dir, capture_output=True, text=True, timeout=120, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SnapshotError(f"could not run kindex to open the copy: {exc}")
    if done.returncode != 0:
        lines = (done.stderr or "").strip().splitlines() or ["(no output)"]
        raise SnapshotError(f"kindex refused the copy: {lines[-1]}")


def _check_and_restamp(db: str) -> None:
    try:
        conn = sqlite3.connect(db)
        try:
            row = conn.execute("PRAGMA quick_check").fetchone()
            if not row or row[0] != "ok":
                raise SnapshotError(
                    f"the copy failed quick_check: {row[0] if row else 'no result'}")
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('kin_profile', ?)",
                (CONTAINER_PROFILE,),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        raise SnapshotError(f"the copy is not a readable kindex store: {exc}")


class Snapshotter:
    def __init__(
        self,
        src: str,
        dst: str,
        python: str,
        attempts: int = 5,
        pause: float = 0.5,
        copy: Callable[[str, str], object] = shutil.copyfile,
        sleep: Callable[[float], None] = time.sleep,
        opener: Optional[Callable[[str], None]] = None,
    ):
        self.src = src
        self.dst = dst
        self.attempts = attempts
        self.pause = pause
        self._copy = copy
        self._sleep = sleep
        self._opener = opener or (lambda path: warm_up(python, path))
        self.current: Optional[Fingerprint] = None

    def refresh(self) -> bool:
        """Swap in a fresh snapshot if the source changed. True if one was."""
        before = fingerprint(self.src)
        if before[0] is None:
            raise SnapshotError(f"{os.path.join(self.src, DB)} is missing")
        if before == self.current:
            return False

        staging = self.dst + ".staging"
        try:
            taken = self._take(staging, before)
            _check_and_restamp(os.path.join(staging, DB))
            self._opener(staging)
            self._swap(staging)
        except SnapshotError:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        except OSError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            raise SnapshotError(f"could not stage the copy: {exc}")
        self.current = taken
        return True

    def _take(self, staging: str, before: Fingerprint) -> Fingerprint:
        for attempt in range(self.attempts):
            if attempt:
                self._sleep(self.pause)
                before = fingerprint(self.src)
                if before[0] is None:
                    raise SnapshotError(f"{os.path.join(self.src, DB)} is missing")
            shutil.rmtree(staging, ignore_errors=True)
            os.makedirs(staging)
            try:
                self._copy(os.path.join(self.src, DB), os.path.join(staging, DB))
                if before[1] is not None:
                    self._copy(os.path.join(self.src, WAL), os.path.join(staging, WAL))
            except FileNotFoundError:
                # A checkpoint removed the WAL between the stat and the copy.
                continue
            if fingerprint(self.src) == before:
                return before
        raise SnapshotError(
            f"{self.src} kept changing during {self.attempts} copy attempts")

    def _swap(self, staging: str) -> None:
        old = self.dst + ".old"
        shutil.rmtree(old, ignore_errors=True)
        if os.path.exists(self.dst):
            os.rename(self.dst, old)
        os.rename(staging, self.dst)
        shutil.rmtree(old, ignore_errors=True)
