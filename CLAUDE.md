# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A [Hermes Agent](https://hermes-agent.nousresearch.com/) plugin that bidirectionally syncs Kanban tasks to Discord Forum channels. Kanban task changes are pushed to Discord as forum threads (Phase 1), and Discord tag changes / new messages are reflected back into Kanban (Phase 2).

## CLI commands

```bash
# Manage the sync watcher via Hermes
hermes kanban-forum-sync status
hermes kanban-forum-sync start
hermes kanban-forum-sync stop
hermes kanban-forum-sync sync     # manual full re-sync

# Enable/reload the plugin in Hermes (official docs use plural "plugins")
hermes plugins enable kanban-forum-sync

# Debug plugin discovery (logs to stderr and ~/.hermes/logs/agent.log)
HERMES_PLUGINS_DEBUG=1 hermes plugins list

# Show which plugins are currently loaded in the active session
/plugins
```

No build step, test suite, or linter is currently configured for this plugin.

## Required Bot permissions

Discord Developer Portal でボットに付与が必要な権限:

| 権限 | 用途 |
|---|---|
| `VIEW_CHANNEL` | Forum チャンネルの参照 |
| `SEND_MESSAGES` | Forum チャンネルへの投稿 |
| `CREATE_PUBLIC_THREADS` | スレッド作成 |
| `SEND_MESSAGES_IN_THREADS` | スレッド内メッセージ投稿 |
| `MANAGE_THREADS` | **必須**: アーカイブ設定 (`archived=True`) |
| `READ_MESSAGE_HISTORY` | Phase 2 コメント同期 |

`MANAGE_THREADS` なしだと `done`/`archived` ステータスへの同期（スレッドアーカイブ）が 403 で失敗する。  
`applied_tags` の変更はスレッドオーナー（Bot 自身が作成したスレッド）かつ非 moderated タグなら `MANAGE_THREADS` 不要。

## Required environment variables

| Variable | Required | Description |
|---|---|---|
| `FORUM_SYNC_BOT_TOKEN` | Yes (or `DISCORD_BOT_TOKEN`) | Discord Bot token |
| `FORUM_SYNC_CHANNEL_ID` | No | Forum channel snowflake; auto-discovered if unset |
| `FORUM_SYNC_POLL_INTERVAL` | No | Polling interval in seconds (default: 15) |
| `FORUM_SYNC_LANG` | No | Forum タグの言語。`en`（デフォルト）または `ja` |
| `FORUM_SYNC_EVENT_DRIVEN` | No | Set to `1` to enable inotify-based event-driven sync (Linux only, default: 0) |

## Architecture

### Module responsibilities

- **`__init__.py`** — Hermes plugin entry point. `register(ctx)` wires the `kanban-forum-sync` CLI subcommand and auto-starts the watcher. Holds the singleton `KanbanForumSyncer`.
- **`syncer.py`** — Core sync engine (`KanbanForumSyncer`). Runs a daemon thread in either polling (`_run_loop_poll`) or inotify event-driven (`_run_loop_inotify`) mode. Handles Forum channel auto-resolution, tag management, and both sync directions.
- **`kanban_watcher.py`** — inotify-based file watcher (`KanbanDBWatcher`). Watches `kanban.db` and `kanban.db-wal` via Linux inotify (ctypes, no extra deps). Context manager; falls back to timeout-only when inotify is unavailable.
- **`discord_forum.py`** — Thin Discord REST v10 client (`DiscordForumClient`). Uses only stdlib (`urllib`). Includes 3-retry backoff for HTTP 429 rate limits. Raises typed exceptions: `DiscordPermissionError` (403), `NotFoundError` (404), `DiscordForumError` (other).
- **`kanban_bridge.py`** — Kanban bridge. Reads from `~/.hermes/kanban.db` with a 120s busy timeout; writes go through `ctx.dispatch_tool()` and the `kanban_*` toolset.
- **`models.py`** — Thread-safe persistent state: `SyncMap` (task_id → thread_id mapping, persisted to `sync_map.json`), `ThreadMetaTracker` (per-thread last-seen message ID, persisted to `thread_meta.json`), `SyncState` (in-memory runtime counters).

### Data flow

**Kanban → Discord (Phase 1):**
1. Polling loop calls `KanbanBridge.get_tasks_changed_since_event(last_event_id)` each cycle.
2. New tasks: `DiscordForumClient.create_thread()` → saves mapping in `SyncMap`.
3. Updated tasks: `DiscordForumClient.update_thread()` with new name, tags, or `archived=True`.
4. `last_event_id` advances to `MAX(task_events.id)` after each cycle.

