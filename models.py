"""
同期状態のデータモデル。
スレッドセーフな SyncMap と SyncState を提供。
"""

import json
import os
import threading
from typing import Optional

SYNC_MAP_PATH = os.path.expanduser(
    "~/.hermes/plugins/kanban-forum-sync/sync_map.json"
)


class SyncMap:
    """kanban_task_id → discord_thread_id の永続マッピング"""

    def __init__(self, path: str = SYNC_MAP_PATH):
        self._path = path
        self._lock = threading.RLock()
        self._data: dict[int, int] = {}
        self._load()

    def _load(self):
        if os.path.exists(self._path):
            with open(self._path) as f:
                raw = json.load(f)
                self._data = {int(k): v for k, v in raw.items()}

    def _save(self):
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        with open(self._path, "w") as f:
            json.dump(self._data, f, indent=2)

    def get(self, kanban_id: int) -> Optional[int]:
        with self._lock:
            return self._data.get(kanban_id)

    def set(self, kanban_id: int, discord_id: int):
        with self._lock:
            self._data[kanban_id] = discord_id
            self._save()

    def remove(self, kanban_id: int):
        with self._lock:
            self._data.pop(kanban_id, None)
            self._save()

    def items(self):
        with self._lock:
            return dict(self._data)


class SyncState:
    """同期エンジンの実行状態"""

    def __init__(self):
        self.state: str = "stopped"  # stopped | running | error
        self.task_count: int = 0
        self.error_count: int = 0
        self.last_sync: Optional[str] = None
        self.last_poll_time: Optional[str] = None
        self.last_error: Optional[str] = None
