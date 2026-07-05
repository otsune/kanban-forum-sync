import importlib.util
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from kanban_forum_sync.kanban_bridge import KanbanBridge


def _bridge():
    # db_path を明示して resolve_kanban_db_path()/DB 接続を回避（__init__ は接続しない）。
    return KanbanBridge(db_path="/tmp/kfs-default-assignee-test.db")


HERMES_CLI_AVAILABLE = importlib.util.find_spec("hermes_cli") is not None


@unittest.skipIf(not HERMES_CLI_AVAILABLE, "hermes_cli not installed in this environment")
class ConfigDefaultAssigneeTest(unittest.TestCase):
    """_config_default_assignee(): load_config 経由・kanban 配下・キャッシュ。"""

    def test_reads_kanban_default_assignee(self):
        b = _bridge()
        with mock.patch("hermes_cli.config.load_config",
                        return_value={"kanban": {"default_assignee": "alice"}}):
            self.assertEqual(b._config_default_assignee(), "alice")

    def test_missing_section_or_key_returns_none(self):
        for cfg in ({}, {"kanban": {}}, {"kanban": {"default_assignee": ""}},
                    {"kanban": {"default_assignee": "   "}}):
            b = _bridge()
            with mock.patch("hermes_cli.config.load_config", return_value=cfg):
                self.assertIsNone(b._config_default_assignee())

    def test_value_is_stripped(self):
        b = _bridge()
        with mock.patch("hermes_cli.config.load_config",
                        return_value={"kanban": {"default_assignee": "  bob  "}}):
            self.assertEqual(b._config_default_assignee(), "bob")

    def test_result_is_cached_across_calls(self):
        b = _bridge()
        with mock.patch("hermes_cli.config.load_config",
                        return_value={"kanban": {"default_assignee": "alice"}}) as lc:
            self.assertEqual(b._config_default_assignee(), "alice")
            self.assertEqual(b._config_default_assignee(), "alice")
            self.assertEqual(b._config_default_assignee(), "alice")
            self.assertEqual(lc.call_count, 1)  # 2回目以降は再読込しない

    def test_none_result_is_also_cached(self):
        b = _bridge()
        with mock.patch("hermes_cli.config.load_config",
                        return_value={"kanban": {}}) as lc:
            self.assertIsNone(b._config_default_assignee())
            self.assertIsNone(b._config_default_assignee())
            self.assertEqual(lc.call_count, 1)  # None も _UNSET と区別してキャッシュ

    def test_load_config_failure_returns_none(self):
        b = _bridge()
        with mock.patch("hermes_cli.config.load_config", side_effect=RuntimeError("boom")):
            self.assertIsNone(b._config_default_assignee())


class CreateTaskAssigneePrecedenceTest(unittest.TestCase):
    """create_task(): assignee 解決順 explicit > env > config > HERMES_PROFILE。"""

    def _bridge_capturing(self):
        b = _bridge()
        captured = {}

        def fake_dispatch(tool, args):
            captured["tool"] = tool
            captured["args"] = args
            return {"task_id": "42"}

        b._dispatch_kanban_tool = fake_dispatch
        # 既知プロファイル検証は別テストで扱う。ここは解決順だけを検証したいので
        # 空集合（= 検証スキップ）にして任意の assignee 名を通す。
        b._known_profiles = lambda: set()
        return b, captured

    def test_explicit_assignee_wins(self):
        b, cap = self._bridge_capturing()
        b._config_default_assignee = lambda: "cfguser"
        with mock.patch.dict(os.environ,
                             {"FORUM_SYNC_DEFAULT_ASSIGNEE": "envuser", "HERMES_PROFILE": "prof"}):
            self.assertEqual(b.create_task("t", assignee="explicit"), "42")
        self.assertEqual(cap["args"]["assignee"], "explicit")

    def test_env_over_config_and_profile(self):
        b, cap = self._bridge_capturing()
        b._config_default_assignee = lambda: "cfguser"
        with mock.patch.dict(os.environ,
                             {"FORUM_SYNC_DEFAULT_ASSIGNEE": "envuser", "HERMES_PROFILE": "prof"}):
            b.create_task("t")
        self.assertEqual(cap["args"]["assignee"], "envuser")

    def test_config_over_profile(self):
        b, cap = self._bridge_capturing()
        b._config_default_assignee = lambda: "cfguser"
        with mock.patch.dict(os.environ, {"HERMES_PROFILE": "prof"}):
            os.environ.pop("FORUM_SYNC_DEFAULT_ASSIGNEE", None)
            b.create_task("t")
        self.assertEqual(cap["args"]["assignee"], "cfguser")

    def test_profile_is_last_fallback(self):
        b, cap = self._bridge_capturing()
        b._config_default_assignee = lambda: None
        with mock.patch.dict(os.environ, {"HERMES_PROFILE": "prof"}):
            os.environ.pop("FORUM_SYNC_DEFAULT_ASSIGNEE", None)
            b.create_task("t")
        self.assertEqual(cap["args"]["assignee"], "prof")

    def test_no_source_errors_without_dispatch(self):
        b, cap = self._bridge_capturing()
        b._config_default_assignee = lambda: None
        with mock.patch.dict(os.environ, {}):
            os.environ.pop("FORUM_SYNC_DEFAULT_ASSIGNEE", None)
            os.environ.pop("HERMES_PROFILE", None)
            self.assertIsNone(b.create_task("t"))
        self.assertNotIn("args", cap)  # 解決不能なら dispatch しない


