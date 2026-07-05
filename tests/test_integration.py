import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.error import HTTPError
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import kanban_forum_sync
from kanban_forum_sync import discord_forum
from kanban_forum_sync import service
from kanban_forum_sync import syncer
from kanban_forum_sync.kanban_bridge import KanbanBridge
from kanban_forum_sync.models import SyncMap, SyncState, ThreadMetaTracker, SyncOriginTracker
from kanban_forum_sync.discord_forum import DiscordForumClient, RateLimitError
from kanban_forum_sync.syncer import KanbanForumSyncer, _build_tag_tables


SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT,
    body TEXT,
    status TEXT DEFAULT 'todo',
    priority INTEGER DEFAULT 0,
    assignee TEXT,
    created_at REAL,
    completed_at REAL
);
CREATE TABLE IF NOT EXISTS task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    kind TEXT,
    payload TEXT,
    created_at REAL DEFAULT (strftime('%s','now'))
);
CREATE TABLE IF NOT EXISTS task_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    author TEXT,
    body TEXT,
    created_at REAL DEFAULT (strftime('%s','now'))
);
"""


def _init_db(path: str):
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def _insert_task(path: str, task_id: str, status: str = "todo", title: str = "Task"):
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at) VALUES (?, ?, ?, strftime('%s','now'))",
        (task_id, title, status),
    )
    conn.commit()
    conn.close()


def _insert_event(path: str, task_id: str, kind: str = "spawned", payload: str = "{}"):
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO task_events (task_id, kind, payload) VALUES (?, ?, ?)",
        (task_id, kind, payload),
    )
    conn.commit()
    conn.close()


def _insert_comment(path: str, task_id: str, author: str = "human", body: str = "hi"):
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO task_comments (task_id, author, body) VALUES (?, ?, ?)",
        (task_id, author, body),
    )
    conn.commit()
    conn.close()


def _latest_event_id(path: str) -> int:
    conn = sqlite3.connect(path, timeout=5)
    row = conn.execute("SELECT MAX(id) as max_id FROM task_events").fetchone()
    conn.close()
    return row[0] or 0


class FakeDiscord:
    def __init__(self):
        self.created_threads = []
        self.updated_threads = []
        self.posted_messages = []
        self.messages_by_thread = {}
        self.raise_on = None
        self.channel_response = {"type": 15, "name": "kanban", "id": "101", "guild_id": "202"}

    def get_thread_messages(self, thread_id, after=None, limit=50):
        if self.raise_on == "get_thread_messages":
            raise discord_forum.DiscordForumError("boom")
        return self.messages_by_thread.get(thread_id, [])

    def get_channel(self):
        if self.raise_on == "get_channel":
            raise discord_forum.NotFoundError("missing", 404)
        return self.channel_response

    def get_bot_guilds(self):
        return [{"id": "202", "name": "TestServer"}]

    def create_thread(self, name, content=None, applied_tags=None):
        if self.raise_on in ("create_thread", "boom"):
            raise discord_forum.DiscordForumError("create failed")
        tid = 1000 + len(self.created_threads)
        self.created_threads.append((name, content, applied_tags, tid))
        return {"id": str(tid), "name": name}

    def update_thread(self, thread_id, **kwargs):
        if self.raise_on == "update_thread":
            raise discord_forum.DiscordForumError("update failed")
        self.updated_threads.append((thread_id, kwargs))
        return True

    def send_message(self, thread_id, content):
        if self.raise_on == "send_message":
            raise discord_forum.DiscordForumError("post failed")
        mid = len(self.posted_messages) + 1
        self.posted_messages.append((thread_id, content, mid))
        return {"id": mid}

    def find_forum_channel(self, guild_id):
        return {"id": 101, "name": "kanban", "type": 15}

    def create_forum_channel(self, guild_id, name, topic):
        tid = 500
        return {"id": tid, "name": name}


class FakeCtx:
    def __init__(self, known_profiles=None):
        self.calls = []
        self.injected = []
        self._known_profiles = known_profiles

    def dispatch_tool(self, tool_name, args):
        self.calls.append((tool_name, args))
        if tool_name == "kanban_comment":
            return '{"ok": true}'
        if tool_name == "kanban_block":
            return '{"ok": true}'
        if tool_name == "kanban_complete":
            return '{"ok": true}'
        if tool_name == "kanban_unblock":
            return '{"ok": true}'
        if tool_name == "kanban_create":
            return '{"ok": true, "task_id": "t_new"}'
        return '{"ok": true}'

    def inject_message(self, content, role="user"):
        self.injected.append((content, role))
        return True

    def get_known_profiles(self):
        return self._known_profiles or set()

class FakeKnownProfilesBridge(KanbanBridge):
    def __init__(self, db_path, ctx=None, known_profiles=None):
        super().__init__(db_path=db_path, ctx=ctx)
        self._known_profiles_cache = known_profiles or set()


class FakeToolSyncer:
    channel_id = 42

    def __init__(self, state="stopped"):
        self.state_obj = SyncState()
        self.state_obj.state = state
        self.calls = []

    def get_state(self):
        return self.state_obj

    def incremental_sync(self):
        self.calls.append("incremental")

    def full_sync(self):
        self.calls.append("full")


class IntegrationTests(unittest.TestCase):
    def _make_bridge(self, db_path: str, ctx=None, known_profiles=None):
        return FakeKnownProfilesBridge(db_path=db_path, ctx=ctx, known_profiles=known_profiles or {"main"})

    def test_end_to_end_sync_new_task_creates_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "kanban.db")
            _init_db(db)
            _insert_task(db, "t1", status="running", title="Task 1")
            _insert_event(db, "t1", kind="spawned")

            ctx = FakeCtx()
            bridge = self._make_bridge(db, ctx)
            state_dir = tmp

            sync_map = SyncMap(path=os.path.join(state_dir, "sync_map.json"))
            origin = SyncOriginTracker(path=os.path.join(state_dir, "origin_map.json"))
            meta = ThreadMetaTracker(path=os.path.join(state_dir, "thread_meta.json"))

            obj = KanbanForumSyncer.__new__(KanbanForumSyncer)
            obj.kanban = bridge
            obj.discord = FakeDiscord()
            obj._sync_map = sync_map
            obj._origin_tracker = origin
            obj._thread_meta = meta
            obj._state = SyncState()
            obj._bot_user_id = "bot"
            obj._channel_id = 101
            obj._sync_lock = threading.RLock()
            obj._rate_backoff = 0
            obj._last_cycle_completed_at = 0.0
            obj._tag_map = {}
            obj._reverse_tag_map = {}
            obj._ctx = ctx

            # Build tags
            s2t, t2s, _ = _build_tag_tables("en")
            obj._tag_map = s2t
            obj._reverse_tag_map = {v: k for k, v in t2s.items()}

            changed = bridge.get_tasks_changed_since_event(0)
            obj._state.last_event_id = _latest_event_id(db)
            obj._sync_task_to_forum(changed[0] if changed else None)

            self.assertEqual(len(obj.discord.created_threads), 1)
            title, *_ = obj.discord.created_threads[0]
            self.assertIn("Task 1", title)
            self.assertEqual(origin.get_origin("t1"), "kanban")

    def test_bidirectional_comment_sync_persists_to_kanban(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "kanban.db")
            _init_db(db)
            _insert_task(db, "t1", status="running", title="Task 1")

            ctx = FakeCtx()
            bridge = self._make_bridge(db, ctx)
            meta = ThreadMetaTracker(path=os.path.join(tmp, "thread_meta.json"))
            meta.set_last_message_id(101, 0)

            obj = KanbanForumSyncer.__new__(KanbanForumSyncer)
            obj.kanban = bridge
            obj.discord = FakeDiscord()
            obj._thread_meta = meta
            obj._state = SyncState()
            obj._bot_user_id = "bot"
            obj._sync_map = SyncMap(path=os.path.join(tmp, "sync_map.json"))
            obj._sync_map.set("t1", 101)

            obj.discord.messages_by_thread[101] = [
                {"id": "1", "content": "hello", "author": {"id": "9", "bot": False}},
                {"id": "2", "content": "world", "author": {"id": "10", "bot": False}},
            ]
            obj._sync_forum_comments()

            comments = ctx.calls
            self.assertEqual(len(comments), 2)
            self.assertEqual(comments[0][0], "kanban_comment")
            self.assertIn("hello", comments[0][1]["body"])
            self.assertEqual(meta.get_last_message_id(101), 2)

    def test_conflict_resolution_incremental_sync_serial(self):
        obj = KanbanForumSyncer.__new__(KanbanForumSyncer)
        obj._sync_lock = threading.RLock()
        concurrency = {"active": 0, "max_active": 0}
        lock = threading.Lock()

        def fake_locked():
            with lock:
                concurrency["active"] += 1
                concurrency["max_active"] = max(concurrency["max_active"], concurrency["active"])
            time.sleep(0.05)
            with lock:
                concurrency["active"] -= 1

        obj._incremental_sync_locked = fake_locked
        threads = [threading.Thread(target=obj.incremental_sync) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(concurrency["max_active"], 1)

    def test_sync_failure_does_not_advance_cursor_on_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "kanban.db")
            _init_db(db)
            _insert_task(db, "t1", status="running")

            ctx = FakeCtx()
            bridge = self._make_bridge(db, ctx)
            meta = ThreadMetaTracker(path=os.path.join(tmp, "thread_meta.json"))
            meta.set_last_message_id(101, 0)

            obj = KanbanForumSyncer.__new__(KanbanForumSyncer)
            obj.kanban = bridge
            obj.discord = FakeDiscord()
            obj.discord.raise_on = "get_thread_messages"
            obj._thread_meta = meta
            obj._state = SyncState()
            obj._bot_user_id = "bot"
            obj._sync_map = SyncMap(path=os.path.join(tmp, "sync_map.json"))
            obj._sync_map.set("t1", 101)

            obj._sync_forum_comments()
            self.assertEqual(ctx.calls, [])
            self.assertEqual(meta.get_last_message_id(101), 0)

    def test_data_consistency_after_full_cycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "kanban.db")
            _init_db(db)
            _insert_task(db, "t1", title="Alpha", status="todo")
            _insert_event(db, "t1", kind="spawned")
            _insert_comment(db, "t1", author="alice", body="first")

            ctx = FakeCtx()
            bridge = self._make_bridge(db, ctx)
            sync_map = SyncMap(path=os.path.join(tmp, "sync_map.json"))
            meta = ThreadMetaTracker(path=os.path.join(tmp, "thread_meta.json"))
            origin = SyncOriginTracker(path=os.path.join(tmp, "origin_map.json"))

            obj = KanbanForumSyncer.__new__(KanbanForumSyncer)
            obj.kanban = bridge
            obj.discord = FakeDiscord()
            obj._sync_map = sync_map
            obj._origin_tracker = origin
            obj._thread_meta = meta
            obj._state = SyncState()
            obj._bot_user_id = "bot"
            obj._channel_id = 101
            obj._sync_lock = threading.RLock()
            obj._rate_backoff = 0
            obj._last_cycle_completed_at = 0.0
            obj._tag_map = {}
            obj._reverse_tag_map = {}
            obj._ctx = ctx

            s2t, t2s, _ = _build_tag_tables("en")
            obj._tag_map = s2t
            obj._reverse_tag_map = {v: k for k, v in t2s.items()}

            # Phase 1: create thread for new task
            changed = bridge.get_tasks_changed_since_event(0)
            obj._state.last_event_id = _latest_event_id(db)
            if changed:
                obj._sync_task_to_forum(changed[0])
            self.assertEqual(len(obj.discord.created_threads), 1)
            thread_id = obj.discord.created_threads[0][3]

            # Phase 2: post kanban comment/event to thread
            obj._sync_kanban_comments_to_forum()
            self.assertGreater(len(obj.discord.posted_messages), 0)

            # Phase 2 reverse: forum reply -> kanban comment
            obj.discord.messages_by_thread[thread_id] = [
                {"id": "50", "content": "discord-reply", "author": {"id": "99", "bot": False}},
            ]
            meta.set_last_message_id(thread_id, 0)
            obj._sync_forum_comments()
            self.assertEqual(len(ctx.calls), 1)
            self.assertIn("discord-reply", ctx.calls[0][1]["body"])

            # Verify persistence files are consistent
            self.assertEqual(sync_map.get("t1"), thread_id)
            self.assertEqual(meta.get_last_message_id(thread_id), 50)
            self.assertEqual(origin.get_origin("t1"), "kanban")

    def test_rate_limit_retry_then_success(self):
        client = DiscordForumClient("token", channel_id=1)
        client._MIN_REQUEST_INTERVAL = 0.0
        call_log = []

        class _FakeResp:
            headers = {}
            def read(self):
                return b'{"id":"1"}'
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False

        def fake_urlopen(request, **kwargs):
            call_log.append(request.full_url)
            if len(call_log) < 3:
                err = HTTPError(
                    url=request.full_url,
                    code=429,
                    msg="Too Many Requests",
                    hdrs={},
                    fp=io.BytesIO(b'{"retry_after": 0.01}'),
                )
                err.headers = {"Retry-After": "0.01"}
                raise err
            return _FakeResp()

        with mock.patch.object(discord_forum, "urlopen", side_effect=fake_urlopen), \
             mock.patch.object(discord_forum.time, "sleep"), \
             mock.patch.object(discord_forum.time, "monotonic", return_value=0.0), \
             mock.patch.object(discord_forum.random, "uniform", return_value=1.0):
            client._request("GET", "/test")

        self.assertEqual(len(call_log), 3)
        self.assertEqual(call_log[0], call_log[1])
        self.assertEqual(call_log[0], call_log[2])

    def test_db_isolation_via_bridge_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "custom_kanban.db")
            _init_db(db)
            _insert_task(db, "t1", status="blocked")
            _insert_event(db, "t1", kind="blocked", payload='{"reason":"needs input"}')

            ctx = FakeCtx()
            bridge = self._make_bridge(db, ctx)
            task = bridge.get_task("t1")
            self.assertEqual(task["status"], "blocked")

            events = bridge.get_events_since("t1", 0, kinds=["blocked"])
            self.assertEqual(len(events), 1)
            self.assertIn("needs input", events[0]["payload"])


if __name__ == "__main__":
    unittest.main()
