"""Tool schemas exposed to Hermes agents."""

KANBAN_FORUM_SYNC_STATUS = {
    "name": "kanban_forum_sync_status",
    "description": (
        "Report the Kanban to Discord Forum sync watcher status: running state, "
        "resolved forum channel id, last sync time, synced task/comment/tag counts, "
        "forum-created task count, and last error if any."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
    },
}

KANBAN_FORUM_SYNC_RESYNC = {
    "name": "kanban_forum_sync_resync",
    "description": (
        "Trigger an immediate Kanban to Discord Forum sync cycle. "
        "mode='incremental' processes new changes; mode='full' re-runs initial_sync "
        "without clearing the task-thread map."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["incremental", "full"],
                "description": "Sync depth. Default is 'incremental'.",
            },
        },
    },
}
