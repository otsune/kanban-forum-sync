"""Kanban SQLite DB の読み取り・変更検出用モジュール。
実DBスキーマ（task_events ベースの変更検出）に合わせた実装。"""

import sqlite3
import os
import logging
from typing import Optional

logger = logging.getLogger(__name__)

KANBAN_DB_PATH = os.path.expanduser("~/.hermes/kanban.db")


class KanbanBridge:
    """Kanban DB への読み取り専用ブリッジ"""

    def __init__(self, db_path: str = KANBAN_DB_PATH):
        self.db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def get_all_tasks(self) -> list[dict]:
        """全タスクを取得（初期同期用）"""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, title, body, status, priority, assignee, "
                "created_at, completed_at FROM tasks"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_tasks_changed_since_event(self, last_event_id: int) -> list[dict]:
        """指定イベントID以降に変更があった全タスクを取得。
        重複排除のためDISTINCT + latest event で1タスク1行。"""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT DISTINCT t.id, t.title, t.body, t.status, t.priority, "
                "t.assignee, t.created_at, t.completed_at "
                "FROM tasks t "
                "JOIN task_events e ON t.id = e.task_id "
                "WHERE e.id > ? "
                "ORDER BY e.id ASC",
                (last_event_id,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_task(self, task_id: str) -> Optional[dict]:
        """特定タスクを取得"""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT id, title, body, status, priority, assignee, "
                "created_at, completed_at FROM tasks WHERE id = ?",
                (task_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_latest_event_id(self) -> int:
        """最新の task_events.id を取得（0の場合はテーブル空）"""
        conn = self._connect()
        try:
            row = conn.execute("SELECT MAX(id) as max_id FROM task_events").fetchone()
            return row["max_id"] or 0
        finally:
            conn.close()

    def get_comments_since(self, task_id: str, event_id: int) -> list[dict]:
        """タスクのコメントを取得（task_comments から）"""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, author, body, created_at FROM task_comments "
                "WHERE task_id = ? AND id > ? ORDER BY id ASC",
                (task_id, event_id)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def update_task_status(self, task_id: str, new_status: str) -> bool:
        """タスクのステータスを更新（Phase 2 フィードバック用）"""
        import time
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE tasks SET status = ? WHERE id = ?",
                (new_status, task_id)
            )
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to update task {task_id}: {e}")
            return False
        finally:
            conn.close()
