"""统一 AgentRunner 边界测试：提问、委派、预算、压缩和取消不依赖真实模型。"""

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
from coding_agent.runtime.context import RunContext
from coding_agent.runtime.goal import GoalEvaluation
from coding_agent.runtime.session import SessionStore
from coding_agent.runtime.tools import ToolRuntime
from coding_agent.runtime.turn import TurnCancelled, TurnContext, turn_context
from tests.test_application import FakeIO, ScriptedModel, dispatch, report_response, text_response, tool_call, tool_response


class RunnerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        for patcher in (
            patch.object(workspace, "WORKDIR", self.root),
            patch("coding_agent.integrations.mcp.get_mcp_config", return_value={}),
            patch.object(environment, "get_environment", return_value=environment.LocalEnvironment(
                environment.ExecutionConfig(timeout=5)
            )),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = Mock()
        self.io = FakeIO()
        self.config = {"model": "fake-model", "max_tokens": 1000, "goal_max_turns": 20}

    def app(self, responses, evaluator=None, session=None):
        model = ScriptedModel(responses)
        application = Application(self.config, self.root, self.io, session, client=self.client,
                                  model_call=model, evaluator=evaluator or Mock())
        self.addCleanup(application.close)
        return application, model

    def runner(self, responses=(), *, role="orchestrator", max_calls=20, model_call=None):
        model = model_call or ScriptedModel(responses)
        runner = AgentRunner(self.client, self.config, self.io, model_call=model)
        context = RunContext(self.root, TurnContext(), role=role, max_calls=max_calls)
        return runner, context, model

    @staticmethod
    def results(messages):
        return {
            block["tool_use_id"]: block for message in messages
            if isinstance(message["content"], list) for block in message["content"]
            if block.get("type") == "tool_result"
        }

    def agent_records(self, role=None):
        records = []
        for path in (self.root / ".task_outputs" / "runs").rglob("*-agent-*.json"):
            record = json.loads(path.read_text(encoding="utf-8"))
            if role is None or record["task"]["role"] == role:
                records.append((path, record))
        return records

    def compactor(self):
        compact = Mock()
        compact.prepare.side_effect = lambda messages, request, *, force_shrink=False: list(messages)
        compact.reactive_compact.side_effect = lambda messages, request: [
            {"role": "user", "content": "[compressed context]"}
        ]
        compact.compact_history.side_effect = lambda messages, request: [
            {"role": "user", "content": "[manual compact]"}
        ]
        return compact

    def test_cache_window_shrinks_only_the_first_model_request(self):
        for cache_window in (False, True):
            with self.subTest(cache_window=cache_window):
                compact = self.compactor()
                runner, context, model = self.runner([
                    tool_response(tool_call("list_files", {}, "list")),
                    text_response("已查看文件。"),
                ])
                context.cache_window = cache_window
                with patch("coding_agent.agents.runner.ContextCompact", return_value=compact), turn_context(context.turn):
                    result = runner.run(AgentTask("orchestrator", "查看目录"), context)
                self.assertEqual(result.status, "completed")
                self.assertEqual(len(model.calls), 2)
                self.assertEqual([call.kwargs["force_shrink"] for call in compact.prepare.call_args_list],
                                 [cache_window, False])

    def test_ask_user_prevents_other_tools_in_same_batch(self):
        (self.root / "app.py").write_text("value = 1\n", encoding="utf-8")
        runner, context, model = self.runner([
            tool_response(
                tool_call("write_file", {"path": "forbidden.py", "content": "bad"}, "write"),
                tool_call("ask_user", {"question": "修改哪个接口？", "options": ["A", "B"]}, "ask"),
                tool_call("read_file", {"path": "app.py"}, "read"),
            )
        ], role="implementation")
        history = [{"role": "user", "content": "需要先确认接口"}]
        with turn_context(context.turn):
            result = runner.run(AgentTask("implementation", "先询问用户"), context, messages=history)

        self.assertEqual(result.status, "needs_input")
        self.assertEqual(len(model.calls), 1)
        self.assertFalse((self.root / "forbidden.py").exists())
        results = self.results(history)
        self.assertFalse(results["ask"]["is_error"])
        self.assertTrue(results["write"]["is_error"])
        self.assertTrue(results["read"]["is_error"])
        self.assertIn("Not executed", results["read"]["content"])
        self.assertNotIn(str(self.root / "app.py"), context.versions)

    def test_multiple_questions_skip_entire_batch_without_creating_pending_answer(self):
        app, _ = self.app([
            tool_response(
                tool_call("ask_user", {"question": "问题一？"}, "ask-1"),
                tool_call("ask_user", {"question": "问题二？"}, "ask-2"),
                tool_call("read_file", {"path": "not-present.py"}, "read"),
            ), text_response("同一批只能提出一个问题。"),
        ])

        app.submit("分析任务")

        self.assertIsNone(app.goal.user_input.pending)
        results = self.results(app.messages)
        self.assertEqual(len(results), 3)
        self.assertTrue(all(result["is_error"] for result in results.values()))

    def test_child_question_is_host_forwarded_and_stops_remaining_dispatches(self):
        def answered(**kwargs):
            payload = json.loads(self.results(kwargs["messages"])["dispatch-1"]["content"])
            self.assertEqual(payload["status"], "answered")
            self.assertEqual(payload["answer"], "PostgreSQL")
            return text_response("已记录真实回答，不重放原子任务。")

        app, model = self.app([
            tool_response(
                tool_call("dispatch_agent", {"role": "codebase", "instruction": "询问数据库"}, "dispatch-1"),
                tool_call("dispatch_agent", {"role": "implementation", "instruction": "不应执行"}, "later-dispatch"),
            ),
            tool_response(tool_call("ask_user", {"question": "选择数据库？",
                                               "options": ["SQLite", "PostgreSQL"]}, "child-ask")),
            answered,
        ])

        result = app.submit("分析数据层")

        self.assertEqual(result.status, "needs_input")
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(app.goal.user_input.pending.tool_use_id, "dispatch-1")
        self.assertEqual(app.goal.user_input.pending.question, "选择数据库？")
        child = json.loads(self.results(app.messages)["dispatch-1"]["content"])
        self.assertEqual(child["status"], "needs_input")
        self.assertTrue(child["evidence_refs"])
        self.assertTrue(self.results(app.messages)["later-dispatch"]["is_error"])
        self.assertNotIn("child-ask", self.results(app.messages))
        child_history = self.agent_records("codebase")[0][1]
        self.assertEqual(child_history["status"], "needs_input")
        self.assertTrue(any(event["type"] == "question" for event in self.io.events))
        self.assertEqual(self.agent_records("implementation"), [])

        app.submit("2")
        self.assertEqual(len(model.calls), 3)
        self.assertIsNone(app.goal.user_input.pending)

    def test_real_answer_updates_placeholder_and_continues_ordinary_turn(self):
        def answered(**kwargs):
            payload = json.loads(self.results(kwargs["messages"])["ask-1"]["content"])
            self.assertEqual(payload["status"], "answered")
            self.assertEqual(payload["answer"], "PostgreSQL")
            return text_response("已记录选择 PostgreSQL。")

        app, model = self.app([
            tool_response(tool_call("ask_user", {
                "question": "选择数据库？", "options": ["SQLite", "PostgreSQL"],
            }, "ask-1")), answered,
        ])
        first = app.submit("设计数据层")
        self.assertEqual(first.status, "needs_input")
        self.assertEqual(json.loads(self.results(app.messages)["ask-1"]["content"])["status"], "waiting_user")

        result = app.submit("2")

        self.assertEqual(result.status, "completed")
        self.assertIsNone(app.goal.user_input.pending)
        self.assertIsNone(app.goal.active)
        self.assertEqual(len(model.calls), 2)

    def test_root_and_child_share_one_model_call_budget(self):
        runner, context, model = self.runner([dispatch("codebase"), report_response("codebase", "定位完成。")], max_calls=2)
        history = [{"role": "user", "content": "查找相关实现"}]
        with turn_context(context.turn):
            result = runner.run(AgentTask("orchestrator", "查找相关实现"), context, messages=history)

        self.assertEqual(len(model.calls), 2)
        self.assertEqual(context.calls[0], 2)
        self.assertEqual(result.status, "limit")
        child = json.loads(self.results(history)["dispatch-1"]["content"])
        self.assertEqual(child["status"], "completed")
        self.assertEqual(child["contract_status"], "validated")
        self.assertEqual(child["role"], "codebase")
        self.assertEqual(self.agent_records("orchestrator")[0][1]["status"], "limit")

    def test_child_at_exhausted_budget_returns_limit_without_extra_model_call(self):
        runner, context, model = self.runner([dispatch("codebase")], max_calls=1)
        history = [{"role": "user", "content": "查找相关实现"}]
        with turn_context(context.turn):
            result = runner.run(AgentTask("orchestrator", "查找相关实现"), context, messages=history)

        self.assertEqual(result.status, "limit")
        self.assertEqual(len(model.calls), 1)
        child = json.loads(self.results(history)["dispatch-1"]["content"])
        self.assertEqual(child["status"], "limit")
        self.assertTrue(child["evidence_refs"])
        self.assertEqual(self.agent_records("codebase")[0][1]["status"], "limit")

    def test_context_limit_has_one_reactive_compact_retry(self):
        compact = self.compactor()
        model = Mock(side_effect=[RuntimeError("prompt_too_long"), text_response("压缩后完成。")])
        runner, context, _ = self.runner(model_call=model)
        history = [{"role": "user", "content": "old context"}]
        with patch("coding_agent.agents.runner.ContextCompact", return_value=compact), turn_context(context.turn):
            result = runner.run(AgentTask("orchestrator", "task"), context, messages=history)

        self.assertEqual(result.status, "completed")
        self.assertEqual(model.call_count, 2)
        compact.reactive_compact.assert_called_once()
        self.assertEqual(model.call_args_list[1].kwargs["messages"][0]["content"], "[compressed context]")

    def test_second_consecutive_context_error_does_not_loop_forever(self):
        compact = self.compactor()
        model = Mock(side_effect=[RuntimeError("prompt_too_long"), RuntimeError("too many tokens")])
        runner, context, _ = self.runner(model_call=model)
        with patch("coding_agent.agents.runner.ContextCompact", return_value=compact), turn_context(context.turn):
            with self.assertRaisesRegex(RuntimeError, "too many tokens"):
                runner.run(AgentTask("orchestrator", "task"), context)

        compact.reactive_compact.assert_called_once()
        self.assertEqual(model.call_count, 2)
        self.assertEqual(self.agent_records("orchestrator")[0][1]["status"], "failed")

    def test_manual_compact_runs_only_after_tool_batch_is_paired(self):
        (self.root / "app.py").write_text("value = 1\n", encoding="utf-8")
        compact = self.compactor()

        def compact_batch(messages, request):
            results = self.results(messages)
            self.assertEqual(set(results), {"compact", "read"})
            self.assertTrue(all(not result["is_error"] for result in results.values()))
            self.assertIn("value = 1", results["read"]["content"])
            return [{"role": "user", "content": "[manual compact]"}]

        compact.compact_history.side_effect = compact_batch
        runner, context, model = self.runner([
            tool_response(tool_call("compact", {}, "compact"),
                          tool_call("read_file", {"path": "app.py"}, "read")),
            text_response("压缩后的正常回答。"),
        ])
        with patch("coding_agent.agents.runner.ContextCompact", return_value=compact), turn_context(context.turn):
            result = runner.run(AgentTask("orchestrator", "task"), context)

        self.assertEqual(result.status, "completed")
        compact.compact_history.assert_called_once()
        self.assertEqual(model.calls[1]["messages"][0]["content"], "[manual compact]")

    def test_child_uses_focused_context_and_returns_trace_reference(self):
        (self.root / "app.py").write_text("value = 1\n", encoding="utf-8")
        runner, context, model = self.runner()
        evidence = context.artifacts.save("previous-evidence", {"exit_code": 7, "status": "failed"})
        request = "只分析 app.py，保留用户修改；已有方案仅为参考"
        model.responses = [
            tool_response(tool_call("dispatch_agent", {"role": "codebase", "instruction": request,
                                                        "context_refs": ["app.py", evidence]}, "child")),
            report_response("codebase", "定位 app.py:1。"), text_response("已汇总定位与证据。"),
        ]
        history = [{"role": "user", "content": "unrelated old conversation must not be copied"}]
        with turn_context(context.turn):
            result = runner.run(AgentTask("orchestrator", "定位代码"), context, messages=history)

        child_messages = model.calls[1]["messages"]
        self.assertEqual(len(child_messages), 1)
        self.assertIn(request, child_messages[0]["content"])
        self.assertIn(evidence, child_messages[0]["content"])
        self.assertNotIn("unrelated old conversation", child_messages[0]["content"])
        child = json.loads(self.results(history)["child"]["content"])
        self.assertEqual(child["contract_status"], "validated")
        self.assertEqual(child["role"], "codebase")
        trace_path = Path(child["evidence_refs"][-1])
        trace = json.loads(trace_path.read_text(encoding="utf-8"))
        self.assertEqual(trace["task"]["instruction"], request)
        self.assertEqual(trace["task"]["context_refs"], ["app.py", evidence])
        self.assertEqual(trace["task"]["original_request"], "定位代码")
        self.assertTrue(trace["task"]["require_report"])
        self.assertIn(str(trace_path), result.evidence_refs)

    def test_cancelled_late_model_response_is_discarded_and_history_is_archived(self):
        history = [{"role": "user", "content": f"existing message {number}"} for number in range(10)]
        runner, context, model = self.runner()

        def late_response(**kwargs):
            context.turn.cancel("test cancelled model")
            return text_response("This late response must never be added")

        model.responses = [late_response]
        with turn_context(context.turn):
            with self.assertRaises(TurnCancelled):
                runner.run(AgentTask("orchestrator", "task"), context, messages=history)

        trace = self.agent_records("orchestrator")[0][1]
        self.assertEqual(trace["status"], "cancelled")
        self.assertEqual(trace["messages"], history)
        self.assertEqual(len(trace["messages"]), 10)
        self.assertNotIn("late response", json.dumps(trace))

    def test_cancel_during_batch_archives_all_calls_with_repaired_results(self):
        (self.root / "app.py").write_text("value = 1\n", encoding="utf-8")
        runner, context, _ = self.runner([tool_response(
            tool_call("read_file", {"path": "app.py"}, "done"),
            tool_call("read_file", {"path": "app.py"}, "cancelled"),
            tool_call("read_file", {"path": "app.py"}, "never-started"),
        )])
        history = [{"role": "user", "content": "task"}]
        checkpoints = []
        original = ToolRuntime.execute

        def execute(runtime, block):
            if block.id == "cancelled":
                context.turn.cancel("test cancellation during batch")
                raise TurnCancelled("test cancellation during batch")
            return original(runtime, block)

        with patch.object(ToolRuntime, "execute", execute), turn_context(context.turn):
            with self.assertRaises(TurnCancelled):
                runner.run(AgentTask("orchestrator", "task"), context, messages=history,
                           checkpoint=lambda: checkpoints.append(copy.deepcopy(history)))

        trace = self.agent_records("orchestrator")[0][1]
        self.assertEqual(trace["status"], "cancelled")
        results = self.results(trace["messages"])
        self.assertEqual(set(results), {"done", "cancelled", "never-started"})
        self.assertFalse(results["done"]["is_error"])
        self.assertTrue(results["cancelled"]["is_error"])
        self.assertTrue(results["never-started"]["is_error"])
        self.assertTrue(any(len(message.get("content", [])) == 3 for snapshot in checkpoints
                            for message in snapshot if message["role"] == "assistant"))

    def test_only_workspace_root_agents_file_is_injected(self):
        (self.root / "AGENTS.md").write_text("ROOT_RULE: keep existing comments", encoding="utf-8")
        package = self.root / "package"
        package.mkdir()
        (package / "AGENTS.md").write_text("NESTED_RULE: must not be auto-loaded", encoding="utf-8")
        runner, context, model = self.runner([text_response("已阅读根目录约定。")], role="codebase")
        with turn_context(context.turn):
            runner.run(AgentTask("codebase", "查看 package", ["package"]), context)

        system = model.calls[0]["system"]
        self.assertIn("ROOT_RULE", system)
        self.assertNotIn("NESTED_RULE", system)
        self.assertIn("cannot grant new permissions", system)

    def test_agents_symlink_outside_workspace_is_rejected_before_model_call(self):
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / "AGENTS.md"
            target.write_text("outside instructions", encoding="utf-8")
            (self.root / "AGENTS.md").symlink_to(target)
            runner, context, model = self.runner([text_response("must not run")])
            with turn_context(context.turn):
                with self.assertRaisesRegex(ValueError, "AGENTS.md"):
                    runner.run(AgentTask("orchestrator", "task"), context)

        self.assertEqual(model.calls, [])
        self.assertEqual(self.agent_records("orchestrator")[0][1]["status"], "failed")

    def test_goal_continue_feedback_reenters_root_without_changing_permissions(self):
        evaluator = Mock()
        evaluator.evaluate.side_effect = [GoalEvaluation("continue", "需要补充检查证据"),
                                          GoalEvaluation("complete", "证据齐全")]
        app, model = self.app([text_response("初步结果。"), text_response("补充结果及证据。")], evaluator)

        app.submit("/goal 解释实现并补充证据")

        self.assertEqual(evaluator.evaluate.call_count, 2)
        self.assertEqual(len(model.calls), 2)
        self.assertIn("Goal gate feedback", json.dumps(model.calls[1]["messages"]))
        self.assertEqual(app.goal.active.status, "complete")
        self.assertEqual(app.plan.mode, "execute")
        self.assertNotIn("write_file", {tool["name"] for tool in model.calls[1]["tools"]})

    def test_failed_shell_result_remains_error_evidence_in_parent(self):
        def parent_summary(**kwargs):
            child = json.loads(self.results(kwargs["messages"])["dispatch-1"]["content"])
            refs = child["evidence_refs"]
            records = [json.loads(Path(ref).read_text(encoding="utf-8")) for ref in refs]
            shell = next(record for record in records if record.get("tool") == "bash")
            self.assertTrue(shell["is_error"])
            self.assertEqual(json.loads(shell["output"])["exit_code"], 7)
            self.assertEqual(child["verification"][0]["exit_code"], 7)
            self.assertEqual(child["verification"][0]["status"], "failed")
            self.assertEqual(child["contract_status"], "validated")
            return text_response("检查失败，不能宣称通过。")

        app, _ = self.app([
            dispatch(), tool_response(tool_call("bash", {"command": "exit 7"}, "failed-check")),
            report_response(summary="检查退出码 7，尚未修复。"), parent_summary,
        ])

        result = app.submit("运行相关检查")

        self.assertIn("失败", result.summary)
        shell = next(record for record in app.last_context.evidence if record.get("command") == "exit 7")
        self.assertEqual(shell["status"], "failed")
        self.assertEqual(shell["exit_code"], 7)

    def test_answered_question_is_saved_in_real_session(self):
        session = SessionStore(self.root).open()
        self.addCleanup(session.close)
        app, _ = self.app([
            tool_response(tool_call("ask_user", {"question": "选择目录？", "options": ["src", "lib"]}, "ask")),
            text_response("已选择 src。"),
        ], session=session)
        app.submit("查找代码")

        app.submit("1")

        persisted = session.store.load(session.id)
        self.assertIsNone(persisted["state"]["pending_question"])
        answer = json.loads(self.results(persisted["messages"])["ask"]["content"])
        self.assertEqual(answer["status"], "answered")
        self.assertEqual(answer["answer"], "src")


if __name__ == "__main__":
    unittest.main()
