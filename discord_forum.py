"""Discord Forum Channel REST API 操作モジュール。
Bot Token 認証で Forum スレッドを作成・更新・アーカイブする。"""

import json
import os
import ssl
import time
import logging
import random
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


class RateLimitError(DiscordForumError):
    """429 の再試行を使い切った場合のエラー。"""
    pass


class DiscordForumClient:
    """Discord Forum API クライアント"""
    _MIN_REQUEST_INTERVAL = 0.25
    _MAX_HARD_RETRIES = 3
    _MAX_RATE_RETRIES = 8
    _MAX_RATE_WAIT = 60.0

    def __init__(self, bot_token: str, channel_id: Optional[int] = None):
        self.token = bot_token
        self.channel_id = channel_id
        self._last_request_ts = 0.0
        self._next_allowed_ts = 0.0
        self._headers = {
            "Authorization": f"Bot {bot_token}",
            "Content-Type": "application/json",
            "User-Agent": "HermesKanbanForumSync/1.0",
        }

    # ---- Low-level request ----

    def _parse_retry_after(self, headers, error_body: str) -> float:
        body_wait = 0.0
        header_wait = 0.0

        try:
            retry_info = json.loads(error_body) if error_body else {}
        except json.JSONDecodeError:
            retry_info = {}

        try:
            body_wait = float(retry_info.get("retry_after", 0) or 0)
        except (TypeError, ValueError):
            body_wait = 0.0

        try:
            header_wait = float(headers.get("Retry-After", 0) or 0)
        except (TypeError, ValueError):
            header_wait = 0.0

        wait = max(header_wait, body_wait, self._MIN_REQUEST_INTERVAL)
        wait *= random.uniform(0.9, 1.1)
        return min(wait, self._MAX_RATE_WAIT)

    def _note_ratelimit_headers(self, headers) -> None:
        remaining = headers.get("X-RateLimit-Remaining")
        reset_after = headers.get("X-RateLimit-Reset-After")
        if remaining != "0" or not reset_after:
            return
        try:
            wait = max(float(reset_after), 0.0)
        except (TypeError, ValueError):
            return
        self._next_allowed_ts = max(self._next_allowed_ts, time.monotonic() + wait)

    def _wait_for_request_slot(self) -> None:
        now = time.monotonic()
        wait = max(
            self._next_allowed_ts - now,
            self._last_request_ts + self._MIN_REQUEST_INTERVAL - now,
        )
        if wait > 0:
            time.sleep(wait)
        self._last_request_ts = time.monotonic()

    def _request(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        url = f"{BASE_URL}{path}"
        # Defense-in-depth: every request must target the Discord REST API over
        # https. `path` is built internally from snowflake IDs today, but this
        # guard guarantees a future/untrusted `path` can never redirect urlopen
        # to file://, http://, or another host.
        if not url.startswith(BASE_URL + "/") and url != BASE_URL:
            raise DiscordForumError(f"refusing non-Discord URL: {url}")
        data = json.dumps(body).encode() if body else None
        hard_attempt = 0
        rate_attempt = 0

        while True:
            self._wait_for_request_slot()
            req = Request(url, data=data, headers=self._headers, method=method)
            try:
                # URL is guarded above to start with BASE_URL (https://discord.com),
                # so no file://, http://, or host swap can reach urlopen.
                with urlopen(req, timeout=30, context=_SSL_CONTEXT) as resp:  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
                    self._note_ratelimit_headers(resp.headers)
                    raw = resp.read().decode()
                    if not raw:
                        return {}
                    return json.loads(raw)
            except HTTPError as e:
                error_body = e.read().decode()
                if e.code == 429:
                    rate_attempt += 1
                    retry_after = self._parse_retry_after(e.headers, error_body)
                    is_global = (
                        e.headers.get("X-RateLimit-Scope") == "global"
                        or e.headers.get("X-RateLimit-Global") == "true"
                    )
                    logger.warning(
                        "Rate limited%s (attempt %d/%d), retrying in %.2fs: %s",
                        " [GLOBAL]" if is_global else "",
                        rate_attempt, self._MAX_RATE_RETRIES, retry_after, path,
                    )
                    self._next_allowed_ts = max(
                        self._next_allowed_ts, time.monotonic() + retry_after
                    )
                    if rate_attempt >= self._MAX_RATE_RETRIES:
                        raise RateLimitError(
                            "Rate limit retries exhausted: %s" % path,
                            http_code=429,
                            body=error_body,
                        )
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
                if e.code >= 500:
                    hard_attempt += 1
                    if hard_attempt < self._MAX_HARD_RETRIES:
                        wait = min(2 ** hard_attempt, 10)
                        logger.warning(
                            "Discord API %s on %s (attempt %d/%d), retrying in %ss",
                            e.code, path, hard_attempt, self._MAX_HARD_RETRIES, wait,
                        )
                        time.sleep(wait)
                        continue
                logger.error("Discord API error %s: %s", e.code, error_body)
                raise DiscordForumError(
                    "API error %s: %s" % (e.code, error_body), http_code=e.code, body=error_body
                )
            except Exception as e:
                hard_attempt += 1
                if hard_attempt < self._MAX_HARD_RETRIES:
                    wait = min(2 ** hard_attempt, 10)
                    logger.warning(
                        "Request failed for %s (attempt %d/%d), retrying in %ss: %s",
                        path, hard_attempt, self._MAX_HARD_RETRIES, wait, e,
                    )
                    time.sleep(wait)
                    continue
                raise DiscordForumError(
                    "Request failed after %d retries: %s" % (self._MAX_HARD_RETRIES, path)
                ) from e

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
                             name: str = "kanban",
                             topic: Optional[str] = None) -> dict:
        """新しい Forum チャンネルを作成（type=15）。

        ``topic`` は Discord フォーラムの「投稿ガイドライン」。未指定なら簡易説明。
        """
        body = {
            "name": name,
            "type": FORUM_CHANNEL_TYPE,
            "topic": topic or "Kanban task board (auto-created by kanban-forum-sync)",
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

    def get_thread_messages(self, thread_id: int, after: Optional[int] = None,
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

    def get_archived_public_threads(self, limit: int = 50) -> list[dict]:
        """Forum チャンネルのアーカイブ済み公開スレッド一覧を取得。

        Discord API v10: GET /channels/{channel_id}/threads/archived/public
        """
        if self.channel_id is None:
            raise ValueError("channel_id is not set")
        result = self._request(
            "GET",
            f"/channels/{self.channel_id}/threads/archived/public?limit={limit}",
        )
        return result.get("threads", [])
