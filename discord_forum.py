"""
Discord Forum Channel REST API 操作モジュール。
Bot Token 認証で Forum スレッドを作成・更新・アーカイブする。
"""

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
        try:
            with urlopen(req) as resp:
                return json.loads(resp.read().decode())
        except HTTPError as e:
            if e.code == 429:  # Rate limited
                retry_after = json.loads(e.read().decode()).get("retry_after", 1)
                logger.warning(f"Rate limited, retrying in {retry_after}s")
                time.sleep(retry_after)
                return self._request(method, path, body)
            logger.error(f"Discord API error {e.code}: {e.read().decode()}")
            raise

    def get_tags(self) -> list[dict]:
        """Forum チャンネルのタグ一覧を取得"""
        channel = self._request("GET", f"/channels/{self.channel_id}")
        return channel.get("available_tags", [])

    def create_thread(self, name: str, content: str, tag_ids: list[int]) -> dict:
        """Forum にスレッドを作成"""
        return self._request("POST", f"/channels/{self.channel_id}/threads", {
            "name": name,
            "message": {"content": content},
            "applied_tags": tag_ids,
        })

    def update_thread(self, thread_id: int, name: str = None,
                      tag_ids: list[int] = None,
                      archived: bool = None, locked: bool = None) -> dict:
        """スレッドのプロパティを更新"""
        body = {}
        if name is not None:
            body["name"] = name
        if tag_ids is not None:
            body["applied_tags"] = tag_ids
        if archived is not None:
            body["archived"] = archived
            body["locked"] = locked if locked is not None else False
        return self._request("PATCH", f"/channels/{thread_id}", body)

    def archive_thread(self, thread_id: int) -> dict:
        """スレッドをアーカイブ"""
        return self.update_thread(thread_id, archived=True)

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
