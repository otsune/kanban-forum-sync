"""Kanban ↔ Discord Forum 同期のコアロジック。
task_events ベースの変更検出でポーリング。"""

import json
import os
import re
import time
import logging
from threading import Thread, Event
from typing import Optional

from .discord_forum import (
    DiscordForumClient,
    DiscordForumError,
    DiscordPermissionError,
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
_KANBAN_STATUSES = {"triage", "todo", "ready", "running", "blocked", "done", "archived"}
_STATUS_ALIASES = {
    "backlog": "triage",
    "scheduled": "ready",
    "review": "running",
}


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
_WORKER_LOG_KINDS = [
    "blocked", "unblocked",
    "spawned", "claimed", "reclaimed",
    "completed", "gave_up",
    "status_change", "promoted", "archived", "stale",
    "protocol_violation", "linked",
]


def _normalize_kanban_status(status: str) -> Optional[str]:
    status = _STATUS_ALIASES.get(status, status)
    return status if status in _KANBAN_STATUSES else None


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
    if kind == "unblocked":
        return "✅ **Unblocked**"
    if kind == "spawned":
        pid = payload.get("pid", "?")
        return f"🤖 **Worker spawned** (PID {pid})"
    if kind == "claimed":
        run_id = payload.get("run_id", "?")
        return f"🔒 **Worker claimed** (run #{run_id})"
    if kind == "reclaimed":
        return "🔄 **Reclaimed** — previous worker expired"
    if kind == "completed":
        summary = payload.get("summary", "")
        return f"🎉 **Completed**\n{summary}" if summary else "🎉 **Completed**"
    if kind == "gave_up":
        error = payload.get("error", "")
        failures = payload.get("failures", "?")
        base = f"❌ **Failed** (attempt {failures})"
        return f"{base}\n{error}" if error else base
    if kind == "status_change":
        # forum_tag_sync 由来は Discord 側の操作が起源なので重複投稿しない
        if payload.get("source") == "forum_tag_sync":
            return ""
        from_s = payload.get("from", "?")
        to_s = payload.get("to", "?")
        source = payload.get("source", "")
        suffix = f" (via {source})" if source else ""
        return f"📊 **Status**: {from_s} → {to_s}{suffix}"
    if kind == "promoted":
        return "⬆️ **Promoted**"
    if kind == "archived":
        return "📦 **Archived**"
    if kind == "stale":
        elapsed = payload.get("elapsed_seconds")
        if elapsed:
            return f"💤 **Stale** — no activity for {elapsed // 3600}h"
        return "💤 **Stale**"
    if kind == "protocol_violation":
        exit_code = payload.get("exit_code", "?")
        return f"⚠️ **Protocol violation** (exit code {exit_code})"
    if kind == "linked":
        target = payload.get("target_id", "")
        return f"🔗 **Linked** → task-{target}" if target else "🔗 **Linked**"
    return ""

_FORUM_GUIDE_URL = "https://support.discord.com/hc/ja/articles/6208479917079"


def _make_tag_dict(name: str, emoji: str = "") -> dict:
    tag: dict = {"name": name, "moderated": False}
    if emoji:
        tag["emoji_name"] = emoji
    return tag


REQUIRED_TAGS = [_make_tag_dict(name, emoji) for name, emoji in STATUS_TAG_EMOJI.items()]

# ---- i18n: admin guide messages ----
ADMIN_GUIDE_MESSAGES: dict[str, str] = {
    "en": """\
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠️  Discord Forum channel does not exist

The bot lacks permission to create channels, so auto-creation failed.
Please ask your server administrator to perform the following steps:

1. Open Discord Server Settings
2. Go to "Channels" → "Create Channel"
3. Channel type: "Forum"
4. Name: "kanban" (or task-board)
5. After creation, set the channel ID via environment variable:
   FORUM_SYNC_CHANNEL_ID=<channel_id>

Alternatively, grant the bot the "Manage Channels" permission to allow auto-creation.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
""",
    "ja": """\
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
""",
}


def get_admin_guide_message() -> str:
    """現在の FORUM_SYNC_LANG に応じた管理者向け案内メッセージを返す。"""
    return ADMIN_GUIDE_MESSAGES.get(_LANG, ADMIN_GUIDE_MESSAGES["en"])


# ---- i18n: forum post guidelines (Discord フォーラムの「投稿ガイドライン」= topic) ----
# Discord の topic 上限は 4096 文字。短くまとめる。
FORUM_GUIDELINES: dict[str, str] = {
    "en": (
        "🔄 This forum is auto-synced with the Kanban board by kanban-forum-sync.\n\n"
        "• Each thread = one Kanban task. Bot-created threads are titled "
        "`task-<id>: <title>` — don't rename them.\n"
        "• Status tags (Todo / Running / Blocked / Done …) mirror the task status. "
        "Change a thread's tag and the Kanban task updates to match.\n"
        "• Reply in a thread → your message is synced back as a comment on the task.\n"
        "• Start a NEW thread (any title not beginning with `task-`) → a new Kanban "
        "task is created from its title, first post, and tag.\n"
        "• Attach files in a thread → posted to the task as a link (full upload coming soon).\n"
        "• Don't post in bot threads if you don't want it mirrored to Kanban."
    ),
    "ja": (
        "🔄 このフォーラムは kanban-forum-sync により Kanban ボードと自動同期されています。\n\n"
        "• 1スレッド = 1 Kanban タスク。Bot が作るスレッドは `task-<id>: <タイトル>` "
        "という名前です（リネームしないでください）。\n"
        "• ステータスタグ（Todo / Running / Blocked / Done …）はタスクの状態と連動します。"
        "スレッドのタグを変えると Kanban タスクの状態も変わります。\n"
        "• スレッドに返信 → その内容がタスクのコメントとして同期されます。\n"
        "• 新しいスレッドを立てる（`task-` で始まらない任意のタイトル）→ タイトル・最初の"
        "投稿・タグから新しい Kanban タスクが作られます。\n"
        "• スレッドにファイル添付 → タスクにリンクとして投稿されます（本格的な取込は近日対応）。\n"
        "• Kanban に反映したくない投稿は Bot スレッドに書かないでください。"
    ),
}


def get_forum_guidelines() -> str:
    """現在の FORUM_SYNC_LANG に応じたフォーラム投稿ガイドライン（topic）を返す。"""
    return FORUM_GUIDELINES.get(_LANG, FORUM_GUIDELINES["en"])


class KanbanForumSyncer:
    """Kanban ↔ Discord Forum 同期エンジン"""

    def __init__(self, bot_token: str, channel_id: Optional[int] = None,
                 poll_interval: int = 15, use_inotify: bool = False, ctx=None):
        self.bot_token = bot_token
        self._channel_id = channel_id
        self.poll_interval = poll_interval
        self._use_inotify = use_inotify
        self._thread: Optional[Thread] = None
        self._stop_event = Event()
        self._state = SyncState()

        self.discord = DiscordForumClient(bot_token, self._channel_id)
        self.kanban = KanbanBridge(ctx=ctx)

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
            created = self.discord.create_forum_channel(
                guild_id, name="kanban", topic=get_forum_guidelines()
            )
            self._set_channel(int(created["id"]))
            logger.info("Created forum #kanban (%s)", created["id"])
            return True
        except DiscordPermissionError:
            self._state.last_error = (
                "Bot lacks 'Manage Channels' permission. "
                "Create a forum channel named 'kanban' or grant the bot 'Manage Channels', "
                f"then set FORUM_SYNC_CHANNEL_ID. Guide: {_FORUM_GUIDE_URL}"
            )
            print(get_admin_guide_message())
            return False
        except DiscordForumError as e:
            self._state.last_error = "Failed to create forum: %s" % e
            print(get_admin_guide_message())
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
        recovering_from_deletion = False

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
            except NotFoundError:
                # 設定済みチャンネルが Discord 側で削除された（404）→ abort せず
                # 自動復旧する: ギルドを再スキャンして既存 forum を探し、無ければ再生成。
                # 削除されたチャンネルから guild_id は取れないため、channel_id 未設定
                # 扱いにして全ギルド探索パスへフォールスルーする。
                logger.warning(
                    "Configured forum channel %s no longer exists on Discord "
                    "(deleted). Auto-recovering: will rediscover or recreate. "
                    "Update FORUM_SYNC_CHANNEL_ID to the new channel afterwards.",
                    self._channel_id,
                )
                recovering_from_deletion = True
                self._channel_id = None
                self.discord.channel_id = None
                # guild_id は None のまま → 下の全ギルド探索へ
            except DiscordForumError as e:
                # 一時的・権限エラーなどは再生成しない（forum 重複生成を避ける）。
                logger.warning(
                    "Configured channel %s not accessible: %s", self._channel_id, e
                )
                self._state.last_error = (
                    "Channel ID %s is not accessible. "
                    "Leave FORUM_SYNC_CHANNEL_ID unset for auto-discovery. "
                    "Guide: %s" % (self._channel_id, _FORUM_GUIDE_URL)
                )
                print(get_admin_guide_message())
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
                print(get_admin_guide_message())
                return False

            for g in guilds:
                existing = self.discord.find_forum_channel(int(g["id"]))
                if existing:
                    self._set_channel(int(existing["id"]))
                    logger.info(
                        "Found forum #%s (%s) in guild '%s'",
                        existing["name"], existing["id"], g["name"],
                    )
                    if recovering_from_deletion:
                        self._reset_state_after_forum_recovery(int(existing["id"]))
                    return True

            if not guilds:
                self._state.last_error = (
                    "Bot is not in any guild. "
                    "Invite the bot to a server first. Guide: %s" % _FORUM_GUIDE_URL
                )
                print(get_admin_guide_message())
                return False

            guild_id = int(guilds[0]["id"])
            logger.info(
                "No forum found. Attempting to create in '%s'...", guilds[0]["name"]
            )

        ok = self._find_or_create_forum_in_guild(guild_id)
        if ok and recovering_from_deletion and self._channel_id is not None:
            self._reset_state_after_forum_recovery(self._channel_id)
        return ok

    def _reset_state_after_forum_recovery(self, new_channel_id: int) -> None:
        """削除された Forum から復旧した直後の後始末。

        旧 Forum チャンネルと共にスレッドも全て消えているため、sync_map と
        thread_meta のエントリは全て stale。これらを破棄して、アクティブな
        タスクが新しい Forum に作り直されるようにする。env の
        FORUM_SYNC_CHANNEL_ID は書き換えられないので更新を促すログを出す。
        """
        self._sync_map.clear()
        self._thread_meta.clear()
        logger.warning(
            "Forum recovery complete → new channel %s. Cleared stale sync_map / "
            "thread_meta (old threads were deleted with the old forum). "
            "Set FORUM_SYNC_CHANNEL_ID=%s to pin the new forum.",
            new_channel_id, new_channel_id,
        )

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

    def _sync_attachment(self, task_id: str, author_name: str, att: dict) -> bool:
        """Discord メッセージの添付ファイル1件を Kanban に同期する。

        【暫定実装】Discord のファイル URL を kanban_comment として投稿する。

        本来は task_attachments に「Upload file」として取り込みたいが、現状の
        Hermes には添付用の toolset ツールも CLI コマンドも存在しない
        （登録ツールは show/list/complete/block/heartbeat/comment/create/
          unblock/link のみ。書き込みは kanban_db.add_attachment の直接DB
          操作しかなく、本プラグインの DB アクセス方針に反する）。
        そのため当面は唯一の実在ツール kanban_comment で URL リンクを残す。
        添付 toolset/CLI を Hermes 本体に追加する PR を申請済み:
        https://github.com/NousResearch/hermes-agent/pull/36019
        これがマージされ installed hermes-agent に入り次第、kanban_attach_url
        経由の本物の取り込みに差し替える。切替手順は ATTACHMENT_TOOLSET_PR_PLAN.md
        の "Upstream PR status" 節を参照。

        成功で True、一時的失敗で False（カーソルを進めず次サイクルで再試行）。
        """
        filename = att.get("filename", "file")
        url = att.get("url", "")
        size = att.get("size", 0)

        if not url:
            logger.warning("Attachment '%s' has no url; skipping", filename)
            return True  # 取りようがないのでカーソルは進める

        size_note = f"（{size} bytes）" if size else ""
        return self.kanban.add_comment(
            task_id, author_name,
            f"📎 Discord 添付ファイル{size_note}: {filename}\n{url}",
        )

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
                if "Resource not found" in str(e):
                    logger.warning("Thread %s no longer exists; removing from sync_map", thread_id)
                    stale_task_id = self._sync_map.get_by_thread_id(thread_id)
                    if stale_task_id:
                        self._sync_map.remove(stale_task_id)
                else:
                    logger.warning("Failed to fetch messages for thread %s: %s", thread_id, e)
                continue

            if not messages:
                continue

            messages = sorted(messages, key=lambda m: int(m["id"]))
            max_processed_id = last_id

            for msg in messages:
                msg_id = int(msg["id"])
                if msg_id <= last_id:
                    continue

                author = msg["author"]
                author_id = str(author.get("id", ""))
                author_name = author.get("username", "unknown")
                is_bot = author.get("bot", False)

                if is_bot or (bot_id and author_id == bot_id):
                    max_processed_id = max(max_processed_id, msg_id)
                    continue

                content = msg.get("content", "").strip()
                attachments = msg.get("attachments", []) or []

                msg_ok = True

                # 添付ファイルを先に同期
                for att in attachments:
                    if not self._sync_attachment(task_id, author_name, att):
                        msg_ok = False
                        break

                # テキスト本文を同期
                if msg_ok and content:
                    if self.kanban.add_comment(task_id, author_name, content):
                        new_comments += 1
                        logger.debug(
                            "Comment synced: %s → task-%s", author_name, task_id
                        )
                    else:
                        msg_ok = False

                if msg_ok:
                    max_processed_id = max(max_processed_id, msg_id)
                else:
                    logger.warning(
                        "Message sync failed for thread %s message %s; "
                        "cursor left at %s for retry",
                        thread_id, msg_id, max_processed_id,
                    )
                    break

            if max_processed_id > last_id:
                self._thread_meta.set_last_message_id(thread_id, max_processed_id)

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
                if "Resource not found" in str(e):
                    logger.warning("Thread %s no longer exists; removing from sync_map", thread_id)
                    stale_task_id = self._sync_map.get_by_thread_id(thread_id)
                    if stale_task_id:
                        self._sync_map.remove(stale_task_id)
                else:
                    logger.warning("Failed to fetch thread %s: %s", thread_id, e)
                continue

            applied = thread.get("applied_tags", [])
            if not isinstance(applied, list):
                continue

            new_status = None
            for tag_id in applied:
                tag_name = self._reverse_tag_map.get(tag_id)
                if tag_name and tag_name in TAG_TO_STATUS:
                    new_status = _normalize_kanban_status(TAG_TO_STATUS[tag_name])
                    if new_status:
                        break

            if new_status:
                current = self.kanban.get_task(task_id)
                if current and current["status"] != new_status:
                    # Don't resurrect archived/done tasks via Forum tag sync.
                    # This prevents an infinite loop where:
                    #   1. Task is completed → archived by the system
                    #   2. Archived thread still has a stale tag (e.g. Todo/Ready)
                    #   3. Tag sync reads the stale tag → changes status back to active
                    #   4. Dispatcher spawns a new worker → archived again → loop
                    if current["status"] in ("archived", "done"):
                        logger.debug(
                            "Tag sync: skipping task-%s (%s) — "
                            "won't resurrect archived/done task to %s",
                            task_id, current["status"], new_status,
                        )
                        continue
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

    def _recover_orphaned_thread(self, thread_id: int, thread_name: str) -> bool:
        """sync_map から消えた bot 作成スレッドを再登録する。

        full_sync() が sync_map.clear() した後に initial_sync() が別スレッドを
        作り直す場合など、旧スレッドが孤立（orphan）することがある。
        スレッド名 "task-{task_id}: {title}" から task_id を抽出して Kanban に
        タスクが存在すれば sync_map に再登録する。
        """
        m = re.match(r"task-(t_[0-9a-f]+)\s*:", thread_name, re.IGNORECASE)
        if not m:
            return False
        task_id = m.group(1).lower()
        task = self.kanban.get_task(task_id)
        if not task:
            logger.debug(
                "Orphan recovery: task-%s not found in Kanban for thread %s; skipping",
                task_id, thread_id,
            )
            return False
        existing = self._sync_map.get(task_id)
        if existing is not None and existing > thread_id:
            # 既存マッピングの方がスノーフレーク ID が大きい（新しい）→ 保持する
            logger.debug(
                "Orphan recovery: task-%s has newer thread %s; skipping older thread %s",
                task_id, existing, thread_id,
            )
            return False
        # スレッドが実際に Discord 上でアクセス可能か確認してからリンクする
        try:
            self.discord.get_channel_by_id(thread_id)
        except Exception as e:
            if "Resource not found" in str(e):
                logger.debug(
                    "Orphan recovery: thread %s returns 404; skipping (may be deleted)",
                    thread_id,
                )
                return False
        self._sync_map.set(task_id, thread_id)
        logger.info(
            "Orphan recovery: re-linked thread %s → task-%s ('%s')",
            thread_id, task_id, thread_name,
        )
        return True

    def _sync_forum_new_threads(self):
        """Forum チャンネルに新しく作成されたスレッドを検出し、Kanban タスクを作成する。

        同期マップに存在しないスレッドを見つけ、タイトル・本文・タグから
        新しい Kanban タスクを生成する。
        """
        if self.channel_id is None:
            return

        try:
            guild_id = self.discord.get_current_guild_id()
            active = self.discord.get_active_threads(guild_id) if guild_id else []
            # guild レベルで全スレッドを取得し、この forum チャンネルのものだけ絞り込む
            threads = [t for t in active if str(t.get("parent_id")) == str(self.channel_id)]
        except Exception as e:
            logger.warning("Failed to fetch active threads: %s", e)
            threads = []

        try:
            archived = self.discord.get_archived_public_threads()
        except Exception as e:
            logger.debug("Failed to fetch archived threads: %s", e)
            archived = []

        all_threads = list({int(t["id"]): t for t in threads + archived}.values())
        if not all_threads:
            return

        new_count = 0
        for thread in all_threads:
            thread_id = int(thread["id"])
            thread_name = thread.get("name", "").strip()

            # Bot が作ったスレッド（task- プレフィックス）は通常スキップ。
            # ただし sync_map に存在しない場合は orphan 回復を試みる。
            if thread_name.lower().startswith("task-"):
                if not self._sync_map.contains_thread(thread_id):
                    self._recover_orphaned_thread(thread_id, thread_name)
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
                        normalized = _normalize_kanban_status(resolved)
                        if normalized:
                            status = normalized
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
                    if "Resource not found" in str(e):
                        logger.warning("Thread %s not found while posting comment; removing stale mapping", thread_id)
                        stale_task_id = self._sync_map.get_by_thread_id(thread_id)
                        if stale_task_id:
                            self._sync_map.remove(stale_task_id)
                    else:
                        logger.warning("Failed to post comment %s to thread %s: %s", c["id"], thread_id, e)
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
                        self.discord.send_message(thread_id, text[:2000])
                        posted += 1
                        time.sleep(0.5)
                    self._thread_meta.set_last_kanban_event_id(thread_id, ev["id"])
                except Exception as e:
                    if "Resource not found" in str(e):
                        logger.warning(
                            "Thread %s not found while posting event %s; removing stale mapping",
                            thread_id, ev["id"],
                        )
                        stale_task_id = self._sync_map.get_by_thread_id(thread_id)
                        if stale_task_id:
                            self._sync_map.remove(stale_task_id)
                    else:
                        logger.warning("Failed to post event %s to thread %s: %s", ev["id"], thread_id, e)
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
                # アーカイブ済み/完了タスクには新スレッドを作らない。
                # 既に終わったタスク（特にユーザーが Discord 側で削除した
                # スレッド）を毎回作り直してしまう不具合を防ぐ。スレッドを持つ
                # まま done/archived になったタスクは update パスでアーカイブされる。
                if task["status"] in ARCHIVE_STATUSES:
                    logger.debug(
                        "Skipping thread creation for archived/done task-%s", task_id
                    )
                    return True
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
                if tag_ids and not self._origin_tracker.is_forum_sourced(task_id):
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
        except NotFoundError:
            # create パス（thread_id is None）での 404 は「親 Forum チャンネルが
            # 消えている」ことを意味する。スレッドではなく Forum 自体の問題なので
            # 再帰してはいけない（無限再帰 → RecursionError になる）。Forum の
            # 復旧は incremental_sync 冒頭の _ensure_channel_alive に任せる。
            if thread_id is None:
                logger.warning(
                    "create_thread 404 for task-%s — Forum channel likely deleted; "
                    "skipping this cycle (channel self-heal will handle it).",
                    task_id,
                )
                return False
            # ここから先は update パスでの 404 = スレッドだけが削除されたケース。
            self._sync_map.remove(task_id)
            # アーカイブ済み/完了タスクは再作成しない（ユーザーが意図的に削除した
            # 終了済みスレッドを作り直さない）。アクティブなタスクのみ再作成する。
            if task["status"] in ARCHIVE_STATUSES:
                logger.info(
                    "Thread %s for archived/done task-%s not found; "
                    "removing stale mapping without re-creating",
                    thread_id, task_id,
                )
                return True
            logger.warning(
                "Thread %s for task-%s not found; removing stale mapping and re-creating",
                thread_id, task_id,
            )
            # 再帰は1段のみ（thread_id=None の create パスに進む）。create が再び
            # 404 を返しても上の `if thread_id is None` で止まるため無限再帰しない。
            return self._sync_task_to_forum(task)
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
        self._state.last_event_id = self.kanban.get_latest_event_id()
        self._state.last_sync = str(int(time.time()))
        logger.info("Initial sync complete: %d synced, %d errors", synced, errors)

    def _ensure_channel_alive(self) -> bool:
        """稼働中に Forum チャンネルが削除されていないか確認し、削除されていたら
        ランタイムで self-heal（再解決・再生成）する。

        _resolve_forum_channel は起動時に1回だけ走るため、長時間動いている watcher は
        稼働後に削除されたチャンネルを掴んだまま 404 を出し続ける。毎ポーリングで
        軽量に生存確認し、404 なら再解決して新しい Forum に追従する。

        Returns: True=チャンネル利用可能, False=復旧できず（このサイクルはスキップ）
        """
        if self._channel_id is not None:
            try:
                channel = self.discord.get_channel()
                if channel.get("type") == FORUM_CHANNEL_TYPE:
                    return True
                # 型が変わった等 → 再解決に委ねる
            except NotFoundError:
                logger.warning(
                    "Forum channel %s disappeared while running; re-resolving (self-heal).",
                    self._channel_id,
                )
            except DiscordForumError as e:
                # 一時的エラーは再解決せずこのサイクルだけ様子見
                logger.debug("Channel health check transient error: %s", e)
                return True

        old_channel = self._channel_id
        ok = self._resolve_forum_channel()
        if ok and self._channel_id != old_channel:
            # 新しい Forum に切り替わった → タグを作り直し、タグマップを再構築する
            # （旧 Forum のタグIDは無効。これを怠るとタグ同期・新規スレッド作成が壊れる）
            logger.info(
                "Switched to forum %s at runtime; rebuilding tags.", self._channel_id
            )
            try:
                self._ensure_tags()
                self._build_tag_map()
            except Exception as e:
                logger.error("Failed to rebuild tags after channel switch: %s", e)
                return False
        return ok

    def incremental_sync(self):
        """増分同期（1回のポーリング）

        Phase 1: Kanban DB の変更を検出 → Forum に反映
        Phase 2: Forum の変更を検出 → Kanban にフィードバック"""
        # 稼働中のチャンネル削除に追従（self-heal）。復旧できなければ今回はスキップ。
        if not self._ensure_channel_alive():
            logger.warning("Forum channel unavailable this cycle; skipping sync.")
            return

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

    def _prepare_initial_sync(self) -> bool:
        delay = max(1, min(self.poll_interval, 30))
        while not self._stop_event.is_set():
            if not self._resolve_forum_channel():
                self._state.state = "error"
                logger.error(
                    "Forum channel resolution failed. Retrying in %ss.", delay
                )
                self._stop_event.wait(delay)
                delay = min(delay * 2, 300)
                continue

            try:
                self.initial_sync()
                self._state.state = "running"
                return True
            except Exception as e:
                logger.error("Initial sync failed: %s", e, exc_info=True)
                self._state.state = "error"
                self._state.last_error = str(e)
                self._stop_event.wait(delay)
                delay = min(delay * 2, 300)
        return False

    def _run_loop_poll(self):
        """固定間隔ポーリングループ"""
        if not self._prepare_initial_sync():
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
        if not self._prepare_initial_sync():
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
        """手動フル同期（CLI/スラッシュコマンド用）

        sync_map は消さずに initial_sync() を呼ぶ。既存マッピングがあるタスクは
        update パスで処理されるため Discord に重複スレッドを作らない。
        404 になったスレッドは _sync_task_to_forum() 内の NotFoundError ハンドラが
        sync_map から除去して自動再作成する。
        """
        if not self._resolve_forum_channel():
            return
        self._ensure_tags()
        self._build_tag_map()
        self.initial_sync()
