"""批量定位只减少往返；权限、输出预算和真实读取证据仍由宿主执行。"""

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from coding_agent.agents.roles import AgentTask, ROLES
from coding_agent.agents.runner import AgentRunner
from coding_agent.runtime import workspace
from coding_agent.runtime.compact import ContextCompact
from coding_agent.runtime.context import RunContext
from coding_agent.runtime.tools import ToolRuntime
from coding_agent.runtime.turn import TurnCancelled, TurnContext, turn_context
from coding_agent.runtime.working_set import BatchReadOutput, CodeWorkingSet, MARKER_PREFIX
from coding_agent.tools.files import FileTools, MAX_OUTPUT_CHARS
from tests.test_application import ScriptedModel, text_response, tool_call, tool_response


class BatchCodeToolsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        for patcher in (
            patch.object(workspace, "WORKDIR", self.root),
            patch("coding_agent.integrations.mcp.get_mcp_config", return_value={}),
            patch("coding_agent.runtime.tools.trigger_hook", return_value=None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.tools = FileTools(self.root)
        self.io = SimpleNamespace(emit=Mock(), confirm=Mock(return_value=True))

    def fixture(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    def runtime(self, role="implementation", mode="execute", approval="auto"):
        context = RunContext(self.root, TurnContext(), role=role, mode=mode, approval_mode=approval)
        working_set = CodeWorkingSet(self.root)
        return ToolRuntime(context, self.io, working_set=working_set), working_set

    def execute(self, runtime, name, arguments):
        with turn_context(runtime.context.turn):
            return runtime.execute(SimpleNamespace(name=name, input=arguments, id="batch-read"))

    def test_read_ranges_deduplicate_defaults_and_keep_displayed_snapshots(self):
        first = self.fixture("first.py", "one\ntwo\nthree\n")
        self.fixture("second.py", "other\n")
        with patch.object(self.tools, "_read_output", wraps=self.tools._read_output) as read:
            output = self.tools.read_files([
                {"path": "first.py", "start_line": 2, "end_line": 3},
                {"path": "./first.py", "start_line": 2, "end_line": 3, "limit": 500},
                {"path": "second.py"},
            ])
        self.assertEqual(read.call_count, 2)
        self.assertIsInstance(output, BatchReadOutput)
        self.assertIn("2: two\n3: three", output)
        self.assertEqual([item["path"] for item in output.snapshots], ["first.py", "second.py"])
        self.assertEqual(output.snapshots[0]["start_line"], 2)
        self.assertEqual(output.snapshots[0]["end_line"], 3)
        self.assertIn(str(first), self.tools.versions)
        self.assertTrue(self.tools.edit("first.py", "two", "updated").startswith("Edited"))

    def test_mixed_read_failures_do_not_grant_versions_or_hide_success(self):
        self.fixture("good.py", "good\n")
        output = self.tools.read_files([
            {"path": "missing.py"}, {"path": "../outside.py"}, {"path": "good.py"},
        ])
        self.assertIn("Error:", output)
        self.assertIn("1: good", output)
        self.assertEqual([item["path"] for item in output.snapshots], ["good.py"])
        self.assertEqual(set(self.tools.versions), {str(self.root / "good.py")})

    def test_whole_batch_structure_is_validated_before_any_read(self):
        self.fixture("good.py", "good\n")
        for bad in ([], [{}], [{"path": "good.py", "limit": True}],
                    [{"path": "good.py", "start_line": 4, "end_line": 3}],
                    [{"path": "good.py", "approved": True}],
                    [{"path": "good.py"}] * 13,
                    [{"path": "good.py"}, {"path": "bad", "limit": 0}]):
            with self.subTest(bad=bad), patch.object(self.tools, "_read_output") as read:
                self.assertTrue(self.tools.read_files(bad).startswith("Error:"))
                read.assert_not_called()
                self.assertEqual(self.tools.versions, {})

    def test_batch_total_budget_and_snapshot_only_contains_visible_content(self):
        items = []
        for index in range(12):
            self.fixture(f"{index}.py", "large = '" + "x" * 30000 + "'\n")
            items.append({"path": f"{index}.py"})
        output = self.tools.read_files(items)
        self.assertLessEqual(len(output), MAX_OUTPUT_CHARS)
        self.assertIn("batch output truncated", output)
        self.assertEqual(len(output.snapshots), 12)
        for snapshot in output.snapshots:
            self.assertIn(snapshot["content"], output)
            self.assertLess(len(snapshot["content"]), 2200)
            self.assertEqual(snapshot["end_line"], 1)

    def test_titles_with_control_characters_also_obey_shared_budget(self):
        items = [{"path": "\n" * 950 + str(index)} for index in range(12)]
        self.assertLessEqual(len(self.tools.read_files(items)), MAX_OUTPUT_CHARS)
        queries = [{"query": "\t" * 4090 + str(index), "path": "."} for index in range(12)]
        with patch.object(self.tools, "_search_code", return_value="result" * 6000):
            self.assertLessEqual(len(self.tools.search_many(queries)), MAX_OUTPUT_CHARS)

    def test_empty_file_is_a_real_read(self):
        self.fixture("empty.py", "")
        output = self.tools.read_files([{"path": "empty.py"}])
        self.assertIn("(empty file)", output)
        self.assertEqual(output.snapshots[0]["end_line"], 0)
        self.assertIn(str(self.root / "empty.py"), self.tools.versions)

    def test_empty_file_remains_a_valid_snapshot_among_large_truncated_reads(self):
        items = []
        for index in range(11):
            self.fixture(f"{index}.py", "body" * 8000)
            items.append({"path": f"{index}.py"})
        self.fixture("empty.py", "")
        items.append({"path": "empty.py"})
        runtime, working_set = self.runtime()
        output = self.execute(runtime, "read_files", {"files": items})
        self.assertFalse(output.is_error)
        self.assertLessEqual(len(output), MAX_OUTPUT_CHARS)
        self.assertIn("batch output truncated", output)
        self.assertEqual(working_set.records["empty.py"]["start_line"], 1)
        self.assertEqual(working_set.records["empty.py"]["end_line"], 0)
        self.assertEqual(working_set.records["empty.py"]["content"], "(empty file)")

    def test_unshown_result_does_not_register_read_version(self):
        self.fixture("good.py", "good\n")
        with patch("coding_agent.tools.files._batch_piece", return_value=("omitted", "")):
            output = self.tools.read_files([{"path": "good.py"}])
        self.assertEqual(output.snapshots, [])
        self.assertEqual(self.tools.versions, {})

    def test_batch_preserves_optimistic_concurrent_change_detection(self):
        target = self.fixture("app.py", "old\n")
        self.tools.read_files([{"path": "app.py"}])
        target.write_text("user change\n", encoding="utf-8")
        self.assertIn("changed since", self.tools.edit("app.py", "old", "agent"))
        self.assertEqual(target.read_text(), "user change\n")

    def test_parent_segments_after_symlink_keep_original_boundary_checks(self):
        self.fixture("visible.py", "must_not_read_the_other_file\n")
        with tempfile.TemporaryDirectory() as directory:
            outside = Path(directory)
            (outside / "subdir").mkdir()
            (self.root / "link").symlink_to(outside / "subdir", target_is_directory=True)
            path = "link/../visible.py"
            self.assertTrue(self.tools.read(path).startswith("Error:"))
            read = self.tools.read_files([{"path": path}])
            self.assertIn("Error:", read)
            self.assertNotIn("must_not_read_the_other_file", read)
            self.assertEqual(read.snapshots, [])
            self.assertEqual(self.tools.versions, {})
            self.assertTrue(self.tools.search_code("must_not_read", path=path).startswith("Error:"))
            searched = self.tools.search_many([{"query": "must_not_read", "path": path}])
            self.assertIn("Error:", searched)
            self.assertNotIn("visible.py:1:", searched)

    def test_search_deduplicates_and_discovers_same_tree_once(self):
        self.fixture("src/main.py", "first needle\nsecond needle\n")
        queries = [{"query": "first", "path": "src"},
                   {"query": "first", "path": "./src", "context_lines": 2, "max_results": 100},
                   {"query": "second", "path": "src"}]
        with patch.object(self.tools.project_files, "scan", wraps=self.tools.project_files.scan) as scan:
            result = self.tools.search_many(queries)
        self.assertEqual(scan.call_count, 1)
        self.assertIn("src/main.py:1:", result)
        self.assertIn("src/main.py:2:", result)
        self.assertEqual(result.count("--- search"), 2)

    def test_search_uses_same_ignore_rules_and_literal_matching_in_both_engines(self):
        literal = "needle.*$(not_shell)"
        self.fixture(".gitignore", "ignored.py\n")
        self.fixture(".agentignore", "other.py\n")
        self.fixture("src/main.py", literal + "\n")
        self.fixture("ignored.py", literal)
        self.fixture("other.py", literal)
        self.fixture("node_modules/dep.py", literal)
        for fallback in (False, True):
            with self.subTest(fallback=fallback):
                manager = (patch("coding_agent.tools.files.shutil.which", return_value=None)
                           if fallback else patch("coding_agent.tools.files.check_cancelled"))
                with manager:
                    result = self.tools.search_many([{"query": literal, "context_lines": 0}])
                self.assertIn("src/main.py:1:", result)
                self.assertNotIn("ignored.py:", result)
                self.assertNotIn("other.py:", result)
                self.assertNotIn("dep.py:", result)

    def test_search_failure_and_no_match_remain_distinct_per_item(self):
        self.fixture("app.py", "existing\n")
        output = self.tools.search_many([
            {"query": "existing", "path": "../outside"},
            {"query": "not present"}, {"query": "existing"},
        ])
        self.assertIn("Error:", output)
        self.assertIn("No matches found", output)
        self.assertIn("app.py:1:", output)
        self.assertEqual(self.tools.versions, {})

    def test_search_validates_whole_batch_and_total_output_budget(self):
        for bad in ([], [{"query": ""}], [{"query": "a\nb"}], [{"query": "a", "max_results": True}],
                    [{"query": "a", "approved": True}], [{"query": "a"}] * 13):
            with self.subTest(bad=bad), patch.object(self.tools, "_search_code") as search:
                self.assertTrue(self.tools.search_many(bad).startswith("Error:"))
                search.assert_not_called()
        with patch.object(self.tools, "_search_code", return_value="match\n" * 6000) as search:
            output = self.tools.search_many([{"query": str(index)} for index in range(12)])
        self.assertEqual(search.call_count, 12)
        self.assertLessEqual(len(output), MAX_OUTPUT_CHARS)
        self.assertIn("batch output truncated", output)

    def test_cancellation_before_and_during_batch_never_authorizes_hidden_reads(self):
        self.fixture("app.py", "code\n")
        for name, args in (("read_files", {"files": [{"path": "app.py"}]}),
                           ("search_many", {"queries": [{"query": "code"}]})):
            turn = TurnContext()
            with self.subTest(name=name), turn_context(turn):
                turn.cancel()
                with self.assertRaises(TurnCancelled):
                    getattr(self.tools, name)(**args)
        turn = TurnContext()
        original = self.tools._read_output

        def interrupted(**kwargs):
            result = original(**kwargs)
            turn.cancel()
            return result

        with turn_context(turn), patch.object(self.tools, "_read_output", side_effect=interrupted):
            with self.assertRaises(TurnCancelled):
                self.tools.read_files([{"path": "app.py"}])
        self.assertEqual(self.tools.versions, {})

    def test_all_roles_and_plan_have_batch_reads_but_cannot_gain_writer_tools(self):
        for role in ROLES:
            runtime, _ = self.runtime(role=role, mode="plan")
            visible = {tool["name"] for tool in runtime.pool()[0]}
            self.assertTrue({"read_files", "search_many"} <= visible)
            self.assertFalse({"write_file", "edit_file", "bash"} & visible)
        for role in ROLES:
            if role == "implementation":
                continue
            runtime, _ = self.runtime(role=role)
            self.assertFalse(runtime.allowed("write_file"))
            self.assertFalse(runtime.allowed("bash"))

    def test_runtime_validation_rejects_capability_flags_before_handler(self):
        runtime, _ = self.runtime()
        with patch.object(runtime.files, "_read_output") as read:
            output = self.execute(runtime, "read_files", {"files": [{"path": "x.py", "execute": True}]})
        self.assertTrue(output.is_error)
        read.assert_not_called()
        self.io.confirm.assert_not_called()

    def test_ask_denial_prevents_whole_batch_and_auto_uses_one_approval(self):
        self.fixture("app.py", "code\n")
        runtime, working_set = self.runtime(approval="ask")
        self.io.confirm.return_value = False
        output = self.execute(runtime, "read_files", {"files": [{"path": "app.py"}]})
        self.assertTrue(output.is_error)
        self.assertEqual(runtime.context.versions, {})
        self.assertEqual(working_set.records, {})
        self.io.confirm.assert_called_once()
        self.io.confirm.reset_mock()
        runtime, _ = self.runtime(mode="plan")
        output = self.execute(runtime, "search_many", {"queries": [{"query": "code"}, {"query": "missing"}]})
        self.assertFalse(output.is_error)
        self.io.confirm.assert_not_called()
        approvals = [item for item in runtime.context.evidence if item.get("kind") == "approval"]
        self.assertEqual(len(approvals), 1)

    def test_runtime_preserves_each_visible_snapshot_and_code_reference(self):
        self.fixture("a.py", "first\nsecond\n")
        self.fixture("b.py", "other\n")
        runtime, working_set = self.runtime()
        output = self.execute(runtime, "read_files", {"files": [
            {"path": "a.py", "start_line": 2}, {"path": "missing.py"}, {"path": "b.py"},
        ]})
        self.assertFalse(output.is_error)
        self.assertEqual(set(working_set.records), {"a.py", "b.py"})
        self.assertEqual(working_set.records["a.py"]["tool_use_id"], "batch-read")
        for snapshot in working_set.records.values():
            self.assertIn(snapshot["content"], output)
        evidence = runtime.context.evidence[-1]
        self.assertEqual(evidence["code_refs"], ["a.py:2", "b.py:1"])
        record = json.loads(Path(evidence["reference"]).read_text(encoding="utf-8"))
        self.assertEqual([item["path"] for item in record["read_snapshots"]], ["a.py", "b.py"])
        self.assertNotIn("content", record["read_snapshots"][0])

    def test_runner_handoff_and_compaction_keep_both_real_read_ranges(self):
        self.fixture("a.py", "first\nACTIVE_CODE_A\n")
        self.fixture("b.py", "ACTIVE_CODE_B\n")
        model = ScriptedModel([
            tool_response(tool_call("read_files", {"files": [
                {"path": "a.py", "start_line": 2}, {"path": "b.py"},
            ]}, "read-both")),
            text_response("已读取两个文件。"),
        ])
        context = RunContext(self.root, TurnContext(), role="codebase")
        runner = AgentRunner(Mock(), {"model": "fake-model", "max_tokens": 1000}, self.io,
                             model_call=model)
        history = [{"role": "user", "content": "核实两个实现位置"}]
        working_set = CodeWorkingSet(self.root)
        with patch("coding_agent.agents.runner.CodeWorkingSet", return_value=working_set), turn_context(context.turn):
            result = runner.run(AgentTask("codebase", "核实两个实现位置"), context, messages=history)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.code_refs, ["a.py:2", "b.py:1"])
        #完整批量结果仍在历史时不重复注入正文；压缩丢掉原结果之后再补回。
        self.assertFalse(any(isinstance(item["content"], str) and item["content"].startswith(MARKER_PREFIX)
                             for item in working_set.attach(history, 40000)))
        compactor = ContextCompact(Mock(), "fake-model", self.root / ".transcripts",
                                  self.root / ".task_outputs" / "tool-results", working_set=working_set)
        with patch.object(compactor, "summarize_history", return_value="读取了两个实现位置"):
            compacted = compactor.compact_history(history, "核实两个实现位置")
        entries = [entry for item in compacted
                   if isinstance(item["content"], str) and item["content"].startswith(MARKER_PREFIX)
                   for entry in json.loads(item["content"][len(MARKER_PREFIX):])["files"]]
        self.assertEqual({entry["path"] for entry in entries}, {"a.py", "b.py"})
        self.assertTrue(all(entry["status"] == "verified_snapshot" for entry in entries))
        self.assertIn("ACTIVE_CODE_A", next(entry["code"] for entry in entries if entry["path"] == "a.py"))
        self.assertIn("ACTIVE_CODE_B", next(entry["code"] for entry in entries if entry["path"] == "b.py"))


if __name__ == "__main__":
    unittest.main()
