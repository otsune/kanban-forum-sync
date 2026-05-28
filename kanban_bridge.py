"""
Kanban SQLite DB の読み取り・変更検出用モジュール。
kanban_db.py の内部テーブルに直接アクセスする。
"""

import sqlite3
import os
import logging
from datetime import datetime
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
                "SELECT id, title, status, priority, assignee, "
                "kanban_card_id, created_at, updated_at FROM tasks"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_updated_since(self, timestamp: str) -> list[dict]:
        """指定時刻以降に更新されたタスクを取得"""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, title, status, priority, assignee, "
                "kanban_card_id, created_at, updated_at FROM tasks "
                "WHERE updated_at > ? ORDER BY updated_at ASC",
                (timestamp,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_task(self, task_id: int) -> Optional[dict]:
        """特定タスクを取得"""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT id, title, status, priority, assignee, "
                "kanban_card_id, created_at, updated_at FROM tasks WHERE id = ?",
                (task_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_comments_since(self, task_id: int, timestamp: str) -> list[dict]:
        """タスクの指定時刻以降のコメントを取得"""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, author, body, created_at FROM comments "
                "WHERE task_id = ? AND created_at > ? ORDER BY created_at ASC",
                (task_id, timestamp)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def update_task_status(self, task_id: int, new_status: str) -> bool:
        """タスクのステータスを更新（Phase 2 フィードバック用）"""
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (new_status, datetime.utcnow().isoformat(), task_id)
            )
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to update task {task_id}: {e}")
            return False
        finally:
            conn.close()
