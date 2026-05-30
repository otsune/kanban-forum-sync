"""Discord Forum Channel REST API 操作モジュール。
Bot Token 認証で Forum スレッドを作成・更新・アーカイブする。"""

import json
import os
import ssl
import time
import logging
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from urllib.parse import urlencode
from typing import Optional

BASE_URL = "https://discord.com/api/v10"
FORUM_CHANNEL_TYPE = 15

logger = logging.getLogger(__name__)

# Homebrew Python は独自の OpenSSL を使うため CA バンドルが不完全な場合がある。
# システムの CA ファイルが存在すれば明示的に使用する。
_SYSTEM_CA = "/etc/ssl/certs/ca-certificates.crt"
_SSL_CONTEXT: ssl.SSLContext | None = None
if os.path.exists(_SYSTEM_CA):
    _SSL_CONTEXT = ssl.create_default_context(cafile=_SYSTEM_CA)


class DiscordForumError(RuntimeError):
    """Discord API エラー。code 属性で HTTP ステータスを保持。"""
    def __init__(self, message: str, http_code: int = 0, body: str = ""):
        super().__init__(message)
        self.http_code = http_code
        self.body = body


class DiscordPermissionError(DiscordForumError):
    """Bot に権限がない場合のエラー（HTTP 403）"""
    pass


class NotFoundError(DiscordForumError):
    """リソースが見つからない場合のエラー（HTTP 404）"""
    pass


