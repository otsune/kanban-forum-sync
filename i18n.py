"""i18n データ・タグテーブル・管理者/フォーラム案内文言。

syncer.py から分離されたロケールデータ層。FORUM_SYNC_LANG に応じた
ステータスタグ名・絵文字・ガイド文言を生成する。
"""

import logging
import os
from typing import Optional

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


def _normalize_kanban_status(status: str) -> Optional[str]:
    status = _STATUS_ALIASES.get(status, status)
    return status if status in _KANBAN_STATUSES else None


def _make_tag_dict(name: str, emoji: str = "") -> dict:
    tag: dict = {"name": name, "moderated": False}
    if emoji:
        tag["emoji_name"] = emoji
    return tag


REQUIRED_TAGS = [_make_tag_dict(name, emoji) for name, emoji in STATUS_TAG_EMOJI.items()]

_FORUM_GUIDE_URL = "https://support.discord.com/hc/ja/articles/6208479917079"

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
