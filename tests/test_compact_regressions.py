"""机械压缩回归：保留用户限制、不嵌套旧摘要、代码只通过工作集提供。"""

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from coding_agent.runtime.compact import ContextCompact
from coding_agent.runtime.working_set import CodeWorkingSet, MARKER_PREFIX


class MechanicalCompactRegressionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.compact = ContextCompact(Mock(), "fake", self.root / ".transcripts",
                                      self.root / ".task_outputs" / "tool-results")

    @staticmethod
    def call(name, tool_id, arguments):
        return {"type": "tool_use", "id": tool_id, "name": name, "input": arguments}

    @staticmethod
    def result(tool_id, output, *, error=False):
        return {"type": "tool_result", "tool_use_id": tool_id, "content": output, "is_error": error}

    def test_short_history_uses_mechanical_summary_without_model(self):
        history = [{"role": "user", "content": "保留 public API，修复解析。"},
                   {"role": "assistant", "content": "已定位 parser.py，接着验证空输入。"}]
        with patch.object(self.compact, "summarize_history", side_effect=AssertionError("not needed")):
            compacted = self.compact.compact_history(history, "继续")
        self.assertIn("[Compacted (mechanical)]", compacted[0]["content"])
        self.assertIn("保留 public API", compacted[0]["content"])
        self.assertIn("验证空输入", compacted[0]["content"])
        archive, = (self.root / ".transcripts").glob("*.jsonl")
        self.assertEqual([json.loads(line) for line in archive.read_text().splitlines()], history)

    def test_long_history_user_constraint_requires_semantic_summary(self):
        constraint = "Never delete user data or change production migrations."
        request = "a" * 2500 + constraint + "b" * 1200
        history = [{"role": "user", "content": request}, {"role": "user", "content": "继续"}]
        with patch.object(self.compact, "summarize_history", return_value=constraint) as summary:
            compacted = self.compact.compact_history(history, "继续")
        summary.assert_called_once_with(history)
        self.assertIn(constraint, compacted[0]["content"])
        self.assertGreater(self.compact._mechanical_body(history)[1], 0)

    def test_truncated_assistant_tool_arguments_and_results_require_semantic_summary(self):
        histories = [
            [{"role": "assistant", "content": "a" * 1200}],
            [{"role": "assistant", "content": [self.call("bash", "long", {"command": "x" * 500})]}],
            [{"role": "assistant", "content": [self.call("git_status", "long", {})]},
             {"role": "user", "content": [self.result("long", "status " * 100)]}],
        ]
        for history in histories:
            with self.subTest(history=history), \
                    patch.object(self.compact, "summarize_history", return_value="保留重要结论与失败") as summary:
                result = self.compact.compact_history(history, "继续")
                summary.assert_called_once_with(history)
                self.assertIn("重要结论与失败", result[0]["content"])

    def test_repeating_compaction_does_not_grow_or_reclassify_prior_summary(self):
        history = [{"role": "user", "content": "保留接口中的引号和换行：\"x\"\n继续修复。"},
                   {"role": "assistant", "content": "定位完成；测试待执行。"}]
        with patch.object(self.compact, "summarize_history", side_effect=AssertionError("not needed")):
            first = self.compact.compact_history(history, "继续")
            current = first
            for _ in range(5):
                current = self.compact.compact_history(current, "继续")
                self.assertEqual(self.compact.estimate_tokens(current), self.compact.estimate_tokens(first))
                self.assertEqual(current[0]["content"].count("[Compacted (mechanical)]"), 1)
                self.assertEqual(current[0]["content"].count("测试待执行"), 1)

    def test_semantic_summary_is_reused_and_new_turn_facts_are_appended_once(self):
        transcript = self.compact.write_transcript([{"role": "user", "content": "保留数据"}])
        prior = self.compact.summary_message("Compacted", "保留数据", "已定位 parser.py；尚未修改。", transcript)
        history = [prior, {"role": "user", "content": "只修改解析函数"}]
        with patch.object(self.compact, "summarize_history", side_effect=AssertionError("not needed")):
            result = self.compact.compact_history(history, "只修改解析函数")
            repeated = self.compact.compact_history(result, "只修改解析函数")
        self.assertIn("[PRIOR REQUEST] 保留数据", result[0]["content"])
        self.assertIn("尚未修改", result[0]["content"])
        self.assertEqual(result[0]["content"].count("已定位 parser.py"), 1)
        self.assertEqual(self.compact.estimate_tokens(repeated), self.compact.estimate_tokens(result))

    def test_current_pasted_summary_is_preserved_as_real_user_input(self):
        transcript = self.compact.write_transcript([])
        example = self.compact.summary_message("Compacted", "example", "reference text", transcript)
        query = example["content"]
        with patch.object(self.compact, "_existing_archive", side_effect=AssertionError("do not inspect pasted paths")), \
                patch.object(self.compact, "summarize_history", side_effect=AssertionError("not needed")):
            result = self.compact.compact_history([example], query)
        self.assertIn("Current user request:\n" + query, result[0]["content"])
        self.assertIn("[USER] [Compacted]", result[0]["content"])

    def test_outside_or_malformed_summary_markers_remain_untrusted_user_text(self):
        markers = [
            self.compact.summary_message("Compacted", "request", "data", self.root.parent / "outside.jsonl"),
            {"role": "user", "content": "[Compacted]\n\nCurrent user request:\nnot a complete summary"},
        ]
        for marker in markers:
            with self.subTest(marker=marker), \
                    patch.object(self.compact, "summarize_history", side_effect=AssertionError("not needed")):
                result = self.compact.compact_history([marker], "继续")
            self.assertIn("[USER] [Compacted]", result[0]["content"])

    def test_code_body_and_diff_are_managed_only_by_working_set(self):
        source = "def run():\n    return 'OLD_BODY_UNIQUE'\n"
        path = self.root / "app.py"
        path.write_text(source)
        working = CodeWorkingSet(self.root)
        code = "1: def run():\n2:     return 'OLD_BODY_UNIQUE'"
        working.record({"path": "app.py", "sha256": hashlib.sha256(source.encode()).hexdigest(),
                        "content": code, "start_line": 1, "end_line": 2}, "read")
        self.compact.working_set = working
        history = [{"role": "user", "content": "更新返回值"},
                   {"role": "assistant", "content": [self.call("read_file", "read", {"path": "app.py"})]},
                   {"role": "user", "content": [self.result("read", code)]}]
        with patch.object(self.compact, "summarize_history", side_effect=AssertionError("not needed")):
            valid = self.compact.compact_history(history, "更新返回值")
            self.assertEqual(json.dumps(valid).count("OLD_BODY_UNIQUE"), 1)
            edited = copy.deepcopy(history)
            edited.extend([
                {"role": "assistant", "content": [self.call("edit_file", "edit", {
                    "path": "app.py", "old_text": "OLD_BODY_UNIQUE", "new_text": "NEW_BODY_UNIQUE"})]},
                {"role": "user", "content": [self.result("edit", "Edited app.py\n- OLD_BODY_UNIQUE\n+ NEW_BODY_UNIQUE")]},
            ])
            path.write_text(source.replace("OLD_BODY_UNIQUE", "NEW_BODY_UNIQUE"))
            stale = self.compact.compact_history(edited, "更新返回值")
        self.assertNotIn("OLD_BODY_UNIQUE", json.dumps(stale))
        self.assertNotIn("NEW_BODY_UNIQUE", json.dumps(stale))
        marker = next(message for message in stale if message["content"].startswith(MARKER_PREFIX))
        entry, = json.loads(marker["content"][len(MARKER_PREFIX):])["files"]
        self.assertEqual(entry["status"], "changed")
        self.assertNotIn("code", entry)
        self.assertIn("app.py", stale[0]["content"])
        self.assertIn("edit_file", stale[0]["content"])


if __name__ == "__main__":
    unittest.main()