class DiscordForumClient:
    """Discord Forum API クライアント"""

    def __init__(self, bot_token: str, channel_id: Optional[int] = None):
        self.token = bot_token
        self.channel_id = channel_id
        self._headers = {
            "Authorization": f"Bot {bot_token}",
            "Content-Type": "application/json",
            "User-Agent": "HermesKanbanForumSync/1.0",
        }

    # ---- Low-level request ----

    def _request(self, method: str, path: str, body: dict = None) -> dict:
        url = f"{BASE_URL}{path}"
        data = json.dumps(body).encode() if body else None
        req = Request(url, data=data, headers=self._headers, method=method)
        max_retries = 3
        for attempt in range(max_retries):
            try:
                with urlopen(req, timeout=30, context=_SSL_CONTEXT) as resp:
                    raw = resp.read().decode()
                    if not raw:
                        return {}
                    return json.loads(raw)
            except HTTPError as e:
                error_body = e.read().decode()
                if e.code == 429:
                    retry_info = {}
                    try:
                        retry_info = json.loads(error_body)
                    except json.JSONDecodeError:
                        pass
                    retry_after = retry_info.get("retry_after", 1)
                    logger.warning(
                        "Rate limited (attempt %d/%d), retrying in %ss",
                        attempt + 1, max_retries, retry_after,
                    )
                    time.sleep(retry_after)
                    continue
                if e.code == 403:
                    raise DiscordPermissionError(
                        "Bot lacks permission: %s" % error_body,
                        http_code=403, body=error_body
                    )
                if e.code == 404:
                    raise NotFoundError(
                        "Resource not found: %s" % path, http_code=404, body=error_body
                    )
                logger.error("Discord API error %s: %s", e.code, error_body)
                raise DiscordForumError(
                    "API error %s: %s" % (e.code, error_body), http_code=e.code, body=error_body
                )
        raise DiscordForumError("Request failed after %d retries: %s" % (max_retries, path))

    # ---- Channel / Guild operations ----

    def get_channel(self) -> dict:
        """Forum チャンネルの情報を取得"""
        if self.channel_id is None:
            raise ValueError("channel_id is not set")
        return self._request("GET", f"/channels/{self.channel_id}")

    def get_channel_by_id(self, channel_id: int) -> dict:
        """任意のチャンネル情報を取得"""
        return self._request("GET", f"/channels/{channel_id}")

    def get_guild_channels(self, guild_id: int) -> list[dict]:
        """サーバーの全チャンネル一覧を取得"""
        return self._request("GET", f"/guilds/{guild_id}/channels")

    def get_current_guild_id(self) -> Optional[int]:
        """設定済みチャンネルからサーバーIDを取得"""
        channel = self.get_channel()
        gid = channel.get("guild_id")
        return int(gid) if gid else None

    def get_bot_guilds(self) -> list[dict]:
        """Bot が参加しているサーバー一覧を取得"""
        return self._request("GET", "/users/@me/guilds")

    FORUM_CANDIDATE_NAMES = ["kanban", "task-board", "task_board", "tasks"]

    def find_forum_channel(self, guild_id: int) -> Optional[dict]:
        """サーバー内で Forum チャンネルを名前で検索。
        候補名: kanban, task-board, task_board, tasks の順で検索。"""
        channels = self.get_guild_channels(guild_id)
        forums = [ch for ch in channels if ch.get("type") == FORUM_CHANNEL_TYPE]

        # 名前優先マッチ
        for name in self.FORUM_CANDIDATE_NAMES:
            for ch in forums:
                if ch["name"].lower() == name:
                    logger.info("Found forum channel #%s (%s)", ch["name"], ch["id"])
                    return ch

        if forums:
            logger.info(
                "No named forum found, using first available: #%s (%s)",
                forums[0]["name"], forums[0]["id"],
            )
            return forums[0]

        return None

    def create_forum_channel(self, guild_id: int,
                             name: str = "kanban") -> dict:
        """新しい Forum チャンネルを作成（type=15）。"""
        body = {
            "name": name,
            "type": FORUM_CHANNEL_TYPE,
            "topic": "Kanban task board (auto-created by kanban-forum-sync)",
            "default_auto_archive_duration": 10080,  # 7 days
            "default_forum_layout": 0,
            "default_sort_order": 0,  # latest activity
        }
        logger.info("Creating forum channel #%s in guild %s...", name, guild_id)
        return self._request("POST", f"/guilds/{guild_id}/channels", body)

    # ---- Tags ----

    def get_tags(self) -> list[dict]:
        """Forum チャンネルのタグ一覧を取得"""
        channel = self.get_channel()
        return channel.get("available_tags", [])

    def create_tags(self, tags: list[dict]) -> list[dict]:
        """Forum チャンネルのタグを一括設定。
        tags: [{"name": str, "moderated": bool, "emoji_name": Optional[str]}, ...]
        Discord API は available_tags 全体を置き換える PATCH が必要。"""
        channel = self._request(
            "PATCH", f"/channels/{self.channel_id}",
            {"available_tags": tags}
        )
        return channel.get("available_tags", [])

    # ---- Thread operations ----

    def create_thread(self, name: str, content: str,
                      applied_tags: list[int]) -> dict:
        """Forum にスレッドを作成"""
        return self._request("POST", f"/channels/{self.channel_id}/threads", {
            "name": name,
            "message": {"content": content},
            "applied_tags": applied_tags,
        })

    def update_thread(self, thread_id: int, **kwargs) -> dict:
        """スレッドのプロパティを更新。
        有効なキーワード: name, archived, locked, applied_tags, rate_limit_per_user
        Discord API: PATCH /channels/{thread_id}"""
        if not kwargs:
            return {}
        return self._request("PATCH", f"/channels/{thread_id}", kwargs)

    def archive_thread(self, thread_id: int) -> dict:
        """スレッドをアーカイブ"""
        return self.update_thread(thread_id, archived=True, locked=False)

    def delete_thread(self, thread_id: int) -> dict:
        """スレッドを削除"""
        return self._request("DELETE", f"/channels/{thread_id}")

    def send_message(self, thread_id: int, content: str) -> dict:
        """スレッドにメッセージを投稿"""
        return self._request("POST", f"/channels/{thread_id}/messages", {
            "content": content,
        })

    def get_thread_messages(self, thread_id: int, after: int = None,
                            limit: int = 50) -> list[dict]:
        """スレッドのメッセージ一覧を取得（フィードバック用 Phase 2）"""
        params = {"limit": min(limit, 100)}
        if after:
            params["after"] = str(after)
        path = f"/channels/{thread_id}/messages?{urlencode(params)}"
        return self._request("GET", path)

    def get_active_threads(self, guild_id: int) -> list[dict]:
        """サーバーのアクティブなスレッド一覧を取得 (Discord API v10)"""
        result = self._request("GET", f"/guilds/{guild_id}/threads/active")
        return result.get("threads", [])

    def get_channel_active_threads(self) -> list[dict]:
        """Forum チャンネルのアクティブなスレッド一覧を取得。

        ``self.channel_id`` が必要。Forum に新規スレッドができたか検出するために使用。
        Discord API v10: GET /channels/{channel_id}/threads/active
        """
        if self.channel_id is None:
            raise ValueError("channel_id is not set")
        result = self._request(
            "GET", f"/channels/{self.channel_id}/threads/active"
        )
        return result.get("threads", [])
