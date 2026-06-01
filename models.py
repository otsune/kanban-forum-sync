"""同期状態のデータモデル。
スレッドセーフな SyncMap, SyncState, ThreadMetaTracker を提供。"""

import json
import os
import shutil
import threading
import time
import fcntl
from typing import Optional

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
SYNC_MAP_PATH = os.path.join(_PLUGIN_DIR, "sync_map.json")
THREAD_META_PATH = os.path.join(_PLUGIN_DIR, "thread_meta.json")


def _load_json_dict(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        backup = f"{path}.corrupt.{int(time.time())}"
        try:
            shutil.move(path, backup)
        except OSError:
            pass
        return {}


def _atomic_save_json(path: str, data: dict, **dump_kwargs) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp_path = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
    lock_path = f"{path}.lock"
    with open(lock_path, "a") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            with open(tmp_path, "w") as f:
                json.dump(data, f, **dump_kwargs)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


class SyncMap:
    """kanban_task_id (str) → discord_thread_id (int) の永続マッピング"""

    def __init__(self, path: str = SYNC_MAP_PATH):
        self._path = path
        self._lock = threading.RLock()
        self._data: dict[str, int] = {}
        self._load()

    def _load(self):
        self._data = _load_json_dict(self._path)

    def _save(self):
        _atomic_save_json(self._path, self._data, indent=2)

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

    def contains_thread(self, thread_id: int) -> bool:
        """この thread_id がすでに同期マップに存在するかを確認。

        Forum → Kanban の新規スレッド検出で使用。
        """
        with self._lock:
            return thread_id in self._data.values()

    def get_by_thread_id(self, thread_id: int) -> Optional[str]:
        """thread_id に対応する Kanban task_id を逆引きする。"""
        with self._lock:
            for task_id, tid in self._data.items():
                if tid == thread_id:
                    return task_id
        return None


class SyncOriginTracker:
    """タスクの起源を追跡。

    - ``kanban``: Kanban 側で作成されたタスク（通常の Phase 1 → Forum スレッド作成）
    - ``forum``: Forum スレッドから自動作成されたタスク（Phase 3）

    永続化: sync_map.json と同じディレクトリに origin_map.json として保存。
    """

    ORIGIN_PATH = os.path.join(_PLUGIN_DIR, "origin_map.json")

    def __init__(self, path: str = ORIGIN_PATH):
        self._path = path
        self._lock = threading.RLock()
        self._data: dict[str, str] = {}  # task_id → "kanban" | "forum"
        self._load()

    def _load(self):
        self._data = _load_json_dict(self._path)

    def _save(self):
        _atomic_save_json(self._path, self._data, indent=2)

    def set_origin(self, task_id: str, origin: str):
        with self._lock:
            self._data[task_id] = origin
            self._save()

    def get_origin(self, task_id: str) -> str:
        with self._lock:
            return self._data.get(task_id, "kanban")

    def is_forum_sourced(self, task_id: str) -> bool:
        return self.get_origin(task_id) == "forum"

    def clear(self):
        with self._lock:
            self._data.clear()
            self._save()


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
        self.forum_task_count: int = 0  # Phase 3: Forum → Kanban で作成したタスク数


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
        self._data = _load_json_dict(self._path)

    def _save(self):
        _atomic_save_json(self._path, self._data, indent=2, sort_keys=True)

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

    def clear(self):
        """全メタデータを破棄（Forum チャンネル削除からの復旧時など）"""
        with self._lock:
            self._data.clear()
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

    def get_worker_log_count(self, thread_id: int) -> int:
        """既に Discord に投稿したワーカーログ発話ブロックの件数。"""
        with self._lock:
            return self._data.get(str(thread_id), {}).get("worker_log_count", 0)

    def set_worker_log_count(self, thread_id: int, count: int):
        with self._lock:
            key = str(thread_id)
            if key not in self._data:
                self._data[key] = {}
            self._data[key]["worker_log_count"] = count
            self._save()

    def keys(self) -> list[int]:
        """全追跡スレッドID"""
        with self._lock:
            return [int(k) for k in self._data]
