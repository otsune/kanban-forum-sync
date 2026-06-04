# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A [Hermes Agent](https://hermes-agent.nousresearch.com/) plugin that bidirectionally syncs Kanban tasks to Discord Forum channels. Kanban task changes are pushed to Discord as forum threads (Phase 1), and Discord tag changes / new messages are reflected back into Kanban (Phase 2). The plugin also exposes Hermes agent tools and a session slash command for runtime management.

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

## Agent tools and slash command

Hermes registers these tools in the `kanban_forum_sync` toolset:

| Tool | Args | Description |
|---|---|---|
| `kanban_forum_sync_status` | none | JSON status: watcher state, channel id, last sync, counters, last error |
| `kanban_forum_sync_resync` | `mode`: `incremental` or `full` | Runs one immediate sync cycle. `full` does not clear `sync_map.json` |

Session command:

```text
/kanban-forum-sync status
/kanban-forum-sync sync
/kanban-forum-sync start
/kanban-forum-sync stop
```

Tests are stdlib `unittest`:

```bash
python3 -m unittest tests.test_sync_safety
```

## Required / recommended Bot permissions

Discord Developer Portal でボットに付与する権限。

### 必須（これが無いと同期が動かない）

| 権限 | 用途 |
|---|---|
| `VIEW_CHANNEL` | Forum チャンネルの参照 |
| `SEND_MESSAGES` | Forum チャンネルへの投稿 |
| `CREATE_PUBLIC_THREADS` | スレッド作成（Kanban → Forum, Phase 1） |
| `SEND_MESSAGES_IN_THREADS` | スレッド内メッセージ投稿（コメント・ログ同期） |
| `MANAGE_THREADS` | アーカイブ設定 (`archived=True`)。`done`/`archived` 同期に必須 |
| `READ_MESSAGE_HISTORY` | Phase 2 コメント同期（スレッドの新着メッセージ取得） |

### 推奨（self-heal / 自動セットアップに必要）

| 権限 | 用途 |
|---|---|
| `MANAGE_CHANNELS` | **推奨**: Forum チャンネルの自動生成・自動復旧。`FORUM_SYNC_CHANNEL_ID` 未設定時の `#kanban` 自動作成、および設定済み Forum が Discord 側で削除された際の再生成（self-heal）に必要 |

### 推奨設定まとめ

上記すべてを付与するのが推奨構成。最小権限で運用する場合は必須6つだけでも動くが、その場合は **Forum チャンネルを手動で作成**し（名前を `kanban`/`task-board`/`task_board`/`tasks` のいずれかにすれば自動検出される。任意名なら `FORUM_SYNC_CHANNEL_ID` を設定）、Bot にそのチャンネルへの上記必須権限を与えること。

### 補足

- `MANAGE_THREADS` なしだと `done`/`archived` ステータスへの同期（スレッドアーカイブ）が 403 で失敗する。
- `applied_tags` の変更はスレッドオーナー（Bot 自身が作成したスレッド）かつ非 moderated タグなら `MANAGE_THREADS` 不要。
- `MANAGE_CHANNELS` なしで設定済み Forum が削除されると、self-heal は既存 Forum の再検出までは行えるが**再生成はできず** `ADMIN_GUIDE_MESSAGE` を表示して再試行を続ける（権限付与または手動作成後、再起動なしで自動復帰する）。

## Required environment variables

| Variable | Required | Description |
|---|---|---|
| `FORUM_SYNC_BOT_TOKEN` | Yes (or `DISCORD_BOT_TOKEN`) | Discord Bot token |
| `FORUM_SYNC_CHANNEL_ID` | No | Forum channel snowflake; auto-discovered if unset |
| `FORUM_SYNC_POLL_INTERVAL` | No | Polling interval in seconds (default: 15) |
| `FORUM_SYNC_LANG` | No | Forum タグの言語。`en`（デフォルト）または `ja` |
| `FORUM_SYNC_EVENT_DRIVEN` | No | Set to `1` to enable inotify-based event-driven sync (Linux only, default: 0) |
| `FORUM_SYNC_DEFAULT_ASSIGNEE` | No | Assignee used when human-created Forum threads become Kanban tasks |
| `HERMES_KANBAN_DB` | No | Kanban SQLite DB path. Resolution is delegated to core (`hermes_cli.kanban_db.kanban_db_path`), so `HERMES_KANBAN_BOARD` and the active board are also honoured. Lets a profile sync a non-default DB. Unset → `~/.hermes/kanban.db`. Non-default paths get isolated state files via `db_slug()`. |

