"""Kanban ↔ Discord Forum 同期のコアロジック。
task_events ベースの変更検出でポーリング。"""

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

# Discord tag emoji for each status
STATUS_TAG_EMOJI = {
    "Triage": "🩺",
    "Todo": "📝",
    "Scheduled": "📅",
    "Ready": "✅",
    "Running": "🔄",
    "Blocked": "🚧",
    "Review": "👀",
    "Done": "🎉",
}


def _make_tag_dict(name: str) -> dict:
    """Forum タグ用の dict を生成"""
    tag = {"name": name, "moderated": False}
    emoji = STATUS_TAG_EMOJI.get(name)
    if emoji:
        tag["emoji_name"] = emoji
    return tag


REQUIRED_TAGS = [_make_tag_dict(n) for n in STATUS_TAG_EMOJI]

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
                 poll_interval: int = 15):
        self.bot_token = bot_token
        self._channel_id = channel_id
        self.poll_interval = poll_interval
        self._thread: Optional[Thread] = None
        self._stop_event = Event()
        self._state = SyncState()

        self.discord = DiscordForumClient(bot_token, self._channel_id)
        self.kanban = KanbanBridge()

        # {kanban_task_id → discord_thread_id}
        self._sync_map = SyncMap()

        # {tag_name → tag_id}
        self._tag_map: dict[str, int] = {}

    @property
    def channel_id(self) -> Optional[int]:
        return self._channel_id

    def get_state(self) -> SyncState:
        return self._state

    # ---- Forum channel auto-resolution ----

    def _resolve_forum_channel(self) -> bool:
        """Forum チャンネルを解決する。
        1. channel_id が設定されていて Forum なら OK
        2. channel_id 未設定 → Bot の参加全サーバーを探索
        3. 非Forumチャンネル指定 → 同サーバー内を検索/作成
        4. 見つからない/作成不可 → 管理者向け案内表示して False

        Returns: True=解決成功, False=解決失敗
        """
        FORUM_GUIDE_URL = "https://support.discord.com/hc/ja/articles/6208479917079"

        guild_id = None

        # --- 1. channel_id が設定されている場合 ---
        if self._channel_id is not None:
            try:
                channel = self.discord.get_channel()
                if channel.get("type") == FORUM_CHANNEL_TYPE:
                    logger.info(
                        f"Channel #{channel['name']} "
                        f"({self._channel_id}) is a valid forum"
                    )
                    return True
                # 存在するがForumではない → 同サーバー内検索へ
                guild_id = int(channel["guild_id"])
                logger.info(
                    f"Channel #{channel['name']} is type="
                    f"{channel.get('type')} (not forum). "
                    f"Searching guild {guild_id}..."
                )
            except (NotFoundError, DiscordForumError) as e:
                logger.warning(
                    f"Configured channel {self._channel_id} "
                    f"not accessible: {e}"
                )
                self._state.last_error = (
                    f"Channel ID {self._channel_id} is not accessible.\n"
                    "Please check the channel ID or leave it unset "
                    "for auto-discovery.\n"
                    f"作成ガイド: {FORUM_GUIDE_URL}"
                )
                print(ADMIN_GUIDE_MESSAGE)
                return False

        # --- 2. guild_id が不明（channel_id 未設定 or 検出失敗） ---
        if guild_id is None:
            logger.info("No channel_id set. Scanning bot's guilds...")
            try:
                guilds = self.discord.get_bot_guilds()
            except DiscordForumError as e:
                self._state.last_error = (
                    f"Cannot list guilds: {e}\n"
                    "Bot may lack 'guilds' OAuth2 scope.\n"
                    "Set FORUM_SYNC_CHANNEL_ID explicitly."
                )
                print(ADMIN_GUIDE_MESSAGE)
                return False

            # 全サーバーの Forum を検索
            for g in guilds:
                gid = int(g["id"])
                existing = self.discord.find_forum_channel(gid)
                if existing:
                    new_id = int(existing["id"])
                    logger.info(
                        f"Found forum #{existing['name']} ({new_id}) "
                        f"in guild '{g['name']}' ({gid})"
                    )
                    self._channel_id = new_id
                    self.discord.channel_id = new_id
                    return True

            # 全サーバーに Forum なし → 作成試行
            if guilds:
                first = guilds[0]
                gid = int(first["id"])
                logger.info(
                    f"No forum found. Attempting to create "
                    f"in '{first['name']}' ({gid})..."
                )
                try:
                    created = self.discord.create_forum_channel(
                        gid, name="kanban"
                    )
                    new_id = int(created["id"])
                    logger.info(f"Created forum #kanban ({new_id})")
                    self._channel_id = new_id
                    self.discord.channel_id = new_id
                    return True
                except PermissionError:
                    self._state.last_error = (
                        "Bot lacks 'Manage Channels' permission.\n"
                        f"作成ガイド: {FORUM_GUIDE_URL}\n\n"
                        "Ask a server admin to:\n"
                        "1. Create a forum channel named 'kanban'\n"
                        "2. Grant 'Manage Channels' to the bot\n"
                        "3. Or set FORUM_SYNC_CHANNEL_ID explicitly"
                    )
                    print(ADMIN_GUIDE_MESSAGE)
                    return False
                except DiscordForumError as e:
                    self._state.last_error = (
                        f"Failed to create forum: {e}"
                    )
                    print(ADMIN_GUIDE_MESSAGE)
                    return False

            # Bot がどのサーバーにも参加していない
            self._state.last_error = (
                "Bot is not in any guild. "
                "Invite the bot to a server first.\n"
                f"作成ガイド: {FORUM_GUIDE_URL}"
            )
            print(ADMIN_GUIDE_MESSAGE)
            return False

        # --- 3. guild_id 確定済み：同サーバー内で検索/作成 ---
        existing = self.discord.find_forum_channel(guild_id)
        if existing:
            new_id = int(existing["id"])
            logger.info(
                f"Switching to forum #{existing['name']} ({new_id})"
            )
            self._channel_id = new_id
            self.discord.channel_id = new_id
            return True

        # 同サーバーに Forum なし → 作成試行
        try:
            created = self.discord.create_forum_channel(
                guild_id, name="kanban"
            )
            new_id = int(created["id"])
            logger.info(f"Created forum #kanban ({new_id})")
            self._channel_id = new_id
            self.discord.channel_id = new_id
            return True
        except PermissionError:
            self._state.last_error = (
                "Bot lacks 'Manage Channels' permission.\n"
                f"作成ガイド: {FORUM_GUIDE_URL}\n\n"
                "Ask a server admin to:\n"
                "1. Create a forum channel named 'kanban'\n"
                "2. Grant 'Manage Channels' to the bot\n"
                "3. Or set FORUM_SYNC_CHANNEL_ID explicitly"
            )
            print(ADMIN_GUIDE_MESSAGE)
            return False
        except DiscordForumError as e:
            self._state.last_error = f"Failed to create forum: {e}"
            print(ADMIN_GUIDE_MESSAGE)
            return False

    # ---- Tag management ----

    def _build_tag_map(self):
        """Forum のタグ一覧を取得し、{tag_name → tag_id} マップを構築"""
        tags = self.discord.get_tags()
        self._tag_map = {t["name"]: t["id"] for t in tags}
        logger.info(
            f"Tag map built with {len(self._tag_map)} tags: "
            f"{list(self._tag_map.keys())}"
        )

    def _ensure_tags(self):
        """必要な Forum タグが存在するか確認し、不足があれば作成する"""
        tags = self.discord.get_tags()
        existing_names = {t["name"] for t in tags}
        missing = [t for t in REQUIRED_TAGS if t["name"] not in existing_names]

        if missing:
            logger.info(
                f"Creating {len(missing)} missing tags: "
                f"{[t['name'] for t in missing]}"
            )
            new_tags = tags + missing
            self.discord.create_tags(new_tags)
            time.sleep(1)  # API反映待ち
        else:
            logger.info("All 8 status tags already exist")

    def _resolve_tag_ids(self, status: str) -> list[int]:
        """ステータスに対応する tag_id のリストを返す"""
        tag_name = STATUS_TO_TAG.get(status)
        if tag_name and tag_name in self._tag_map:
            return [self._tag_map[tag_name]]
        return []

    # ---- Thread content generation ----

    def _thread_title(self, task: dict) -> str:
        """タスクからスレッド名を生成"""
        return f"task-{task['id']}: {task['title']}"

    def _thread_content(self, task: dict) -> str:
        """タスクから初期スレッドメッセージを生成"""
        lines = [
            f"**{task['title']}**",
            f"Status: **{task['status']}**",
        ]

        if task.get("body"):
            lines.append(f"\n{task['body']}")

        details = []
        details.append(f"Priority: {task.get('priority', '—')}")
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
                # 新規タスク → スレッド作成
                tag_ids = self._resolve_tag_ids(task["status"])
                result = self.discord.create_thread(
                    name=self._thread_title(task),
                    content=self._thread_content(task),
                    applied_tags=tag_ids,
                )
                new_thread_id = result.get("id")
                if new_thread_id:
                    self._sync_map.set(task_id, new_thread_id)
                    logger.info(
                        f"Created thread for task-{task_id}: {new_thread_id}"
                    )
                    return True
                logger.error(f"create_thread returned no id for task-{task_id}")
                return False
            else:
                # 既存タスク → 差分更新
                kwargs = {}
                kwargs["name"] = self._thread_title(task)

                tag_ids = self._resolve_tag_ids(task["status"])
                if tag_ids:
                    kwargs["applied_tags"] = tag_ids

                if task["status"] in ARCHIVE_STATUSES:
                    kwargs["archived"] = True
                    kwargs["locked"] = False

                if kwargs:
                    self.discord.update_thread(thread_id, **kwargs)
                    logger.info(f"Updated thread for task-{task_id}")
                return True
        except Exception as e:
            logger.error(f"Failed to sync task-{task_id}: {e}")
            return False

    def initial_sync(self):
        """初回フル同期"""
        logger.info("Starting initial sync...")
        self._ensure_tags()
        # タグマップを再構築（新規作成タグの ID を反映）
        self._build_tag_map()

        tasks = self.kanban.get_all_tasks()
        synced = 0
        errors = 0

        for task in tasks:
            if self._sync_task_to_forum(task):
                synced += 1
            else:
                errors += 1
            # レート制限対策: タスク間で最低 0.5 秒待機
            time.sleep(0.5)

        self._state.task_count = synced
        self._state.error_count = errors
        self._state.last_sync = str(int(time.time()))
        logger.info(
            f"Initial sync complete: {synced} synced, {errors} errors"
        )

    def incremental_sync(self):
        """増分同期（1回のポーリング）"""
        last_id = self._state.last_event_id
        changed = self.kanban.get_tasks_changed_since_event(last_id)

        if changed:
            logger.info(
                f"Incremental sync: {len(changed)} changed tasks "
                f"(events > {last_id})"
            )
            for task in changed:
                self._sync_task_to_forum(task)
                time.sleep(0.5)

        # Poll 最新のイベント ID を記録
        latest_event = self.kanban.get_latest_event_id()
        if latest_event > self._state.last_event_id:
            self._state.last_event_id = latest_event
            logger.debug(f"Updated last_event_id to {latest_event}")

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
        # 起動時に Forum チャンネルを解決
        if not self._resolve_forum_channel():
            self._state.state = "error"
            logger.error("Forum channel resolution failed. Sync aborted.")
            return

        try:
            self.initial_sync()
        except Exception as e:
            logger.error(f"Initial sync failed: {e}", exc_info=True)
            self._state.state = "error"
            self._state.last_error = str(e)
            return

        while not self._stop_event.is_set():
            try:
                self.incremental_sync()
            except Exception as e:
                logger.error(f"Incremental sync failed: {e}", exc_info=True)
                self._state.last_error = str(e)
            self._stop_event.wait(self.poll_interval)

    def full_sync(self):
        """手動フル同期（CLI/スラッシュコマンド用）"""
        # チャンネル解決もやり直す
        if not self._resolve_forum_channel():
            return
        self._ensure_tags()
        self._build_tag_map()
        # 既存の同期マップをクリアして再同期
        for task_id in list(self._sync_map.items().keys()):
            self._sync_map.remove(task_id)
        self.initial_sync()
