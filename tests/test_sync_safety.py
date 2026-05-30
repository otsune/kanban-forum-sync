import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from kanban_forum_sync import syncer
from kanban_forum_sync.kanban_bridge import KanbanBridge
from kanban_forum_sync.models import SyncMap, SyncState, ThreadMetaTracker
from kanban_forum_sync.syncer import KanbanForumSyncer, _build_tag_tables


class FakeSyncMap:
    def items(self):
        return {"task-1": 123}


class FakeDiscord:
    def __init__(self, messages):
        self.messages = messages

    def get_thread_messages(self, thread_id, after=None, limit=50):
        return self.messages


class FakeKanban:
    def __init__(self, fail_on_body=None):
        self.fail_on_body = fail_on_body
        self.comments = []

    def add_comment(self, task_id, author, body):
        if body == self.fail_on_body:
            return False
        self.comments.append((task_id, author, body))
        return True


class FakeCtx:
    def __init__(self):
        self.calls = []

    def dispatch_tool(self, tool_name, args):
        self.calls.append((tool_name, args))
        if tool_name == "kanban_create":
            return '{"ok": true, "task_id": "t_from_tool"}'
        return '{"ok": true}'


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


if __name__ == "__main__":
    unittest.main()
