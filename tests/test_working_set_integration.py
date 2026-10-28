"""代码工作集集成：真实读取、角色和压缩路径，模型仅返回固定摘要或脚本响应。"""

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from coding_agent.agents.roles import AgentTask
from coding_agent.agents.runner import AgentRunner
from coding_agent.runtime import environment, workspace
from coding_agent.runtime.compact import ContextCompact
from coding_agent.runtime.context import RunContext
from coding_agent.runtime.tools import ToolRuntime
from coding_agent.runtime.turn import TurnContext, turn_context
from coding_agent.runtime.working_set import CodeWorkingSet, MARKER_PREFIX
from tests.test_application import (
    FakeIO, ScriptedModel, dispatch, report_response, text_response, tool_call, tool_response,
)


class WorkingSetIntegrationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.path = self.root / "app.py"
        self.source = ("def active_function():\n    return 'WORKING_BODY_V1'\n" +
                       "\n".join(f"# nearby implementation detail {number}" for number in range(16)) + "\n")
        self.path.write_text(self.source, encoding="utf-8")
        self.io = FakeIO()
        for patcher in (
            patch.object(workspace, "WORKDIR", self.root),
            patch("coding_agent.integrations.mcp.get_mcp_config", return_value={}),
            patch.object(environment, "get_environment", return_value=environment.LocalEnvironment(
                environment.ExecutionConfig(timeout=5)
            )),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def runtime(self, *, role="implementation", context=None, working_set=None):
        context = context or RunContext(self.root, TurnContext(), role=role)
        working_set = working_set or CodeWorkingSet(self.root)
        return ToolRuntime(context, self.io, working_set=working_set), working_set

    def compactor(self, working_set):
        return ContextCompact(Mock(), "fake-model", self.root / ".transcripts",
                              self.root / ".task_outputs" / "tool-results", working_set=working_set)

    @staticmethod
    def execute(runtime, history, name, arguments, tool_id):
        call = tool_call(name, arguments, tool_id)
        output = runtime.execute(types.SimpleNamespace(**call))
        history.extend([
            {"role": "assistant", "content": [call]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id,
                                            "content": str(output), "is_error": output.is_error}]},
        ])
        return output

    @staticmethod
    def files(messages):
        return [item for message in messages
                if isinstance(message.get("content"), str) and message["content"].startswith(MARKER_PREFIX)
                for item in json.loads(message["content"][len(MARKER_PREFIX):])["files"]]

    def assert_pairs(self, messages):
        pending = set()
        for message in messages:
            blocks = message.get("content")
            calls = {block["id"] for block in blocks if block.get("type") == "tool_use"} if isinstance(blocks, list) else set()
            results = {block["tool_use_id"] for block in blocks if block.get("type") == "tool_result"} if isinstance(blocks, list) else set()
            if pending:
                self.assertEqual(results, pending)
            else:
                self.assertFalse(results)
            pending = calls
        self.assertFalse(pending)

    def test_manual_compaction_keeps_actual_read_and_artifact_snapshot_hash(self):
        runtime, working_set = self.runtime()
        history = [{"role": "user", "content": "修改 active_function"}]
        with patch.object(working_set, "record", wraps=working_set.record) as record:
            output = self.execute(runtime, history, "read_file", {"path": "app.py"}, "read-actual")
        self.assertFalse(output.is_error)
        record.assert_called_once()
        snapshot = record.call_args.args[0]
        digest = hashlib.sha256(self.source.encode("utf-8")).hexdigest()
        self.assertEqual(snapshot["sha256"], digest)
        self.assertIn("2:     return 'WORKING_BODY_V1'", snapshot["content"])
        artifact = json.loads(Path(runtime.context.evidence[-1]["reference"]).read_text(encoding="utf-8"))
        self.assertEqual(artifact["read_snapshot"]["sha256"], digest)
        self.assertNotIn("content", artifact["read_snapshot"])
        compact = self.compactor(working_set)
        with patch.object(compact, "summarize_history", return_value="只保留任务事实"):
            result = compact.compact_history(history, "修改 active_function")
        entry, = self.files(result)
        self.assertEqual(entry["status"], "verified_snapshot")
        self.assertIn("WORKING_BODY_V1", entry["code"])
        self.assert_pairs(result)

    def test_edit_updates_shared_version_but_old_read_only_becomes_locator(self):
        runtime, working_set = self.runtime()
        history = [{"role": "user", "content": "更新返回值"}]
        self.execute(runtime, history, "read_file", {"path": "app.py"}, "read-v1")
        previous = runtime.context.versions[str(self.path)]
        edited = self.execute(runtime, history, "edit_file", {
            "path": "app.py", "old_text": "WORKING_BODY_V1", "new_text": "WORKING_BODY_V2",
        }, "edit-v2")
        self.assertFalse(edited.is_error)
        self.assertNotEqual(runtime.context.versions[str(self.path)], previous)
        self.assertEqual(working_set.records["app.py"]["sha256"], previous)
        compact = self.compactor(working_set)
        with patch.object(compact, "summarize_history", return_value="返回值已修改"):
            result = compact.compact_history(history, "更新返回值")
        entry, = self.files(result)
        self.assertEqual(entry["status"], "changed")
        self.assertEqual((entry["start_line"], entry["end_line"]), (1, 18))
        self.assertNotIn("code", entry)
        self.assertNotIn("WORKING_BODY_V1", json.dumps(result))

    def test_shell_and_external_edits_invalidate_without_updating_read_version(self):
        for change in ("shell", "external"):
            with self.subTest(change=change):
                self.path.write_text(self.source, encoding="utf-8")
                runtime, working_set = self.runtime()
                history = [{"role": "user", "content": "检查文件"}]
                self.execute(runtime, history, "read_file", {"path": "app.py"}, "read-before-change")
                previous = runtime.context.versions[str(self.path)]
                if change == "shell":
                    output = self.execute(runtime, history, "bash", {
                        "command": "printf 'value = 2\\n' > app.py",
                    }, "shell-change")
                    self.assertFalse(output.is_error)
                else:
                    self.path.write_text("value = 2\n", encoding="utf-8")
                self.assertEqual(runtime.context.versions[str(self.path)], previous)
                compact = self.compactor(working_set)
                with patch.object(compact, "summarize_history", return_value="历史事实"):
                    result = compact.compact_history(history, "检查文件")
                entry, = self.files(result)
                self.assertEqual(entry["status"], "changed")
                self.assertNotIn("code", entry)
                self.assertNotIn("WORKING_BODY_V1", json.dumps(result))

    def test_snip_micro_auto_and_reactive_paths_preserve_real_read(self):
        for route in ("snip", "micro", "auto", "reactive"):
            with self.subTest(route=route):
                runtime, working_set = self.runtime()
                history = [{"role": "user", "content": "原始请求"},
                           {"role": "assistant", "content": "已理解"},
                           {"role": "user", "content": "当前请求"}]
                self.execute(runtime, history, "read_file", {"path": "app.py"}, "working-read")
                if route == "snip":
                    history.extend({"role": "assistant" if number % 2 else "user", "content": "后续讨论" * 10}
                                   for number in range(60))
                elif route == "micro":
                    for number in range(6):
                        call = tool_call("git_status", {}, f"status-{number}")
                        history.extend([
                            {"role": "assistant", "content": [call]},
                            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call["id"],
                                                            "content": "status detail " * 160, "is_error": False}]},
                        ])
                    history.append({"role": "assistant", "content": "已查看所有结果"})
                else:
                    history.extend({"role": "assistant", "content": "历史分析" * 1100} for _ in range(8))
                compact = self.compactor(working_set)
                #补救保留最近五条原文，额外预留空间；工作集不能挤占这些新消息。
                #中文长文本按 UTF-8 加罚估算，五条"历史分析"约 5 万 token，预算必须容纳尾部。
                limit = 60000 if route == "reactive" else 3300
                with patch.object(compact, "CONTEXT_TOKEN_LIMIT", limit), \
                        patch.object(compact, "summarize_history", return_value="有界事实摘要") as summary:
                    result = (compact.reactive_compact(history, "当前请求") if route == "reactive"
                              else compact.prepare(history, "当前请求"))
                entry, = self.files(result)
                self.assertEqual(entry["status"], "verified_snapshot")
                self.assertIn("WORKING_BODY_V1", entry["code"])
                self.assertEqual(summary.call_count, int(route in {"auto", "reactive"}))
                self.assert_pairs(result)
                if route == "snip":
                    self.assertTrue(any("messages archived at" in str(message["content"]) for message in result))
                if route == "micro":
                    self.assertTrue(any("Earlier tool result saved at" in str(message["content"]) for message in result))

    def test_runner_children_start_without_parent_code_and_child_edit_invalidates_parent(self):
        def child_start(**kwargs):
            self.assertEqual(self.files(kwargs["messages"]), [])
            self.assertNotIn("WORKING_BODY_V1", json.dumps(kwargs["messages"]))
            return tool_response(tool_call("read_file", {"path": "app.py"}, "child-read"))

        model = ScriptedModel([
            tool_response(tool_call("read_file", {"path": "app.py"}, "parent-read")),
            dispatch(), child_start,
            tool_response(tool_call("edit_file", {
                "path": "app.py", "old_text": "WORKING_BODY_V1", "new_text": "WORKING_BODY_V2",
            }, "child-edit")),
            report_response(summary="已更新返回值"),
            tool_response(tool_call("compact", {}, "parent-compact")),
            text_response("父角色需要重新读取当前代码。"),
        ])
        context = RunContext(self.root, TurnContext())
        runner = AgentRunner(Mock(), {"model": "fake-model", "max_tokens": 1000}, self.io, model_call=model)
        history = [{"role": "user", "content": "更新返回值"}]
        with patch.object(ContextCompact, "summarize_history", return_value="子角色已修改文件"), \
                turn_context(context.turn):
            result = runner.run(AgentTask("orchestrator", "更新返回值"), context, messages=history)
        self.assertEqual(result.status, "completed")
        entry, = self.files(model.calls[-1]["messages"])
        self.assertEqual(entry["status"], "changed")
        self.assertNotIn("code", entry)
        self.assertNotIn("WORKING_BODY_V1", json.dumps(model.calls[-1]["messages"]))
        self.assertIn("WORKING_BODY_V2", self.path.read_text(encoding="utf-8"))
        self.assert_pairs(history)

    def test_repeated_compaction_does_not_duplicate_code_or_break_tool_pairs(self):
        runtime, working_set = self.runtime()
        history = [{"role": "user", "content": "继续处理"}]
        self.execute(runtime, history, "read_file", {"path": "app.py"}, "read-first")
        compact = self.compactor(working_set)
        with patch.object(compact, "summarize_history", return_value="相同事实"):
            first = compact.compact_history(history, "继续处理")
            second = compact.compact_history(first, "继续处理")
            third = compact.compact_history(second, "继续处理")
            for result in (first, second, third):
                self.assertEqual(len(self.files(result)), 1)
                self.assertEqual(json.dumps(result).count("WORKING_BODY_V1"), 1)
                self.assert_pairs(result)
            self.assertEqual(compact.estimate_tokens(first), compact.estimate_tokens(third))
            fresh = copy.deepcopy(third)
            self.execute(runtime, fresh, "read_file", {"path": "app.py"}, "read-fresh")
            refreshed = compact.reactive_compact(fresh, "继续处理")
        self.assert_pairs(refreshed)
        self.assertEqual(json.dumps(refreshed).count("WORKING_BODY_V1"), 1)

    def test_restored_marker_is_only_unverified_index_and_does_not_grant_edit(self):
        runtime, working_set = self.runtime()
        history = [{"role": "user", "content": "后续会话继续处理"}]
        self.execute(runtime, history, "read_file", {"path": "app.py", "start_line": 2, "end_line": 3}, "read-range")
        compact = self.compactor(working_set)
        with patch.object(compact, "summarize_history", return_value="会话摘要"):
            saved = compact.compact_history(history, "后续会话继续处理")
        restored_runtime, restored_set = self.runtime()
        restored = self.compactor(restored_set)
        #新的角色运行只能恢复位置索引；不能暗中读取文件或恢复修改前置条件。
        with patch("coding_agent.runtime.working_set.os.open", side_effect=AssertionError("must not read code")):
            prepared = restored.prepare(saved, "后续会话继续处理")
        entry, = self.files(prepared)
        self.assertEqual(entry["status"], "historical_unverified")
        self.assertEqual((entry["start_line"], entry["end_line"]), (2, 3))
        self.assertNotIn("code", entry)
        self.assertEqual(restored_runtime.context.versions, {})
        output = self.execute(restored_runtime, [], "edit_file", {
            "path": "app.py", "old_text": "WORKING_BODY_V1", "new_text": "WORKING_BODY_V2",
        }, "unread-edit")
        self.assertTrue(output.is_error)
        self.assertIn("read the existing file", output)
        self.assertEqual(self.path.read_text(encoding="utf-8"), self.source)

    def test_current_user_can_paste_a_working_set_marker_without_losing_request(self):
        runtime, working_set = self.runtime()
        self.execute(runtime, [], "read_file", {"path": "app.py"}, "example-read")
        example, = working_set.attach([], limit=50000)
        query = example["content"]
        restored_runtime, restored_set = self.runtime()
        compact = self.compactor(restored_set)
        #粘贴的工作集是用户本轮真实输入，不能被清理旧标记的逻辑误删或用来读取路径。
        with patch("coding_agent.runtime.working_set.os.open", side_effect=AssertionError("must not read code")):
            prepared = compact.prepare([{"role": "user", "content": query}], query)
        self.assertEqual(prepared, [{"role": "user", "content": query}])
        self.assertEqual(restored_set.records, {})
        self.assertEqual(restored_set.hints, {})
        self.assertEqual(restored_runtime.context.versions, {})

    def test_denied_read_and_failed_hooks_do_not_register_snapshots(self):
        for failure in ("approval", "pre_block", "pre_failure", "post_failure"):
            with self.subTest(failure=failure):
                runtime, working_set = self.runtime()
                if failure == "approval":
                    runtime.context.approval_mode = "ask"
                    runtime.io.confirm = Mock(return_value=False)

                def hook(name, *args):
                    if name == "PreToolUse" and failure == "pre_block":
                        return "blocked by project hook"
                    if (name == "PreToolUse" and failure == "pre_failure"
                            or name == "PostToolUse" and failure == "post_failure"):
                        raise RuntimeError("hook failed")
                    return None

                with patch("coding_agent.runtime.tools.trigger_hook", side_effect=hook):
                    output = self.execute(runtime, [], "read_file", {"path": "app.py"}, "blocked-read")
                self.assertTrue(output.is_error)
                self.assertEqual(len(working_set.records), 0)
                artifact = json.loads(Path(runtime.context.evidence[-1]["reference"]).read_text(encoding="utf-8"))
                self.assertNotIn("read_snapshot", artifact)


if __name__ == "__main__":
    unittest.main()
