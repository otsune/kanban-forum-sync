"""Kanban ↔ Discord Forum 同期のコアロジック。
task_events ベースの変更検出でポーリング。"""

import json
import os
import time
import logging
from threading import Thread, Event
from typing import Optional

from .discord_forum import (
    DiscordForumClient,
    DiscordForumError,
    PermissionError,
    NotFoundError,
    FORUM_CHANNEL_TYPE,
)
from .kanban_bridge import KanbanBridge
from .models import SyncState, SyncMap, SyncOriginTracker, ThreadMetaTracker

logger = logging.getLogger(__name__)

# ---- i18n: tag name locale data ----
# (status, en_name, ja_name, emoji)
_LOCALE_DATA = [
    ("triage",    "Triage",    "トリアージ", "🩺"),
    ("todo",      "Todo",      "未着手",     "📝"),
    ("scheduled", "Scheduled", "予定済み",   "📅"),
    ("ready",     "Ready",     "着手可能",   "✅"),
    ("running",   "Running",   "進行中",     "🔄"),
    ("blocked",   "Blocked",   "停滞中",     "🚧"),
    ("review",    "Review",    "レビュー中", "👀"),
    ("done",      "Done",      "完了",       "🎉"),
]

# Phase 2 専用タグ（Kanban ステータスには存在しない）
_EXTRA_TAGS: dict[str, dict[str, str]] = {
    "en": {"Backlog": "backlog"},
    "ja": {"バックログ": "backlog"},
}

_SUPPORTED_LANGS = ("en", "ja")


def _build_tag_tables(lang: str) -> tuple[dict, dict, dict]:
    """言語設定から STATUS_TO_TAG / TAG_TO_STATUS / STATUS_TAG_EMOJI を生成する。"""
    use_ja = lang == "ja"
    status_to_tag: dict[str, str] = {}
    tag_to_status: dict[str, str] = {}
    status_tag_emoji: dict[str, str] = {}

    for status, en, ja, emoji in _LOCALE_DATA:
        tag = ja if use_ja else en
        status_to_tag[status] = tag
        tag_to_status[tag] = status
        status_tag_emoji[tag] = emoji

    status_to_tag["archived"] = status_to_tag["done"]
    tag_to_status.update(_EXTRA_TAGS.get(lang, _EXTRA_TAGS["en"]))

    return status_to_tag, tag_to_status, status_tag_emoji


_LANG = os.environ.get("FORUM_SYNC_LANG", "en").strip().lower()
if _LANG not in _SUPPORTED_LANGS:
    logger.warning("Unsupported FORUM_SYNC_LANG=%r; falling back to 'en'", _LANG)
    _LANG = "en"

STATUS_TO_TAG, TAG_TO_STATUS, STATUS_TAG_EMOJI = _build_tag_tables(_LANG)

# Statuses that trigger thread archiving (locale-independent)
ARCHIVE_STATUSES = {"done", "archived"}

# task_events の種別のうち Discord に通知するもの
_WORKER_LOG_KINDS = ["blocked", "spawned"]


def _format_worker_event(ev: dict) -> str:
    kind = ev["kind"]
    payload: dict = {}
    if ev.get("payload"):
        try:
            payload = json.loads(ev["payload"])
        except Exception:
            pass
    if kind == "blocked":
        reason = payload.get("reason", "")
        return f"🚧 **Blocked**: {reason}" if reason else "🚧 **Blocked**"
    if kind == "spawned":
        pid = payload.get("pid", "?")
        return f"🤖 **Worker spawned** (PID {pid})"
    return ""

_FORUM_GUIDE_URL = "https://support.discord.com/hc/ja/articles/6208479917079"


def _make_tag_dict(name: str, emoji: str = "") -> dict:
    tag: dict = {"name": name, "moderated": False}
    if emoji:
        tag["emoji_name"] = emoji
    return tag


REQUIRED_TAGS = [_make_tag_dict(name, emoji) for name, emoji in STATUS_TAG_EMOJI.items()]

