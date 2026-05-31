# PR Plan: task attachment toolset + CLI for hermes-agent

Target repo: https://github.com/NousResearch/hermes-agent

## Context / motivation

The Kanban board already has full attachment **storage**:

- DB table `task_attachments(id, task_id, filename, stored_path, content_type, size, uploaded_by, created_at)` — `hermes_cli/kanban_db.py`.
- Module helpers in `hermes_cli/kanban_db.py`:
  - `attachments_root(board)` / `task_attachments_dir(task_id, board)` — resolve the on-disk dir.
  - `add_attachment(conn, task_id, *, filename, stored_path, content_type=None, size=0, uploaded_by=None) -> int` (line ~2460; **keyword-only** args after `task_id`) — inserts the metadata row and appends an `attached` event; **caller writes the blob to `stored_path` first**.
  - `list_attachments(conn, task_id)`, `get_attachment(conn, id)`, `delete_attachment(conn, id)`.
- A **dashboard HTTP API** already exposes upload/list/download/delete: `plugins/kanban/dashboard/plugin_api.py`
  - `POST /tasks/{task_id}/attachments` (multipart `file` + optional `uploaded_by`, 25 MB cap), `GET .../attachments`, `GET /attachments/{id}`, `DELETE /attachments/{id}`.

**What's missing:** there is no **agent toolset tool** and no **`hermes kanban` CLI subcommand** for attachments. Registered kanban tools are only: `kanban_show`, `kanban_list`, `kanban_complete`, `kanban_block`, `kanban_heartbeat`, `kanban_comment`, `kanban_create`, `kanban_unblock`, `kanban_link` (`tools/kanban_tools.py`). The CLI (`hermes_cli/kanban.py`) has no `attach` subcommand.

This blocks programmatic/agent-driven attachment writes that don't go through the dashboard HTTP server. Concretely, the `kanban_forum_sync` plugin wants to sync Discord-forum file uploads into `task_attachments` as real "Upload file" attachments, but it can only call toolset tools / CLI (direct `sqlite3`/DB writes are disallowed by its policy). Today it falls back to posting the Discord file URL as a `kanban_comment` — a link, not a real attachment.

## Goal

Add a first-class attachment surface mirroring the existing comment surface, so agents and the CLI can create/list attachments without touching the DB directly or requiring the dashboard server.

## Scope

Three new tools + three new CLI subcommands, all thin wrappers over the existing `kanban_db` helpers (same pattern as `kanban_comment`).

### 1. Toolset tools — `tools/kanban_tools.py`

Follow the exact pattern of `kanban_comment` (handler at line ~1190, registration at ~1345). Each handler: resolve `task_id` via `_default_task_id`, `from hermes_cli import kanban_db`, `conn = kanban_db.connect()`, do work, `conn.commit()`, return JSON, `finally: conn.close()`. Gate with `check_fn=_check_kanban_mode`. Respect `_enforce_worker_task_ownership(tid)` for the write tools (workers should only attach to their own task).

