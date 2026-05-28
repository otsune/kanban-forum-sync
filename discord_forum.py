"""Discord Forum Channel REST API 操作モジュール。
Bot Token 認証で Forum スレッドを作成・更新・アーカイブする。"""

import json
import time
import logging
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from urllib.parse import urlencode

BASE_URL = "https://discord.com/api/v10"

logger = logging.getLogger(__name__)


class DiscordForumClient:
    """Discord Forum API クライアント"""

    def __init__(self, bot_token: str, channel_id: int):
        self.token = bot_token
        self.channel_id = channel_id
        self._headers = {
            "Authorization": f"Bot {bot_token}",
            "Content-Type": "application/json",
            "User-Agent": "HermesKanbanForumSync/1.0",
        }

    def _request(self, method: str, path: str, body: dict = None) -> dict:
        url = f"{BASE_URL}{path}"
        data = json.dumps(body).encode() if body else None
        req = Request(url, data=data, headers=self._headers, method=method)
        max_retries = 3
        for attempt in range(max_retries):
            try:
                with urlopen(req) as resp:
                    return json.loads(resp.read().decode())
            except HTTPError as e:
                if e.code == 429:
                    retry_info = json.loads(e.read().decode())
                    retry_after = retry_info.get("retry_after", 1)
                    logger.warning(
                        f"Rate limited (attempt {attempt+1}/{max_retries}), "
                        f"retrying in {retry_after}s"
                    )
                    time.sleep(retry_after)
                    continue
                error_body = e.read().decode()
                logger.error(f"Discord API error {e.code}: {error_body}")
                raise
        raise RuntimeError(f"Request failed after {max_retries} retries: {path}")

    # ---- Channel-level operations ----

    def get_channel(self) -> dict:
        """Forum チャンネルの情報を取得"""
        return self._request("GET", f"/channels/{self.channel_id}")

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

    def get_active_threads(self) -> list[dict]:
        """Forum のアクティブなスレッド一覧を取得"""
        result = self._request(
            "GET", f"/channels/{self.channel_id}/threads/active"
        )
        return result.get("threads", [])
