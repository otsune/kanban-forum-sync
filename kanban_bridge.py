"""Kanban 読み取り・toolset 書き込みモジュール。
読み取りは SQLite、書き込みは Hermes の kanban_* tools 経由で行う。"""

import sqlite3
import os
import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 「未計算」を表すセンチネル（None は「設定なし」を意味するため区別する）。
_UNSET = object()

# フォールバック用の既定 DB パス（コアの解決 API が使えない場合のみ使用）。
KANBAN_DB_PATH = os.path.expanduser("~/.hermes/kanban.db")

KANBAN_STATUSES = {"triage", "todo", "ready", "running", "blocked", "done", "archived"}


def resolve_kanban_db_path() -> str:
    """Kanban DB パスを解決する。

    解決ロジックはコア（``hermes_cli.kanban_db.kanban_db_path``）に委譲する。
    これにより ``HERMES_KANBAN_DB`` → ``HERMES_KANBAN_BOARD`` → 既定ボード の
    優先順位が Hermes 本体と常に一致する（プラグイン側で二重に持たない）。

    コアが import できない/API が変わった場合は、生 env と既定パスに退避する。
    """
    try:
        from hermes_cli import kanban_db as _kdb
        return str(_kdb.kanban_db_path())
    except Exception as e:
        logger.warning(
            "Could not use hermes_cli.kanban_db.kanban_db_path (%s); "
            "falling back to HERMES_KANBAN_DB env / default", e,
        )
        return os.environ.get("HERMES_KANBAN_DB", "").strip() or KANBAN_DB_PATH


