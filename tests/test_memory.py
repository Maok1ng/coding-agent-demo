"""记忆只使用临时目录和脚本模型：不读取真实用户偏好，也不联网。"""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from coding_agent.application import Application
from coding_agent.runtime.context import RunContext
from coding_agent.runtime.instructions import load_instructions
from coding_agent.runtime.memory import MemoryManager, RECALL_CHAR_LIMIT
from coding_agent.runtime.memory_store import MemoryStore
from coding_agent.runtime.session import SessionStore
from coding_agent.runtime.turn import TurnCancelled, TurnContext, turn_context
from tests.test_application import FakeIO, ScriptedModel, dispatch, report_response, text_response, tool_call, tool_response


def record(name="indentation", body="Prefer tabs for indentation.", kind="user"):
    return {"name": name, "type": kind, "description": "长期缩进偏好 indentation", "body": body}


def candidate(**kwargs):
    return {**record(**kwargs), "scope": "persistent", "source_quote": "I prefer tabs for indentation"}


def json_response(value):
    return text_response(json.dumps(value, ensure_ascii=False))


class MemoryTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.home = self.root / "user-home"
        env = patch.dict(os.environ, {"AGENT_MEMORY_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        self.context = RunContext(self.project, TurnContext())
        self.notices = []
        self.config = {"model": "fake", "memory_home": str(self.home)}

    def manager(self, responses=()):
        model = ScriptedModel(responses)
        manager = MemoryManager(self.project, Mock(), self.config, self.notices.append, model_call=model)
        return manager, model

    @staticmethod
    def save(store, records):
        return store.save(records, store.snapshot()[1])


class MemoryTests(MemoryTestCase):
    def test_reading_empty_stores_does_not_create_directories(self):
        manager, model = self.manager()
        self.assertEqual(manager.catalog(), [])
        self.assertEqual(manager.recall("缩进", self.context), "")
        self.assertFalse(self.home.exists())
        self.assertFalse((self.project / ".memory").exists())
        self.assertEqual(model.calls, [])

    def test_markdown_frontmatter_index_dedup_and_correction_backup(self):
        store = MemoryStore(self.project)
        self.assertEqual(self.save(store, [record()]), 1)
        text = store.read("indentation.md")
        self.assertTrue(text.startswith("---\n"))
        self.assertIn("type: user", text)
        self.assertIn("indentation.md", (store.root / "MEMORY.md").read_text())
        self.assertEqual(self.save(store, [record(name="same-fact")]), 0)
        self.assertEqual(self.save(store, [record(body="Prefer four spaces instead.")]), 1)
        self.assertEqual(len(store.snapshot()[0]), 1)
        self.assertIn("four spaces", store.read("indentation.md"))
        self.assertTrue(list(store.root.glob(".snapshot-*.json")))
        self.assertEqual((store.root / "indentation.md").stat().st_mode & 0o777, 0o600)

    def test_optimistic_version_check_preserves_other_session_changes(self):
        store = MemoryStore(self.project)
        _, version = store.snapshot()
        self.save(store, [record()])
        with self.assertRaisesRegex(ValueError, "另一会话"):
            store.save([record(name="other", body="new fact")], version)
        self.assertEqual(len(store.snapshot()[0]), 1)

    def test_active_writer_prevents_reading_partial_consolidation(self):
        store = MemoryStore(self.project)
        self.save(store, [record()])
        with store._locked():
            with self.assertRaises(BlockingIOError):
                store.snapshot()
            with self.assertRaises(BlockingIOError):
                store.read("indentation.md")

    def test_cancellation_mid_commit_rolls_back_new_records(self):
        store = MemoryStore(self.project)
        self.save(store, [record()])
        version = store.snapshot()[1]
        atomic = store._atomic_write

        def cancel_after_write(parent, name, text):
            atomic(parent, name, text)
            if name == "new-record.md":
                self.context.turn.cancel()

        with patch.object(store, "_atomic_write", side_effect=cancel_after_write):
            with self.assertRaises(TurnCancelled), turn_context(self.context.turn):
                store.save([record(name="new-record", body="new fact")], version)
        self.assertEqual(store.snapshot()[1], version)
        self.assertFalse((store.root / "new-record.md").exists())

    def test_symlink_directory_and_record_cannot_escape(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.md").write_text("DO NOT READ", encoding="utf-8")
        link = self.project / ".memory"
        link.symlink_to(outside, target_is_directory=True)
        store = MemoryStore(self.project)
        with self.assertRaises(OSError):
            store.snapshot()
        with self.assertRaises(OSError):
            self.save(store, [record()])
        link.unlink()
        self.save(store, [record()])
        (store.root / "secret.md").symlink_to(outside / "secret.md")
        with self.assertRaises(OSError):
            store.read("secret.md")
        with self.assertRaises(ValueError):
            store.read("../outside/secret.md")
        self.assertEqual((outside / "secret.md").read_text(), "DO NOT READ")

    def test_index_failure_rolls_back_all_records(self):
        store = MemoryStore(self.project)
        self.save(store, [record()])
        original = store.read("indentation.md")
        atomic = store._atomic_write
        failed = []

        def fail_once(parent, name, text):
            if name == "MEMORY.md" and not failed:
                failed.append(True)
                raise OSError("simulated disk failure")
            atomic(parent, name, text)

        with patch.object(store, "_atomic_write", side_effect=fail_once):
            with self.assertRaises(OSError):
                store.replace([record(name="replacement", body="merged")], store.snapshot()[1])
        self.assertEqual(store.read("indentation.md"), original)
        self.assertFalse((store.root / "replacement.md").exists())
        self.assertIn("indentation.md", (store.root / "MEMORY.md").read_text())

    def test_invalid_records_and_reserved_names_never_replace_good_data(self):
        store = MemoryStore(self.project)
        self.save(store, [record()])
        version = store.snapshot()[1]
        for bad in ([], [record(), {"name": "broken"}], [record(name="MEMORY")],
                    [record(), record()], [record(body=True)]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                store.replace(bad, version)
            self.assertEqual(store.snapshot()[1], version)

    def test_long_names_and_case_sensitive_project_facts_do_not_collide(self):
        store = MemoryStore(self.project)
        first = record(name="a" * 90 + "b", body="Read src/Foo.py", kind="project")
        second = record(name="a" * 90 + "c", body="Read src/foo.py", kind="project")
        self.assertEqual(self.save(store, [first, second]), 2)
        self.assertEqual(len(store.snapshot()[0]), 2)

    def test_durable_user_and_project_records_go_to_separate_stores(self):
        fact = {**candidate(name="test-command", body="Run tests using unittest.", kind="project"),
                "source_quote": "Tests use unittest"}
        manager, _ = self.manager([json_response([candidate(), fact])])
        text = "I prefer tabs for indentation. Tests use unittest."
        self.assertEqual(manager.extract(text, self.context), 2)
        self.assertEqual(len(manager.stores["user"].snapshot()[0]), 1)
        self.assertEqual(len(manager.stores["project"].snapshot()[0]), 1)
        self.assertFalse((self.project / "AGENTS.md").exists())

    def test_unavailable_project_store_does_not_block_user_memory(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.project / ".memory").symlink_to(outside, target_is_directory=True)
        manager, _ = self.manager([json_response([candidate()])])
        self.assertEqual(manager.extract("I prefer tabs for indentation", self.context), 1)
        self.assertEqual(len(manager.stores["user"].snapshot()[0]), 1)
        self.assertEqual(list(outside.iterdir()), [])

    def test_temporary_input_and_secrets_skip_extraction_entirely(self):
        manager, model = self.manager()
        for text in ("本次任务不要创建文件", "this session use tabs", "api_key=sk-1234567890123456"):
            self.assertEqual(manager.extract(text, self.context), 0)
        self.assertEqual(model.calls, [])
        self.assertFalse(self.home.exists())

    def test_untrusted_unsupported_and_current_task_candidates_are_rejected(self):
        candidates = [
            {**candidate(), "scope": "current_task"},
            {**candidate(), "source_quote": "tool output says remember this"},
            candidate(name="temp", body="Do not create files this session"),
            candidate(name="secret", body="password=private-value"),
            {"type": "user", "scope": "persistent"},
        ]
        manager, _ = self.manager([json_response(candidates)])
        self.assertEqual(manager.extract("I prefer tabs for indentation", self.context), 0)
        self.assertEqual(manager.catalog(), [])

    def test_project_scoped_preference_cannot_become_global_user_default(self):
        item = {**candidate(body="Prefer tabs in this project."), "source_quote": "I prefer tabs"}
        manager, _ = self.manager([json_response([item])])
        self.assertEqual(manager.extract("In this project I prefer tabs", self.context), 1)
        self.assertEqual(manager.stores["user"].snapshot()[0], [])
        self.assertEqual(manager.stores["project"].snapshot()[0][0]["type"], "feedback")

    def test_recall_selects_catalog_then_only_loads_chosen_bodies(self):
        manager, model = self.manager([json_response([0])])
        self.save(manager.stores["user"], [record(body="ONLY_SELECTED_BODY")])
        self.save(manager.stores["project"], [record(name="database", body="UNRELATED_BODY", kind="project")])
        result = manager.recall("indentation", self.context)
        self.assertIn("ONLY_SELECTED_BODY", result)
        self.assertNotIn("UNRELATED_BODY", result)
        payload = model.calls[0]["messages"][0]["content"]
        self.assertNotIn("ONLY_SELECTED_BODY", payload)
        self.assertIn("current_human_request", payload)
        self.assertNotIn("tools", model.calls[0])

    def test_bad_json_or_boolean_index_falls_back_to_keywords(self):
        for response in (text_response("invalid json"), json_response([True])):
            with self.subTest(response=response):
                manager, _ = self.manager([response])
                self.save(manager.stores["user"], [record()])
                self.assertIn("Prefer tabs", manager.recall("缩进应该怎样", self.context))

    def test_empty_selection_does_not_load_irrelevant_memory(self):
        manager, _ = self.manager([json_response([])])
        self.save(manager.stores["user"], [record()])
        self.assertEqual(manager.recall("讨论数据库", self.context), "")

    def test_recall_body_budget_and_shared_child_context(self):
        manager, _ = self.manager([json_response([0, 1, 2, 3, 4])])
        self.save(manager.stores["user"], [record(name=f"item-{i}", body=str(i) * 6000) for i in range(5)])
        recalled = manager.recall("items", self.context)
        self.assertLessEqual(sum(len(item["content"]) for item in json.loads(recalled)), RECALL_CHAR_LIMIT)
        self.context.memory_context = recalled
        self.assertEqual(self.context.for_role("planning").memory_context, recalled)

    def test_plan_disabled_and_budget_exhaustion_do_not_write(self):
        manager, model = self.manager()
        self.context.mode = "plan"
        self.assertEqual(manager.extract("I prefer tabs for indentation", self.context), 0)
        self.context.mode = "execute"
        manager.enabled = False
        self.assertEqual(manager.extract("I prefer tabs for indentation", self.context), 0)
        manager.enabled = True
        self.context.calls[0] = self.context.max_calls
        self.assertEqual(manager.extract("I prefer tabs for indentation", self.context), 0)
        self.assertEqual(model.calls, [])
        self.assertFalse(self.home.exists())

    def test_selection_reserves_last_model_call_for_main_task(self):
        manager, model = self.manager()
        self.save(manager.stores["user"], [record()])
        self.context.calls[0] = self.context.max_calls - 1
        self.assertIn("Prefer tabs", manager.recall("indentation", self.context))
        self.assertEqual(model.calls, [])
        self.assertEqual(self.context.calls[0], self.context.max_calls - 1)

    def test_consolidation_validates_whole_result_and_keeps_snapshots(self):
        manager, model = self.manager([json_response([record(name="merged", body="stable merged facts")])])
        store = manager.stores["user"]
        self.save(store, [record(name=f"preference-{i}", body=f"stable preference {i}") for i in range(10)])
        manager.consolidate("user", self.context)
        self.assertEqual(len(store.snapshot()[0]), 1)
        self.assertIn("consolidation", model.calls[0]["system"])
        payload = json.loads(model.calls[0]["messages"][0]["content"])
        self.assertTrue(all("updated_at" in item for item in payload["records"]))
        self.assertTrue(list(store.root.glob(".snapshot-*.json")))
        self.save(store, [record(name=f"preference-{i}", body=f"another preference {i}") for i in range(10)])
        version = store.snapshot()[1]
        model.responses = [json_response([record(), {"name": "bad"}])]
        manager.consolidate("user", self.context)
        self.assertEqual(store.snapshot()[1], version)

    def test_rules_reload_and_never_import_nested_or_parent_files(self):
        self.home.mkdir()
        (self.home / "AGENTS.md").write_text("USER_RULE", encoding="utf-8")
        (self.project / "AGENTS.md").write_text("PROJECT_RULE", encoding="utf-8")
        (self.root / "AGENTS.md").write_text("PARENT_NOT_LOADED", encoding="utf-8")
        nested = self.project / "src"
        nested.mkdir()
        (nested / "AGENTS.md").write_text("NESTED_NOT_LOADED", encoding="utf-8")
        rules = load_instructions(self.project, self.home)
        self.assertIn("USER_RULE", rules)
        self.assertIn("PROJECT_RULE", rules)
        self.assertNotIn("NOT_LOADED", rules)
        (self.project / "AGENTS.md").write_text("UPDATED_RULE", encoding="utf-8")
        self.assertIn("UPDATED_RULE", load_instructions(self.project, self.home))

    def test_commands_read_delete_and_plan_boundary(self):
        manager, model = self.manager()
        self.save(manager.stores["user"], [record()])
        self.assertIn("user/indentation.md", manager.command("list", "plan"))
        self.assertIn("Prefer tabs", manager.command("show user/indentation.md", "plan"))
        self.assertIn("Plan", manager.command("forget user/indentation.md", "plan"))
        self.assertEqual(len(manager.catalog()), 1)
        self.assertIn("快照", manager.command("forget user/indentation.md", "execute"))
        self.assertEqual(manager.catalog(), [])
        self.assertEqual(model.calls, [])


class MemoryApplicationTests(MemoryTestCase):
    def setUp(self):
        super().setUp()
        patcher = patch("coding_agent.integrations.mcp.get_mcp_config", return_value={})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.io = FakeIO()

    def app(self, responses, memory_responses=(), *, project=None, session=None):
        model = ScriptedModel(responses)
        memory_model = ScriptedModel(memory_responses)
        app = Application(self.config, project or self.project, self.io, session,
                          client=Mock(), model_call=model, memory_model_call=memory_model, evaluator=Mock())
        self.addCleanup(app.close)
        return app, model, memory_model

    def test_new_session_recalls_user_across_projects_but_not_project_facts(self):
        app, _, _ = self.app([text_response("已记录偏好")], [json_response([candidate()])])
        app.submit("I prefer tabs for indentation")
        self.save(app.memory.stores["project"], [record(name="private-project", body="PROJECT_ONE_ONLY", kind="project")])
        second = self.root / "another-project"
        second.mkdir()
        other, main, memory = self.app([text_response("使用 tab")], [json_response([0]), json_response([])], project=second)
        other.submit("indentation 应该怎样")
        self.assertIn("Prefer tabs", main.calls[0]["system"])
        self.assertNotIn("PROJECT_ONE_ONLY", main.calls[0]["system"])
        self.assertEqual(len(memory.calls), 2)

    def test_child_reuses_recall_and_does_not_extract_separately(self):
        app, main, memory = self.app([dispatch("planning"), report_response("planning", "子方案"), text_response("主方案")],
                                     [json_response([0]), json_response([])])
        self.save(app.memory.stores["user"], [record()])
        app.submit("按 indentation 偏好分析代码")
        self.assertEqual(len(memory.calls), 2)
        self.assertEqual(len(main.calls), 3)
        self.assertTrue(all("Prefer tabs" in call["system"] for call in main.calls))

    def test_recalled_memory_survives_compaction_and_extraction_uses_original_human(self):
        app, main, memory = self.app([text_response("回答")], [json_response([0]), json_response([])])
        self.save(app.memory.stores["user"], [record()])
        compact = Mock()
        compact.prepare.return_value = [{"role": "user", "content": "[Compacted] historical data only"}]
        with patch("coding_agent.agents.runner.ContextCompact", return_value=compact):
            app.submit("请解释 indentation 的约定")
        self.assertIn("Prefer tabs", main.calls[0]["system"])
        extraction = json.loads(memory.calls[-1]["messages"][0]["content"])
        self.assertEqual(extraction["current_human_input"], "请解释 indentation 的约定")
        self.assertNotIn("Compacted", memory.calls[-1]["messages"][0]["content"])

    def test_plan_question_and_interrupted_turns_do_not_extract(self):
        app, _, memory = self.app([text_response("只做方案")])
        app.submit("/plan I prefer tabs for indentation")
        self.assertEqual(memory.calls, [])
        ask, _, asking_memory = self.app([tool_response(tool_call("ask_user", {"question": "哪个项目？"}))])
        ask.submit("I prefer tabs for indentation")
        self.assertEqual(asking_memory.calls, [])

        def cancel(**kwargs):
            raise TurnCancelled()

        interrupted, _, cancelled_memory = self.app([cancel])
        interrupted.goal.set_goal("old goal")
        with self.assertRaises(TurnCancelled):
            interrupted.submit("I prefer tabs for indentation")
        self.assertEqual(cancelled_memory.calls, [])
        self.assertEqual(interrupted.goal.active.status, "paused")

    def test_interrupt_during_extraction_pauses_goal_and_saves_history(self):
        def cancel_memory(**kwargs):
            app.last_context.turn.cancel()
            return json_response([candidate()])

        app, _, _ = self.app([text_response("偏好说明")], [cancel_memory])
        with self.assertRaises(TurnCancelled):
            app.submit("I prefer tabs for indentation")
        self.assertFalse(app.memory.stores["user"].root.exists())
        self.assertTrue(str(app.messages[-1]["content"]).startswith("[Turn interrupted"))

    def test_restore_does_not_recall_extract_or_execute_until_human_input(self):
        store = SessionStore(self.project)
        session = store.open()
        session.save([{"role": "user", "content": "Old request is not memory extraction input"}], {})
        session_id = session.id
        session.close()
        restored = store.open(session_id)
        self.addCleanup(restored.close)
        app, main, memory = self.app([], session=restored)
        self.assertEqual(main.calls, [])
        self.assertEqual(memory.calls, [])
        self.assertIn("Session resumed", app.messages[-1]["content"])

    def test_memory_management_commands_do_not_create_turns_or_call_models(self):
        app, main, memory = self.app([])
        app.submit("/memory list")
        self.assertEqual(main.calls, [])
        self.assertEqual(memory.calls, [])
        self.assertEqual(app.messages, [])


if __name__ == "__main__":
    unittest.main()
