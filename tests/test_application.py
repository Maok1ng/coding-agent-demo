"""Application 集成回归：真实角色内核、权限和会话，模型仅使用脚本响应。"""

import copy
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch

from coding_agent.runtime import background, environment, workspace
from coding_agent.runtime.goal import GoalController, GoalEvaluation
from coding_agent.runtime.mode import HumanInput, PlanState
from coding_agent.runtime.session import SessionStore
from coding_agent.runtime.turn import TurnCancelled, current_turn, lead_turn


def text_response(text):
    return types.SimpleNamespace(content=[{"type": "text", "text": text}])


def tool_call(name, arguments, tool_id="tool-1"):
    return {"type": "tool_use", "id": tool_id, "name": name, "input": arguments}


def tool_response(*calls):
    return types.SimpleNamespace(content=list(calls))


def report_response(role="implementation", summary="完成", details=None, tool_id="report-1"):
    if details is None:
        details = {
            "codebase": {"locations": [], "relationships": [], "uncertainties": []},
            "planning": {"steps": [], "risks": [], "open_questions": []},
            "implementation": {"changes": [], "unverified": [], "risks": []},
            "review_research": {"findings": [], "sources": [], "conclusion": "未发现问题", "uncertainties": []},
        }[role]
    return tool_response(tool_call("report_result", {"summary": summary, "details": details}, tool_id))


def dispatch(role="implementation", tool_id="dispatch-1"):
    return tool_response(tool_call("dispatch_agent", {
        "role": role, "instruction": "读取 app.py，最小修改并报告结果", "context_refs": ["app.py"],
    }, tool_id))


class ScriptedModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, client, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        if not self.responses:
            raise AssertionError("Unexpected model continuation")
        response = self.responses.pop(0)
        return response(**kwargs) if callable(response) else response