## Architecture

### Module responsibilities

- **`__init__.py`** — Hermes plugin entry point. `register(ctx)` wires the `kanban-forum-sync` CLI subcommand, `/kanban-forum-sync` session command, agent tools, and auto-starts the watcher.
- **`service.py`** — Shared runtime accessors for plugin context and the singleton `KanbanForumSyncer`, used by both `__init__.py` and `tools.py`.
- **`schemas.py`** — JSON schemas/descriptions for the Hermes agent tools.
- **`tools.py`** — Tool handlers. They follow the Hermes handler contract: accept `args: dict` plus `**kwargs`, return JSON strings, and convert failures into JSON errors.
- **`syncer.py`** — Core sync engine (`KanbanForumSyncer`). Runs a daemon thread in either polling (`_run_loop_poll`) or inotify event-driven (`_run_loop_inotify`) mode. Handles Forum channel auto-resolution, tag management, and both sync directions.
- **`kanban_watcher.py`** — inotify-based file watcher (`KanbanDBWatcher`). Watches `kanban.db` and `kanban.db-wal` via Linux inotify (ctypes, no extra deps). Context manager; falls back to timeout-only when inotify is unavailable.
- **`discord_forum.py`** — Thin Discord REST v10 client (`DiscordForumClient`). Uses only stdlib (`urllib`). Rate limiting is handled in-client: 429 retries use a separate budget from hard failures, `Retry-After`/`X-RateLimit-*` headers are honored, requests are smoothed by a minimum inter-request gap, and exhausted 429s raise `RateLimitError`. Other typed exceptions remain `DiscordPermissionError` (403), `NotFoundError` (404), `DiscordForumError` (other).
- **`kanban_bridge.py`** — Kanban bridge. Reads from `~/.hermes/kanban.db` with a 120s busy timeout; writes go through `ctx.dispatch_tool()` and the `kanban_*` toolset.
- **`models.py`** — Thread-safe persistent state: `SyncMap` (task_id → thread_id mapping, persisted to `sync_map.json`), `ThreadMetaTracker` (per-thread last-seen message ID, persisted to `thread_meta.json`), `SyncState` (in-memory runtime counters).

### Data flow

**Kanban → Discord (Phase 1):**
1. Polling loop calls `KanbanBridge.get_tasks_changed_since_event(last_event_id)` each cycle.
2. New tasks: `DiscordForumClient.create_thread()` → saves mapping in `SyncMap`.
3. Updated tasks: `DiscordForumClient.update_thread()` with new name, tags, or `archived=True`.
4. `last_event_id` advances to `MAX(task_events.id)` after each cycle.
5. **Comments + worker events + worker logs → thread** (`_sync_kanban_comments_to_forum`): per mapped thread it posts (a) new `task_comments`, (b) new `task_events` of `_WORKER_LOG_KINDS` formatted by `_format_worker_event`, and (c) **worker speech from the per-task text log** `~/.hermes/kanban/logs/<task_id>.log`. The agent's spoken/thinking output (e.g. "判断が必要なため、タスクをブロックしました") lives **only** in that log file, not in `task_events` — `KanbanBridge.get_worker_log_messages()` extracts the `╭─ ⚕ Hermes ─╮ … ╰─╯` box bodies and they're posted as `📝 Worker log`. Cursor is a posted-block count (`ThreadMetaTracker.get/set_worker_log_count`), advanced one block at a time (at-least-once).

