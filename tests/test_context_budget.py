"""完整模型请求预算：临时归档和固定响应，不连接真实模型。"""

import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from coding_agent.runtime.budget import ContextBudgetError, RequestBudget
from coding_agent.runtime.compact import ContextCompact
from coding_agent.runtime.model import cached_arguments
from coding_agent.runtime.working_set import CodeWorkingSet, MARKER_PREFIX


class ContextBudgetTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()

    def compact(self, limit=6000, *, system="system", tools=None, output=1000, working_set=None):
        compact = ContextCompact(Mock(), "fake", self.root / ".transcripts",
                                 self.root / ".task_outputs" / "tool-results",
                                 token_limit=limit, working_set=working_set)
        compact.configure_budget(system, [] if tools is None else tools, output)
        return compact

    @staticmethod
    def response(text="Prior implementation and checks are archived."):
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])

    @staticmethod
    def history(length=3000, count=12):
        current = {"role": "user", "content": "Only modify app.py; preserve its public interface."}
        old = [{"role": "assistant", "content": f"Old observation {index}: " + "x" * length}
               for index in range(count)]
        tail = [{"role": "assistant", "content": "Recent conclusion."} for _ in range(3)]
        call = {"role": "assistant", "content": [
            {"type": "tool_use", "id": "latest", "name": "read_file", "input": {"path": "app.py"}}]}
        result = {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "latest", "content": "LATEST_RESULT", "is_error": False}]}
        return [current, *old, *tail, call, result], current, call, result

    def test_fixed_prefix_output_and_protocol_are_counted(self):
        compact = self.compact(4000, system="s" * 3000, output=2400)
        history = [{"role": "user", "content": "u" * 1500}]
        self.assertLess(compact.estimate_tokens(history), 4000)
        with patch("coding_agent.runtime.compact.call_model") as model:
            with self.assertRaisesRegex(ContextBudgetError, "当前用户输入"):
                compact.prepare(history, history[0]["content"])
        model.assert_not_called()
        self.assertEqual(history[0]["content"], "u" * 1500)
        self.assertIn("output_reserve=2400", compact.budget.describe())

    def test_count_actual_tool_schema_not_a_fixed_tool_allowance(self):
        compact = self.compact()
        small = compact.budget
        large = compact.configure_budget("system", [{"name": "search", "description": "d" * 3000}], 1000)
        self.assertGreater(large.fixed_tokens, small.fixed_tokens)
        self.assertLess(large.history_limit, small.history_limit)
        self.assertEqual(large.history_limit + large.fixed_tokens + large.output_tokens + large.protocol_tokens,
                         large.total_limit)

    def test_many_short_messages_count_the_real_text_block_envelopes(self):
        compact = self.compact(2500, system="", output=500)
        history = [{"role": "user", "content": "current request"}]
        while compact.budget.history_tokens(history) <= compact.history_limit:
            history.append({"role": "assistant", "content": "short fact"})
        #仅数原字符串 JSON 会漏掉每条消息的 text block 封装，误以为请求仍然装得下。
        self.assertLess(compact.estimate_tokens(history), compact.history_limit)
        with patch("coding_agent.runtime.compact.call_model") as model:
            actual = compact.prepare(history, "current request")
        model.assert_not_called()
        self.assertIn("mechanical", actual[0]["content"])
        compact.ensure_fit(actual)
        cached = cached_arguments({"system": "", "tools": [], "messages": actual})
        from coding_agent.runtime.tokens import json_tokens
        self.assertLessEqual(json_tokens(cached) + compact.budget.output_tokens, 2500)

    def test_configuration_requires_real_positive_integers(self):
        for invalid in (True, "6000", 6000.0, 999):
            with self.subTest(context=invalid), self.assertRaises(ValueError):
                RequestBudget.from_request(invalid, "", [], 100)
        for invalid in (True, "1000", 1000.0, 0, -1):
            with self.subTest(output=invalid), self.assertRaises(ValueError):
                self.compact(output=invalid)

    def test_fixed_prefix_cannot_fit_fails_before_summary_or_archive(self):
        with patch("coding_agent.runtime.compact.call_model") as model:
            with self.assertRaisesRegex(ContextBudgetError, "已占满"):
                self.compact(3000, system="s" * 9000, output=1000)
        model.assert_not_called()
        self.assertFalse((self.root / ".transcripts").exists())

    def test_soft_threshold_defers_when_fixed_overhead_makes_it_unreachable(self):
        compact = self.compact(4000, system="s" * 500, output=3000)
        history = [{"role": "user", "content": "current request"},
                   *[{"role": "assistant", "content": "old"} for _ in range(12)]]
        with patch("coding_agent.runtime.compact.call_model") as model:
            for _ in range(4):
                self.assertEqual(compact.prepare(history, "current request"), history)
        model.assert_not_called()
        self.assertFalse((self.root / ".transcripts").exists())
        compact.ensure_fit(history)

    def test_hard_limit_compacts_old_history_preserves_current_and_recent_pair(self):
        compact = self.compact()
        history, current, call, result = self.history()
        with patch("coding_agent.runtime.compact.call_model", return_value=self.response()) as model:
            actual = compact.prepare(history, current["content"])
        model.assert_called_once()
        compact.ensure_fit(actual)
        self.assertIn(current, actual)
        self.assertEqual(actual[-2:], [call, result])
        self.assertEqual(history[0], current)
        archives = list((self.root / ".transcripts").glob("*.jsonl"))
        self.assertEqual(len(archives), 1)
        self.assertIn("Old observation 0", archives[0].read_text(encoding="utf-8"))

    def test_protected_batch_that_cannot_fit_is_never_truncated(self):
        compact = self.compact(3000, output=500)
        history, current, call, result = self.history(length=10, count=6)
        result["content"][0]["content"] = "UNSEEN" * 1500
        with patch("coding_agent.runtime.compact.call_model") as model:
            with self.assertRaisesRegex(ContextBudgetError, "近期工具批次"):
                compact.prepare(history, current["content"])
        model.assert_not_called()
        self.assertEqual(history[-2:], [call, result])
        self.assertEqual(result["content"][0]["content"], "UNSEEN" * 1500)

    def test_batch_pairing_is_preserved_even_across_the_recent_cut(self):
        compact = self.compact()
        history, current, call, result = self.history()
        history[-2:] = [call, result, *[{"role": "assistant", "content": "seen"} for _ in range(4)]]
        with patch("coding_agent.runtime.compact.call_model", return_value=self.response()):
            actual = compact.prepare(history, current["content"])
        self.assertIn(call, actual)
        self.assertIn(result, actual)
        self.assertEqual(actual.index(result), actual.index(call) + 1)

    def test_summary_helper_has_its_own_bounded_request(self):
        compact = self.compact(3000, output=500)
        history, current, _, _ = self.history(length=30000)
        with patch("coding_agent.runtime.compact.call_model", return_value=self.response()) as model:
            actual = compact.prepare(history, current["content"])
        request = model.call_args.kwargs
        helper = RequestBudget.from_request(3000, request["system"], [], request["max_tokens"])
        self.assertLessEqual(helper.history_tokens(request["messages"]), helper.history_limit)
        self.assertIn("middle omitted", request["messages"][0]["content"])
        self.assertIn(current, actual)
        compact.ensure_fit(actual)

    def test_too_small_summary_budget_stops_without_calling_helper(self):
        compact = self.compact(1500, output=100)
        history, current, _, _ = self.history()
        with patch("coding_agent.runtime.compact.call_model") as model:
            with self.assertRaisesRegex(ContextBudgetError, "已占满"):
                compact.prepare(history, current["content"])
        model.assert_not_called()

    def test_oversized_summary_is_a_clear_error_not_an_oversized_main_request(self):
        compact = self.compact()
        history, current, _, _ = self.history()
        with patch("coding_agent.runtime.compact.call_model", return_value=self.response("x" * 30000)):
            with self.assertRaisesRegex(ContextBudgetError, "无法放入预算"):
                compact.prepare(history, current["content"])
        self.assertIn(current, history)

    def test_automatic_summary_does_not_recompress_without_real_growth(self):
        compact = self.compact(8000)
        history, current, _, _ = self.history(length=2000, count=8)
        self.assertLess(compact.budget.history_tokens(history), compact.history_limit)
        self.assertGreater(compact.budget.history_tokens(history), compact.budget.soft_history_limit)
        with patch("coding_agent.runtime.compact.call_model", return_value=self.response()) as model:
            actual = compact.prepare(history, current["content"])
            for _ in range(5):
                actual = compact.prepare(actual, current["content"])
        model.assert_called_once()
        self.assertIn(current, actual)
        compact.ensure_fit(actual)

    def test_idle_force_shrink_does_not_buy_an_extra_summary_under_hard_limit(self):
        compact = self.compact(8000)
        history, current, _, _ = self.history(length=2000, count=8)
        with patch("coding_agent.runtime.compact.call_model") as model:
            actual = compact.prepare(history, current["content"], force_shrink=True)
        model.assert_not_called()
        compact.ensure_fit(actual)

    def test_manual_and_reactive_use_configured_budget(self):
        for mode in ("compact_history", "reactive_compact"):
            with self.subTest(mode=mode):
                compact = self.compact()
                history, current, call, result = self.history()
                with patch("coding_agent.runtime.compact.call_model", return_value=self.response()):
                    actual = getattr(compact, mode)(history, current["content"])
                compact.ensure_fit(actual)
                self.assertIn(current, actual)
                self.assertEqual(actual[-2:], [call, result])

    def test_working_set_uses_remaining_history_budget(self):
        source = "def active():\n    return 42\n"
        (self.root / "app.py").write_text(source, encoding="utf-8")
        working = CodeWorkingSet(self.root)
        working.record({"path": "app.py", "sha256": hashlib.sha256(source.encode()).hexdigest(),
                        "content": "1: def active():\n2:     return 42", "start_line": 1, "end_line": 2})
        compact = self.compact(6000, system="s" * 3000, output=1000, working_set=working)
        history, current, _, _ = self.history()
        with patch("coding_agent.runtime.compact.call_model", return_value=self.response()):
            actual = compact.prepare(history, current["content"])
        marker, = [message for message in actual if isinstance(message["content"], str)
                   and message["content"].startswith(MARKER_PREFIX)]
        entry, = json.loads(marker["content"][len(MARKER_PREFIX):])["files"]
        self.assertEqual(entry["status"], "verified_snapshot")
        self.assertIn("return 42", entry["code"])
        compact.ensure_fit(actual)
        self.assertLessEqual(compact.budget.history_tokens(actual), compact.history_limit)


if __name__ == "__main__":
    unittest.main()