**Discord → Kanban (Phase 2):**
- **Comments**: New thread messages (non-bot) → `KanbanBridge.add_comment()` → `kanban_comment`. Uses `ThreadMetaTracker` to track the last durably processed message ID per thread.
- **Attachments** (interim): Files on a thread message (non-bot) → `_sync_attachment()` posts the Discord file URL as a `kanban_comment`. There is currently **no attachment toolset tool or CLI** in Hermes (registered kanban tools are only show/list/complete/block/heartbeat/comment/create/unblock/link; the only writer is `kanban_db.add_attachment`, a direct-DB call that violates this plugin's DB-access policy). So files are surfaced as comment links rather than true `task_attachments` uploads. Once an attachment toolset/CLI lands in hermes-agent (see `ATTACHMENT_TOOLSET_PR_PLAN.md`), swap `_sync_attachment()` to do a real upload. Attachments share the per-message `last_message_id` cursor with comments — a message advances the cursor only after all its attachments **and** its text body sync successfully (at-least-once; a partial failure re-runs the whole message next cycle).
- **Tag changes**: `applied_tags` on each thread → reverse-lookup in `_tag_map` → `KanbanBridge.update_task_status()`. Only semantic tool transitions are applied (`kanban_block`, `kanban_complete`, `kanban_unblock`); unsupported arbitrary status edits are skipped rather than written directly to SQLite.

### Forum channel auto-resolution order

`_resolve_forum_channel()` in `syncer.py` tries in order:
1. Validate the configured `FORUM_SYNC_CHANNEL_ID` as a Forum (type=15).
2. If channel is wrong type → search the same guild for a named Forum.
3. If no `FORUM_SYNC_CHANNEL_ID` → scan all bot guilds for `kanban`/`task-board`/`task_board`/`tasks`.
4. If none found → attempt to create `#kanban` in the first guild.
5. If creation fails (403) → print `ADMIN_GUIDE_MESSAGE` and abort.

### Persistence files

Both JSON files live inside the plugin directory:
- `sync_map.json` — `{task_id: discord_thread_id}` mapping. Cleared by `full_sync()`.
- `thread_meta.json` — `{thread_id: {last_message_id: int}}` per-thread cursor for Phase 2 comment sync.

### Status ↔ tag mapping

`STATUS_TO_TAG` / `TAG_TO_STATUS` / `STATUS_TAG_EMOJI` / `REQUIRED_TAGS` は `_build_tag_tables(lang)` から生成される（`syncer.py` モジュール読み込み時に `FORUM_SYNC_LANG` を参照）。`"done"` と `"archived"` は同じタグにマップされ、アーカイブ時に `archived=True` をセットする。必要なタグは起動時に `_ensure_tags()` で自動作成。言語切り替え時は Discord 側の古いタグを手動削除する必要がある（タグIDが変わるため）。

### Relationship to `kanban_notify_subs`

Hermes has a `kanban_notify_subs` table for push notifications (`task_id`, `platform`, `chat_id`, `thread_id`, `user_id`, `notifier_profile`, `last_event_id`). This plugin deliberately does **not** use it—Forum sync requires a bidirectional task↔thread mapping that the notification table's schema doesn't support. Independent `sync_map.json` keeps this plugin decoupled from Hermes internals. Future integration (adding `forum_sync` as a platform) would require changes to Hermes core.

> Note: the official documentation incorrectly called this table `task_subscriptions`. The actual DDL in `kanban_db.py` confirms the name is `kanban_notify_subs`.

## Kanban DB schema facts (from official docs)

The `tasks` table has many additional columns not queried by this plugin. From `kanban_db.py` DDL: `created_by`, `started_at`, `workspace_kind`, `workspace_path`, `branch_name`, `claim_lock`, `claim_expires`, `tenant`, `result`, `idempotency_key`, `consecutive_failures`, `worker_pid`, `last_failure_error`, `max_runtime_seconds`, `last_heartbeat_at`, `current_run_id`, `workflow_template_id`, `current_step_key`, `skills`, `model_override`, `max_retries`, `session_id`. Note: the official docs listed `workspace` and `scheduled_at` which do not exist; the actual column names are `workspace_kind`/`workspace_path` and `started_at`. The DB runs in **WAL mode**, making concurrent reads safe without extra configuration.

**Official task statuses (7 total):** `triage`, `todo`, `ready`, `running`, `blocked`, `done`, `archived`.  
Forum-only tags are normalized before any Kanban write: `Backlog` → `triage`, `Scheduled` → `ready`, and `Review` → `running`. The bridge refuses non-standard statuses.

**Multi-board DB path:** Default is `~/.hermes/kanban.db`, but per-board DBs live at `~/.hermes/kanban/boards/<slug>/kanban.db`. `KanbanBridge` hardcodes the default path (`KANBAN_DB_PATH` constant in `kanban_bridge.py`) and accepts `db_path` as a constructor argument, but `__init__.py` never passes it — there is no env var override. Supporting non-default boards requires wiring an env var (e.g. `FORUM_SYNC_DB_PATH`) through `_get_syncer()` → `KanbanForumSyncer` → `KanbanBridge`.

**Event-driven mode (inotify):** `FORUM_SYNC_EVENT_DRIVEN=1` enables `_run_loop_inotify()` which uses Linux inotify to watch `kanban.db` and `kanban.db-wal`. The loop reacts immediately to DB writes instead of sleeping for `POLL_INTERVAL`. `poll_interval` becomes the fallback timeout (runs Phase 2 Discord polling regardless). Implemented in `kanban_watcher.py` via ctypes + select, no extra dependencies. Note: the Hermes source has no `WS /api/plugins/kanban/events` WebSocket endpoint; the "kanban tail" command uses the same DB polling as this plugin.

**Event kinds:** Discord-origin writes use Kanban tools rather than raw `task_events` inserts, so event kinds come from Hermes core.

## Design decisions

- **Polling over Discord Gateway**: REST polling every 15 s was chosen over Gateway WebSocket intents for simplicity and lower coupling to Hermes. Gateway intents are the natural upgrade path if lower latency is needed.
- **JSON sync map over DB**: `sync_map.json` was chosen over storing task↔thread mappings in the Kanban SQLite DB to keep the plugin stateless with respect to the Kanban schema and easy to reset by deleting the file.
- **Event-ID-based change detection**: `task_events.id` (monotonically increasing) is used instead of `tasks.updated_at` timestamps to avoid timezone/clock-skew issues.
- **Per-task exception isolation**: `_sync_task_to_forum()` wraps each task in try/except so one failure doesn't abort the whole sync cycle.

## Hermes plugin API notes

**Auto-start:** The watcher is started directly in `register(ctx)` (not via a hook). `post_plugin_init` is not in Hermes's `VALID_HOOKS` and `invoke_hook("post_plugin_init")` is never called in the Hermes source, so hook-based startup does not work. The watcher start is wrapped in try/except so a missing token or bad config logs a warning instead of aborting plugin load.

**`register_cli_command` signature:** `register_cli_command(name, help, setup_fn, ...)` — `help` is a required second positional argument.

**Slash command handler signature differs from CLI:** When implementing the planned `/kanban-forum-sync` slash command via `ctx.register_command()`, the handler receives a raw argument string (not an argparse Namespace):
```python
def handler(raw_args: str) -> str: ...  # can also be async
ctx.register_command(name="kanban-forum-sync", handler=handler, description="...")
```
This is distinct from `ctx.register_cli_command()` which uses argparse subparsers.

**`ctx.dispatch_tool()`** — available to invoke any Hermes tool (including `kanban_*` tools) from within the plugin, with full approval pipeline and workspace wiring. Useful if future features need to interact with the Kanban agent surface rather than the DB directly.

**`ctx.inject_message(content, role="user")`** — queues a message into the active agent conversation (starts a new turn if idle, interrupts if mid-turn). This is the correct mechanism for the currently-missing "Hermes agent notification on tag changes" feature: when `_sync_forum_tags()` updates a Kanban status, call `ctx.inject_message()` to notify the agent. Returns `False` in gateway mode with no CLI reference.

**`pre_llm_call` context injection** — a hook that can inject text into the current turn's user message by returning `{"context": "..."}`. Not used now, but could surface sync status or pending Forum comments into agent conversations.

**`pre_gateway_dispatch` hook** — fires when the gateway receives a message; can skip, rewrite, or allow. Not used now but available if the plugin needs to intercept Discord gateway events directly.

## What's not yet implemented (Phase 3)

- Discord slash command `/kanban-forum-sync` — designed in the spec (`ctx.register_command()`), not wired in current `__init__.py`
- `hermes kanban-forum-sync setup` — guided setup subcommand
- Hermes agent notification on tag changes (Phase 2 partial: DB is updated, but no agent event is emitted)
- Multiple Forum channel support

## Open design questions

- **Task deletion**: Rows deleted from `tasks` are invisible to the event-based poller. Deleted tasks will leave orphaned Discord threads with no automatic cleanup.
- **Forum thread manually un-archived**: If a user manually un-archives a `done`/`archived` thread in Discord, the next sync cycle will re-archive it (the Kanban status hasn't changed). There is no reconciliation for this.
- **Multiple Hermes profiles running simultaneously**: No file-level locking on `sync_map.json` or `thread_meta.json`; concurrent writes from two instances could corrupt state.
