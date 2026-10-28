"""真实文件工具的预览、审批、落盘闭环；只使用临时工作区，不请求模型。"""

import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from coding_agent.runtime import tools, workspace
from coding_agent.runtime.context import RunContext
from coding_agent.runtime.tools import ToolRuntime
from coding_agent.runtime.turn import SessionExit, TurnCancelled, TurnContext, turn_context


class ChangePreviewTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        for patcher in (patch.object(workspace, "WORKDIR", self.root),
                        patch("coding_agent.integrations.mcp.get_mcp_config", return_value={})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.hook = patch.object(tools, "trigger_hook", return_value=None)
        self.trigger_hook = self.hook.start()
        self.addCleanup(self.hook.stop)
        self.events = []
        self.io = SimpleNamespace(emit=self.events.append, confirm=Mock(return_value=True))

    def runtime(self, mode="ask", role="implementation", plan="execute"):
        return ToolRuntime(RunContext(self.root, TurnContext(), role=role, mode=plan,
                                      approval_mode=mode), self.io)

    def invoke(self, runtime, name="write_file", **arguments):
        with turn_context(runtime.context.turn):
            return runtime.execute(SimpleNamespace(name=name, input=arguments, id="preview-test"))

    def fixture(self, runtime, path="main.py", content="before\n"):
        target = self.root / path
        target.write_text(content, encoding="utf-8")
        runtime.files.read(path)
        return target

    def preview(self):
        return next(event for event in self.events if event["type"] == "change_preview")

    def test_preview_precedes_consent_and_binds_diff_and_hashes_to_actual_write(self):
        runtime = self.runtime()
        target = self.fixture(runtime)

        def consent(name, arguments, reason):
            self.assertEqual(target.read_text(), "before\n")
            event = self.preview()
            self.assertIn("-before\n+after", event["diff"])
            self.assertEqual(event["approval_action"], "confirm")
            self.assertNotIn("_change_preview", arguments)
            return True

        self.io.confirm.side_effect = consent
        output = self.invoke(runtime, "edit_file", path="main.py", old_text="before", new_text="after")
        self.assertFalse(output.is_error)
        self.assertEqual(target.read_text(), "after\n")
        event = self.preview()
        self.assertIn(event["diff"], output)
        artifact = json.loads(Path(event["reference"]).read_text())
        self.assertEqual(artifact["diff"], event["diff"])
        self.assertEqual(artifact["after_sha256"], hashlib.sha256(target.read_bytes()).hexdigest())
        approval = next(item for item in runtime.context.evidence if item.get("kind") == "approval")
        self.assertEqual(approval["change_reference"], event["reference"])
        self.assertEqual(approval["before_sha256"], hashlib.sha256(b"before\n").hexdigest())
        self.assertEqual(approval["after_sha256"], event["after_sha256"])

    def test_rejected_new_candidate_creates_no_target_directory_and_is_not_reused(self):
        runtime = self.runtime()
        self.io.confirm.return_value = False
        output = self.invoke(runtime, path="new/main.py", content="candidate\n")
        self.assertTrue(output.is_error)
        self.assertFalse((self.root / "new").exists())
        self.assertEqual(runtime.context.versions, {})
        self.assertIn("--- /dev/null", self.preview()["diff"])
        #拒绝不缓存成批准；下一次调用重新生成自己的候选。
        self.events.clear()
        self.io.confirm.return_value = True
        self.assertFalse(self.invoke(runtime, path="new/main.py", content="different\n").is_error)
        self.assertEqual((self.root / "new/main.py").read_text(), "different\n")
        self.assertEqual(self.io.confirm.call_count, 2)

    def test_auto_and_full_preview_without_adding_human_confirmation(self):
        for mode in ("auto", "full"):
            with self.subTest(mode=mode):
                self.events.clear()
                runtime = self.runtime(mode)
                self.assertFalse(self.invoke(runtime, path=f"{mode}.py", content="value = 1\n").is_error)
                self.assertEqual(self.preview()["approval_action"], "allow")
                self.assertEqual(self.preview()["approval_mode"], mode)
        self.io.confirm.assert_not_called()

    def test_auto_sensitive_file_still_requires_confirmation_after_preview(self):
        runtime = self.runtime("auto")
        self.io.confirm.return_value = False
        output = self.invoke(runtime, path="AGENTS.md", content="project rules\n")
        self.assertTrue(output.is_error)
        self.assertFalse((self.root / "AGENTS.md").exists())
        self.assertEqual(self.preview()["approval_action"], "confirm")
        self.io.confirm.assert_called_once()

    def test_changed_file_cannot_be_authorized_by_rereading_during_confirmation(self):
        runtime = self.runtime()
        target = self.fixture(runtime)

        def consent(*args):
            target.write_text("user changed\n", encoding="utf-8")
            runtime.files.read("main.py")
            return True

        self.io.confirm.side_effect = consent
        output = self.invoke(runtime, path="main.py", content="candidate\n")
        self.assertTrue(output.is_error)
        self.assertIn("changed before writing", output)
        self.assertEqual(target.read_text(), "user changed\n")
        self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def test_new_file_created_during_confirmation_is_preserved(self):
        runtime = self.runtime()
        target = self.root / "new.py"

        def consent(*args):
            target.write_text("user created\n", encoding="utf-8")
            return True

        self.io.confirm.side_effect = consent
        output = self.invoke(runtime, path="new.py", content="candidate\n")
        self.assertTrue(output.is_error)
        self.assertIn("created concurrently", output)
        self.assertEqual(target.read_text(), "user created\n")

    def test_cancellation_and_exit_in_confirmation_never_write(self):
        for interruption in (TurnCancelled("Esc"), KeyboardInterrupt(), SessionExit()):
            with self.subTest(interruption=type(interruption).__name__):
                self.events.clear()
                runtime = self.runtime()
                self.io.confirm.side_effect = interruption
                with self.assertRaises(type(interruption)):
                    self.invoke(runtime, path="new.py", content="candidate\n")
                self.assertFalse((self.root / "new.py").exists())
                self.assertFalse(runtime.context.evidence[-1]["approved"])

    def test_cancel_arriving_with_approval_prevents_apply(self):
        runtime = self.runtime()

        def consent(*args):
            runtime.context.turn.cancel()
            return True

        self.io.confirm.side_effect = consent
        with self.assertRaises(TurnCancelled):
            self.invoke(runtime, path="new/main.py", content="candidate\n")
        self.assertFalse((self.root / "new").exists())

    def test_argument_mutation_in_confirmation_or_hook_cannot_change_candidate(self):
        runtime = self.runtime()
        block = SimpleNamespace(name="write_file", input={"path": "original.py", "content": "approved\n"})

        def consent(name, arguments, reason):
            arguments["content"] = "modified in prompt\n"
            block.input["path"] = "redirect.py"
            return True

        def hook(kind, source, *args):
            if kind == "PreToolUse":
                source.input["content"] = "modified in hook\n"

        self.io.confirm.side_effect = consent
        self.trigger_hook.side_effect = hook
        with turn_context(runtime.context.turn):
            output = runtime.execute(block)
        self.assertFalse(output.is_error)
        self.assertEqual((self.root / "original.py").read_text(), "approved\n")
        self.assertFalse((self.root / "redirect.py").exists())

    def test_hook_denial_or_permission_change_after_approval_prevents_apply(self):
        for action in ("deny", "plan"):
            with self.subTest(action=action):
                runtime = self.runtime()

                def hook(kind, *args):
                    if kind == "PreToolUse":
                        if action == "deny":
                            return "policy rejection"
                        runtime.context.mode = "plan"

                self.trigger_hook.side_effect = hook
                output = self.invoke(runtime, path="new.py", content="candidate\n")
                self.assertTrue(output.is_error)
                self.assertFalse((self.root / "new.py").exists())

    def test_invalid_or_forbidden_writes_have_no_preview_or_confirmation(self):
        runtime = self.runtime()
        self.fixture(runtime, content="duplicate duplicate\n")
        invalid = self.invoke(runtime, "edit_file", path="main.py", old_text="duplicate", new_text="new")
        self.assertTrue(invalid.is_error)
        for role, mode in (("implementation", "plan"), ("planning", "execute"), ("orchestrator", "execute")):
            output = self.invoke(self.runtime(role=role, plan=mode), path="new.py", content="candidate\n")
            self.assertTrue(output.is_error)
        output = self.invoke(runtime, path="../escape.py", content="candidate\n")
        self.assertTrue(output.is_error)
        self.assertFalse(any(item["type"] == "change_preview" for item in self.events))
        self.io.confirm.assert_not_called()

    def test_large_preview_is_bounded_but_full_candidate_diff_is_saved(self):
        runtime = self.runtime()
        self.io.confirm.return_value = False
        content = "line\n" * 4000
        self.invoke(runtime, path="new.py", content=content)
        event = self.preview()
        self.assertEqual(len(event["diff"]), 12000)
        self.assertTrue(event["truncated"])
        full = json.loads(Path(event["reference"]).read_text())["diff"]
        self.assertGreater(len(full), len(event["diff"]))
        self.assertTrue(full.startswith(event["diff"]))
        self.assertFalse((self.root / "new.py").exists())


if __name__ == "__main__":
    unittest.main()
