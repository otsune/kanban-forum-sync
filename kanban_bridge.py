"""Kanban SQLite DB の読み取り・書き込みモジュール。
実DBスキーマ（task_events ベースの変更検出）に合わせた実装。"""

import sqlite3
import os
import json
import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)

KANBAN_DB_PATH = os.path.expanduser("~/.hermes/kanban.db")

TASK_COLS = "id, title, body, status, priority, assignee, created_at, completed_at"


class KanbanBridge:
    """Kanban DB への読み書きブリッジ"""

    def __init__(self, db_path: str = KANBAN_DB_PATH):
        self.db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ---- 読み取り ----

    def get_all_tasks(self) -> list[dict]:
        """全タスクを取得（初期同期用）"""
        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT {TASK_COLS} FROM tasks"
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
                "SELECT DISTINCT t.id, t.title, t.body, t.status, "
                "t.priority, t.assignee, t.created_at, t.completed_at "
                "FROM tasks t "
                "JOIN task_events e ON t.id = e.task_id "
                "WHERE e.id > ? "
                "ORDER BY e.id ASC",
                (last_event_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_task(self, task_id: str) -> Optional[dict]:
        """特定タスクを取得"""
        conn = self._connect()
        try:
            row = conn.execute(
                f"SELECT {TASK_COLS} FROM tasks WHERE id = ?",
                (task_id,),
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

    def get_comments_since(self, task_id: str, last_id: int) -> list[dict]:
        """タスクのコメントを取得"""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, author, body, created_at FROM task_comments "
                "WHERE task_id = ? AND id > ? ORDER BY id ASC",
                (task_id, last_id),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_events_since(self, task_id: str, last_id: int,
                         kinds: list[str] = None) -> list[dict]:
        """タスクのイベントを取得（ワーカーログ用）"""
        conn = self._connect()
        try:
            if kinds:
                placeholders = ",".join("?" * len(kinds))
                rows = conn.execute(
                    f"SELECT id, kind, payload, created_at FROM task_events "
                    f"WHERE task_id = ? AND id > ? AND kind IN ({placeholders}) "
                    f"ORDER BY id ASC",
                    (task_id, last_id, *kinds),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, kind, payload, created_at FROM task_events "
                    "WHERE task_id = ? AND id > ? ORDER BY id ASC",
                    (task_id, last_id),
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ---- 書き込み（Phase 2 + Phase 3: 新規タスク作成含む） ----

    def create_task(self, title: str, body: str = "",
                    status: str = "triage",
                    assignee: Optional[str] = None) -> Optional[str]:
        """Forum スレッドから新規 Kanban タスクを作成。

        Returns the new task id, or None on failure.
        ステータスデフォルトは ``triage`` — 確認・割り振りが必要な状態。
        """
        if not title or not title.strip():
            logger.error("create_task: title is required")
            return None
        import uuid
        task_id = f"t_{uuid.uuid4().hex[:8]}"
        now = int(time.time())
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO tasks "
                "(id, title, body, assignee, status, priority, created_at) "
                "VALUES (?, ?, ?, ?, ?, 0, ?)",
                (task_id, title.strip(), body, assignee, status, now),
            )
            payload = json.dumps({
                "title": title.strip(),
                "status": status,
                "source": "forum_thread_sync",
            })
            conn.execute(
                "INSERT INTO task_events (task_id, kind, payload, created_at) "
                "VALUES (?, 'created', ?, ?)",
                (task_id, payload, now),
            )
            conn.commit()
            logger.info("Created task-%s from forum thread: %s", task_id, title)
            return task_id
        except Exception as e:
            logger.error("Failed to create task from forum thread: %s", e)
            return None
        finally:
            conn.close()

    # ---- 書き込み（Phase 2: フィードバック同期用） ----

    def add_comment(self, task_id: str, author: str, body: str) -> bool:
        """タスクにコメントを追加（コメント＋イベント）"""
        conn = self._connect()
        try:
            now = int(time.time())
            cursor = conn.execute(
                "INSERT INTO task_comments (task_id, author, body, created_at) "
                "VALUES (?, ?, ?, ?)",
                (task_id, author, body, now),
            )
            conn.execute(
                "INSERT INTO task_events (task_id, kind, payload, created_at) "
                "VALUES (?, 'commented', ?, ?)",
                (task_id, json.dumps({"comment_id": cursor.lastrowid}), now),
            )
            conn.commit()
            logger.info("Added comment to task-%s by %s", task_id, author)
            return True
        except Exception as e:
            logger.error("Failed to add comment to task %s: %s", task_id, e)
            return False
        finally:
            conn.close()

    def record_event(self, task_id: str, kind: str,
                     payload: Optional[str] = None) -> bool:
        """タスクイベントを記録"""
        conn = self._connect()
        try:
            now = int(time.time())
            conn.execute(
                "INSERT INTO task_events (task_id, kind, payload, created_at) "
                "VALUES (?, ?, ?, ?)",
                (task_id, kind, payload, now),
            )
            conn.commit()
            return True
        except Exception as e:
            logger.error("Failed to record event for task %s: %s", task_id, e)
            return False
        finally:
            conn.close()

    def update_task_status(self, task_id: str, new_status: str) -> bool:
        """タスクのステータスを更新し、イベントを記録"""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT status FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if not row:
                logger.warning("Task %s not found", task_id)
                return False
            old_status = row["status"]
            if old_status == new_status:
                return True

            now = int(time.time())
            conn.execute(
                "UPDATE tasks SET status = ? WHERE id = ?",
                (new_status, task_id),
            )
            payload = json.dumps({"from": old_status, "to": new_status,
                                   "source": "forum_tag_sync"})
            conn.execute(
                "INSERT INTO task_events (task_id, kind, payload, created_at) "
                "VALUES (?, 'status_change', ?, ?)",
                (task_id, payload, now),
            )
            conn.commit()
            logger.info(
                "Task-%s status updated: %s → %s (from forum tag)",
                task_id, old_status, new_status,
            )
            return True
        except Exception as e:
            logger.error("Failed to update task %s: %s", task_id, e)
            return False
        finally:
            conn.close()
