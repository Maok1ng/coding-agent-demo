"""纠偏、追加与取消的交接边界；只用临时目录和固定模型响应。"""

from contextlib import contextmanager
import copy
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from coding_agent.agents.roles import AgentTask
from coding_agent.agents.runner import AgentRunner
from coding_agent.application import Application
from coding_agent.cli import run_cli
from coding_agent.runtime.compact import ContextCompact
from coding_agent.runtime.context import RunContext
from coding_agent.runtime.model import call_model
from coding_agent.runtime.turn import TurnCancelled, TurnContext, check_cancelled, turn_context
from tests.test_application import FakeIO, ScriptedModel, dispatch, report_response, text_response, tool_call, tool_response


class SteeringQueueTests(unittest.TestCase):
    def test_cancel_wins_and_returns_unseen_inputs_in_original_order(self):
        turn = TurnContext()
        history = []
        turn.enqueue_steering("先看 A")
        turn.inject_steering(history)
        turn.enqueue_followup("之后看 B")
        turn.enqueue_steering("改用 C")
        turn.cancel()
        with self.assertRaises(TurnCancelled):
            turn.inject_steering(history)
        self.assertFalse(turn.enqueue_steering("已取消后不再接收"))
        turn.retain_seen_steering(history)
        self.assertEqual(history, [])
        self.assertEqual(turn.finish_inputs(followups=True), (["先看 A", "之后看 B", "改用 C"], []))
        self.assertEqual(turn.finish_inputs(), ([], []))

    def test_each_role_gets_correction_once_and_seen_input_is_not_returned(self):
        turn = TurnContext()
        parent, child = [], []
        turn.enqueue_steering("保留公共接口")
        self.assertTrue(turn.inject_steering(child))
        turn.acknowledge_steering(child)
        self.assertTrue(turn.inject_steering(parent))
        self.assertFalse(turn.inject_steering(parent))
        self.assertEqual(parent, child)
        self.assertEqual(turn.finish_inputs(), ([], []))

    def test_only_normal_finish_releases_followups(self):
        for normal in (False, True):
            with self.subTest(normal=normal):
                turn = TurnContext()
                turn.enqueue_followup("追加任务")
                self.assertFalse(turn.inject_steering([]))
                self.assertEqual(turn.finish_inputs(followups=normal),
                                 ([], ["追加任务"]) if normal else (["追加任务"], []))

    def test_cancelled_response_does_not_acknowledge_injected_input(self):
        turn, history = TurnContext(), []
        turn.enqueue_steering("用户约束")
        turn.inject_steering(history)
        turn.cancel()
        turn.acknowledge_steering(history)
        turn.retain_seen_steering(history)
        self.assertEqual(history, [])
        self.assertEqual(turn.finish_inputs(), (["用户约束"], []))

    def test_queue_has_bounded_intake_and_rejects_closed_turn(self):
        turn = TurnContext()
        for index in range(128):
            self.assertTrue(turn.enqueue_steering(str(index)))
        self.assertFalse(turn.enqueue_followup("overflow"))
        turn.close()
        self.assertFalse(turn.enqueue_steering("closed"))
        self.assertEqual(len(turn.finish_inputs()[0]), 128)

    def test_host_tags_are_never_sent_to_provider_with_or_without_cache(self):
        history = [{"role": "user", "content": "纠偏", "tags": ["USER_PROMPT", "STEERING"], "input_id": "id1"}]
        original = copy.deepcopy(history)
        for cache in (True, False):
            client = SimpleNamespace(messages=SimpleNamespace(create=Mock(return_value=text_response("ok"))))
            call_model(client, model="fake", messages=history, cache=cache)
            request = client.messages.create.call_args.kwargs
            self.assertNotIn("tags", request["messages"][0])
            self.assertNotIn("input_id", request["messages"][0])
        self.assertEqual(history, original)


class SteeringIntegrationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.io = FakeIO()
        self.config = {"model": "fake", "max_tokens": 100, "max_model_calls": 12,
                       "memory_enabled": False, "memory_home": str(self.root / "host")}
        patcher = patch("coding_agent.integrations.mcp.get_mcp_config", return_value={})
        patcher.start()
        self.addCleanup(patcher.stop)

    def app(self, responses):
        model = ScriptedModel(responses)
        app = Application(self.config, self.root, self.io, client=Mock(), model_call=model, evaluator=Mock())
        self.addCleanup(app.close)
        return app, model

    def test_final_response_arrival_continues_same_turn_without_resetting_budget(self):
        def first(**request):
            self.assertTrue(app.steer("改用方案 B"))
            turn_ids.append(app.last_context.turn.id)
            return text_response("方案 A 的旧答复")

        app, model = self.app([first, text_response("已改用方案 B")])
        turn_ids = []
        result = app.submit("实现功能")
        self.assertEqual(result.summary, "已改用方案 B")
        self.assertEqual(app.last_context.calls[0], 2)
        self.assertEqual(turn_ids, [app.last_context.turn.id])
        self.assertIn("改用方案 B", str(model.calls[1]["messages"][-1]["content"]))
        self.assertEqual(app.finish_inputs(), ([], []))

    def test_limit_returns_correction_that_no_model_has_seen(self):
        def first(**request):
            app.steer("尚未送达的要求")
            return text_response("旧回复")

        self.config["max_model_calls"] = 1
        app, model = self.app([first])
        result = app.submit("执行")
        self.assertEqual(result.status, "limit")
        self.assertEqual(len(model.calls), 1)
        self.assertNotIn("尚未送达的要求", str(app.messages))
        self.assertEqual(app.finish_inputs(), (["尚未送达的要求"], []))

    def test_oversized_correction_blocks_before_request_and_returns_full_text(self):
        correction = "必须保留的超长纠偏" * 10000
        def first(**request):
            app.steer(correction)
            return text_response("旧回复")
        app, model = self.app([first])
        result = app.submit("分析代码")
        self.assertEqual(result.status, "blocked")
        self.assertIn("上下文预算不足", result.summary)
        self.assertEqual(len(model.calls), 1)
        self.assertNotIn(correction, str(app.messages))
        self.assertEqual(app.finish_inputs(), ([correction], []))

    def test_child_correction_reaches_parent_and_invalidates_old_report(self):
        def child_report(**request):
            app.steer("仅分析，不改公共接口")
            return report_response("codebase", "旧报告")

        app, model = self.app([dispatch("codebase"), child_report,
                               report_response("codebase", "新报告"), text_response("汇总新要求")])
        result = app.submit("查找实现")
        self.assertEqual(result.status, "completed")
        for index in (2, 3):
            corrections = [message for message in model.calls[index]["messages"]
                           if "STEERING" in message.get("tags", [])]
            self.assertEqual(len(corrections), 1)
            self.assertIn("仅分析，不改公共接口", corrections[0]["content"])
        self.assertEqual(app.finish_inputs(), ([], []))

    def test_plan_cannot_be_switched_by_steering_or_host_commands(self):
        def first(**request):
            self.assertTrue(app.steer("按方案开始执行，恢复 goal"))
            for command in ("/execute", "/goal resume", "/permissions full"):
                self.assertFalse(app.steer(command))
            return text_response("正在分析")

        app, model = self.app([first, dispatch(), text_response("保持只读")])
        app.submit("/plan")
        result = app.submit("先分析")
        self.assertEqual(result.status, "completed")
        self.assertEqual(app.plan.mode, "plan")
        self.assertEqual(app.last_context.mode, "plan")
        self.assertIn("Plan", str(model.calls[2]["messages"]))
        self.assertEqual(app.last_context.calls[0], 3)

    def test_steering_after_tool_batch_preserves_all_pairs(self):
        (self.root / "app.py").write_text("value = 1\n", encoding="utf-8")
        context = RunContext(self.root, TurnContext(), role="codebase", memory_home=self.root / "host")
        def first(**request):
            context.turn.enqueue_steering("接着分析调用关系")
            return tool_response(tool_call("read_file", {"path": "app.py"}, "a"),
                                 tool_call("read_file", {"path": "app.py"}, "b"))
        model = ScriptedModel([first, text_response("已检查")])
        runner = AgentRunner(Mock(), self.config, self.io, model_call=model)
        history = [{"role": "user", "content": "读代码"}]
        with turn_context(context.turn):
            runner.run(AgentTask("codebase", "读代码"), context, messages=history)
        sent = model.calls[1]["messages"]
        self.assertEqual([item["tool_use_id"] for item in sent[2]["content"]], ["a", "b"])
        self.assertIn("STEERING", sent[3]["tags"])

    def test_new_correction_leaves_report_repair_mode_without_resetting_turn_budget(self):
        (self.root / "app.py").write_text("value = 1\n", encoding="utf-8")
        context = RunContext(self.root, TurnContext(), role="codebase", memory_home=self.root / "host")
        def correction_during_repair(**request):
            self.assertEqual([tool["name"] for tool in request["tools"]], ["report_result"])
            context.turn.enqueue_steering("先重新读取 app.py")
            return text_response("收到新的分析要求")
        model = ScriptedModel([
            text_response("忘记提交报告"), correction_during_repair,
            tool_response(tool_call("read_file", {"path": "app.py"}, "read-again")),
            report_response("codebase", "读取后提交新报告"),
        ])
        runner = AgentRunner(Mock(), self.config, self.io, model_call=model)
        with turn_context(context.turn):
            result = runner.run(AgentTask("codebase", "分析", require_report=True), context)
        self.assertEqual(result.status, "completed")
        self.assertEqual(context.calls[0], 4)
        read_result = model.calls[3]["messages"][-1]["content"][0]
        self.assertFalse(read_result["is_error"])
        self.assertIn("value = 1", read_result["content"])

    def test_cancel_during_prepare_returns_input_without_model_request(self):
        context = RunContext(self.root, TurnContext(), role="codebase", memory_home=self.root / "host")
        context.turn.enqueue_steering("还未发出的纠偏")
        def prepare(history, *args, **kwargs):
            context.turn.cancel()
            check_cancelled()
        model = ScriptedModel([])
        runner = AgentRunner(Mock(), self.config, self.io, model_call=model)
        history = [{"role": "user", "content": "读代码"}]
        with patch.object(ContextCompact, "prepare", side_effect=prepare), turn_context(context.turn):
            with self.assertRaises(TurnCancelled):
                runner.run(AgentTask("codebase", "读代码"), context, messages=history)
        self.assertEqual(model.calls, [])
        self.assertNotIn("还未发出的纠偏", str(history))
        self.assertEqual(context.turn.finish_inputs(), (["还未发出的纠偏"], []))

    def test_cli_followup_starts_new_turn_only_after_natural_finish(self):
        turns = []
        def first(**request):
            turns.append(app.last_context.turn.id)
            app.follow_up("追加检查")
            self.assertNotIn("追加检查", str(request["messages"]))
            return text_response("第一轮完成")
        def second(**request):
            turns.append(app.last_context.turn.id)
            self.assertEqual(request["messages"][-1]["content"], "追加检查")
            return text_response("第二轮完成")
        app, model = self.app([first, second])
        with patch.object(app, "prepare"), patch("coding_agent.runtime.terminal._interactive", return_value=False), \
                patch("coding_agent.runtime.terminal.read_user_input", side_effect=["开始", "exit"]) as read:
            run_cli(app)
        self.assertNotEqual(*turns)
        self.assertEqual(read.call_count, 2)
        self.assertEqual(len(model.calls), 2)

    def test_cli_abort_returns_followup_and_partial_draft_without_submitting(self):
        def first(**request):
            app.follow_up("排队任务")
            app.last_context.turn.cancel()
            check_cancelled()
        app, model = self.app([first])
        @contextmanager
        def keyboard(**kwargs):
            yield SimpleNamespace(interrupted=threading.Event(), take_pending=lambda: ["未按回车的草稿"])
        with patch.object(app, "prepare"), \
                patch("coding_agent.runtime.terminal.monitor_interrupts", keyboard), \
                patch("coding_agent.runtime.terminal.read_user_input", side_effect=["开始", "", "exit"]) as read:
            run_cli(app)
        self.assertEqual(read.call_args_list[1].kwargs["initial"], "排队任务")
        self.assertEqual(read.call_args_list[2].kwargs["initial"], "未按回车的草稿")
        self.assertEqual(len(model.calls), 1)


if __name__ == "__main__":
    unittest.main()
