"""
kanban-forum-sync: Hermes Kanban → Discord Forum 同期プラグイン

register(ctx) で Hermes プラグインシステムに登録。
起動時に Watcher を開始し、Kanban DB の変更を Discord Forum に同期する。
"""

import os
import logging
from .syncer import KanbanForumSyncer

logger = logging.getLogger(__name__)
_syncer_instance = None


def _get_syncer():
    global _syncer_instance
    if _syncer_instance is None:
        _syncer_instance = KanbanForumSyncer(
            bot_token=os.environ["FORUM_SYNC_BOT_TOKEN"],
            channel_id=int(os.environ["FORUM_SYNC_CHANNEL_ID"]),
            poll_interval=int(os.environ.get("FORUM_SYNC_POLL_INTERVAL", "15")),
        )
    return _syncer_instance


# ---- CLI サブコマンド ----


def cli_setup(subparsers):
    parser = subparsers.add_parser(
        "kanban-forum-sync",
        help="Kanban ↔ Discord Forum 同期の管理",
    )
    sub = parser.add_subparsers(dest="kanban_forum_command")

    p_status = sub.add_parser("status", help="同期状態を表示")
    p_status.set_defaults(handler=cli_status)

    p_start = sub.add_parser("start", help="同期 watcher を開始")
    p_start.set_defaults(handler=cli_start)

    p_stop = sub.add_parser("stop", help="同期 watcher を停止")
    p_stop.set_defaults(handler=cli_stop)

    p_sync = sub.add_parser("sync", help="フル同期を即時実行")
    p_sync.set_defaults(handler=cli_sync)


def cli_status(args):
    syncer = _get_syncer()
    status = syncer.get_status()
    print(f"Syncer status: {status.state}")
    print(f"Monitored tasks: {status.task_count}")
    print(f"Last sync: {status.last_sync}")
    print(f"Errors: {status.error_count}")


def cli_start(args):
    syncer = _get_syncer()
    syncer.start()
    print("Watcher started.")


def cli_stop(args):
    syncer = _get_syncer()
    syncer.stop()
    print("Watcher stopped.")


def cli_sync(args):
    syncer = _get_syncer()
    syncer.full_sync()
    print("Full sync complete.")


# ---- 起動フック ----


def _on_post_plugin_init(ctx):
    """Hermes 起動時に自動で watcher を開始"""
    syncer = _get_syncer()
    syncer.start()
    logger.info("Kanban ↔ Discord Forum sync watcher started.")


# ---- 登録 ----


def register(ctx):
    ctx.register_hook("post_plugin_init", _on_post_plugin_init)
    ctx.register_cli_command("kanban-forum-sync", setup_fn=cli_setup)
