"""失败保护与完整请求预算的整轮回归；使用临时工作区和脚本模型。"""

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from coding_agent.agents.roles import AgentTask
from coding_agent.agents.runner import AgentRunner
from coding_agent.application import Application
from coding_agent.runtime import environment, workspace
from coding_agent.runtime.compact import ContextCompact
from coding_agent.runtime.context import RunContext
from coding_agent.runtime.edit_recovery import RECOVERY_MESSAGE, STOP_MESSAGE
from coding_agent.runtime.model import cached_arguments
from coding_agent.runtime.tokens import json_tokens
from coding_agent.runtime.tools import ToolRuntime
from coding_agent.runtime.turn import TurnContext, turn_context
from tests.test_application import FakeIO, ScriptedModel, text_response, tool_call, tool_response


class RunnerRecoveryBudgetTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        for patcher in (
            patch.object(workspace, "WORKDIR", self.root),
            patch("coding_agent.integrations.mcp.get_mcp_config", return_value={}),
            patch.object(environment, "get_environment", return_value=environment.LocalEnvironment(
                environment.ExecutionConfig(timeout=5))),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.config = {"model": "fake-model", "max_tokens": 1000,
                       "memory_home": str(self.root / "host"), "memory_enabled": False}
        self.client, self.io = Mock(), FakeIO()
        self.target = self.root / "app.py"
        self.target.write_text("duplicate duplicate\n", encoding="utf-8")

    def runner(self, responses, role="implementation"):
        model = ScriptedModel(responses)
        runner = AgentRunner(self.client, self.config, self.io, model_call=model)
        context = RunContext(self.root, TurnContext(), role=role, approval_mode="full")
        return runner, context, model

    @staticmethod
    def results(messages):
        return {block["tool_use_id"]: block for message in messages
                if isinstance(message["content"], list) for block in message["content"]
                if block.get("type") == "tool_result"}

    @staticmethod
    def bad_edit(index):
        return tool_call("edit_file", {"path": "app.py", "old_text": "duplicate",
                                      "new_text": "fixed"}, f"edit-{index}")

    def failures(self, *, trailing=()):
        return [tool_response(tool_call("read_file", {"path": "app.py"}, "read")),
                *[tool_response(self.bad_edit(index)) for index in range(1, 6)],
                tool_response(self.bad_edit(6), *trailing)]

    def records(self, role):
        return [json.loads(path.read_text(encoding="utf-8"))
                for path in (self.root / ".task_outputs" / "runs").rglob(f"*-agent-{role}.json")]

    def test_sixth_failure_stops_model_and_pairs_remaining_batch_without_writes(self):
        runner, context, model = self.runner(self.failures(trailing=[
            tool_call("write_file", {"path": "never.py", "content": "bad"}, "later-write"),
            tool_call("read_file", {"path": "app.py"}, "later-read"),
        ]))
        history = [{"role": "user", "content": "修复 app.py"}]
        with turn_context(context.turn):
            result = runner.run(AgentTask("implementation", "修复 app.py"), context, messages=history)

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.summary, STOP_MESSAGE)
        self.assertEqual(len(model.calls), 7)
        self.assertTrue(context.edit_recovery.stopped)
        self.assertEqual(self.target.read_text(), "duplicate duplicate\n")
        self.assertFalse((self.root / "never.py").exists())
        results = self.results(history)
        self.assertIn(RECOVERY_MESSAGE, results["edit-3"]["content"])
        self.assertIn(STOP_MESSAGE, results["edit-6"]["content"])
        for tool_id in ("later-write", "later-read"):
            self.assertTrue(results[tool_id]["is_error"])
            self.assertIn("Not executed", results[tool_id]["content"])
            self.assertNotIn(tool_id, context.turn.started_tools)
        record = self.records("implementation")[0]
        self.assertEqual(record["status"], "blocked")
        self.assertEqual(len(self.results(record["messages"])), 9)

    def test_stop_preserves_prior_applied_changes_and_cannot_be_reset_by_compact(self):
        responses = self.failures()
        responses.insert(1, tool_response(tool_call("write_file", {
            "path": "done.py", "content": "already_applied = True\n"}, "earlier-write")))
        responses.insert(5, tool_response(tool_call("compact", {}, "compact")))
        runner, context, model = self.runner(responses)
        with turn_context(context.turn), patch("coding_agent.runtime.compact.call_model") as helper:
            result = runner.run(AgentTask("implementation", "修复 app.py"), context)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(len(model.calls), 9)
        self.assertTrue(context.edit_recovery.stopped)
        self.assertEqual((self.root / "done.py").read_text(), "already_applied = True\n")
        self.assertIn(str(self.root / "done.py"), context.changes)
        helper.assert_not_called()

    def test_child_stop_blocks_parent_second_dispatch_and_goal_until_new_human_turn(self):
        evaluator = Mock()
        model = ScriptedModel([tool_response(
            tool_call("dispatch_agent", {"role": "implementation", "instruction": "修复 app.py"}, "first"),
            tool_call("dispatch_agent", {"role": "implementation", "instruction": "不应执行"}, "later"),
        ), *self.failures(), text_response("这次是匹配不唯一导致的错误。")])
        app = Application(self.config, self.root, self.io, client=self.client,
                          model_call=model, evaluator=evaluator)
        self.addCleanup(app.close)

        result = app.submit("/goal 修复 app.py")

        self.assertEqual(result.status, "blocked")
        self.assertEqual(app.goal.active.status, "blocked")
        self.assertEqual(len(model.calls), 8)
        self.assertEqual(len(self.records("implementation")), 1)
        self.assertEqual(json.loads(self.results(app.messages)["first"]["content"])["status"], "blocked")
        self.assertTrue(self.results(app.messages)["later"]["is_error"])
        self.assertIn("Not executed", self.results(app.messages)["later"]["content"])
        evaluator.evaluate.assert_not_called()

        answer = app.submit("解释这次错误")
        self.assertEqual(answer.status, "completed")
        self.assertEqual(len(model.calls), 9)
        self.assertFalse(app.last_context.edit_recovery.stopped)
        self.assertEqual(app.goal.active.status, "blocked")
        evaluator.evaluate.assert_not_called()

    def test_fixed_prefix_overflow_is_blocked_without_any_model_request(self):
        self.config["context_token_limit"] = 1000
        runner, context, model = self.runner([])
        original = {"role": "user", "content": "原始请求必须保留"}
        history = [copy.deepcopy(original)]
        with turn_context(context.turn), patch("coding_agent.runtime.compact.call_model") as helper:
            result = runner.run(AgentTask("implementation", original["content"]), context, messages=history)
        self.assertEqual(result.status, "blocked")
        self.assertIn("上下文预算不足", result.summary)
        self.assertIn("output_reserve=1000", result.summary)
        self.assertEqual(history[0], original)
        self.assertEqual(model.calls, [])
        helper.assert_not_called()
        self.assertEqual(context.calls, [0])
        self.assertTrue(any(item.get("kind") == "context_budget" for item in context.evidence))

    def test_oversized_current_request_is_preserved_and_not_sent_to_summary(self):
        runner, context, model = self.runner([])
        request = "完整用户要求" * 6000
        history = [{"role": "user", "content": request}]
        with turn_context(context.turn), patch("coding_agent.runtime.compact.call_model") as helper:
            result = runner.run(AgentTask("implementation", request), context, messages=history)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(history[0]["content"], request)
        self.assertEqual(model.calls, [])
        helper.assert_not_called()

    def test_host_goal_is_blocked_when_context_budget_cannot_fit(self):
        self.config["context_token_limit"] = 1000
        evaluator, model = Mock(), ScriptedModel([])
        app = Application(self.config, self.root, self.io, client=self.client,
                          model_call=model, evaluator=evaluator)
        self.addCleanup(app.close)
        result = app.submit("/goal 修复 app.py")
        self.assertEqual(result.status, "blocked")
        self.assertEqual(app.goal.active.status, "blocked")
        evaluator.evaluate.assert_not_called()
        self.assertEqual(model.calls, [])

    def test_budget_uses_frozen_request_inputs_and_refreshes_dynamic_tool_catalog(self):
        self.config["context_token_limit"] = 80000
        runner, context, model = self.runner([
            tool_response(tool_call("list_files", {}, "list")), text_response("完成")])
        compact = ContextCompact(self.client, self.config["model"], self.root / ".transcripts",
                                 self.root / ".task_outputs" / "tool-results", token_limit=80000)
        configured = []
        configure = compact.configure_budget

        def observe(system, tools, maximum):
            budget = configure(system, tools, maximum)
            configured.append((copy.deepcopy(system), copy.deepcopy(tools), maximum, budget))
            return budget

        compact.configure_budget = observe
        pool = ToolRuntime.pool

        def dynamic_pool(runtime):
            tools, handlers = pool(runtime)
            if model.calls:
                tools = [*tools, {"name": "mcp__host__read_extra", "description": "read " * 500,
                                  "input_schema": {"type": "object", "properties": {}}}]
            return tools, handlers

        extra = Mock(side_effect=["first host state", "second host state"])
        with (turn_context(context.turn), patch("coding_agent.agents.runner.ContextCompact", return_value=compact),
              patch.object(runner, "_system", wraps=runner._system) as system_builder,
              patch.object(ToolRuntime, "pool", dynamic_pool)):
            result = runner.run(AgentTask("implementation", "查看目录"), context, extra_context=extra)
        self.assertEqual(result.status, "completed")
        self.assertEqual(extra.call_count, 2)
        self.assertEqual(system_builder.call_count, 2)
        self.assertEqual(len(configured), 2)
        self.assertGreater(configured[1][3].fixed_tokens, configured[0][3].fixed_tokens)
        for call, (system, tools, maximum, budget) in zip(model.calls, configured):
            self.assertEqual((call["system"], call["tools"], call["max_tokens"]), (system, tools, maximum))
            compact.ensure_fit(call["messages"])
            actual = cached_arguments(call)
            payload = {key: actual[key] for key in ("system", "tools", "messages")}
            self.assertLessEqual(json_tokens(payload) + maximum, budget.total_limit)

    def test_runner_rejects_invalid_budget_values_without_silent_integer_conversion(self):
        for value in (True, "16000", 999, None):
            with self.subTest(context_token_limit=value):
                self.config["context_token_limit"] = value
                runner, context, model = self.runner([])
                with turn_context(context.turn), self.assertRaisesRegex(ValueError, "context_token_limit"):
                    runner.run(AgentTask("implementation", "任务"), context)
                self.assertEqual(model.calls, [])
        self.config["context_token_limit"] = 16000
        for value in (False, "1000", 0):
            with self.subTest(max_tokens=value):
                self.config["max_tokens"] = value
                runner, context, model = self.runner([])
                with turn_context(context.turn), self.assertRaisesRegex(ValueError, "max_tokens"):
                    runner.run(AgentTask("implementation", "任务"), context)
                self.assertEqual(model.calls, [])


if __name__ == "__main__":
    unittest.main()