**Discord → Kanban (Phase 2):**
- **Comments**: New thread messages (non-bot) → `KanbanBridge.add_comment()` → `kanban_comment`. Uses `ThreadMetaTracker` to track the last durably processed message ID per thread.
- **Attachments** (interim): Files on a thread message (non-bot) → `_sync_attachment()` posts the Discord file URL as a `kanban_comment`. There is currently **no attachment toolset tool or CLI** in Hermes (registered kanban tools are only show/list/complete/block/heartbeat/comment/create/unblock/link; the only writer is `kanban_db.add_attachment`, a direct-DB call that violates this plugin's DB-access policy). So files are surfaced as comment links rather than true `task_attachments` uploads. Once an attachment toolset/CLI lands in hermes-agent (see `docs/plans/ATTACHMENT_TOOLSET_PR_PLAN.md`), swap `_sync_attachment()` to do a real upload. Attachments share the per-message `last_message_id` cursor with comments — a message advances the cursor only after all its attachments **and** its text body sync successfully (at-least-once; a partial failure re-runs the whole message next cycle).
- **Tag changes**: `applied_tags` on each thread → reverse-lookup in `_tag_map` → `KanbanBridge.update_task_status()`. Only semantic tool transitions are applied (`kanban_block`, `kanban_complete`, `kanban_unblock`); unsupported arbitrary status edits are skipped rather than written directly to SQLite. After a successful status update, `_sync_forum_tags()` calls `ctx.inject_message(..., role="user")` to notify the active agent conversation; injection failures are debug-logged and do not fail the sync.

### Forum channel auto-resolution order

`_resolve_forum_channel()` in `syncer.py` tries in order:
1. Validate the configured `FORUM_SYNC_CHANNEL_ID` as a Forum (type=15).
2. If channel is wrong type → search the same guild for a named Forum.
3. If no `FORUM_SYNC_CHANNEL_ID` → scan all bot guilds for `kanban`/`task-board`/`task_board`/`tasks`.
4. If none found → attempt to create `#kanban` in the first guild. The new forum's **Post Guidelines** (the channel `topic`) is set to `get_forum_guidelines()` — a short i18n (`FORUM_GUIDELINES`, en/ja per `FORUM_SYNC_LANG`) explainer of the Kanban sync and how to use it (thread=task, status tags, comment/attachment sync, new-thread→new-task). Only written on creation; an already-existing forum's topic is left untouched.
5. If creation fails (403) → print `ADMIN_GUIDE_MESSAGE` and abort.

**Configured forum deleted (self-heal):** if the configured `FORUM_SYNC_CHANNEL_ID` returns 404 (`NotFoundError` — the forum was deleted on Discord), step 1 does **not** abort. It clears the dead `channel_id` and falls through to steps 3–4 (rediscover an existing forum, else recreate `#kanban`). On success `_reset_state_after_forum_recovery()` clears `sync_map` + `thread_meta` (every thread died with the old forum, so all entries are stale) and logs a warning to update `FORUM_SYNC_CHANNEL_ID` to the new channel. Active tasks get fresh threads in the new forum on subsequent sync cycles. Note: a non-404 `DiscordForumError` (transient/permission) still aborts with the guide — only a confirmed 404 triggers recreation, to avoid spawning duplicate forums on a hiccup.

**Runtime self-heal (no restart needed):** `_resolve_forum_channel()` only runs once at startup, so a long-running watcher that resolved a now-deleted channel would otherwise stay stuck on the dead id forever (only a full hermes restart re-resolved it). `incremental_sync()` now calls `_ensure_channel_alive()` first each cycle: a cheap `get_channel()` health check that, on a 404, re-runs `_resolve_forum_channel()` to rediscover/recreate the forum mid-flight. When the channel actually changes it rebuilds tags (`_ensure_tags()` + `_build_tag_map()`, since the old forum's tag IDs are now invalid). Transient (non-404) errors are ignored for that cycle (no re-resolve). **Caveat:** the watcher lives in the long-running hermes **gateway** process; `hermes kanban-forum-sync stop/start` are separate short-lived CLI processes with their own singleton and do **not** restart the gateway watcher — only a full hermes restart (or this runtime self-heal) moves the gateway watcher to a new channel.

### Persistence files

JSON state files live inside the plugin directory:
- `sync_map.json` — `{task_id: discord_thread_id}` mapping. Cleared by `full_sync()`.
- `thread_meta.json` — `{thread_id: {last_message_id, last_comment_id, last_kanban_event_id, worker_log_count}}` per-thread cursors.
- `origin_map.json` — `{task_id: "kanban"|"forum"}` task origin tracker.

**Per-DB isolation (slug):** when the syncer runs against a non-default Kanban DB (a different profile's `HERMES_KANBAN_DB`, or a per-board DB), the state files are suffixed with a `db_slug()` derived from the DB path — e.g. `sync_map_toomo.json`, `thread_meta_toomo.json`. The default `~/.hermes/kanban.db` yields an empty slug, keeping the original filenames (back-compat). This lets multiple gateway profiles each sync a different DB ↔ a different forum without clobbering each other's state. `db_slug()` lives in `models.py`; for `…/kanban/boards/<board>/kanban.db` it uses the board name, otherwise `<parentdir>_<basename>`.

### Status ↔ tag mapping

`STATUS_TO_TAG` / `TAG_TO_STATUS` / `STATUS_TAG_EMOJI` / `REQUIRED_TAGS` は `_build_tag_tables(lang)` から生成される（`syncer.py` モジュール読み込み時に `FORUM_SYNC_LANG` を参照）。`"done"` と `"archived"` は同じタグにマップされ、アーカイブ時に `archived=True` をセットする。必要なタグは起動時に `_ensure_tags()` で自動作成。言語切り替え時は Discord 側の古いタグを手動削除する必要がある（タグIDが変わるため）。

### Relationship to `kanban_notify_subs`

Hermes has a `kanban_notify_subs` table for push notifications (`task_id`, `platform`, `chat_id`, `thread_id`, `user_id`, `notifier_profile`, `last_event_id`). This plugin deliberately does **not** use it—Forum sync requires a bidirectional task↔thread mapping that the notification table's schema doesn't support. Independent `sync_map.json` keeps this plugin decoupled from Hermes internals. Future integration (adding `forum_sync` as a platform) would require changes to Hermes core.

> Note: the official documentation incorrectly called this table `task_subscriptions`. The actual DDL in `kanban_db.py` confirms the name is `kanban_notify_subs`.

## Kanban DB schema facts (from official docs)

The `tasks` table has many additional columns not queried by this plugin. From `kanban_db.py` DDL: `created_by`, `started_at`, `workspace_kind`, `workspace_path`, `branch_name`, `claim_lock`, `claim_expires`, `tenant`, `result`, `idempotency_key`, `consecutive_failures`, `worker_pid`, `last_failure_error`, `max_runtime_seconds`, `last_heartbeat_at`, `current_run_id`, `workflow_template_id`, `current_step_key`, `skills`, `model_override`, `max_retries`, `session_id`. Note: the official docs listed `workspace` and `scheduled_at` which do not exist; the actual column names are `workspace_kind`/`workspace_path` and `started_at`. The DB runs in **WAL mode**, making concurrent reads safe without extra configuration.

**Official task statuses (7 total):** `triage`, `todo`, `ready`, `running`, `blocked`, `done`, `archived`.  
Forum-only tags are normalized before any Kanban write: `Backlog` → `triage`, `Scheduled` → `ready`, and `Review` → `running`. The bridge refuses non-standard statuses.

**Multi-board / multi-profile DB path:** Default is `~/.hermes/kanban.db`, but per-board DBs live at `~/.hermes/kanban/boards/<slug>/kanban.db`. DB-path resolution is **delegated to Hermes core**: `KanbanBridge` (via `resolve_kanban_db_path()` in `kanban_bridge.py`) calls `hermes_cli.kanban_db.kanban_db_path()`, which honours `HERMES_KANBAN_DB` → `HERMES_KANBAN_BOARD` → active/default board in the same order as the rest of Hermes (so the plugin never duplicates that precedence logic; it falls back to raw env + `~/.hermes/kanban.db` only if core can't be imported). An explicit `KanbanForumSyncer(db_path=…)` still wins for tests. The resolved DB path also drives `db_slug()` so each DB gets isolated state files (see Persistence files). This is how a separate profile (e.g. `toomo`) with its own `.env` + `HERMES_KANBAN_DB` + `FORUM_SYNC_CHANNEL_ID` syncs a different board to a different forum. Each profile runs in its own gateway process, so its own singleton `_syncer_instance` reads that process's env. Note: `HERMES_KANBAN_BOARD=<slug>` only resolves to the board DB once that board actually exists (`hermes kanban boards create <slug>`); core falls back to the default DB for a non-existent board. Pinning `HERMES_KANBAN_DB` directly avoids that ordering concern.

**Event-driven mode (inotify):** `FORUM_SYNC_EVENT_DRIVEN=1` enables `_run_loop_inotify()` which uses Linux inotify to watch `kanban.db` and `kanban.db-wal`. The loop reacts immediately to DB writes instead of sleeping for `POLL_INTERVAL`. `poll_interval` becomes the fallback timeout (runs Phase 2 Discord polling regardless). Implemented in `kanban_watcher.py` via ctypes + select, no extra dependencies. Note: the Hermes source has no `WS /api/plugins/kanban/events` WebSocket endpoint; the "kanban tail" command uses the same DB polling as this plugin.

**Rate limit handling:** `incremental_sync()` now fetches the Forum thread list once per cycle and shares it across comment sync, tag sync, and new-thread detection. That removes the old per-thread tag GETs and lets comment sync skip `get_thread_messages()` when the shared `last_message_id` shows no new activity. At the loop level, 429 exhaustion triggers an extra cycle backoff, and inotify mode also enforces a small minimum interval between completed cycles to avoid bursty re-entry on rapid DB writes.

**Event kinds:** Discord-origin writes use Kanban tools rather than raw `task_events` inserts, so event kinds come from Hermes core.

## Design decisions

- **Polling over Discord Gateway**: REST polling every 15 s was chosen over Gateway WebSocket intents for simplicity and lower coupling to Hermes. Gateway intents are the natural upgrade path if lower latency is needed.
- **JSON sync map over DB**: `sync_map.json` was chosen over storing task↔thread mappings in the Kanban SQLite DB to keep the plugin stateless with respect to the Kanban schema and easy to reset by deleting the file.
- **Event-ID-based change detection**: `task_events.id` (monotonically increasing) is used instead of `tasks.updated_at` timestamps to avoid timezone/clock-skew issues.
- **Per-task exception isolation**: `_sync_task_to_forum()` wraps each task in try/except so one failure doesn't abort the whole sync cycle.
- **In-process cycle serialization**: `incremental_sync()` / `full_sync()` / `initial_sync()` acquire a reentrant `self._sync_lock` (`RLock`). The watcher thread's periodic cycle and the `kanban_forum_sync_resync` tool / `/kanban-forum-sync sync` (which now run in the **same** gateway process via the shared singleton) can no longer overlap, preventing duplicate comment/log posts from interleaved check-then-act cursor advances. Reentrant so `full_sync()→initial_sync()` nesting works. Note: this is in-process only — two syncers pointed at the **same** DB across processes still share state files with no cross-process lock (see open questions).

## Hermes plugin API notes

**Auto-start:** The watcher is started directly in `register(ctx)` (not via a hook). `post_plugin_init` is not in Hermes's `VALID_HOOKS` and `invoke_hook("post_plugin_init")` is never called in the Hermes source, so hook-based startup does not work. The watcher start is wrapped in try/except so a missing token or bad config logs a warning instead of aborting plugin load.

**`register_cli_command` signature:** `register_cli_command(name, help, setup_fn, ...)` — `help` is a required second positional argument.

**Slash command handler signature differs from CLI:** The `/kanban-forum-sync` command registered via `ctx.register_command()` receives a raw argument string (not an argparse Namespace):
```python
def handler(raw_args: str) -> str: ...  # can also be async
ctx.register_command(name="kanban-forum-sync", handler=handler, description="...")
```
This is distinct from `ctx.register_cli_command()` which uses argparse subparsers.

**`ctx.dispatch_tool()`** — available to invoke any Hermes tool (including `kanban_*` tools) from within the plugin, with full approval pipeline and workspace wiring. Useful if future features need to interact with the Kanban agent surface rather than the DB directly.

**`ctx.inject_message(content, role="user")`** — queues a message into the active agent conversation (starts a new turn if idle, interrupts if mid-turn). This plugin uses it when `_sync_forum_tags()` updates a Kanban status from a Discord tag change. Returns `False` in gateway mode with no CLI reference.

**`pre_llm_call` context injection** — a hook that can inject text into the current turn's user message by returning `{"context": "..."}`. Not used now, but could surface sync status or pending Forum comments into agent conversations.

**`pre_gateway_dispatch` hook** — fires when the gateway receives a message; can skip, rewrite, or allow. Not used now but available if the plugin needs to intercept Discord gateway events directly.

## What's not yet implemented

- `hermes kanban-forum-sync setup` — guided setup subcommand
- Multiple Forum channel support

## Open design questions

- **Task deletion**: Rows deleted from `tasks` are invisible to the event-based poller. Deleted tasks will leave orphaned Discord threads with no automatic cleanup.
- **Forum thread manually un-archived**: If a user manually un-archives a `done`/`archived` thread in Discord, the next sync cycle will re-archive it (the Kanban status hasn't changed). There is no reconciliation for this.
- **Multiple Hermes profiles running simultaneously**: profiles that point at **different** `HERMES_KANBAN_DB` paths now get isolated state files via `db_slug()`, so they no longer clobber each other. Two instances pointed at the **same** DB (same slug) still share state files with no file-level locking — concurrent writes could corrupt state. Run at most one syncer per DB.