ADMIN_GUIDE_MESSAGE = """
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠️  Discord Forum チャンネルが存在しません

Bot にチャンネル作成権限がないため、自動生成できませんでした。
以下の手順でサーバー管理者に依頼してください：

1. Discord サーバー設定を開く
2. 「チャンネル」→「チャンネルを作成」を選択
3. チャンネルタイプ: 「フォーラム」
4. 名前: 「kanban」（または task-board）
5. 作成後、チャンネルIDを以下の環境変数に設定:
   FORUM_SYNC_CHANNEL_ID=<チャンネルID>

または、Bot に「チャンネルを管理」権限を付与すれば自動生成されます。
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""


class KanbanForumSyncer:
    """Kanban ↔ Discord Forum 同期エンジン"""

    def __init__(self, bot_token: str, channel_id: Optional[int] = None,
                 poll_interval: int = 15, use_inotify: bool = False):
        self.bot_token = bot_token
        self._channel_id = channel_id
        self.poll_interval = poll_interval
        self._use_inotify = use_inotify
        self._thread: Optional[Thread] = None
        self._stop_event = Event()
        self._state = SyncState()

        self.discord = DiscordForumClient(bot_token, self._channel_id)
        self.kanban = KanbanBridge()

        self._sync_map = SyncMap()
        self._origin_tracker = SyncOriginTracker()
        self._tag_map: dict[str, int] = {}
        self._reverse_tag_map: dict[int, str] = {}
        self._thread_meta = ThreadMetaTracker()
        self._bot_user_id: Optional[str] = None

    @property
    def channel_id(self) -> Optional[int]:
        return self._channel_id

    def get_state(self) -> SyncState:
        return self._state

    # ---- Forum channel auto-resolution ----

    def _set_channel(self, channel_id: int) -> None:
        self._channel_id = channel_id
        self.discord.channel_id = channel_id

    def _find_or_create_forum_in_guild(self, guild_id: int) -> bool:
        """指定サーバーで Forum チャンネルを検索し、なければ作成する。"""
        existing = self.discord.find_forum_channel(guild_id)
        if existing:
            self._set_channel(int(existing["id"]))
            logger.info("Switched to forum #%s (%s)", existing["name"], existing["id"])
            return True
        try:
            created = self.discord.create_forum_channel(guild_id, name="kanban")
            self._set_channel(int(created["id"]))
            logger.info("Created forum #kanban (%s)", created["id"])
            return True
        except PermissionError:
            self._state.last_error = (
                "Bot lacks 'Manage Channels' permission. "
                "Create a forum channel named 'kanban' or grant the bot 'Manage Channels', "
                f"then set FORUM_SYNC_CHANNEL_ID. Guide: {_FORUM_GUIDE_URL}"
            )
            print(ADMIN_GUIDE_MESSAGE)
            return False
        except DiscordForumError as e:
            self._state.last_error = "Failed to create forum: %s" % e
            print(ADMIN_GUIDE_MESSAGE)
            return False

    def _resolve_forum_channel(self) -> bool:
        """Forum チャンネルを解決する。

        1. channel_id が設定されていて Forum なら OK
        2. 非 Forum チャンネル指定 → 同サーバー内を検索/作成
        3. channel_id 未設定 → Bot の参加全サーバーを探索
        4. 見つからない/作成不可 → 管理者向け案内表示して False

        Returns: True=解決成功, False=解決失敗
        """
        guild_id = None

        if self._channel_id is not None:
            try:
                channel = self.discord.get_channel()
                if channel.get("type") == FORUM_CHANNEL_TYPE:
                    logger.info(
                        "Channel #%s (%s) is a valid forum",
                        channel["name"], self._channel_id,
                    )
                    return True
                guild_id = int(channel["guild_id"])
                logger.info(
                    "Channel #%s is type=%s (not forum). Searching guild %s...",
                    channel["name"], channel.get("type"), guild_id,
                )
            except (NotFoundError, DiscordForumError) as e:
                logger.warning(
                    "Configured channel %s not accessible: %s", self._channel_id, e
                )
                self._state.last_error = (
                    "Channel ID %s is not accessible. "
                    "Leave FORUM_SYNC_CHANNEL_ID unset for auto-discovery. "
                    "Guide: %s" % (self._channel_id, _FORUM_GUIDE_URL)
                )
                print(ADMIN_GUIDE_MESSAGE)
                return False

        if guild_id is None:
            logger.info("No channel_id set. Scanning bot's guilds...")
            try:
                guilds = self.discord.get_bot_guilds()
            except DiscordForumError as e:
                self._state.last_error = (
                    "Cannot list guilds: %s. "
                    "Bot may lack 'guilds' OAuth2 scope. "
                    "Set FORUM_SYNC_CHANNEL_ID explicitly." % e
                )
                print(ADMIN_GUIDE_MESSAGE)
                return False

            for g in guilds:
                existing = self.discord.find_forum_channel(int(g["id"]))
                if existing:
                    self._set_channel(int(existing["id"]))
                    logger.info(
                        "Found forum #%s (%s) in guild '%s'",
                        existing["name"], existing["id"], g["name"],
                    )
                    return True

            if not guilds:
                self._state.last_error = (
                    "Bot is not in any guild. "
                    "Invite the bot to a server first. Guide: %s" % _FORUM_GUIDE_URL
                )
                print(ADMIN_GUIDE_MESSAGE)
                return False

            guild_id = int(guilds[0]["id"])
            logger.info(
                "No forum found. Attempting to create in '%s'...", guilds[0]["name"]
            )

        return self._find_or_create_forum_in_guild(guild_id)

    # ---- Tag management ----

    def _build_tag_map(self):
        tags = self.discord.get_tags()
        self._tag_map = {t["name"]: t["id"] for t in tags}
        self._reverse_tag_map = {t["id"]: t["name"] for t in tags}
        logger.info(
            "Tag map built with %d tags: %s",
            len(self._tag_map), list(self._tag_map.keys()),
        )

    def _ensure_tags(self):
        """必要な Forum タグが存在するか確認し、不足があれば作成する"""
        tags = self.discord.get_tags()
        existing_names = {t["name"] for t in tags}
        missing = [t for t in REQUIRED_TAGS if t["name"] not in existing_names]

        if missing:
            logger.info(
                "Creating %d missing tags: %s",
                len(missing), [t["name"] for t in missing],
            )
            self.discord.create_tags(tags + missing)
            time.sleep(1)  # API反映待ち
        else:
            logger.info("All 8 status tags already exist")

    def _resolve_tag_ids(self, status: str) -> list[int]:
        tag_name = STATUS_TO_TAG.get(status)
        if tag_name and tag_name in self._tag_map:
            return [self._tag_map[tag_name]]
        return []

    # ---- Phase 2: Forum → Kanban feedback sync ----

    def _resolve_bot_user_id(self) -> Optional[str]:
        if self._bot_user_id is None:
            try:
                user = self.discord._request("GET", "/users/@me")
                self._bot_user_id = str(user["id"])
                logger.info("Bot user ID: %s", self._bot_user_id)
            except Exception as e:
                logger.warning("Failed to get bot user ID: %s", e)
        return self._bot_user_id

    def _sync_forum_comments(self):
        """Forum スレッドの新規メッセージを検出し、Kanban コメントとして追加する。"""
        sync_items = self._sync_map.items()
        if not sync_items:
            return

        bot_id = self._resolve_bot_user_id()
        new_comments = 0

        for task_id, thread_id in sync_items.items():
            last_id = self._thread_meta.get_last_message_id(thread_id)
            after_param = last_id if last_id > 0 else None

            try:
                messages = self.discord.get_thread_messages(
                    thread_id, after=after_param, limit=50
                )
            except Exception as e:
                logger.warning("Failed to fetch messages for thread %s: %s", thread_id, e)
                continue

            if not messages:
                continue

            # after なし（初回）は降順、after あり（追加分）は昇順で返る
            if after_param is None:
                messages.reverse()

            for msg in messages:
                msg_id = int(msg["id"])
                author = msg["author"]
                author_id = str(author.get("id", ""))
                author_name = author.get("username", "unknown")
                is_bot = author.get("bot", False)

                if is_bot or (bot_id and author_id == bot_id):
                    self._thread_meta.set_last_message_id(thread_id, msg_id)
                    continue

                content = msg.get("content", "").strip()
                if content:
                    if self.kanban.add_comment(task_id, author_name, content):
                        new_comments += 1
                        logger.debug(
                            "Comment synced: %s → task-%s", author_name, task_id
                        )

                self._thread_meta.set_last_message_id(thread_id, msg_id)

            time.sleep(0.5)  # レート制限

        if new_comments:
            self._state.comment_count += new_comments
            logger.info("Synced %d new comment(s) from forum", new_comments)

    def _sync_forum_tags(self):
        """Forum スレッドのタグ変更を検出し、Kanban ステータスに反映する。"""
        sync_items = self._sync_map.items()
        if not sync_items:
            return

        changed = 0
        for task_id, thread_id in sync_items.items():
            try:
                thread = self.discord.get_channel_by_id(thread_id)
            except Exception as e:
                logger.warning("Failed to fetch thread %s: %s", thread_id, e)
                continue

            applied = thread.get("applied_tags", [])
            if not isinstance(applied, list):
                continue

            new_status = None
            for tag_id in applied:
                tag_name = self._reverse_tag_map.get(tag_id)
                if tag_name and tag_name in TAG_TO_STATUS:
                    new_status = TAG_TO_STATUS[tag_name]
                    break

            if new_status:
                current = self.kanban.get_task(task_id)
                if current and current["status"] != new_status:
                    if self.kanban.update_task_status(task_id, new_status):
                        changed += 1
                        logger.info(
                            "Tag sync: task-%s %s → %s",
                            task_id, current["status"], new_status,
                        )

            time.sleep(0.5)

        if changed:
            self._state.tag_sync_count += changed
            logger.info("Tag sync: %d task(s) status updated from forum tags", changed)

    # ---- Phase 3: Forum 新規スレッド → Kanban タスク作成 ----

    def _sync_forum_new_threads(self):
        """Forum チャンネルに新しく作成されたスレッドを検出し、Kanban タスクを作成する。

        同期マップに存在しないスレッドを見つけ、タイトル・本文・タグから
        新しい Kanban タスクを生成する。
        """
        if self.channel_id is None:
            return

        try:
            threads = self.discord.get_channel_active_threads()
        except Exception as e:
            logger.warning("Failed to fetch active threads: %s", e)
            return

        if not threads:
            return

        new_count = 0
        for thread in threads:
            thread_id = int(thread["id"])
            thread_name = thread.get("name", "").strip()

            # Bot が作ったスレッド（task- プレフィックス）はスキップ
            if thread_name.lower().startswith("task-"):
                continue

            # すでに同期マップにあるスレッドはスキップ
            if self._sync_map.contains_thread(thread_id):
                continue

            # スレッドのスターターポスト（最初のメッセージ）を本文として取得
            body_lines = []
            try:
                msgs = self.discord.get_thread_messages(thread_id, limit=50)
                if msgs:
                    # API は降順（最新が先頭）。一番古いメッセージ = スターターポスト
                    for msg in reversed(msgs):
                        author = msg.get("author", {})
                        if author.get("bot", False):
                            continue
                        body_lines.append(msg.get("content", "").strip())
                        break  # 最初の人間の発言だけ取る
            except Exception as e:
                logger.debug("Failed to get first message for thread %s: %s", thread_id, e)

            body = "\n".join(line for line in body_lines if line)

            # タグから初期ステータスを解決
            status = "triage"
            applied_tags = thread.get("applied_tags", [])
            if isinstance(applied_tags, list):
                for tag_id in applied_tags:
                    tag_name = self._reverse_tag_map.get(tag_id)
                    if tag_name and tag_name in TAG_TO_STATUS:
                        resolved = TAG_TO_STATUS[tag_name]
                        # backlog は triage にマップ（非標準ステータス回避）
                        if resolved == "backlog":
                            resolved = "triage"
                        status = resolved
                        break

            # Kanban タスク作成
            task_id = self.kanban.create_task(
                title=thread_name,
                body=body,
                status=status,
            )
            if task_id:
                self._sync_map.set(task_id, thread_id)
                self._origin_tracker.set_origin(task_id, "forum")
                self._state.task_count += 1
                self._state.forum_task_count += 1
                new_count += 1
                logger.info(
                    "Forum → Kanban: created task-%s from thread '%s' (status=%s)",
                    task_id, thread_name, status,
                )
            else:
                logger.error(
                    "Failed to create Kanban task from forum thread '%s'",
                    thread_name,
                )

            time.sleep(0.5)  # レート制限

        if new_count:
            logger.info("Phase 3: created %d Kanban task(s) from new forum threads", new_count)

    # ---- Phase 1 拡張: Kanban コメント・ワーカーログ → Forum ----

    def _sync_kanban_comments_to_forum(self):
        """task_comments とワーカーログ (blocked/spawned) を Discord スレッドに投稿する。"""
        sync_items = self._sync_map.items()
        if not sync_items:
            return

        posted = 0
        for task_id, thread_id in sync_items.items():
            # --- コメント ---
            last_comment_id = self._thread_meta.get_last_comment_id(thread_id)
            try:
                comments = self.kanban.get_comments_since(task_id, last_comment_id)
            except Exception as e:
                logger.warning("Failed to fetch comments for task %s: %s", task_id, e)
                comments = []

            for c in comments:
                try:
                    text = f"💬 **{c['author']}**\n{c['body']}"
                    self.discord.send_message(thread_id, text[:2000])
                    self._thread_meta.set_last_comment_id(thread_id, c["id"])
                    posted += 1
                    time.sleep(0.5)
                except Exception as e:
                    logger.warning(
                        "Failed to post comment %s to thread %s: %s",
                        c["id"], thread_id, e,
                    )
                    break

            # --- ワーカーログ ---
            last_ev_id = self._thread_meta.get_last_kanban_event_id(thread_id)
            try:
                events = self.kanban.get_events_since(task_id, last_ev_id, _WORKER_LOG_KINDS)
            except Exception as e:
                logger.warning("Failed to fetch events for task %s: %s", task_id, e)
                events = []

            for ev in events:
                try:
                    text = _format_worker_event(ev)
                    if text:
                        self.discord.send_message(thread_id, text)
                        posted += 1
                        time.sleep(0.5)
                    self._thread_meta.set_last_kanban_event_id(thread_id, ev["id"])
                except Exception as e:
                    logger.warning(
                        "Failed to post event %s to thread %s: %s",
                        ev["id"], thread_id, e,
                    )
                    break

        if posted:
            logger.info("Synced %d comment/log(s) to forum threads", posted)

    # ---- Thread content generation ----

    def _thread_title(self, task: dict) -> str:
        return f"task-{task['id']}: {task['title']}"

    def _thread_content(self, task: dict) -> str:
        lines = [
            f"**{task['title']}**",
            f"Status: **{task['status']}**",
        ]

        if task.get("body"):
            lines.append(f"\n{task['body']}")

        details = [f"Priority: {task.get('priority', '—')}"]
        if task.get("assignee"):
            details.append(f"Assignee: {task['assignee']}")
        lines.append("\n" + " | ".join(details))

        return "\n".join(lines)

    # ---- Sync logic ----

    def _sync_task_to_forum(self, task: dict) -> bool:
        """1件のタスクを Forum に同期する"""
        task_id = task["id"]
        thread_id = self._sync_map.get(task_id)

        try:
            if thread_id is None:
                tag_ids = self._resolve_tag_ids(task["status"])
                result = self.discord.create_thread(
                    name=self._thread_title(task),
                    content=self._thread_content(task),
                    applied_tags=tag_ids,
                )
                new_thread_id = result.get("id")
                if new_thread_id:
                    self._sync_map.set(task_id, int(new_thread_id))
                    logger.info("Created thread for task-%s: %s", task_id, new_thread_id)
                    return True
                logger.error("create_thread returned no id for task-%s", task_id)
                return False
            else:
                kwargs: dict = {}

                # Forum 発祥のスレッドは名前を書き換えない（人間がつけたタイトルを保持）
                if not self._origin_tracker.is_forum_sourced(task_id):
                    kwargs["name"] = self._thread_title(task)

                tag_ids = self._resolve_tag_ids(task["status"])
                if tag_ids:
                    kwargs["applied_tags"] = tag_ids

                if task["status"] in ARCHIVE_STATUSES:
                    kwargs["archived"] = True
                    kwargs["locked"] = False
                else:
                    # アーカイブ済みスレッドへの他フィールド PATCH は 400 になる。
                    # archived=False を先行して送ることで解除と更新を1リクエストで行う。
                    # スレッドオーナー（Bot）は MANAGE_THREADS なしに解除できる。
                    kwargs["archived"] = False

                self.discord.update_thread(thread_id, **kwargs)
                logger.info("Updated thread for task-%s", task_id)
                return True
        except Exception as e:
            logger.error("Failed to sync task-%s: %s", task_id, e)
            return False

    def initial_sync(self):
        """初回フル同期"""
        logger.info("Starting initial sync...")
        self._ensure_tags()
        self._build_tag_map()

        tasks = self.kanban.get_all_tasks()
        synced = 0
        errors = 0

        for task in tasks:
            if self._sync_task_to_forum(task):
                synced += 1
            else:
                errors += 1
            time.sleep(0.5)

        self._state.task_count = synced
        self._state.error_count = errors
        self._state.last_sync = str(int(time.time()))
        logger.info("Initial sync complete: %d synced, %d errors", synced, errors)

    def incremental_sync(self):
        """増分同期（1回のポーリング）

        Phase 1: Kanban DB の変更を検出 → Forum に反映
        Phase 2: Forum の変更を検出 → Kanban にフィードバック"""
        last_id = self._state.last_event_id
        changed = self.kanban.get_tasks_changed_since_event(last_id)

        if changed:
            logger.info(
                "Incremental sync: %d changed tasks (events > %d)",
                len(changed), last_id,
            )
            for task in changed:
                self._sync_task_to_forum(task)
                time.sleep(0.5)

        latest_event = self.kanban.get_latest_event_id()
        if latest_event > self._state.last_event_id:
            self._state.last_event_id = latest_event
            logger.debug("Updated last_event_id to %d", latest_event)

        self._sync_forum_comments()
        self._sync_forum_tags()
        self._sync_forum_new_threads()
        self._sync_kanban_comments_to_forum()

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
        mode = "inotify" if self._use_inotify else "poll"
        logger.info("Syncer started (mode: %s, interval: %ds)", mode, self.poll_interval)

    def stop(self):
        """ポーリングループを停止"""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
        self._state.state = "stopped"
        logger.info("Syncer stopped")

    def _run_loop(self):
        if self._use_inotify:
            self._run_loop_inotify()
        else:
            self._run_loop_poll()

    def _run_loop_poll(self):
        """固定間隔ポーリングループ"""
        if not self._resolve_forum_channel():
            self._state.state = "error"
            logger.error("Forum channel resolution failed. Sync aborted.")
            return

        try:
            self.initial_sync()
        except Exception as e:
            logger.error("Initial sync failed: %s", e, exc_info=True)
            self._state.state = "error"
            self._state.last_error = str(e)
            return

        while not self._stop_event.is_set():
            try:
                self.incremental_sync()
            except Exception as e:
                logger.error("Incremental sync failed: %s", e, exc_info=True)
                self._state.last_error = str(e)
            self._stop_event.wait(self.poll_interval)

    def _run_loop_inotify(self):
        """inotify によるイベント駆動ループ。

        kanban.db への書き込みを OS が即座に通知する。
        poll_interval はフォールバック（Phase 2 Discord ポーリング含む）として使用。
        inotify 未使用環境では自動的にインターバル待機に縮退する。
        """
        if not self._resolve_forum_channel():
            self._state.state = "error"
            logger.error("Forum channel resolution failed. Sync aborted.")
            return

        try:
            self.initial_sync()
        except Exception as e:
            logger.error("Initial sync failed: %s", e, exc_info=True)
            self._state.state = "error"
            self._state.last_error = str(e)
            return

        from .kanban_watcher import KanbanDBWatcher
        logger.info(
            "Event-driven sync active (inotify) — watching %s", self.kanban.db_path
        )
        with KanbanDBWatcher(self.kanban.db_path) as watcher:
            while not self._stop_event.is_set():
                watcher.wait(timeout=self.poll_interval)
                if self._stop_event.is_set():
                    break
                try:
                    self.incremental_sync()
                except Exception as e:
                    logger.error("Incremental sync failed: %s", e, exc_info=True)
                    self._state.last_error = str(e)

    def full_sync(self):
        """手動フル同期（CLI/スラッシュコマンド用）"""
        if not self._resolve_forum_channel():
            return
        self._ensure_tags()
        self._build_tag_map()
        self._sync_map.clear()
        self._origin_tracker.clear()
        self.initial_sync()
