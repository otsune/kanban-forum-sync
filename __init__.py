"""
kanban-forum-sync: Hermes Kanban → Discord Forum 同期プラグイン

register(ctx) で Hermes プラグインシステムに登録。
起動時に Watcher を開始し、Kanban DB の変更を Discord Forum に同期する。
"""

import logging
import os
import sys
from . import schemas, service, tools

logger = logging.getLogger(__name__)


def _is_dedicated_gateway() -> bool:
    """この hermes プロセスが「同期 watcher を回すべき専用ゲートウェイ」かを判定する。

    Hermes のプラグインは、専用ゲートウェイだけでなく対話 TUI・各 kanban worker・
    各種 CLI 呼び出しなど **あらゆる** プロセスで読み込まれ、その全てが
    register() で watcher を自動起動してしまう。複数 watcher が同じ DB を奪い合うと
    重複タスク・重複スレッドの暴走になる（実害として確認済み）。

    そこで watcher を起動するのは `... gateway run` 系の専用ゲートウェイに限定する。
    対話 TUI（tui_gateway / `hermes --tui`）・worker（`... chat ... work kanban`）・
    CLI は watcher を起動しない。複数ゲートウェイが同一 DB を見る場合の最終防衛は
    syncer 側の DB ロック（watcher.lock）が担う。

    緊急避難として ``FORUM_SYNC_FORCE_WATCHER=1`` で強制起動も可能。
    """
    if os.environ.get("FORUM_SYNC_FORCE_WATCHER") == "1":
        return True
    argv = sys.argv or []
    return "gateway" in argv and "run" in argv


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
    syncer = service.get_syncer_or_none()
    if syncer is None:
        print("kanban-forum-sync: syncer unavailable (bot token 未設定?)")
        return None
    return syncer


# ---- Slash command ----


def _format_status(syncer):
    state = syncer.get_state()
    text = (
        f"state={state.state} channel={syncer.channel_id or '(auto)'} "
        f"tasks={state.task_count} comments={state.comment_count} "
        f"tags={state.tag_sync_count} forum_tasks={state.forum_task_count}"
    )
    if state.last_error:
        text += f"\nlast_error={state.last_error}"
    return text


def _slash_handler(raw_args: str) -> str:
    parts = (raw_args or "").strip().split()
    action = (parts[0].lower() if parts else "status")
    syncer = service.get_syncer_or_none()
    if syncer is None:
        return "kanban-forum-sync: syncer unavailable (bot token 未設定?)"
    try:
        if action == "status":
            return _format_status(syncer)
        if action == "sync":
            syncer.full_sync()
            return "Full sync complete."
        if action == "start":
            syncer.start()
            return "Watcher started."
        if action == "stop":
            syncer.stop()
            return "Watcher stopped."
        return f"unknown action '{action}'. use: status|sync|start|stop"
    except Exception as e:
        return f"kanban-forum-sync: {action} failed: {e}"


# ---- 登録 ----


def register(ctx):
    service.set_ctx(ctx)
    ctx.register_cli_command(
        "kanban-forum-sync",
        "Kanban ↔ Discord Forum sync management",
        setup_fn=cli_setup,
    )
    ctx.register_tool(
        name="kanban_forum_sync_status",
        toolset="kanban_forum_sync",
        schema=schemas.KANBAN_FORUM_SYNC_STATUS,
        handler=tools.kanban_forum_sync_status,
    )
    ctx.register_tool(
        name="kanban_forum_sync_resync",
        toolset="kanban_forum_sync",
        schema=schemas.KANBAN_FORUM_SYNC_RESYNC,
        handler=tools.kanban_forum_sync_resync,
    )
    ctx.register_command(
        name="kanban-forum-sync",
        handler=_slash_handler,
        description="Kanban ↔ Discord Forum sync: status|sync|start|stop",
    )
    if not _is_dedicated_gateway():
        logger.info(
            "kanban-forum-sync: not a dedicated `gateway run` process "
            "(argv=%s); watcher NOT auto-started. The sync watcher runs only "
            "in the dedicated gateway to avoid multiple competing watchers. "
            "Set FORUM_SYNC_FORCE_WATCHER=1 to override.",
            (sys.argv or [])[:4],
        )
        return

    try:
        syncer = service.get_syncer()
        syncer.start()
        logger.info("Kanban ↔ Discord Forum sync watcher started.")
    except Exception as e:
        logger.warning("kanban-forum-sync: watcher not started: %s", e)
