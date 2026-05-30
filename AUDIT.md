# Audit — `kanban-forum-sync` plugin

> Comprehensive code audit (correctness, data integrity / concurrency, DB-access-policy
> compliance, reliability, security). Generated 2026-05-30.

## Context

`kanban-forum-sync` is a Hermes plugin that bidirectionally syncs Kanban tasks with a
Discord Forum channel. It runs a daemon thread that polls (or inotify-watches) `~/.hermes/kanban.db`,
pushes task changes to Discord threads, and writes Discord-side changes (comments, tag changes,
new threads) back into the Kanban DB.

Code reviewed lives in `~/.hermes/plugins/kanban_forum_sync/` (underscore dir — the hyphen dir
`kanban-forum-sync/` holds only the runtime JSON state). Findings about Hermes core were verified
against `~/.hermes/hermes-agent/hermes_cli/kanban_db.py`, `tools/kanban_tools.py`, and
`gateway/run.py`.

**Headline:** the plugin writes directly to `kanban.db` via raw `sqlite3` with no `busy_timeout`,
while Hermes core treats that DB as a serialized WAL store guarded by a 120s busy-timeout and a
continuous dispatcher writer. This both **violates the saved DB-access policy** (`use CLI/toolset,
not direct sqlite3`) and is a plausible contributor to the **dozens of `kanban.db.corrupt.*`
backups** dated 2026-05-29/30 sitting in `~/.hermes/`.

Severity counts: **2 critical, 4 high, 4 medium, 4 low.**

---

## Findings by severity

### 🔴 CRITICAL

**C1 — Direct SQLite writes to `kanban.db` bypass the Kanban toolset (policy violation + corruption/lock risk).**
- Location: `kanban_bridge.py:24-27` (`_connect`), and all writers: `create_task` (120-159),
  `add_comment` (163-185), `update_task_status` (206-242), `record_event` (187-204).
- `_connect()` does `sqlite3.connect(self.db_path)` with **no `busy_timeout`, no WAL pragma, no lock**.
  Core opens the same file with `busy_timeout=120000`, `journal_mode=WAL`, `synchronous=FULL`
  (`kanban_db.py:1016-1028`, `1311-1322`) and runs a dispatcher that claims tasks / inserts events /
  spawns workers **every 60s** (`gateway/run.py:5364-5415`). A plugin write that collides with a
  dispatcher tick gets an immediate `database is locked` (no wait) — and concurrent uncoordinated
  writers are exactly the corruption pattern evidenced by the `kanban.db.corrupt.*` files.
- Policy: saved memory `feedback_kanban_db_access` — *sqlite3 direct manipulation forbidden; use
  CLI/toolset (disaster recovery excepted)*. This plugin's whole write path violates that.
