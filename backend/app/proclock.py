"""A lock shared by every process using the same database.

The scheduler's thread lock only covers one process. Two API workers, or the
server plus `python -m app.ingest.pipeline --once`, would each refresh -- and
pay for enrichment, scraping and Apify reads -- at the same moment. This lock
lives in a file beside the database, so it covers every process using it.

It is an OS byte-range lock, not a marker file: the OS drops it when the
holder exits, so a crashed refresh can never leave it stuck.
"""

from __future__ import annotations

import os
from pathlib import Path

from .config import settings

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class ProcessLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> bool:
        """Take the lock without waiting. False when another process holds it."""
        if self._fd is not None:
            raise RuntimeError("lock already held by this object")
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            if os.name == "nt":
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            if os.name == "nt":
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def refresh_lock() -> ProcessLock:
    """The lock every refresh takes: the scheduled one, "Refresh now", and the CLI."""
    return ProcessLock(settings.db_path.with_name(settings.db_path.name + ".refresh.lock"))