- **`kanban_attach`** — inline upload.
  - Args: `task_id` (str, optional→env), `filename` (str, required), `content_base64` (str, required), `content_type` (str, optional).
  - Body: base64-decode with a hard **25 MB** cap (reuse the dashboard's `_MAX_ATTACHMENT_BYTES`; consider hoisting it to `kanban_db` so dashboard + tool + CLI share one constant). Compute `dest = task_attachments_dir(tid) / _safe_attachment_name(filename)`; `mkdir(parents=True, exist_ok=True)`; write bytes; then `add_attachment(conn, tid, filename, str(dest), content_type, size=len(data), uploaded_by="agent")`. Return `{"ok": True, "attachment_id": id, "size": n}`.
  - Reuse/share `_safe_attachment_name()` from `plugin_api.py` (hoist into `kanban_db.py` to avoid duplication + path-traversal risk).

- **`kanban_attach_url`** — fetch-by-URL upload (server-side download).
  - Args: `task_id` (optional), `url` (str, required), `content_type` (str, optional), `title`/`filename` (str, optional — default derived from URL path).
  - Body: stream-download `url` with the 25 MB cap (mirror the dashboard's streaming-with-cap logic), write to disk, then `add_attachment(...)`. Return same shape. (This is the variant the forum-sync plugin most wants — pass the Discord CDN URL and let the server fetch it.)

- **`kanban_attachments`** (or `kanban_list_attachments`) — read.
  - Args: `task_id` (optional). Returns `{"attachments": [ {id, filename, content_type, size, uploaded_by, created_at}, ... ]}` via `list_attachments`. Gate with `_check_kanban_mode` (read is safe; no ownership check).

Registration blocks: copy the `registry.register(name=..., handler=..., description=..., schema={...})` shape from `kanban_comment`. Mark `content_base64`/`url`/`filename` required appropriately in the JSON schema.

### 2. CLI subcommands — `hermes_cli/kanban.py`

Follow `cmd_comment` (handler ~1623) + subparser registration (~2847 `sub.add_parser("comment", ...)` / `set_defaults(func=...)`).

- **`hermes kanban attach <task_id> <path> [--content-type ...] [--name ...] [--author ...]`** — read the local file, write into `task_attachments_dir`, call `add_attachment`. Reuse one shared upload helper with the tool (see refactor note).
- **`hermes kanban attachments <task_id>`** — list (tabulate like other list output).
- **`hermes kanban attach-rm <attachment_id>`** — delete via `delete_attachment` (also unlinks the blob; `delete_attachment` already returns the row so the CLI can remove the file).

### 3. Shared helper (refactor)

To avoid three copies of "validate name + enforce size cap + write blob + insert row", add one helper in `hermes_cli/kanban_db.py`, e.g.:

```python
def store_attachment_bytes(conn, task_id, filename, data: bytes,
                           content_type=None, uploaded_by=None,
                           board=None, max_bytes=_MAX_ATTACHMENT_BYTES) -> int:
    """Validate name, enforce size cap, write blob under task_attachments_dir,
    insert the metadata row, return attachment id."""
```

Then the dashboard endpoint, the new tools, and the new CLI commands all call this one path. Move `_MAX_ATTACHMENT_BYTES` and `_safe_attachment_name` into `kanban_db.py` and have `plugin_api.py` import them (keeps the existing dashboard behavior identical).

## Files to change

- `hermes_cli/kanban_db.py` — add `store_attachment_bytes()`, hoist `_MAX_ATTACHMENT_BYTES` + `_safe_attachment_name()`.
- `tools/kanban_tools.py` — 3 handlers + 3 `registry.register(...)` blocks.
- `hermes_cli/kanban.py` — 3 `cmd_*` handlers + 3 subparser registrations.
- `plugins/kanban/dashboard/plugin_api.py` — refactor `upload_task_attachment` to call `store_attachment_bytes` (no behavior change).
- Docs: update kanban tool/CLI reference docs to list the new surface.

## Tests

- Extend `tests/plugins/test_kanban_attachments.py`:
  - tool: `kanban_attach` round-trips bytes → row + file on disk; oversize rejected with a clean tool-error; `kanban_attachments` lists it; worker-ownership enforced for foreign `task_id`.
  - `kanban_attach_url` fetches a small local HTTP fixture; oversize streamed body is rejected mid-download.
  - CLI: `hermes kanban attach/attachments/attach-rm` happy paths + size cap.
  - dashboard parity: existing endpoint still works after refactor.

## Acceptance

- An agent with the kanban toolset (or a dispatcher-spawned worker) can attach a file to a task via `kanban_attach` / `kanban_attach_url` and see it via `kanban_attachments`, with no dashboard server running and no direct `sqlite3` use by callers.
- `hermes kanban attach` works from the shell.
- 25 MB cap enforced uniformly across dashboard + tool + CLI (single shared constant/helper).

## Follow-up in kanban_forum_sync (this repo, after the PR lands)

Swap `_sync_attachment()` (in `syncer.py`) from the interim `kanban_comment` link back to a real upload via `KanbanBridge` → `kanban_attach_url` (pass the Discord CDN `url`; the server fetches it within the same sync cycle while the signed URL is still valid). Keep the 25 MB pre-check to avoid an infinite retry loop on oversize files (post the URL as a comment for those). See the `_sync_attachment` docstring which already points here.