class KanbanBridge:
    """Kanban DB への読み書きブリッジ"""

    def __init__(self, db_path: Optional[str] = None, ctx=None):
        # db_path 明示指定が最優先。未指定ならコア委譲で解決。
        self.db_path = db_path or resolve_kanban_db_path()
        self.ctx = ctx
        # kanban.default_assignee の解決結果をインスタンス寿命でキャッシュ。
        # load_config() は呼び出し毎に config 全体を deepcopy して返すため、
        # bulk 同期で create_task を多数回呼ぶ際の無駄を避ける。
        # （config 変更の反映は bridge 再生成＝gateway 再起動時。default_assignee は
        #   稀にしか変わらないため許容。）
        self._default_assignee_cache: Any = _UNSET
        # ディスク上の有効プロファイル集合（= 有効な assignee）のキャッシュ。
        self._known_profiles_cache: Any = _UNSET

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=120)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=120000")
        return conn

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        conn = self._connect()
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def _query_one(self, sql: str, params: tuple = ()) -> Optional[dict]:
        conn = self._connect()
        try:
            row = conn.execute(sql, params).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    # ---- 読み取り ----

    def get_all_tasks(self) -> list[dict]:
        """全タスクを取得（初期同期用）"""
        return self._query(
            "SELECT id, title, body, status, priority, assignee, "
            "created_at, completed_at FROM tasks"
        )

    def get_tasks_changed_since_event(self, last_event_id: int) -> list[dict]:
        """指定イベントID以降に変更があった全タスクを取得。
        重複排除のため GROUP BY t.id で1タスク1行に厳密に制限。"""
        return self._query(
            "SELECT t.id, t.title, t.body, t.status, "
            "t.priority, t.assignee, t.created_at, t.completed_at "
            "FROM tasks t "
            "JOIN task_events e ON t.id = e.task_id "
            "WHERE e.id > ? "
            "GROUP BY t.id "
            "ORDER BY MAX(e.id) ASC",
            (last_event_id,),
        )

    def get_task(self, task_id: str) -> Optional[dict]:
        """特定タスクを取得"""
        row = self._query_one(
            "SELECT id, title, body, status, priority, assignee, "
            "created_at, completed_at FROM tasks WHERE id = ?",
            (task_id,),
        )
        return row

    def get_latest_event_id(self) -> int:
        """最新の task_events.id を取得（0の場合はテーブル空）"""
        row = self._query_one("SELECT MAX(id) as max_id FROM task_events")
        return (row or {}).get("max_id") or 0

    def get_comments_since(self, task_id: str, last_id: int) -> list[dict]:
        """タスクのコメントを取得"""
        return self._query(
            "SELECT id, author, body, created_at FROM task_comments "
            "WHERE task_id = ? AND id > ? ORDER BY id ASC",
            (task_id, last_id),
        )

    def get_events_since(self, task_id: str, last_id: int,
                         kinds: Optional[list[str]] = None) -> list[dict]:
        """タスクのイベントを取得（ワーカーログ用）"""
        if kinds:
            placeholders = ",".join("?" * len(kinds))
            return self._query(
                f"SELECT id, kind, payload, created_at FROM task_events "
                f"WHERE task_id = ? AND id > ? AND kind IN ({placeholders}) "
                f"ORDER BY id ASC",
                (task_id, last_id, *kinds),
            )
        return self._query(
            "SELECT id, kind, payload, created_at FROM task_events "
            "WHERE task_id = ? AND id > ? ORDER BY id ASC",
            (task_id, last_id),
        )

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

    def _dispatch_kanban_tool_ex(
        self, tool_name: str, args: dict
    ) -> tuple[Optional[dict], bool]:
        """``(result, permanent)`` を返す。

        ``permanent=True`` は「ツールが実行され、明示的にエラーを返した」場合
        （例: URL 拒否、ファイル未検出、サイズ超過など）— リトライしても
        同じ結果になるため、呼び出し側はフォールバックしてカーソルを進めてよい。
        ``permanent=False`` は「dispatch 自体が例外/未接続で失敗した」場合
        （ネットワーク瞬断など一時的の可能性がある）— カーソルを進めず
        次サイクルで再試行すべき。成功時は ``(result, False)``。
        """
        if self.ctx is None:
            logger.error("%s requires plugin ctx.dispatch_tool; write skipped", tool_name)
            return None, False

        try:
            raw = self.ctx.dispatch_tool(tool_name, args)
            result = json.loads(raw) if isinstance(raw, str) else raw
        except Exception as e:
            logger.error("%s dispatch failed: %s", tool_name, e)
            return None, False

        if not isinstance(result, dict):
            logger.error("%s returned non-object result: %r", tool_name, result)
            return None, False
        if result.get("error"):
            logger.error("%s failed: %s", tool_name, result["error"])
            return None, True
        return result, False

    def _dispatch_kanban_tool(self, tool_name: str, args: dict) -> Optional[dict]:
        result, _permanent = self._dispatch_kanban_tool_ex(tool_name, args)
        return result

    def _config_default_assignee(self) -> Optional[str]:
        """アクティブプロファイルの ``kanban.default_assignee`` を返す。

        Hermes コアの設定ローダ経由で取得する（コア dispatcher と同じ読み方:
        ``kanban.py`` / ``kanban_decompose.py``）。これによりプロファイル対応・
        YAML 正準・キー位置正確になる。手書きの行パースはしない。
        注: プロファイル解決には spawner が ``HERMES_HOME`` を渡す必要がある
        （未設定時は default プロファイルにフォールバック。issue #18594）。
        結果はインスタンス寿命でキャッシュする（``__init__`` 参照）。
        """
        if self._default_assignee_cache is not _UNSET:
            return self._default_assignee_cache
        result: Optional[str] = None
        try:
            from hermes_cli.config import load_config
            kanban_cfg = load_config().get("kanban") or {}
            value = (kanban_cfg.get("default_assignee") or "").strip()
            result = value or None
        except Exception as e:
            logger.debug("Failed to read kanban.default_assignee: %s", e)
            result = None
        self._default_assignee_cache = result
        return result

    def _known_profiles(self) -> set:
        """ディスク上の有効なプロファイル名（= 有効な assignee）の集合を返す。

        Hermes コアの ``list_profiles_on_disk()`` に委譲する
        （``~/.hermes/profiles/<name>`` + 既定の ``default``）。forum→kanban の
        タスク作成時に assignee が実在プロファイルかを検証するために使う。
        実在しない assignee（例: ``otsune``）を渡すと ``kanban_create`` が
        エラーになり、同期が固まる/暴走の一因になるため、無効な候補は弾く。

        取得できない/空のときは空集合を返す。呼び出し側はその場合に検証を
        スキップして従来動作を保つ（コア未導入のテスト環境など）。
        結果はインスタンス寿命でキャッシュ（``__init__`` 参照）。
        """
        if self._known_profiles_cache is not _UNSET:
            return self._known_profiles_cache
        profiles: set = set()
        try:
            from hermes_cli.kanban_db import list_profiles_on_disk
            profiles = set(list_profiles_on_disk())
        except Exception as e:
            logger.debug("Failed to list profiles on disk: %s", e)
        self._known_profiles_cache = profiles
        return profiles

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

        # assignee 解決順: explicit > env > config > HERMES_PROFILE。
        # ただし実在するプロファイル（_known_profiles）に限定する。実在しない
        # assignee（例: "otsune"）を渡すと kanban_create がエラーになり同期が
        # 止まる/暴走するため、無効な候補はスキップして次の候補へフォールバックする。
        # （プロファイル集合が取得できない場合は検証を行わず従来動作を保つ。）
        known = self._known_profiles()
        candidates = [
            assignee,
            os.environ.get("FORUM_SYNC_DEFAULT_ASSIGNEE"),
            self._config_default_assignee(),
            os.environ.get("HERMES_PROFILE"),
        ]
        resolved_assignee: Optional[str] = None
        rejected: list[str] = []
        for cand in candidates:
            if not cand or not cand.strip():
                continue
            name = cand.strip()
            if known and name not in known and name.lower() not in known:
                rejected.append(name)
                continue
            resolved_assignee = name
            break
        if rejected:
            logger.warning(
                "create_task: ignoring assignee(s) %s — not a known Hermes "
                "profile. Valid profiles: %s",
                rejected, sorted(known) or "(unknown)",
            )
        if not resolved_assignee:
            logger.error(
                "create_task requires a valid profile assignee for kanban_create. "
                "Set kanban.default_assignee / FORUM_SYNC_DEFAULT_ASSIGNEE to one "
                "of: %s",
                sorted(known) or "(create a profile with `hermes -p <name> setup`)",
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

    def attach_url(self, task_id: str, url: str,
                   content_type: Optional[str] = None,
                   filename: Optional[str] = None) -> Optional[bool]:
        """添付ファイルを URL 経由で追加する（``kanban_attach_url`` ツール経由）。

        Hermes 側が ``url`` をサーバー側でフェッチして ``task_attachments`` に
        保存する（``tools/url_safety.py`` 経由の SSRF ガード、25MB 上限）。
        Discord CDN の署名付き URL は即時取得なら有効。

        戻り値は3値:
        - ``True``  — 取込成功
        - ``False`` — 恒久的失敗（ツールが明示的にエラーを返した。例: URL 拒否
          [SSRF ガード]、期限切れ CDN URL の 404、サイズ超過）。呼び出し側は
          フォールバック（例: URL をコメント投稿）してカーソルを進めてよい
        - ``None``  — 一時的失敗（dispatch 自体が失敗）。カーソルを進めず
          次サイクルで再試行すべき
        """
        args: dict = {"task_id": task_id, "url": url}
        if content_type:
            args["content_type"] = content_type
        if filename:
            args["filename"] = filename
        result, permanent = self._dispatch_kanban_tool_ex("kanban_attach_url", args)
        if result:
            logger.info("Attached url '%s' to task-%s", filename or url, task_id)
            return True
        return False if permanent else None

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
