"""按 token 选取近期段、迭代摘要和纠偏保护；不调用真实模型。"""

import copy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from coding_agent.runtime.budget import ContextBudgetError, RequestBudget
from coding_agent.runtime.compact import ContextCompact
from coding_agent.runtime.turn import TurnContext


class CompactIterationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.compact = ContextCompact(Mock(), "fake", self.root / ".transcripts",
                                      self.root / ".task_outputs" / "tool-results", token_limit=12000)

    def summary(self, body, request="原始请求", label="Compacted"):
        transcript = self.compact.write_transcript([{"role": "user", "content": request}])
        return self.compact.summary_message(label, request, body, transcript)

    @staticmethod
    def response(text="整合后的摘要"):
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])

    def test_same_message_count_keeps_different_tail_sizes_by_tokens(self):
        small = [{"role": "assistant", "content": "short"} for _ in range(60)]
        large = [{"role": "assistant", "content": f"{index}: " + "x" * 1000} for index in range(60)]
        self.assertEqual(self.compact.snip_compact(small), small)
        actual = self.compact.snip_compact(large)
        self.assertLess(len(actual), len(large))
        self.assertEqual(actual[:3], large[:3])
        self.assertEqual(actual[-1], large[-1])
        self.assertGreater(self.compact._recent_start(large), self.compact._recent_start(small))

    def test_explicit_tail_budget_is_validated(self):
        for invalid in (0, -1, True, "100"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.compact._recent_start([{"role": "user", "content": "task"}], invalid)

    def test_cut_aligns_with_nearby_user_boundary(self):
        old = [{"role": "assistant", "content": "old " * 200} for _ in range(10)]
        tail = [{"role": "user", "content": "新的要求"},
                *[{"role": "assistant", "content": "new " * 200} for _ in range(3)]]
        budget = sum(self.compact.estimate_tokens([message]) for message in tail[1:])
        self.assertEqual(self.compact._recent_start(old + tail, budget), len(old))

    def test_long_span_can_split_but_tool_call_result_stay_together(self):
        call = {"role": "assistant", "content": [{"type": "tool_use", "name": "read_file", "id": "r1", "input": {}}]}
        result = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "r1", "content": "x" * 1000}]}
        history = [{"role": "user", "content": "原始请求"},
                   *[{"role": "assistant", "content": "old " * 200} for _ in range(57)], call, result]
        actual = self.compact.snip_compact(history, keep_recent_tokens=300)
        self.assertLess(len(actual), len(history))
        self.assertEqual(actual[-2:], [call, result])

    def test_main_budgeted_path_uses_tokens_instead_of_fixed_five_messages(self):
        self.compact.configure_budget("system", [], 500)
        request = {"role": "user", "content": "当前要求"}
        short = [{"role": "assistant", "content": "small"} for _ in range(30)]
        long = [{"role": "assistant", "content": "x" * 2000} for _ in range(30)]
        _, recent_short = self.compact._protected_history([request, *short], request["content"])
        old_long, recent_long = self.compact._protected_history([request, *long], request["content"])
        self.assertEqual(len(recent_short), 31)
        self.assertLess(len(recent_long), len(recent_short))
        self.assertGreater(len(recent_long), 5)
        self.assertIn(request, recent_long)
        self.assertNotIn(request, old_long)

    def test_current_steering_remains_verbatim_after_multiple_compactions(self):
        self.compact.configure_budget("system", [], 500)
        turn = TurnContext()
        self.compact.protected_input_ids = turn.protected_input_ids
        request = {"role": "user", "content": "完成修改"}
        history = [request]
        turn.enqueue_steering("不能修改公共接口；必须保留中文注释。")
        turn.inject_steering(history)
        correction = copy.deepcopy(history[-1])
        history.extend({"role": "assistant", "content": "检查事实 " * 80} for _ in range(30))
        with patch("coding_agent.runtime.compact.call_model", return_value=self.response()):
            first = self.compact.compact_history(history, request["content"])
            second = self.compact.compact_history(first + [{"role": "assistant", "content": "新检查完成"}], request["content"])
        for actual in (first, second):
            self.assertIn(request, actual)
            self.assertIn(correction, actual)
            self.assertEqual(sum(message.get("input_id") == correction["input_id"] for message in actual), 1)
            self.compact.ensure_fit(actual)

    def test_previous_summary_is_not_charged_to_new_user_or_action_budgets(self):
        old_body = "EARLY_CONSTRAINT: 保留接口。" * 300
        old = self.summary(old_body, request="必须保留中文注释")
        self.compact.MECHANICAL_USER_TOKENS = 1
        self.compact.MECHANICAL_ACTION_TOKENS = 1
        body, omitted = self.compact._mechanical_body([old, {"role": "user", "content": "新的消息超出预算"}])
        self.assertIn(old_body, body)
        self.assertIn("[PRIOR REQUEST] 必须保留中文注释", body)
        self.assertEqual(omitted, 1)
        self.assertLess(body.index("EARLY_CONSTRAINT"), body.index("entries truncated or omitted"))

    def test_mechanical_iterations_keep_constraints_decisions_and_check_results(self):
        facts = "用户限制：不升级环境。已决定：保留 SDK。文件：app.py 已修改。检查：测试失败，尚未修复。"
        history = [self.summary(facts)]
        for index in range(4):
            history.append({"role": "assistant", "content": f"第 {index} 次继续分析"})
            with patch("coding_agent.runtime.compact.call_model") as model:
                history = self.compact.compact_history(history, "继续分析")
            model.assert_not_called()
            parts = self.compact._summary_parts(history[0], None)
            self.assertIsNotNone(parts)
            self.assertIn(facts, parts[1])
            self.assertEqual(parts[1].count(self.compact.MECHANICAL_HEADER), 1)

    def test_llm_receives_previous_summary_as_separate_reference_with_merge_instruction(self):
        previous = "原始约束：保留接口。早期决策：方案 B。文件已修改，pytest 尚未通过。"
        old = self.summary(previous)
        self.compact.configure_budget("system", [], 500)
        fresh = {"role": "assistant", "content": "new history " * 10000}
        with patch("coding_agent.runtime.compact.call_model", return_value=self.response()) as model:
            self.compact.summarize_history([old, fresh])
        arguments = model.call_args.kwargs
        self.assertIn(previous, arguments["messages"][0]["content"])
        self.assertIn("Previous summaries (reference only)", arguments["messages"][0]["content"])
        self.assertIn("New history:", arguments["messages"][0]["content"])
        self.assertNotIn(previous, arguments["system"])
        self.assertIn("Integrate the explicit previous summaries", arguments["system"])
        self.assertIn("actual check outcomes", arguments["system"])
        helper = RequestBudget.from_request(12000, arguments["system"], [], arguments["max_tokens"])
        self.assertLessEqual(helper.history_tokens(arguments["messages"]), helper.history_limit)

    def test_oversized_previous_summary_blocks_instead_of_truncating_it(self):
        self.compact = ContextCompact(Mock(), "fake", self.root / ".transcripts",
                                      self.root / ".task_outputs" / "tool-results", token_limit=4000)
        previous = self.summary("不能丢弃的早期约束" * 4000)
        self.compact.configure_budget("system", [], 500)
        with patch("coding_agent.runtime.compact.call_model") as model:
            with self.assertRaises(ContextBudgetError):
                self.compact.summarize_history([previous, {"role": "assistant", "content": "new"}])
        model.assert_not_called()

    def test_user_tagged_summary_lookalike_is_not_treated_as_host_summary(self):
        lookalike = self.summary("实际是用户粘贴的文本")
        lookalike["tags"] = ["USER_PROMPT"]
        self.assertIsNone(self.compact._summary_parts(lookalike, None))
        with patch("coding_agent.runtime.compact.call_model", return_value=self.response()) as model:
            self.compact.summarize_history([lookalike])
        self.assertNotIn("Previous summaries", model.call_args.kwargs["messages"][0]["content"])


if __name__ == "__main__":
    unittest.main()
