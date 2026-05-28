"""
Kanban ↔ Discord Forum 同期のコアロジック。
ポーリングループで変更を検出し、適切な API 操作を実行する。
"""

import time
import logging
from threading import Thread, Event
from datetime import datetime, timezone
from typing import Optional

from .discord_forum import DiscordForumClient
from .kanban_bridge import KanbanBridge
from .models import SyncState, SyncMap

logger = logging.getLogger(__name__)

# Kanban status → Discord tag name mapping
STATUS_TO_TAG = {
    "triage": "Triage",
    "todo": "Todo",
    "scheduled": "Scheduled",
    "ready": "Ready",
    "running": "Running",
    "blocked": "Blocked",
    "review": "Review",
    "done": "Done",
    "archived": "Done",
}

# Statuses that trigger thread archiving
ARCHIVE_STATUSES = {"done", "archived"}


class KanbanForumSyncer:
    """Kanban ↔ Discord Forum 同期エンジン"""

    def __init__(self, bot_token: str, channel_id: int, poll_interval: int = 15):
        self.discord = DiscordForumClient(bot_token, channel_id)
        self.kanban = KanbanBridge()
        self.poll_interval = poll_interval
        self._thread: Optional[Thread] = None
        self._stop_event = Event()
        self._state = SyncState()

        # {kanban_task_id → discord_thread_id}
        self._sync_map = SyncMap()

        # {tag_name → tag_id}
        self._tag_map: dict[str, int] = {}

    def get_status(self) -> SyncState:
        return self._state

    # ---- Tag management ----

    def _build_tag_map(self):
        """Forum のタグ一覧を取得し、{tag_name → tag_id} マップを構築"""
        tags = self.discord.get_tags()
        self._tag_map = {t["name"]: t["id"] for t in tags}
        logger.info(f"Tag map built with {len(self._tag_map)} tags")

    def _resolve_tag_ids(self, status: str) -> list[int]:
        """ステータスに対応する tag_id のリストを返す"""
        tag_name = STATUS_TO_TAG.get(status)
        if tag_name and tag_name in self._tag_map:
            return [self._tag_map[tag_name]]
        return []

    # ---- Thread content generation ----

    def _thread_name(self, task: dict) -> str:
        """タスクからスレッド名を生成"""
        return f"task-{task['id']}: {task['title']}"

    def _thread_content(self, task: dict) -> str:
        """タスクから初期スレッドメッセージを生成"""
        lines = [
            f"**{task['title']}**",
            f"Status: **{task['status']}**",
            f"Priority: {task.get('priority', '—')}",
        ]
        if task.get("assignee"):
            lines.append(f"Assignee: {task['assignee']}")
        if task.get("kanban_card_id"):
            lines.append(f"Card: {task['kanban_card_id']}")
        return "\n".join(lines)

    # ---- Sync logic ----

    def _sync_task_to_forum(self, task: dict) -> bool:
        """1件のタスクを Forum に同期する"""
        task_id = task["id"]
        thread_id = self._sync_map.get(task_id)

        try:
            if thread_id is None:
                # 新規タスク → スレッド作成
                tag_ids = self._resolve_tag_ids(task["status"])
                result = self.discord.create_thread(
                    name=self._thread_name(task),
                    content=self._thread_content(task),
                    tag_ids=tag_ids,
                )
                new_thread_id = result.get("id")
                if new_thread_id:
                    self._sync_map.set(task_id, new_thread_id)
                    logger.info(f"Created thread for task-{task_id}: {new_thread_id}")
                    return True
                logger.error(f"create_thread returned no id for task-{task_id}")
                return False
            else:
                # 既存タスク → 差分更新
                updates = {}
                updates["name"] = self._thread_name(task)

                tag_ids = self._resolve_tag_ids(task["status"])
                if tag_ids:
                    updates["applied_tags"] = tag_ids

                if task["status"] in ARCHIVE_STATUSES:
                    updates["archived"] = True
                    updates["locked"] = False

                if updates:
                    self.discord.update_thread(thread_id, **updates)
                    logger.info(f"Updated thread for task-{task_id}")
                return True
        except Exception as e:
            logger.error(f"Failed to sync task-{task_id}: {e}")
            return False

    def initial_sync(self):
        """初回フル同期"""
        logger.info("Starting initial sync...")
        self._build_tag_map()

        tasks = self.kanban.get_all_tasks()
        synced = 0
        errors = 0

        for task in tasks:
            if self._sync_task_to_forum(task):
                synced += 1
            else:
                errors += 1
            # レート制限対策: 1秒あたり1件
            time.sleep(1)

        self._state.last_sync = datetime.now(timezone.utc).isoformat()
        self._state.task_count = synced
        self._state.error_count = errors
        logger.info(f"Initial sync complete: {synced} synced, {errors} errors")

    def incremental_sync(self):
        """増分同期（1回のポーリング）"""
        if not self._state.last_poll_time:
            self._state.last_poll_time = datetime.now(timezone.utc).isoformat()
            return

        last_poll = self._state.last_poll_time
        now = datetime.now(timezone.utc).isoformat()

        updated = self.kanban.get_updated_since(last_poll)
        if updated:
            logger.info(f"Incremental sync: {len(updated)} changed tasks")
            for task in updated:
                self._sync_task_to_forum(task)

        self._state.last_poll_time = now

    # ---- Thread lifecycle ----

    def start(self):
        """バックグラウンドスレッドでポーリングループを開始"""
        if self._thread and self._thread.is_alive():
            logger.warning("Syncer already running")
            return

        self._stop_event.clear()
        self._thread = Thread(
            target=self._run_loop, daemon=True, name="kanban-forum-sync"
        )
        self._thread.start()
        self._state.state = "running"
        logger.info(f"Syncer started (poll interval: {self.poll_interval}s)")

    def stop(self):
        """ポーリングループを停止"""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
        self._state.state = "stopped"
        logger.info("Syncer stopped")

    def _run_loop(self):
        """ポーリングループ本体"""
        try:
            self.initial_sync()
        except Exception as e:
            logger.error(f"Initial sync failed: {e}")
            self._state.state = "error"
            self._state.last_error = str(e)
            return

        self._state.last_poll_time = datetime.now(timezone.utc).isoformat()
        while not self._stop_event.is_set():
            try:
                self.incremental_sync()
            except Exception as e:
                logger.error(f"Incremental sync failed: {e}")
                self._state.last_error = str(e)
            self._stop_event.wait(self.poll_interval)

    def full_sync(self):
        """手動フル同期（CLI/スラッシュコマンド用）"""
        self._build_tag_map()
        self.initial_sync()
