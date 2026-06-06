import os
import sys
import tempfile
import threading
import time
import unittest
import io
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from urllib.error import HTTPError

import kanban_forum_sync
from kanban_forum_sync import discord_forum
from kanban_forum_sync import service
from kanban_forum_sync import syncer
from kanban_forum_sync import tools
from kanban_forum_sync.kanban_bridge import KanbanBridge
from kanban_forum_sync.models import SyncMap, SyncState, ThreadMetaTracker
from kanban_forum_sync.discord_forum import DiscordForumClient, RateLimitError
from kanban_forum_sync.syncer import KanbanForumSyncer, _build_tag_tables


class FakeSyncMap:
    def items(self):
        return {"task-1": 123}


class FakeDiscord:
    def __init__(self, messages):
        self.messages = messages
        self.calls = []

    def get_thread_messages(self, thread_id, after=None, limit=50):
        self.calls.append((thread_id, after, limit))
        return self.messages

    def get_channel_by_id(self, thread_id):
        raise AssertionError("per-thread channel GET should not be used")


class FakeKanban:
    def __init__(self, fail_on_body=None):
        self.fail_on_body = fail_on_body
        self.comments = []

    def add_comment(self, task_id, author, body):
        if body == self.fail_on_body:
            return False
        self.comments.append((task_id, author, body))
        return True


class FakeTagKanban:
    def __init__(self):
        self.status_updates = []

    def get_task(self, task_id):
        return {"id": task_id, "status": "todo"}

    def update_task_status(self, task_id, status):
        self.status_updates.append((task_id, status))
        return True


class FakeCtx:
    def __init__(self):
        self.calls = []

    def dispatch_tool(self, tool_name, args):
        self.calls.append((tool_name, args))
        if tool_name == "kanban_create":
            return '{"ok": true, "task_id": "t_from_tool"}'
        return '{"ok": true}'


class FakeRegisterCtx:
    def __init__(self):
        self.cli = []
        self.tools = []
        self.commands = []

    def register_cli_command(self, name, help, setup_fn):
        self.cli.append((name, help, setup_fn))

    def register_tool(self, **kwargs):
        self.tools.append(kwargs)

    def register_command(self, **kwargs):
        self.commands.append(kwargs)


class FakeInjectCtx:
    def __init__(self):
        self.messages = []

    def inject_message(self, content, role="user"):
        self.messages.append((content, role))
        return True


class FakeStatusBridge(KanbanBridge):
    def __init__(self, ctx, status):
        super().__init__(ctx=ctx)
        self.status = status

    def get_task(self, task_id):
        return {"id": task_id, "status": self.status}


def make_message(message_id, content, bot=False):
    return {
        "id": str(message_id),
        "content": content,
        "author": {"id": str(message_id), "username": f"user-{message_id}", "bot": bot},
    }