- A full toolset already exists to do this safely: `kanban_create`, `kanban_comment`,
  `kanban_block`, `kanban_complete`, `kanban_unblock`, `kanban_link`, etc.
  (`tools/kanban_tools.py:1302-1381`), invokable from a plugin via `ctx.dispatch_tool()`
  (documented in the plugin's own `CLAUDE.md:137`).
- **Fix:** route all writes through `ctx.dispatch_tool()` → kanban tools. Note a real impedance
  mismatch to design around: core has **no generic "set status" tool** — only semantic transitions
  (`complete`/`block`/`unblock`). See H4. As an interim hardening even before the tool migration,
  add `PRAGMA busy_timeout=120000` (and keep reads WAL-friendly) to `_connect()`.

**C2 — Silent, permanent comment loss on transient write failure.**
- Location: `syncer.py:360-379` (`_sync_forum_comments`).
- `self._thread_meta.set_last_message_id(thread_id, msg_id)` runs **unconditionally** (line 379),
  even when `self.kanban.add_comment(...)` returned `False` (line 373). `add_comment` returns `False`
  on any exception incl. `database is locked` (`kanban_bridge.py:181-183`). So a transient DB lock
  (very likely given C1) advances the cursor past a comment that was never persisted → the comment
  is **never retried and lost forever**.
- **Fix:** advance the per-thread cursor only after the comment is durably written; on failure, stop
  advancing for that thread so the next cycle retries. Combine with H2's max-id tracking.

### 🟠 HIGH

**H1 — JSON state writes are non-atomic; a crash mid-write corrupts state and the next load crashes the plugin.**
- Location: `models.py` `_save` (`28-31`, `95-98`, `151-154`) open `"w"` and `json.dump` in place;
  `_load` (`23-26`, `90-93`, `146-149`) calls `json.load` with **no error handling**.
- A crash/kill during `_save` truncates `sync_map.json` / `thread_meta.json` / `origin_map.json`;
  the next `__init__` → `_load` raises `json.JSONDecodeError`, the `SyncMap()`/etc. constructor
  throws, and the watcher fails to start (`__init__.py:104-108` swallows it to a warning → silent
  no-sync). A corrupt `sync_map.json` also means lost task↔thread mapping → duplicate threads.
- **Fix:** write to a temp file + `os.replace()` (atomic on POSIX); wrap `_load` in try/except that
  backs up the corrupt file and starts empty.

**H2 — Comment-sync cursor regresses due to a wrong Discord message-ordering assumption → duplicate Kanban comments.**
- Location: `syncer.py:356-379`. The code reverses only when `after_param is None` (line 357),
  assuming `?after=` returns **ascending**. Discord's `GET /channels/{id}/messages` returns
  messages **newest-first even with `after`**. Iterating newest→oldest while calling
  `set_last_message_id` every iteration (line 379) leaves the cursor at the **oldest** id of the
  batch → next poll re-fetches already-synced messages → duplicate comments inserted into Kanban.
- **Fix:** make it order-independent — compute `max(int(m["id"]) for m in messages)` and set the
  cursor once after the loop. This also dovetails with the C2 fix.

**H3 — Double full re-sync on every startup (`last_event_id` never initialized).**
- Location: `initial_sync` (`syncer.py:660-680`) never sets `self._state.last_event_id`; it stays
  `0`. The first `incremental_sync` then calls `get_tasks_changed_since_event(0)`
  (`syncer.py:687-688`) which returns **every task that has any event** and re-syncs them all — a
  second full pass of Discord writes (rate-limit pressure, redundant thread PATCHes) right after the
  initial sync.
- **Fix:** at the end of `initial_sync`, set `self._state.last_event_id = self.kanban.get_latest_event_id()`.

**H4 — Non-standard statuses written to Kanban can strand tasks.**
- Location: `TAG_TO_STATUS` / `_sync_forum_tags` (`syncer.py:387-435`) and `update_task_status`
  (`kanban_bridge.py:206-242`). The status vocabulary includes `scheduled`, `review`, `backlog`,
  none of which are in core's set (`triage,todo,ready,running,blocked,done,archived`). Phase 3 maps
  `backlog→triage` (`syncer.py:500-501`) but the tag→status path does **not** sanitize, so a Discord
  tag can push a task into a status the dispatcher doesn't recognize → task stuck.
- Also: the plugin writes `task_events.kind='status_change'`, which **does not exist** in core's
  taxonomy (verified against all `_append_event` call sites) — invisible to dashboards. Folds into
  C1's tool migration (use `complete`/`block`/`unblock`).
- **Fix:** restrict writes to the 7 official statuses; drop/translate the rest.

### 🟡 MEDIUM

**M1 — Sync thread aborts permanently on a transient startup failure.**
- `_run_loop_poll` / `_run_loop_inotify` (`syncer.py:742-753`, `770-781`) `return` if
  `_resolve_forum_channel()` or `initial_sync()` fails once. A brief Discord outage at boot kills
  sync until a manual `hermes kanban-forum-sync start`.
- **Fix:** retry channel resolution / initial sync with bounded backoff instead of returning.

**M2 — Worker-log messages aren't length-capped → Discord 400.**
- `syncer.py:576` posts `_format_worker_event(ev)` (a `blocked` reason can be arbitrarily long)
  without the `[:2000]` truncation applied to comments (`553`). Discord rejects >2000 chars with 400.
- **Fix:** truncate worker-event text to 2000 chars.

**M3 — No cross-instance locking on JSON state.**
- `models.py` has no file lock; two Hermes profiles writing `sync_map.json`/`thread_meta.json`
  concurrently interleave and corrupt state (documented open question in `CLAUDE.md:156`).
- **Fix:** advisory `flock` around `_save`, or a single-instance pid guard at watcher start.

**M4 — Phase 3 redundant thread PATCH clobbers human-applied Discord tags.**
- A forum-sourced task's `created` event makes the next Phase 1 cycle treat it as "changed"
  (`incremental_sync`), and `_sync_task_to_forum` PATCHes `applied_tags` to the single resolved
  status tag (`syncer.py:640-642`), overwriting any extra tags the human set on their own thread.
- **Fix:** skip the `applied_tags` PATCH for forum-sourced threads (or merge rather than replace).

### 🟢 LOW

- **L1** — Custom `PermissionError` (`discord_forum.py:35`) shadows the builtin; rename to
  `DiscordPermissionError` to avoid confusion in `syncer.py`.
- **L2** — `cli_status`/`cli_stop` call `_get_syncer()` which raises a bare traceback if no token is
  set (`__init__.py:24-27`); wrap CLI handlers to print a friendly message.
- **L3** — Doc drift: README status-mapping emojis (🟡 Triage, ⬜ Todo…) don't match the code
  (`_LOCALE_DATA`: 🩺 Triage, 📝 Todo…), and README presents `scheduled`/`review` as if standard.
- **L4** — No tests or linter configured (`CLAUDE.md:28`). Add smoke tests for `_build_tag_tables`,
  message-ordering/cursor logic, and JSON round-trip/corruption recovery.

---

## Prioritized remediation plan

Recommended order (each phase independently shippable):

1. **Stop the bleeding (C2, H2, H1)** — smallest, highest-value correctness fixes, no architecture
   change:
   - `_sync_forum_comments`: track `max` message id, advance cursor once, only after success (C2+H2).
   - `models.py`: atomic `os.replace` save + tolerant `_load` (H1).
   - `initial_sync`: set `last_event_id` at the end (H3).
   - Add `PRAGMA busy_timeout=120000` to `KanbanBridge._connect` as interim hardening (part of C1).

2. **DB-access policy compliance (C1, H4)** — the structural fix:
   - Replace `KanbanBridge` writes (`create_task`, `add_comment`, `update_task_status`,
     `record_event`) with `ctx.dispatch_tool()` calls to `kanban_create` / `kanban_comment` /
     `kanban_block` / `kanban_complete` / `kanban_unblock`. This requires threading `ctx` from
     `register(ctx)` → `KanbanForumSyncer` → `KanbanBridge` (currently `ctx` is dropped after CLI
     registration in `__init__.py:97-108`).
   - Sanitize statuses to the 7 official values; map the rest (H4).
   - Keep reads as direct WAL sqlite (allowed for reads; add busy_timeout) or move to `kanban_show`.

3. **Reliability hardening (M1–M4)** — backoff on startup failures, truncate worker logs, cross-
   instance lock, skip tag-clobber for forum-sourced threads.

4. **Polish (L1–L4)** — rename shadowed exception, friendly CLI errors, doc sync, add a minimal
   test suite.

> Note on scope: Phase 2 (C1) is the largest change and the one that satisfies the saved DB policy.
> If the appetite is small, Phase 1 alone removes the data-loss and corruption-on-crash bugs and is
> safe to ship on its own.

---

## Verification

- **Static / unit:** add and run pytest smoke tests:
  - `_build_tag_tables("ja")` / `("en")` produce expected `STATUS_TO_TAG` / reverse maps.
  - Feed a descending-order Discord message list to the comment-sync cursor logic and assert the
    cursor ends at `max(id)` and no comment is dropped on a simulated `add_comment` failure (C2/H2).
  - JSON round-trip: corrupt `sync_map.json` to `{` and assert `SyncMap()` loads empty + backs up the
    corrupt file (H1).
- **DB safety (C1):** with the gateway dispatcher running, drive a burst of `add_comment`/status
  writes and confirm no `database is locked` and no new `kanban.db.corrupt.*` files appear; after the
  tool migration, confirm comments/status show up via `hermes kanban-forum-sync status` and the
  Kanban dashboard, and that `task_events` rows use core's standard `kind` values (no `status_change`).
- **End-to-end:** `hermes kanban-forum-sync sync`, then: create a task → thread appears; change a
  Discord tag → status updates (and is a legal status); reply in a thread → exactly one Kanban
  comment (no duplicate on the next poll); restart Hermes → no second full re-sync and no duplicate
  threads (H3).
- **Crash test (H1):** `kill -9` the process mid-sync, restart, confirm the watcher still loads
  (state recovered, not a silent no-sync).
