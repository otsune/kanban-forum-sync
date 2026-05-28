"""inotify-based watcher for kanban.db changes (Linux only).

Falls back to interval-only behaviour when inotify is unavailable
(non-Linux, Docker without inotify, DB path not found, etc.).
"""
import ctypes
import ctypes.util
import logging
import os
import select
from typing import Optional

logger = logging.getLogger(__name__)

_IN_MODIFY = 0x00000002
_IN_CLOSE_WRITE = 0x00000008
_WATCH_MASK = _IN_MODIFY | _IN_CLOSE_WRITE


class KanbanDBWatcher:
    """Watch kanban.db and its WAL file via inotify.

    Usage::

        with KanbanDBWatcher(db_path) as watcher:
            while not stop_event.is_set():
                watcher.wait(timeout=poll_interval)
                do_sync()
    """

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._ifd: Optional[int] = None
        self.available = False

    def __enter__(self) -> "KanbanDBWatcher":
        self.available = self._init_inotify()
        if not self.available:
            logger.warning(
                "inotify unavailable; event-driven mode falls back "
                "to interval-based sync (poll_interval as timeout)"
            )
        return self

    def __exit__(self, *_) -> None:
        if self._ifd is not None:
            try:
                os.close(self._ifd)
            except OSError:
                pass
            self._ifd = None

    def _init_inotify(self) -> bool:
        try:
            lib = ctypes.util.find_library("c")
            if not lib:
                return False
            libc = ctypes.CDLL(lib, use_errno=True)
            ifd = libc.inotify_init1(0)
            if ifd < 0:
                return False
            self._ifd = ifd
            libc.inotify_add_watch(ifd, self._db_path.encode(), _WATCH_MASK)
            wal = self._db_path + "-wal"
            if os.path.exists(wal):
                libc.inotify_add_watch(ifd, wal.encode(), _WATCH_MASK)
            return True
        except Exception as exc:
            logger.debug("inotify init failed: %s", exc)
            return False

    def wait(self, timeout: float = 30.0) -> bool:
        """Block until a DB change is detected or *timeout* seconds elapse.

        Returns True when a change was detected, False on timeout.
        When inotify is unavailable, sleeps for *timeout* and returns True
        so the caller always runs a sync cycle.
        """
        if not self.available or self._ifd is None:
            import time
            time.sleep(timeout)
            return True

        rlist, _, _ = select.select([self._ifd], [], [], timeout)
        if rlist:
            try:
                os.read(self._ifd, 4096)  # drain the event queue
            except OSError:
                pass
            return True
        return False
