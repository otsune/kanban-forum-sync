"""Runtime accessors shared by the plugin entry point and tool handlers."""

import logging
import os

from .syncer import KanbanForumSyncer

logger = logging.getLogger(__name__)

_syncer_instance = None
_plugin_ctx = None


def set_ctx(ctx):
    """Store the active Hermes plugin context."""
    global _plugin_ctx
    _plugin_ctx = ctx
    if _syncer_instance is not None:
        _syncer_instance.set_ctx(ctx)


def get_ctx():
    return _plugin_ctx


def get_syncer():
    """Return the singleton syncer, creating it from environment config."""
    global _syncer_instance
    if _syncer_instance is None:
        channel_id_str = os.environ.get("FORUM_SYNC_CHANNEL_ID", "").strip()
        channel_id = int(channel_id_str) if channel_id_str else None
        bot_token = os.environ.get("FORUM_SYNC_BOT_TOKEN")
        if not bot_token:
            bot_token = os.environ.get("DISCORD_BOT_TOKEN")
            if not bot_token:
                raise RuntimeError(
                    "FORUM_SYNC_BOT_TOKEN も DISCORD_BOT_TOKEN も設定されていません"
                )
        use_inotify = os.environ.get("FORUM_SYNC_EVENT_DRIVEN", "").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        # DB path resolution is delegated to KanbanBridge so it follows Hermes core.
        _syncer_instance = KanbanForumSyncer(
            bot_token=bot_token,
            channel_id=channel_id,
            poll_interval=int(os.environ.get("FORUM_SYNC_POLL_INTERVAL", "15")),
            use_inotify=use_inotify,
            ctx=_plugin_ctx,
        )
    return _syncer_instance


def get_syncer_or_none():
    try:
        return get_syncer()
    except RuntimeError as e:
        logger.warning("kanban-forum-sync: %s", e)
        return None