class CreateTaskAssigneeValidationTest(unittest.TestCase):
    """create_task(): 実在プロファイルに限定する検証＋フォールバック。"""

    def _bridge_capturing(self):
        b = _bridge()
        captured = {}

        def fake_dispatch(tool, args):
            captured["tool"] = tool
            captured["args"] = args
            return {"task_id": "42"}

        b._dispatch_kanban_tool = fake_dispatch
        return b, captured

    def test_unknown_assignee_skipped_falls_back_to_valid(self):
        # FORUM_SYNC_DEFAULT_ASSIGNEE=otsune（実在しない）→ 弾いて config の main へ。
        b, cap = self._bridge_capturing()
        b._known_profiles = lambda: {"main"}
        b._config_default_assignee = lambda: "main"
        with mock.patch.dict(os.environ, {"FORUM_SYNC_DEFAULT_ASSIGNEE": "otsune"}):
            os.environ.pop("HERMES_PROFILE", None)
            self.assertEqual(b.create_task("t"), "42")
        self.assertEqual(cap["args"]["assignee"], "main")

    def test_explicit_unknown_skipped_for_valid_env(self):
        b, cap = self._bridge_capturing()
        b._known_profiles = lambda: {"url_memo"}
        b._config_default_assignee = lambda: None
        with mock.patch.dict(os.environ, {"FORUM_SYNC_DEFAULT_ASSIGNEE": "url_memo"}):
            os.environ.pop("HERMES_PROFILE", None)
            self.assertEqual(b.create_task("t", assignee="otsune"), "42")
        self.assertEqual(cap["args"]["assignee"], "url_memo")

    def test_case_insensitive_match(self):
        b, cap = self._bridge_capturing()
        b._known_profiles = lambda: {"main"}
        b._config_default_assignee = lambda: None
        with mock.patch.dict(os.environ, {"FORUM_SYNC_DEFAULT_ASSIGNEE": "Main"}):
            os.environ.pop("HERMES_PROFILE", None)
            self.assertEqual(b.create_task("t"), "42")
        self.assertEqual(cap["args"]["assignee"], "Main")

    def test_all_invalid_returns_none_without_dispatch(self):
        b, cap = self._bridge_capturing()
        b._known_profiles = lambda: {"main"}
        b._config_default_assignee = lambda: None
        with mock.patch.dict(os.environ,
                             {"FORUM_SYNC_DEFAULT_ASSIGNEE": "otsune",
                              "HERMES_PROFILE": "ghost"}):
            self.assertIsNone(b.create_task("t"))
        self.assertNotIn("args", cap)

    def test_empty_known_set_disables_validation(self):
        # プロファイル集合が取得できない場合は検証せず従来動作（任意名を通す）。
        b, cap = self._bridge_capturing()
        b._known_profiles = lambda: set()
        b._config_default_assignee = lambda: None
        with mock.patch.dict(os.environ, {"FORUM_SYNC_DEFAULT_ASSIGNEE": "otsune"}):
            os.environ.pop("HERMES_PROFILE", None)
            self.assertEqual(b.create_task("t"), "42")
        self.assertEqual(cap["args"]["assignee"], "otsune")


if __name__ == "__main__":
    unittest.main()
