"""Hermes tool handlers for kanban-forum-sync."""

import json

from . import service


def _state_payload(syncer):
    state = syncer.get_state()
    return {
        "state": state.state,
        "channel_id": syncer.channel_id,
        "last_sync": state.last_sync,
        "last_event_id": state.last_event_id,
        "tasks": state.task_count,
        "comments": state.comment_count,
        "tag_syncs": state.tag_sync_count,
        "forum_tasks": state.forum_task_count,
        "errors": state.error_count,
        "last_error": state.last_error,
    }


def kanban_forum_sync_status(args: dict, **kwargs) -> str:
    try:
        syncer = service.get_syncer_or_none()
        if syncer is None:
            return json.dumps({"error": "syncer unavailable (missing bot token?)"})
        return json.dumps(_state_payload(syncer))
    except Exception as e:
        return json.dumps({"error": f"status failed: {e}"})


def kanban_forum_sync_resync(args: dict, **kwargs) -> str:
    try:
        syncer = service.get_syncer_or_none()
        if syncer is None:
            return json.dumps({"error": "syncer unavailable (missing bot token?)"})

        mode = str((args or {}).get("mode") or "incremental").strip().lower()
        if mode == "full":
            syncer.full_sync()
        elif mode == "incremental":
            syncer.incremental_sync()
        else:
            return json.dumps(
                {"error": "invalid mode; expected 'incremental' or 'full'"}
            )
        payload = _state_payload(syncer)
        payload.update({"ok": True, "mode": mode})
        return json.dumps(payload)
    except Exception as e:
        return json.dumps({"error": f"resync failed: {e}"})
