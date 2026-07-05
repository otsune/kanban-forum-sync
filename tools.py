"""Hermes tool handlers for kanban-forum-sync."""

import json

from . import service


def kanban_forum_sync_status(args: dict, **kwargs) -> str:
    try:
        syncer = service.get_syncer_or_none()
        if syncer is None:
            return json.dumps({"error": "syncer unavailable (missing bot token?)"})
        return json.dumps(syncer.status_dict())
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
        payload = syncer.status_dict()
        payload.update({"ok": True, "mode": mode})
        return json.dumps(payload)
    except Exception as e:
        return json.dumps({"error": f"resync failed: {e}"})
