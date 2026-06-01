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
_plugin_ctx = None


def _get_syncer():
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
        use_inotify = os.environ.get("FORUM_SYNC_EVENT_DRIVEN", "").strip().lower() in ("1", "true", "yes")
        # DB パス解決は KanbanBridge（resolve_kanban_db_path → コア委譲）に一任する。
        # HERMES_KANBAN_DB / HERMES_KANBAN_BOARD / 既定ボードを Hermes 本体と同じ
        # 順序で解決し、別 profile が別 DB の Kanban を扱えるようにする。
        _syncer_instance = KanbanForumSyncer(
            bot_token=bot_token,
            channel_id=channel_id,
            poll_interval=int(os.environ.get("FORUM_SYNC_POLL_INTERVAL", "15")),
            use_inotify=use_inotify,
            ctx=_plugin_ctx,
        )
    return _syncer_instance


# ---- CLI サブコマンド ----


def cli_setup(parser):
    sub = parser.add_subparsers(dest="kanban_forum_command")

    p_status = sub.add_parser("status", help="同期状態を表示")
    p_status.set_defaults(func=cli_status)

    p_start = sub.add_parser("start", help="同期 watcher を開始")
    p_start.set_defaults(func=cli_start)

    p_stop = sub.add_parser("stop", help="同期 watcher を停止")
    p_stop.set_defaults(func=cli_stop)

    p_sync = sub.add_parser("sync", help="フル同期を即時実行")
    p_sync.set_defaults(func=cli_sync)

    parser.set_defaults(func=lambda args: parser.print_help())


def cli_status(args):
    syncer = _get_syncer_or_print_error()
    if syncer is None:
        return
    state = syncer.get_state()
    ch = syncer.channel_id or "(auto)"
    print(f"Syncer state: {state.state}")
    print(f"Forum channel: {ch}")
    print(f"Monitored tasks: {state.task_count}")
    print(f"Last sync: {state.last_sync}")
    print(f"Last event ID: {state.last_event_id}")
    print(f"Errors: {state.error_count}")
    print(f"Comments synced (Phase 2): {state.comment_count}")
    print(f"Tag syncs (Phase 2): {state.tag_sync_count}")
    print(f"Forum→Kanban tasks created (Phase 3): {state.forum_task_count}")
    if state.last_error:
        print(f"Last error: {state.last_error}")


def cli_start(args):
    syncer = _get_syncer_or_print_error()
    if syncer is None:
        return
    syncer.start()
    print("Watcher started.")


def cli_stop(args):
    syncer = _get_syncer_or_print_error()
    if syncer is None:
        return
    syncer.stop()
    print("Watcher stopped.")


def cli_sync(args):
    syncer = _get_syncer_or_print_error()
    if syncer is None:
        return
    syncer.full_sync()
    print("Full sync complete.")


def _get_syncer_or_print_error():
    try:
        return _get_syncer()
    except RuntimeError as e:
        print(f"kanban-forum-sync: {e}")
        return None


# ---- 登録 ----


def register(ctx):
    global _plugin_ctx
    _plugin_ctx = ctx
    if _syncer_instance is not None:
        _syncer_instance.kanban.ctx = ctx
    ctx.register_cli_command(
        "kanban-forum-sync",
        "Kanban ↔ Discord Forum sync management",
        setup_fn=cli_setup,
    )
    try:
        syncer = _get_syncer()
        syncer.start()
        logger.info("Kanban ↔ Discord Forum sync watcher started.")
    except Exception as e:
        logger.warning("kanban-forum-sync: watcher not started: %s", e)