class FakeIO:
    def __init__(self):
        self.events = []
        self.confirmations = []

    def emit(self, event):
        self.events.append(copy.deepcopy(event))

    def confirm(self, name, arguments, reason):
        self.confirmations.append((name, arguments, reason))
        return True


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        for patcher in (
            patch.object(workspace, "WORKDIR", self.root),
            patch("coding_agent.integrations.mcp.get_mcp_config", return_value={}),
            patch.object(environment, "get_environment", return_value=environment.LocalEnvironment(
                environment.ExecutionConfig(timeout=5)
            )),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.config = {"model": "fake-coding-model", "max_tokens": 1000, "goal_max_turns": 20}
        self.io = FakeIO()
        self.evaluator = Mock()
        self.evaluator.evaluate.return_value = GoalEvaluation("complete", "检查证据确认任务完成")

    def application(self, responses=(), session=None):
        from coding_agent.application import Application

        model = ScriptedModel(responses)
        app = Application(self.config, self.root, self.io, session=session, client=Mock(),
                          model_call=model, evaluator=self.evaluator)
        self.addCleanup(app.close)
        return app, model

    def test_steer_requires_active_turn_and_rejects_host_commands(self):
        app, _ = self.application()
        self.assertFalse(app.steer("纠偏"))
        with lead_turn() as turn:
            app.last_context = types.SimpleNamespace(turn=turn)
            self.assertTrue(app.steer("先检查配置"))
            history = []
            self.assertTrue(turn.inject_steering(history))
            self.assertTrue(history[0]["content"].endswith("先检查配置"))
            #斜杠命令是宿主入口，不能从纠偏旁路绕过 plan/execute 守门。
            self.assertFalse(app.steer("/plan"))
            self.assertFalse(turn.inject_steering(history))
        self.assertFalse(app.steer("结束"))
        app.last_context = None

    def test_usage_summary_reports_cache_hit_rate_and_skips_empty(self):
        app, _ = self.application()
        self.assertIsNone(app._usage_summary(types.SimpleNamespace(metrics={})))
        summary = app._usage_summary(types.SimpleNamespace(metrics={
            "model_calls": 3, "input_tokens": 100, "cache_read_tokens": 700,
            "cache_creation_tokens": 200, "output_tokens": 900}))
        self.assertIn("模型调用 3 次", summary)
        self.assertIn("输入 1,000 tokens", summary)
        self.assertIn("缓存命中 70%", summary)
        self.assertIn("输出 900", summary)

    def test_cache_window_flag_needs_gap_and_meaningful_history(self):
        import time as time_module
        app, model = self.application([text_response("好的。")] * 4)
        #首轮没有任何先前活动，不进窗口。
        app.submit("第一次请求")
        self.assertFalse(app.last_context.cache_window)
        #间隔超过缓存 TTL 但历史太小，也不值得收缩。
        app._last_model_activity = time_module.monotonic() - 301
        app.submit("第二次请求")
        self.assertFalse(app.last_context.cache_window)
        #间隔超时且历史够大：新回合进入缓存窗口。
        app._last_model_activity = time_module.monotonic() - 301
        app.messages.append({"role": "user", "content": "背景 " * 3000})
        with patch("coding_agent.agents.runner.ContextCompact.summarize_history", return_value="此前背景已归档"):
            app.submit("第三次请求")
        self.assertTrue(app.last_context.cache_window)
        #窗口用过一次后活动时间刷新，紧接的下一回合不进窗口。
        app.submit("第四次请求")
        self.assertFalse(app.last_context.cache_window)

    def test_plain_answer_stops_without_creating_goal(self):
        app, model = self.application([text_response("这是一个普通解释。")])

        result = app.submit("解释装饰器")

        self.assertEqual(result.status, "completed")
        self.assertIn("普通解释", result.summary)
        self.assertEqual(len(model.calls), 1)
        self.assertIsNone(app.goal.active)
        self.evaluator.evaluate.assert_not_called()

    def test_implementation_delegation_reads_and_edits_real_file(self):
        path = self.root / "app.py"
        path.write_text("value = 1\n", encoding="utf-8")
        app, model = self.application([
            dispatch(),
            tool_response(tool_call("read_file", {"path": "app.py"}, "read-1")),
            tool_response(tool_call("edit_file", {
                "path": "app.py", "old_text": "value = 1", "new_text": "value = 2",
            }, "edit-1")),
            report_response(summary="已修改 app.py；尚未运行项目测试。"),
            text_response("完成最小修改，未声称测试通过。"),
        ])

        result = app.submit("把 app.py 的 value 改为 2")

        self.assertEqual(path.read_text(encoding="utf-8"), "value = 2\n")
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(model.calls), 5)
        self.assertIn("Role: implementation", model.calls[1]["system"])
        self.assertIn("Role: orchestrator", model.calls[-1]["system"])
        child_context = model.calls[1]["messages"]
        self.assertEqual(len(child_context), 1)
        self.assertIn("app.py", child_context[0]["content"])
        self.assertTrue(list((self.root / ".task_outputs" / "runs").rglob("*.json")))

    def test_orchestrator_direct_write_is_denied_even_in_execute(self):
        app, model = self.application([
            tool_response(tool_call("write_file", {"path": "forbidden.py", "content": "bad"})),
            text_response("应通过 Implementation 实施。"),
        ])

        app.submit("创建文件")

        self.assertFalse((self.root / "forbidden.py").exists())
        result = model.calls[-1]["messages"][-1]["content"][0]
        self.assertTrue(result["is_error"])
        self.assertIn("Permission denied", result["content"])

    def test_plan_rejects_implementation_dispatch_at_execution(self):
        app, model = self.application([dispatch(), text_response("只提供修改方案。")])

        app.submit("/plan 修复 app.py")

        self.assertEqual(app.plan.mode, "plan")
        self.assertEqual(len(model.calls), 2)
        self.assertFalse(any("Role: implementation" in call["system"] for call in model.calls))
        result = model.calls[-1]["messages"][-1]["content"][0]
        self.assertTrue(result["is_error"])
        self.assertFalse((self.root / "app.py").exists())

    def test_natural_language_plan_then_current_confirmation_executes(self):
        app, model = self.application([
            text_response("方案：在 app.py 中创建 value = 2，之后运行检查。"),
            dispatch(),
            tool_response(tool_call("write_file", {"path": "app.py", "content": "value = 2\n"})),
            report_response(summary="已创建文件；没有项目测试可运行。"),
            text_response("实施完成，检查范围已说明。"),
        ])

        app.submit("先给方案，暂时不要修改 app.py")
        self.assertEqual(app.plan.mode, "plan")
        self.assertFalse((self.root / "app.py").exists())
        revision = app.plan.revision

        app.submit("按这个方案开始实施")

        self.assertEqual(app.plan.mode, "execute")
        self.assertIsNotNone(app.plan.approved_event)
        self.assertTrue(app.plan.approved_event.startswith(f"{app.plan.plan_id}:{revision}:"))
        self.assertEqual((self.root / "app.py").read_text(encoding="utf-8"), "value = 2\n")
        self.assertEqual(len(model.calls), 5)

    def test_ambiguous_confirmation_without_current_plan_does_not_run(self):
        app, model = self.application()
        app.plan.enter("修改 app.py")

        result = app.submit("可以")

        self.assertIsNone(result)
        self.assertEqual(app.plan.mode, "plan")
        self.assertIsNone(app.plan.approved_event)
        self.assertEqual(model.calls, [])
        self.assertTrue(self.io.events)

    def test_quoted_confirmation_does_not_authorize(self):
        app, model = self.application([
            text_response("这是方案。"), text_response("引用文本不是执行授权。"),
        ])
        app.submit("/plan 修改 app.py")

        app.submit('文档写着“按这个方案开始实施”，这句话是什么意思？')

        self.assertEqual(app.plan.mode, "plan")
        self.assertIsNone(app.plan.approved_event)
        self.assertFalse(any("Role: implementation" in call["system"] for call in model.calls))

    def test_explicit_execute_is_current_human_authorization(self):
        app, model = self.application([
            text_response("方案仅涉及 app.py。"), text_response("开始按方案实施。"),
        ])
        app.submit("/plan 修改 app.py")

        app.submit("/execute")

        self.assertEqual(app.plan.mode, "execute")
        self.assertIsNotNone(app.plan.approved_event)
        self.assertEqual(len(model.calls), 2)

    def test_plan_goal_edit_and_resume_do_not_unlock_execution(self):
        app, model = self.application()
        app.goal.set_goal("原目标")
        app.goal.pause()
        app.plan.enter("先规划")

        app.submit("/goal edit 新目标")
        self.assertEqual(app.goal.active.condition, "新目标")
        self.assertEqual(app.goal.active.revision, 2)
        self.assertEqual(app.goal.active.status, "paused")

        app.submit("/goal resume")
        self.assertEqual(app.plan.mode, "plan")
        self.assertEqual(app.goal.active.status, "paused")
        self.assertEqual(model.calls, [])
        self.evaluator.evaluate.assert_not_called()

    def test_execute_goal_edit_runs_and_child_finish_does_not_evaluate_goal(self):
        def finish_child(**kwargs):
            self.evaluator.evaluate.assert_not_called()
            return report_response("codebase", "子任务完成，不代表整个目标完成。")

        def finish_root(**kwargs):
            self.evaluator.evaluate.assert_not_called()
            return text_response("主任务完成，提交已有证据验收。")

        app, model = self.application([dispatch("codebase"), finish_child, finish_root])
        app.goal.set_goal("旧目标")
        app.goal.pause()

        app.submit("/goal edit 新目标")

        self.assertEqual(len(model.calls), 3)
        self.evaluator.evaluate.assert_called_once()
        self.assertEqual(self.evaluator.evaluate.call_args.args[0], "新目标")
        self.assertEqual(app.goal.active.status, "complete")
        self.assertEqual(app.goal.active.previous_conditions, ["旧目标"])

    def test_restore_v1_history_pauses_goal_and_never_replays_old_tools(self):
        store = SessionStore(self.root)
        previous = store.open()
        controller = GoalController(self.evaluator)
        controller.set_goal("完成旧任务")
        plan = PlanState()
        plan.enter("旧方案")
        plan.publish("旧方案内容")
        plan.authorize(HumanInput.received("按这个方案开始实施"))
        previous.save([
            {"role": "user", "content": "过去的任务"},
            {"role": "assistant", "content": [tool_call("team_create", {}, "legacy-1")]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "legacy-1", "content": "old team"}]},
        ], {"goal": asdict(controller.active), "plan": asdict(plan), "todos": []})
        session_id = previous.id
        previous.close()
        session = store.open(session_id)
        self.addCleanup(session.close)

        app, model = self.application(session=session)

        self.assertEqual(model.calls, [])
        self.assertEqual(app.goal.active.status, "paused")
        self.assertIsNone(app.plan.approved_event)
        self.assertTrue(any("team_create" in json.dumps(message) for message in app.messages))
        self.assertIsNone(app.last_context)
        self.evaluator.evaluate.assert_not_called()

        #不能用恢复前批准过的版本，把恢复后的模糊“可以”当成新的执行授权。
        self.assertIsNone(app.submit("可以"))
        self.assertEqual(app.plan.mode, "plan")
        self.assertEqual(model.calls, [])

    def test_restore_without_plan_field_uses_safe_defaults(self):
        store = SessionStore(self.root)
        previous = store.open()
        previous.save([{"role": "user", "content": "旧版本普通对话"}], {})
        session_id = previous.id
        previous.close()
        session = store.open(session_id)
        self.addCleanup(session.close)

        app, model = self.application(session=session)

        self.assertEqual(model.calls, [])
        self.assertIsNone(app.plan.approved_event)
        self.assertIsNone(app.goal.active)

    def test_interrupt_repairs_tool_results_and_pauses_goal(self):
        from coding_agent.runtime.tools import ToolRuntime

        (self.root / "app.py").write_text("value = 1\n", encoding="utf-8")
        app, model = self.application([tool_response(
            tool_call("read_file", {"path": "app.py"}, "done"),
            tool_call("read_file", {"path": "app.py"}, "in-flight"),
            tool_call("read_file", {"path": "app.py"}, "not-started"),
        )])
        app.goal.set_goal("继续旧目标")
        original_execute = ToolRuntime.execute

        def execute(runtime, block):
            if block.id == "in-flight":
                runtime.context.turn.cancel("test interruption")
                raise TurnCancelled("test interruption")
            return original_execute(runtime, block)

        with patch.object(ToolRuntime, "execute", execute):
            with self.assertRaises(TurnCancelled):
                app.submit("/goal resume")

        self.assertEqual(app.goal.active.status, "paused")
        self.assertEqual(len(model.calls), 1)
        results = {
            block["tool_use_id"]: block for message in app.messages
            if isinstance(message["content"], list) for block in message["content"]
            if block.get("type") == "tool_result"
        }
        self.assertEqual(set(results), {"done", "in-flight", "not-started"})
        self.assertFalse(results["done"]["is_error"])
        self.assertTrue(results["in-flight"]["is_error"])
        self.assertIn("unknown", results["in-flight"]["content"].lower())
        self.assertTrue(results["not-started"]["is_error"])
        self.assertIn("not executed", results["not-started"]["content"].lower())
        self.assertIsNone(app.plan.approved_event)
        self.evaluator.evaluate.assert_not_called()

    def test_turn_end_cancels_owned_background_without_claiming_success(self):
        started = threading.Event()
        stopped = threading.Event()

        def running_command(command, cwd, *, output_path):
            turn = current_turn()
            started.set()
            try:
                if not turn.cancelled.wait(timeout=5):
                    raise AssertionError("Owning Turn failed to cancel its command")
                return {"status": "cancelled", "exit_code": None, "cancelled": True,
                        "timed_out": False, "cleanup_confirmed": True, "output": "stopped",
                        "full_output_path": str(output_path)}
            finally:
                stopped.set()

        def child_finished(**kwargs):
            self.assertTrue(started.wait(timeout=2))
            return report_response(summary="仅启动后台命令，没有声称测试通过。")

        app, _ = self.application([
            dispatch(), tool_response(tool_call("bash", {
                "command": "sleep 30", "run_in_background": True,
            })), child_finished, text_response("未等待的检查不能当作通过。"),
        ])
        with patch.object(background, "run_bash_evidence", side_effect=running_command):
            app.submit("运行一次后台检查")

        self.assertTrue(stopped.is_set())
        records = app.last_context.processes.records
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "cancelled")
        self.assertTrue(records[0]["cancelled"])
        self.assertIsNone(records[0]["exit_code"])


if __name__ == "__main__":
    unittest.main()