class SyncSafetyTests(unittest.TestCase):
    def make_syncer(self, messages, kanban):
        obj = KanbanForumSyncer.__new__(KanbanForumSyncer)
        obj._sync_map = FakeSyncMap()
        obj._thread_meta = ThreadMetaTracker(path=os.path.join(self.tmpdir, "thread_meta.json"))
        obj.discord = FakeDiscord(messages)
        obj.kanban = kanban
        obj._state = SyncState()
        obj._bot_user_id = "bot"
        return obj

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self.tmp.name
        self._sleep = syncer.time.sleep
        syncer.time.sleep = lambda _seconds: None

    def tearDown(self):
        syncer.time.sleep = self._sleep
        self.tmp.cleanup()

    def test_build_tag_tables_keeps_backlog_out_of_status_to_tag(self):
        status_to_tag, tag_to_status, _emoji = _build_tag_tables("en")
        self.assertEqual(status_to_tag["triage"], "Triage")
        self.assertEqual(tag_to_status["Backlog"], "backlog")
        self.assertNotIn("backlog", status_to_tag)

    def test_descending_messages_advance_cursor_to_max_id_once(self):
        messages = [
            make_message(103, "third"),
            make_message(102, "second"),
            make_message(101, "first"),
        ]
        kanban = FakeKanban()
        obj = self.make_syncer(messages, kanban)

        obj._sync_forum_comments()

        self.assertEqual(
            kanban.comments,
            [
                ("task-1", "user-101", "first"),
                ("task-1", "user-102", "second"),
                ("task-1", "user-103", "third"),
            ],
        )
        self.assertEqual(obj._thread_meta.get_last_message_id(123), 103)

    def test_comment_failure_does_not_advance_past_failed_message(self):
        messages = [
            make_message(103, "third"),
            make_message(102, "second"),
            make_message(101, "first"),
        ]
        kanban = FakeKanban(fail_on_body="second")
        obj = self.make_syncer(messages, kanban)

        obj._sync_forum_comments()

        self.assertEqual(kanban.comments, [("task-1", "user-101", "first")])
        self.assertEqual(obj._thread_meta.get_last_message_id(123), 101)

    def test_comment_sync_skips_fetch_when_shared_thread_meta_shows_no_change(self):
        kanban = FakeKanban()
        obj = self.make_syncer([], kanban)
        obj._thread_meta.set_last_message_id(123, 200)

        obj._sync_forum_comments({123: {"id": "123", "last_message_id": "200"}})

        self.assertEqual(obj.discord.calls, [])
        self.assertEqual(kanban.comments, [])

    def test_tag_sync_uses_shared_thread_payload_instead_of_per_thread_get(self):
        obj = KanbanForumSyncer.__new__(KanbanForumSyncer)
        obj._sync_map = FakeSyncMap()
        obj._thread_meta = ThreadMetaTracker(path=os.path.join(self.tmpdir, "thread_meta.json"))
        obj.discord = FakeDiscord([])
        obj.kanban = FakeTagKanban()
        obj._state = SyncState()
        obj._reverse_tag_map = {7: "Running"}

        obj._sync_forum_tags({123: {"id": "123", "applied_tags": [7]}})

        self.assertEqual(obj.kanban.status_updates, [("task-1", "running")])

    def test_tag_sync_injects_agent_message_after_status_update(self):
        ctx = FakeInjectCtx()
        obj = KanbanForumSyncer.__new__(KanbanForumSyncer)
        obj._sync_map = FakeSyncMap()
        obj._thread_meta = ThreadMetaTracker(path=os.path.join(self.tmpdir, "thread_meta.json"))
        obj.discord = FakeDiscord([])
        obj.kanban = FakeTagKanban()
        obj._state = SyncState()
        obj._reverse_tag_map = {7: "Running"}
        obj._ctx = ctx

        obj._sync_forum_tags({123: {"id": "123", "applied_tags": [7]}})

        self.assertEqual(obj.kanban.status_updates, [("task-1", "running")])
        self.assertEqual(len(ctx.messages), 1)
        self.assertIn("task-1", ctx.messages[0][0])
        self.assertEqual(ctx.messages[0][1], "user")

    def test_tag_sync_skips_thread_absent_from_shared_list_without_dropping(self):
        # archived 先頭50件に載らない生存スレッドを誤って drop しないこと。
        dropped = []
        obj = KanbanForumSyncer.__new__(KanbanForumSyncer)
        obj._sync_map = FakeSyncMap()
        obj._thread_meta = ThreadMetaTracker(path=os.path.join(self.tmpdir, "thread_meta.json"))
        obj.discord = FakeDiscord([])
        obj.kanban = FakeTagKanban()
        obj._state = SyncState()
        obj._reverse_tag_map = {7: "Running"}
        obj._drop_stale_thread = lambda tid: dropped.append(tid)

        # 共有リストに thread 123 が無い（>50 archived シナリオ）
        obj._sync_forum_tags({999: {"id": "999", "applied_tags": [7]}})

        self.assertEqual(dropped, [])
        self.assertEqual(obj.kanban.status_updates, [])

    def test_corrupt_json_is_backed_up_and_loads_empty(self):
        path = os.path.join(self.tmpdir, "sync_map.json")
        with open(path, "w") as f:
            f.write("{")

        sync_map = SyncMap(path=path)

        self.assertEqual(sync_map.items(), {})
        backups = [name for name in os.listdir(self.tmpdir) if name.startswith("sync_map.json.corrupt.")]
        self.assertEqual(len(backups), 1)

    def test_kanban_writes_go_through_dispatch_tool(self):
        ctx = FakeCtx()
        bridge = KanbanBridge(ctx=ctx)
        # このテストは dispatch 経路の検証が主眼。プロファイル検証は別テストで
        # 扱うので空集合（検証スキップ）にして explicit assignee を素通しさせる。
        bridge._known_profiles = lambda: set()

        self.assertTrue(bridge.add_comment("t1", "alice", "hello"))
        task_id = bridge.create_task("Forum title", "body", assignee="router")

        self.assertEqual(task_id, "t_from_tool")
        self.assertEqual(ctx.calls[0][0], "kanban_comment")
        self.assertEqual(ctx.calls[0][1]["task_id"], "t1")
        self.assertIn("Discord: alice", ctx.calls[0][1]["body"])
        self.assertEqual(ctx.calls[1][0], "kanban_create")
        self.assertEqual(ctx.calls[1][1]["title"], "Forum title")
        self.assertEqual(ctx.calls[1][1]["assignee"], "router")

    def test_status_writes_use_semantic_kanban_tools_only(self):
        ctx = FakeCtx()

        self.assertTrue(FakeStatusBridge(ctx, "running").update_task_status("t1", "blocked"))
        self.assertTrue(FakeStatusBridge(ctx, "running").update_task_status("t1", "done"))
        self.assertTrue(FakeStatusBridge(ctx, "blocked").update_task_status("t1", "ready"))
        self.assertFalse(FakeStatusBridge(ctx, "running").update_task_status("t1", "todo"))

        self.assertEqual(
            [name for name, _args in ctx.calls],
            ["kanban_block", "kanban_complete", "kanban_unblock"],
        )

    def test_request_raises_rate_limit_error_after_dedicated_429_budget(self):
        client = DiscordForumClient("token", channel_id=1)
        client._MIN_REQUEST_INTERVAL = 0.0
        err = HTTPError(
            url="https://discord.com/api/v10/test",
            code=429,
            msg="Too Many Requests",
            hdrs={"Retry-After": "0.5"},
            fp=io.BytesIO(b'{"retry_after": 0.1}'),
        )

        with mock.patch.object(discord_forum, "urlopen", side_effect=err), \
             mock.patch.object(discord_forum.time, "sleep"), \
             mock.patch.object(discord_forum.time, "monotonic", return_value=0.0), \
             mock.patch.object(discord_forum.random, "uniform", return_value=1.0):
            with self.assertRaises(RateLimitError) as ctx:
                client._request("GET", "/test")

        self.assertEqual(ctx.exception.http_code, 429)
        self.assertIn("Rate limit retries exhausted", str(ctx.exception))

    def test_tool_handlers_return_json_and_never_raise_for_basic_paths(self):
        class FakeToolSyncer:
            channel_id = 42

            def __init__(self):
                self.state = SyncState()
                self.state.state = "running"
                self.calls = []

            def get_state(self):
                return self.state

            def incremental_sync(self):
                self.calls.append("incremental")

            def full_sync(self):
                self.calls.append("full")

        fake = FakeToolSyncer()
        with mock.patch.object(service, "get_syncer_or_none", return_value=fake):
            status = tools.json.loads(tools.kanban_forum_sync_status({}, extra=True))
            resync = tools.json.loads(
                tools.kanban_forum_sync_resync({"mode": "full"})
            )
            invalid = tools.json.loads(
                tools.kanban_forum_sync_resync({"mode": "bad"})
            )

        self.assertEqual(status["state"], "running")
        self.assertTrue(resync["ok"])
        self.assertEqual(resync["mode"], "full")
        self.assertEqual(fake.calls, ["full"])
        self.assertIn("invalid mode", invalid["error"])

    def test_register_declares_cli_tools_and_slash_command(self):
        ctx = FakeRegisterCtx()
        with mock.patch.object(service, "get_syncer") as get_syncer:
            get_syncer.side_effect = RuntimeError("missing token")
            kanban_forum_sync.register(ctx)

        self.assertEqual(ctx.cli[0][0], "kanban-forum-sync")
        self.assertEqual(
            sorted(tool["name"] for tool in ctx.tools),
            ["kanban_forum_sync_resync", "kanban_forum_sync_status"],
        )
        self.assertEqual(ctx.commands[0]["name"], "kanban-forum-sync")

    def test_slash_handler_routes_status_and_actions(self):
        class FakeSlashSyncer:
            channel_id = None

            def __init__(self):
                self.state = SyncState()
                self.calls = []

            def get_state(self):
                return self.state

            def full_sync(self):
                self.calls.append("full")

            def start(self):
                self.calls.append("start")

            def stop(self):
                self.calls.append("stop")

        fake = FakeSlashSyncer()
        with mock.patch.object(service, "get_syncer_or_none", return_value=fake):
            status = kanban_forum_sync._slash_handler("")
            sync_msg = kanban_forum_sync._slash_handler("sync")
            start_msg = kanban_forum_sync._slash_handler("start")
            stop_msg = kanban_forum_sync._slash_handler("stop")
            unknown = kanban_forum_sync._slash_handler("bad")

        self.assertIn("state=stopped", status)
        self.assertEqual(sync_msg, "Full sync complete.")
        self.assertEqual(start_msg, "Watcher started.")
        self.assertEqual(stop_msg, "Watcher stopped.")
        self.assertEqual(fake.calls, ["full", "start", "stop"])
        self.assertIn("unknown action", unknown)

    def test_incremental_sync_serializes_concurrent_cycles(self):
        # watcher スレッドとツール起動 resync が重なってもサイクルは直列化される。
        obj = KanbanForumSyncer.__new__(KanbanForumSyncer)
        obj._sync_lock = threading.RLock()
        state = {"active": 0, "max_active": 0}
        lock = threading.Lock()

        def fake_locked():
            with lock:
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
            time.sleep(0.05)
            with lock:
                state["active"] -= 1

        obj._incremental_sync_locked = fake_locked

        threads = [threading.Thread(target=obj.incremental_sync) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # ロックが効いていれば同時実行は常に 1 本
        self.assertEqual(state["max_active"], 1)


if __name__ == "__main__":
    unittest.main()
