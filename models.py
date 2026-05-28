"""同期状態のデータモデル。
スレッドセーフな SyncMap, SyncState, ThreadMetaTracker を提供。"""

import json
import os
import threading
from typing import Optional

SYNC_MAP_PATH = os.path.expanduser(
    "~/.hermes/plugins/kanban-forum-sync/sync_map.json"
)
THREAD_META_PATH = os.path.expanduser(
    "~/.hermes/plugins/kanban-forum-sync/thread_meta.json"
)


class SyncMap:
    """kanban_task_id (str) → discord_thread_id (int) の永続マッピング"""

    def __init__(self, path: str = SYNC_MAP_PATH):
        self._path = path
        self._lock = threading.RLock()
        self._data: dict[str, int] = {}
        self._load()

    def _load(self):
        if os.path.exists(self._path):
            with open(self._path) as f:
                self._data = json.load(f)

    def _save(self):
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        with open(self._path, "w") as f:
            json.dump(self._data, f, indent=2)

    def get(self, kanban_id: str) -> Optional[int]:
        with self._lock:
            return self._data.get(kanban_id)

    def set(self, kanban_id: str, discord_id: int):
        with self._lock:
            self._data[kanban_id] = discord_id
            self._save()

    def remove(self, kanban_id: str):
        with self._lock:
            self._data.pop(kanban_id, None)
            self._save()

    def clear(self):
        with self._lock:
            self._data.clear()
            self._save()

    def items(self) -> dict[str, int]:
        with self._lock:
            return dict(self._data)


class SyncState:
    """同期エンジンの実行状態"""

    def __init__(self):
        self.state: str = "stopped"  # stopped | running | error
        self.task_count: int = 0
        self.error_count: int = 0
        self.last_event_id: int = 0  # 最後に処理した task_events.id
        self.last_sync: Optional[str] = None
        self.last_error: Optional[str] = None
        self.comment_count: int = 0  # Phase 2: 同期したコメント数
        self.tag_sync_count: int = 0  # Phase 2: 同期したタグ変更数


class ThreadMetaTracker:
    """スレッドごとのメタデータ（last_message_id など）を永続化する。

    JSON 構造:
      {"<thread_id>": {"last_message_id": <int>}, ...}
    """

    def __init__(self, path: str = THREAD_META_PATH):
        self._path = path
        self._lock = threading.RLock()
        self._data: dict[str, dict] = {}
        self._load()

    def _load(self):
        if os.path.exists(self._path):
            with open(self._path) as f:
                self._data = json.load(f)

    def _save(self):
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        with open(self._path, "w") as f:
            json.dump(self._data, f, indent=2, sort_keys=True)

    def get_last_message_id(self, thread_id: int) -> int:
        """追跡しているスレッドの最後に処理したメッセージID"""
        with self._lock:
            return self._data.get(str(thread_id), {}).get("last_message_id", 0)

    def set_last_message_id(self, thread_id: int, message_id: int):
        """最後に処理したメッセージIDを記録"""
        with self._lock:
            key = str(thread_id)
            if key not in self._data:
                self._data[key] = {}
            self._data[key]["last_message_id"] = message_id
            self._save()

    def remove(self, thread_id: int):
        """スレッドのメタデータを削除（スレッド削除時など）"""
        with self._lock:
            self._data.pop(str(thread_id), None)
            self._save()

    def get_last_comment_id(self, thread_id: int) -> int:
        """Kanban→Discord で最後に投稿したコメントID"""
        with self._lock:
            return self._data.get(str(thread_id), {}).get("last_comment_id", 0)

    def set_last_comment_id(self, thread_id: int, comment_id: int):
        with self._lock:
            key = str(thread_id)
            if key not in self._data:
                self._data[key] = {}
            self._data[key]["last_comment_id"] = comment_id
            self._save()

    def get_last_kanban_event_id(self, thread_id: int) -> int:
        """Kanban→Discord で最後に投稿したイベントID"""
        with self._lock:
            return self._data.get(str(thread_id), {}).get("last_kanban_event_id", 0)

    def set_last_kanban_event_id(self, thread_id: int, event_id: int):
        with self._lock:
            key = str(thread_id)
            if key not in self._data:
                self._data[key] = {}
            self._data[key]["last_kanban_event_id"] = event_id
            self._save()

    def keys(self) -> list[int]:
        """全追跡スレッドID"""
        with self._lock:
            return [int(k) for k in self._data]
