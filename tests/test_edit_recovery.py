"""重复编辑保护使用真实临时文件；不调用模型、网络或用户工作区。"""

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from coding_agent.runtime import tools, workspace
from coding_agent.runtime.context import RunContext
from coding_agent.runtime.edit_recovery import (
    EditRecovery, MAX_FAILURE_PATHS, RECOVERY_MESSAGE, RECOVERY_THRESHOLD,
    STOP_MESSAGE, STOP_THRESHOLD,
)
from coding_agent.runtime.tools import ToolRuntime
from coding_agent.runtime.turn import SessionExit, TurnCancelled, TurnContext, turn_context


class EditRecoveryTests(unittest.TestCase):
    def failed(self, guard, path="/main.py", reason="Expected 1 occurrence", **changes):
        arguments = {"path": path, "old_text": "before", "new_text": "after", **changes}
        return guard.failed("edit_file", arguments, reason, path)

    def test_identical_failure_warns_once_then_stops(self):
        guard = EditRecovery()
        markers = [self.failed(guard) for _ in range(STOP_THRESHOLD)]
        self.assertEqual(markers, [None, None, "recover", None, None, "stop"])
        self.assertTrue(guard.stopped)
        self.assertEqual(guard.snapshot()["failures"][0]["count"], STOP_THRESHOLD)

    def test_changed_candidate_or_reason_starts_new_streak(self):
        for changes in ({"new_text": "different"}, {"reason": "file changed"}):
            with self.subTest(changes=changes):
                guard = EditRecovery()
                self.failed(guard)
                self.failed(guard)
                self.assertIsNone(self.failed(guard, **changes))
                self.assertEqual(guard.snapshot()["failures"][0]["count"], 1)

    def test_actual_success_clears_only_its_file_and_cannot_undo_stop(self):
        guard = EditRecovery()
        self.failed(guard)
        self.failed(guard, path="/other.py")
        guard.succeeded("/main.py")
        self.assertEqual([item["path"] for item in guard.snapshot()["failures"]], ["/other.py"])
        for _ in range(STOP_THRESHOLD):
            self.failed(guard)
        guard.succeeded("/main.py")
        self.assertTrue(guard.stopped)
        self.assertEqual(self.failed(guard, new_text="cannot resume"), "stop")

    def test_alternating_files_keep_independent_counters(self):
        guard = EditRecovery()
        for _ in range(RECOVERY_THRESHOLD - 1):
            self.failed(guard)
            self.failed(guard, path="/other.py")
        self.assertEqual(self.failed(guard), "recover")
        self.assertEqual(self.failed(guard, path="/other.py"), "recover")

    def test_canonical_path_dictionary_order_and_tool_identity(self):
        guard = EditRecovery()
        for arguments in ({"path": "./main.py", "old_text": "a", "new_text": "b"},
                          {"new_text": "b", "old_text": "a", "path": "main.py"}):
            guard.failed("edit_file", arguments, "same reason", "/project/main.py")
        self.assertEqual(guard.snapshot()["failures"][0]["count"], 2)
        self.assertIsNone(guard.failed("write_file", {"path": "main.py", "content": "b"},
                                      "same reason", "/project/main.py"))
        self.assertEqual(guard.snapshot()["failures"][0]["count"], 1)

    def test_state_is_bounded_and_contains_no_candidate_text(self):
        guard = EditRecovery()
        code = "private source content\n" * 10000
        for index in range(MAX_FAILURE_PATHS + 5):
            self.failed(guard, path=f"/{index}.py", new_text=code)
        snapshot = guard.snapshot()
        self.assertEqual(len(snapshot["failures"]), MAX_FAILURE_PATHS)
        self.assertNotIn(code, repr(vars(guard)))
        self.assertNotIn("private source", json.dumps(snapshot))
        self.assertEqual(len(snapshot["failures"][0]["fingerprint"]), 64)


class EditRecoveryRuntimeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        for patcher in (patch.object(workspace, "WORKDIR", self.root),
                        patch("coding_agent.integrations.mcp.get_mcp_config", return_value={})):
            patcher.start()
            self.addCleanup(patcher.stop)
        hook = patch.object(tools, "trigger_hook", return_value=None)
        self.hook = hook.start()
        self.addCleanup(hook.stop)
        self.events = []
        self.io = SimpleNamespace(emit=self.events.append, confirm=Mock(return_value=True))
        self.context = RunContext(self.root, TurnContext(), role="implementation", approval_mode="full")
        self.runtime = ToolRuntime(self.context, self.io)
        self.target = self.root / "main.py"
        self.target.write_text("duplicate duplicate\n", encoding="utf-8")
        self.runtime.files.read("main.py")
        self.counter = 0

    def invoke(self, name="edit_file", runtime=None, **arguments):
        runtime = runtime or self.runtime
        if not arguments and name == "edit_file":
            arguments = {"path": "main.py", "old_text": "duplicate", "new_text": "new"}
        self.counter += 1
        with turn_context(runtime.context.turn):
            return runtime.execute(SimpleNamespace(name=name, input=arguments, id=f"call-{self.counter}"))

    def count(self):
        failures = self.context.edit_recovery.snapshot()["failures"]
        return failures[0]["count"] if failures else 0

    def tool_artifact(self):
        item = next(item for item in reversed(self.context.evidence) if "tool" in item)
        return json.loads(Path(item["reference"]).read_text(encoding="utf-8"))

    def test_actual_matching_errors_keep_raw_reason_and_warn_stop_markers(self):
        outputs = [self.invoke() for _ in range(STOP_THRESHOLD)]
        self.assertTrue(all(item.is_error for item in outputs))
        self.assertTrue(all("Expected 1 non-empty occurrence, found 2" in item for item in outputs))
        self.assertEqual(outputs[RECOVERY_THRESHOLD - 1].edit_recovery, "recover")
        self.assertIn(RECOVERY_MESSAGE, outputs[RECOVERY_THRESHOLD - 1])
        self.assertEqual(outputs[-1].edit_recovery, "stop")
        self.assertIn(STOP_MESSAGE, outputs[-1])
        self.assertEqual(self.target.read_text(), "duplicate duplicate\n")
        notices = [item for item in self.events if item["type"] == "warning"]
        self.assertEqual([item["text"] for item in notices], [RECOVERY_MESSAGE, STOP_MESSAGE])
        artifact = self.tool_artifact()
        self.assertEqual(artifact["output"], str(outputs[-1]))
        self.assertEqual(artifact["edit_recovery"], "stop")
        self.assertTrue(artifact["edit_recovery_state"]["stopped"])

    def test_successful_reads_and_different_tool_ids_do_not_clear_failures(self):
        self.invoke()
        self.invoke("read_file", path="main.py")
        self.invoke()
        self.invoke("read_file", path="main.py", start_line=1, end_line=1)
        output = self.invoke()
        self.assertEqual(output.edit_recovery, "recover")
        self.assertEqual(self.count(), RECOVERY_THRESHOLD)

    def test_different_failing_candidate_resets_and_real_write_clears_streak(self):
        self.invoke()
        self.invoke()
        self.invoke(path="main.py", old_text="missing", new_text="different")
        self.assertEqual(self.count(), 1)
        output = self.invoke(path="main.py", old_text="duplicate duplicate", new_text="one")
        self.assertFalse(output.is_error)
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.target.read_text(), "one\n")

    def test_failure_is_shared_across_roles_but_new_turn_starts_fresh(self):
        child = self.context.for_role("implementation")
        other = ToolRuntime(child, self.io)
        self.assertIs(child.edit_recovery, self.context.edit_recovery)
        self.invoke()
        self.invoke(runtime=other)
        self.assertEqual(self.invoke().edit_recovery, "recover")
        fresh = RunContext(self.root, TurnContext())
        self.assertIsNot(fresh.edit_recovery, self.context.edit_recovery)
        self.assertEqual(fresh.edit_recovery.snapshot(), {"stopped": False, "failures": []})

    def test_rebuilt_runtime_or_working_history_cannot_reset_host_state(self):
        self.invoke()
        self.invoke()
        rebuilt = ToolRuntime(self.context, self.io)
        self.assertEqual(self.invoke(runtime=rebuilt).edit_recovery, "recover")

    def test_stopped_guard_prevents_new_tools_and_dispatch(self):
        for _ in range(STOP_THRESHOLD):
            self.invoke()
        callback = Mock()
        orchestrator = ToolRuntime(self.context.for_role("orchestrator"), self.io, dispatch=callback)
        result = self.invoke("dispatch_agent", runtime=orchestrator,
                             role="implementation", instruction="try again")
        self.assertTrue(result.is_error)
        self.assertEqual(result.edit_recovery, "stop")
        callback.assert_not_called()
        self.assertEqual(self.invoke("read_file", path="main.py").edit_recovery, "stop")

    def test_permission_plan_schema_boundary_and_read_errors_do_not_count(self):
        for role, mode in (("implementation", "plan"), ("planning", "execute")):
            context = self.context.for_role(role)
            context.mode = mode
            self.invoke(runtime=ToolRuntime(context, self.io))
        self.invoke(path="main.py", old_text="duplicate", new_text=123)
        self.invoke(path="main.py", old_text="duplicate", new_text="new", extra=True)
        self.invoke(path="../outside.py", old_text="missing", new_text="new")
        self.invoke("read_file", path="missing.py")
        self.assertEqual(self.count(), 0)

    def test_user_denial_and_hook_rejection_or_error_do_not_count(self):
        self.context.approval_mode = "ask"
        self.io.confirm.return_value = False
        self.invoke("write_file", path="main.py", content="candidate\n")
        self.io.confirm.return_value = True
        self.hook.return_value = "denied"
        self.invoke("write_file", path="main.py", content="candidate\n")
        self.hook.side_effect = ValueError("hook error")
        self.invoke("write_file", path="main.py", content="candidate\n")
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.target.read_text(), "duplicate duplicate\n")

    def test_post_tool_error_keeps_actual_write_success_and_resets_counter(self):
        self.invoke()
        self.invoke()

        def hook(kind, *args):
            if kind == "PostToolUse":
                raise ValueError("post hook failed")

        self.hook.side_effect = hook
        output = self.invoke("write_file", path="main.py", content="applied\n")
        self.assertTrue(output.is_error)
        self.assertEqual(self.target.read_text(), "applied\n")
        self.assertEqual(self.count(), 0)
        self.assertIn(str(self.target), self.context.changes)

    def test_cancelled_or_exited_prepare_apply_and_confirmation_do_not_count(self):
        for interruption in (TurnCancelled("Esc"), SessionExit(), KeyboardInterrupt()):
            for method in ("prepare_change", "apply_change"):
                with self.subTest(method=method, error=type(interruption).__name__):
                    with patch.object(self.runtime.files, method, side_effect=interruption):
                        with self.assertRaises(type(interruption)):
                            self.invoke("write_file", path="main.py", content="candidate\n")
                    self.assertEqual(self.count(), 0)
        self.context.approval_mode = "ask"
        self.io.confirm.side_effect = TurnCancelled("Esc")
        with self.assertRaises(TurnCancelled):
            self.invoke("write_file", path="main.py", content="candidate\n")
        self.assertEqual(self.count(), 0)

    def test_apply_concurrent_change_is_counted_but_not_overwritten(self):
        self.context.approval_mode = "ask"

        def consent(*args):
            self.target.write_text("user changed\n", encoding="utf-8")
            return True

        self.io.confirm.side_effect = consent
        output = self.invoke("write_file", path="main.py", content="candidate\n")
        self.assertIn("file changed before writing", output)
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.target.read_text(), "user changed\n")

    def test_os_permissions_and_unexpected_extension_failures_do_not_count(self):
        for error in (PermissionError("not authorized"), RuntimeError("extension failed")):
            with self.subTest(error=type(error).__name__):
                with patch.object(self.runtime.files, "apply_change", side_effect=error):
                    self.assertTrue(self.invoke("write_file", path="main.py", content="candidate\n").is_error)
                self.assertEqual(self.count(), 0)

    def test_no_matching_text_and_stale_read_or_missing_file_are_real_failures(self):
        self.invoke(path="main.py", old_text="missing", new_text="new")
        self.assertEqual(self.count(), 1)
        self.target.write_text("changed externally\n", encoding="utf-8")
        self.invoke("write_file", path="main.py", content="candidate\n")
        self.assertEqual(self.count(), 1)
        self.assertIn("file changed since it was read", self.tool_artifact()["output"])
        self.target.unlink()
        self.invoke("write_file", path="main.py", content="candidate\n")
        self.assertIn("file disappeared", self.tool_artifact()["output"])


if __name__ == "__main__":
    unittest.main()
