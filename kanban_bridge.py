"""Kanban 読み取り・toolset 書き込みモジュール。
読み取りは SQLite、書き込みは Hermes の kanban_* tools 経由で行う。"""

import sqlite3
import os
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

KANBAN_DB_PATH = os.path.expanduser("~/.hermes/kanban.db")

TASK_COLS = "id, title, body, status, priority, assignee, created_at, completed_at"
KANBAN_STATUSES = {"triage", "todo", "ready", "running", "blocked", "done", "archived"}


class KanbanBridge:
    """Kanban DB への読み書きブリッジ"""

    def __init__(self, db_path: str = KANBAN_DB_PATH, ctx=None):
        self.db_path = db_path
        self.ctx = ctx

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=120)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=120000")
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
        重複排除のため GROUP BY t.id で1タスク1行に厳密に制限。"""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT t.id, t.title, t.body, t.status, "
                "t.priority, t.assignee, t.created_at, t.completed_at "
                "FROM tasks t "
                "JOIN task_events e ON t.id = e.task_id "
                "WHERE e.id > ? "
                "GROUP BY t.id "
                "ORDER BY MAX(e.id) ASC",
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

    # ---- ワーカー実行ログ（per-task テキストログ） ----
    #
    # ~/.hermes/kanban/logs/<task_id>.log にワーカーエージェントの発話・思考が
    # 記録される（task_events とは別ソース）。Hermes の発話は罫線ボックス
    #   ╭─ ⚕ Hermes ─╮
    #       本文…
    #   ╰──────────╯
    # に入る。このボックス本文だけを抽出して返す。

    WORKER_LOG_DIR = os.path.expanduser("~/.hermes/kanban/logs")
    _HERMES_BOX_TOP = "╭─"
    _HERMES_BOX_MARK = "Hermes"
    _HERMES_BOX_BOT = "╰─"

    def get_worker_log_messages(self, task_id: str) -> list[str]:
        """per-task ワーカーログから Hermes 発話ボックスの本文を順に返す。

        ファイルが無ければ空リスト。各 run の追記が連結されているため、
        返り値は時系列順。カーソル管理は呼び出し側（投稿済み件数）が行う。
        """
        path = os.path.join(self.WORKER_LOG_DIR, f"{task_id}.log")
        if not os.path.exists(path):
            return []
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except Exception as e:
            logger.warning("Failed to read worker log %s: %s", path, e)
            return []

        blocks: list[str] = []
        i = 0
        n = len(lines)
        while i < n:
            line = lines[i]
            if self._HERMES_BOX_TOP in line and self._HERMES_BOX_MARK in line:
                body: list[str] = []
                i += 1
                while i < n and self._HERMES_BOX_BOT not in lines[i]:
                    body.append(lines[i].strip())
                    i += 1
                text = "\n".join(body).strip()
                if text:
                    blocks.append(text)
            i += 1
        return blocks

    # ---- 書き込み（Kanban toolset 経由） ----

    def _dispatch_kanban_tool(self, tool_name: str, args: dict) -> Optional[dict]:
        if self.ctx is None:
            logger.error("%s requires plugin ctx.dispatch_tool; write skipped", tool_name)
            return None

        try:
            raw = self.ctx.dispatch_tool(tool_name, args)
            result = json.loads(raw) if isinstance(raw, str) else raw
        except Exception as e:
            logger.error("%s dispatch failed: %s", tool_name, e)
            return None

        if not isinstance(result, dict):
            logger.error("%s returned non-object result: %r", tool_name, result)
            return None
        if result.get("error"):
            logger.error("%s failed: %s", tool_name, result["error"])
            return None
        return result

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
        if status not in KANBAN_STATUSES:
            logger.warning("create_task: unsupported status %r; using triage", status)
            status = "triage"

        resolved_assignee = (
            assignee
            or os.environ.get("FORUM_SYNC_DEFAULT_ASSIGNEE")
            or os.environ.get("HERMES_PROFILE")
        )
        if not resolved_assignee:
            logger.error(
                "create_task requires assignee for kanban_create; set "
                "FORUM_SYNC_DEFAULT_ASSIGNEE"
            )
            return None

        args = {
            "title": title.strip(),
            "body": body,
            "assignee": resolved_assignee,
            "triage": status != "blocked",
            "initial_status": "blocked" if status == "blocked" else "running",
        }
        result = self._dispatch_kanban_tool("kanban_create", args)
        task_id = result.get("task_id") if result else None
        if task_id:
            logger.info("Created task-%s from forum thread: %s", task_id, title)
            return str(task_id)
        return None

    def add_comment(self, task_id: str, author: str, body: str) -> bool:
        """タスクにコメントを追加"""
        comment_body = f"**Discord: {author}**\n{body}"
        result = self._dispatch_kanban_tool(
            "kanban_comment",
            {"task_id": task_id, "body": comment_body},
        )
        if result:
            logger.info("Added comment to task-%s by %s", task_id, author)
            return True
        return False

    def record_event(self, task_id: str, kind: str,
                     payload: Optional[str] = None) -> bool:
        """任意イベントは Kanban toolset に存在しないためコメントとして記録する。"""
        body = f"Forum sync event: {kind}"
        if payload:
            body = f"{body}\n\n```json\n{payload}\n```"
        return self.add_comment(task_id, "forum-sync", body)

    def update_task_status(self, task_id: str, new_status: str) -> bool:
        """Kanban toolset で表現できる範囲でステータスを更新する。"""
        if new_status not in KANBAN_STATUSES:
            logger.warning("Refusing unsupported status %r for task %s", new_status, task_id)
            return False

        current = self.get_task(task_id)
        if not current:
            logger.warning("Task %s not found", task_id)
            return False
        old_status = current["status"]
        if old_status == new_status:
            return True

        if new_status == "blocked":
            result = self._dispatch_kanban_tool(
                "kanban_block",
                {"task_id": task_id, "reason": "Blocked from Discord forum tag"},
            )
        elif new_status == "done":
            result = self._dispatch_kanban_tool(
                "kanban_complete",
                {
                    "task_id": task_id,
                    "summary": "Marked done from Discord forum tag",
                    "metadata": {"source": "forum_tag_sync", "from": old_status},
                },
            )
        elif new_status == "ready" and old_status == "blocked":
            result = self._dispatch_kanban_tool("kanban_unblock", {"task_id": task_id})
        else:
            logger.warning(
                "No Kanban tool can transition task-%s from %s to %s; skipped",
                task_id, old_status, new_status,
            )
            return False

        if result:
            logger.info(
                "Task-%s status updated: %s → %s (from forum tag)",
                task_id, old_status, new_status,
            )
            return True
        return False
