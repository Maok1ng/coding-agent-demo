"""无网络测试：验收协议、主循环续跑、子会话隔离与失败边界。"""

import copy
import importlib.util
import io
import json
import sys
import tempfile
import types
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from coding_agent.runtime.goal import GoalController, GoalError, GoalEvaluation, PromptGoalEvaluator, transcript_text

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def text_response(text):
    return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=text)])


def tool_call(name, arguments, tool_id="tool-1"):
    return types.SimpleNamespace(type="tool_use", id=tool_id, name=name, input=arguments)


def tool_response(*blocks):
    return types.SimpleNamespace(content=list(blocks))


class FakeMessages:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def client_with(responses):
    return types.SimpleNamespace(messages=FakeMessages(responses))


class GoalTests(unittest.TestCase):
    def test_evaluator_is_tool_free_and_preserves_tool_evidence(self):
        client = client_with([text_response('{"decision":"complete","reason":"exit_code=0"}')])
        evaluator = PromptGoalEvaluator(client, "judge")
        history = [{"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": "test-1", "content": "exit_code=0",
        }]}]
        self.assertEqual(evaluator.evaluate("tests pass", history).decision, "complete")
        call = client.messages.calls[0]
        self.assertNotIn("tools", call)
        payload = json.loads(call["messages"][0]["content"])
        self.assertIn("test-1", payload["conversation"])
        self.assertIn("exit_code=0", payload["conversation"])

    def test_invalid_evaluator_output_is_rejected(self):
        for output in ["not json", "[]", '{"decision":"unknown","reason":"done"}',
                       '{"decision":"complete","reason":" "}',
                       '{"decision":"wait_user","reason":"missing"}',
                       '{"decision":[],"reason":"done"}',
                       '{"decision":"wait_user","reason":"missing","question":"db?","options":"bad"}']:
            with self.subTest(output=output), self.assertRaises(GoalError):
                PromptGoalEvaluator(client_with([text_response(output)]), "judge").evaluate("goal", [])

    def test_trimming_keeps_recent_tool_result(self):
        messages = [{"role": "user", "content": "old" * 200},
                    {"role": "assistant", "content": "recent evidence"}]
        transcript = transcript_text(messages, max_chars=100)
        self.assertIn("recent evidence", transcript)
        self.assertNotIn("old", transcript)

    def test_evaluator_wait_user_requires_a_concrete_question(self):
        client = client_with([text_response(json.dumps({
            "decision": "wait_user", "reason": "database preference missing",
            "question": "Which database?", "options": ["SQLite", "PostgreSQL"],
        }))])
        result = PromptGoalEvaluator(client, "judge").evaluate("build app", [])
        self.assertEqual(result.decision, "wait_user")
        self.assertEqual(result.question, "Which database?")
        self.assertEqual(result.options, ("SQLite", "PostgreSQL"))

    def test_foreign_session_and_no_goal_do_not_invoke_evaluator(self):
        evaluator = Mock()
        controller = GoalController(evaluator)
        messages = []
        controller.begin_query(messages)
        self.assertIsNone(controller.stop_hook(messages))
        controller.set_goal("tests pass")
        self.assertIsNone(controller.stop_hook(list(messages)))
        evaluator.evaluate.assert_not_called()

    def test_pending_background_defers_without_evaluation(self):
        evaluator = Mock()
        controller = GoalController(evaluator, background_pending=lambda: True)
        controller.set_goal("tests pass")
        messages = []
        controller.begin_query(messages)
        with redirect_stdout(io.StringIO()):
            self.assertIsNone(controller.stop_hook(messages))
        self.assertIsNotNone(controller.active)
        evaluator.evaluate.assert_not_called()


class GoalIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        #不读取真实凭据，也不需要安装 API SDK；只替换外部初始化。
        fake_anthropic = types.ModuleType("anthropic")
        fake_anthropic.Anthropic = lambda **kwargs: client_with([])
        fake_skills = types.ModuleType("coding_agent.tools.skills")
        fake_skills.SKILL_LOADER = types.SimpleNamespace(catalog=lambda: "", load=lambda name: "")
        real_open = open

        def config_open(path, *args, **kwargs):
            if Path(path).resolve() == PROJECT_ROOT / "config.json":
                return io.StringIO(json.dumps({
                    "api_key": "fake", "base_url": "https://example.invalid",
                    "model": "worker", "max_tokens": 1000,
                }))
            return real_open(path, *args, **kwargs)

        spec = importlib.util.spec_from_file_location("goal_main_under_test", PROJECT_ROOT / "coding_agent" / "agents" / "loop.py")
        cls.main = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"anthropic": fake_anthropic, "coding_agent.tools.skills": fake_skills}), \
                patch("builtins.open", side_effect=config_open):
            spec.loader.exec_module(cls.main)

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.output = io.StringIO()
        self.stack.enter_context(redirect_stdout(self.output))
        self.evaluator = Mock()
        self.goal = GoalController(self.evaluator, block_cap=2)
        self.main = type(self).main
        self.stack.enter_context(patch.object(self.main, "GOAL", self.goal))
        self.stack.enter_context(patch.object(self.main, "session_history", []))
        #仍走真正的 trigger_hook；替换注册表中的旧 controller 为本测试目标。
        hook_registry = self.main.trigger_hook.__globals__["HOOKS"]
        self.stack.enter_context(patch.dict(hook_registry, {"Stop": [self.goal.stop_hook]}))
        self.stack.enter_context(patch.object(self.main, "COMPACTOR", types.SimpleNamespace(prepare=lambda messages, request: messages)))
        self.stack.enter_context(patch.object(self.main, "consume_cron_queue", return_value=[]))
        self.stack.enter_context(patch.object(self.main.team, "consume_lead_inbox", return_value=[]))
        self.inject = self.stack.enter_context(patch.object(self.main, "inject_background_results", return_value=0))

    def use_worker(self, responses):
        client = client_with(responses)
        self.stack.enter_context(patch.object(self.main, "client", client))
        return client

    def test_unmet_goal_feedback_reaches_next_worker_call(self):
        client = self.use_worker([text_response("changed"), text_response("verified")])
        self.evaluator.evaluate.side_effect = [GoalEvaluation("continue", "missing tests"), GoalEvaluation("complete", "tests passed")]
        self.main.submit_query("/goal tests pass")
        self.assertIsNone(self.goal.active)
        self.assertEqual(len(client.messages.calls), 2)
        self.assertIn("missing tests", str(client.messages.calls[1]["messages"]))
        self.assertIn("tests pass", client.messages.calls[1]["system"])
        self.assertEqual(self.main.session_history[0]["content"], "tests pass")

    def test_tool_results_are_seen_by_gate_only_after_worker_stops(self):
        tool = types.SimpleNamespace(type="tool_use", id="test-1", name="bash", input={"command": "test"})
        client = self.use_worker([types.SimpleNamespace(content=[tool]), text_response("verified")])
        self.evaluator.evaluate.return_value = GoalEvaluation("complete", "tests passed")
        self.stack.enter_context(patch.object(self.main, "execute_tool", return_value="exit_code=0"))
        self.main.submit_query("/goal tests pass")
        self.assertEqual(len(client.messages.calls), 2)
        self.evaluator.evaluate.assert_called_once()
        history = self.evaluator.evaluate.call_args.args[1]
        self.assertIn("exit_code=0", str(history))

    def test_normal_query_never_calls_gate_model(self):
        self.use_worker([text_response("answer")])
        self.main.submit_query("explain this")
        self.evaluator.evaluate.assert_not_called()

    def test_subagent_inherits_the_leads_assignment_directory(self):
        from coding_agent.runtime import workspace
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            task_dir = root / "assigned-task"
            task_dir.mkdir()
            sub_client = client_with([
                tool_response(tool_call("write_file", {"path": "result.txt", "content": "done"})),
                text_response("written"),
            ])
            with patch.object(workspace, "WORKDIR", root), \
                    patch.object(self.main.team, "assignment_cwd", return_value=task_dir), \
                    patch.dict(self.main.run_subagent.__globals__, {"client": sub_client}):
                self.main.TOOL_HANDLERS["task"]("write the result")
            self.assertEqual((task_dir / "result.txt").read_text(), "done")
            self.assertFalse((root / "result.txt").exists())
            self.assertIn(str(task_dir), sub_client.messages.calls[0]["system"])

    def test_completed_lead_task_does_not_block_next_assignment(self):
        from coding_agent.runtime import workspace
        team = self.main.team
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory).resolve()
            tasks = root / ".tasks"
            for module, name, value in (
                (workspace, "WORKDIR", root), (team, "WORKDIR", root),
                (team, "TASKS_DIR", tasks), (team, "TASKS_ROOT", tasks),
                (team, "TASK_LOCK_PATH", tasks / ".lock"),
                (team, "teammate_assignments", {}), (team, "assignment_versions", {}),
            ):
                stack.enter_context(patch.object(module, name, value))
            first = team.create_task("first")
            second = team.create_task("second")
            team.claim_task(first.id)
            self.use_worker([
                tool_response(tool_call("complete_task", {"task_id": first.id})),
                text_response("completed"),
            ])
            self.main.submit_query("finish the first task")
            self.assertTrue(team.claim_task(second.id).startswith("Claimed "))

    def test_status_and_clear_commands_do_not_call_worker(self):
        client = self.use_worker([])
        self.goal.set_goal("tests pass")
        self.main.submit_query("/goal")
        self.assertIsNotNone(self.goal.active)
        self.main.submit_query("/goal clear")
        self.assertIsNone(self.goal.active)
        self.assertEqual(client.messages.calls, [])
        self.assertEqual(self.main.session_history, [])

    def test_block_limit_returns_without_claiming_completion(self):
        client = self.use_worker([text_response("pending")] * 3)
        self.evaluator.evaluate.return_value = GoalEvaluation("continue", "missing tests")
        self.main.submit_query("/goal tests pass")
        self.assertEqual(len(client.messages.calls), 3)
        self.assertIsNotNone(self.goal.active)
        self.assertIn("limit", self.goal.last_status)

    def test_evaluator_error_keeps_goal_active(self):
        self.use_worker([text_response("candidate")])
        self.evaluator.evaluate.side_effect = GoalError("invalid JSON")
        self.main.submit_query("/goal tests pass")
        self.assertIsNotNone(self.goal.active)
        self.assertIn("error", self.goal.last_status)

    def test_blocked_goal_is_retained_until_user_resumes(self):
        self.use_worker([text_response("cannot proceed")])
        self.evaluator.evaluate.return_value = GoalEvaluation("blocked", "resource unavailable")
        self.main.submit_query("/goal tests pass")
        self.assertIsNotNone(self.goal.active)
        self.assertEqual(self.goal.active.status, "blocked")
        self.assertFalse(self.goal.can_run_automatically())
        self.use_worker([text_response("verified")])
        self.evaluator.evaluate.return_value = GoalEvaluation("complete", "resource restored")
        self.main.submit_query("资源已恢复，请继续")
        self.assertIsNone(self.goal.active)

    def test_worker_turn_limit_also_covers_repeated_tool_calls(self):
        tool = types.SimpleNamespace(type="tool_use", id="t1", name="bash", input={"command": "test"})
        client = self.use_worker([types.SimpleNamespace(content=[tool])])
        self.stack.enter_context(patch.object(self.main, "GOAL_MAX_TURNS", 1))
        self.stack.enter_context(patch.object(self.main, "execute_tool", return_value="still pending"))
        self.main.submit_query("/goal tests pass")
        self.assertEqual(len(client.messages.calls), 1)
        self.assertIsNotNone(self.goal.active)
        self.evaluator.evaluate.assert_not_called()

    def test_new_background_evidence_goes_to_worker_before_gate(self):
        client = self.use_worker([text_response("waiting"), text_response("verified")])
        count = 0

        def inject_once(messages):
            nonlocal count
            count += 1
            if count == 2:
                messages.append({"role": "user", "content": "[Background completed] exit_code=0"})
                return 1
            return 0

        self.inject.side_effect = inject_once
        self.evaluator.evaluate.return_value = GoalEvaluation("complete", "verified")
        self.main.submit_query("/goal tests pass")
        self.evaluator.evaluate.assert_called_once()
        self.assertIsNone(self.goal.active)
        self.assertIn("exit_code=0", str(client.messages.calls[1]["messages"]))

    def test_ask_user_waits_without_goal_and_delivers_real_answer(self):
        client = self.use_worker([
            tool_response(tool_call("ask_user", {"question": "Which database?",
                          "options": ["SQLite", "PostgreSQL"]}, "ask-1")),
            text_response("Using PostgreSQL"),
        ])
        self.main.submit_query("build app")
        self.assertEqual(len(client.messages.calls), 1)
        self.assertIn("Which database?", self.output.getvalue())
        self.assertIn("2. PostgreSQL", self.output.getvalue())
        self.assertIsNotNone(self.goal.user_input.pending)
        self.main.submit_query("2")
        self.assertIsNone(self.goal.user_input.pending)
        history = client.messages.calls[1]["messages"]
        result = json.loads(history[-2]["content"][0]["content"])
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["answer"], "PostgreSQL")
        self.assertEqual(history[-1]["content"], "PostgreSQL")
        self.evaluator.evaluate.assert_not_called()

    def test_ask_user_bypasses_goal_gate_then_resumes_goal(self):
        client = self.use_worker([
            tool_response(tool_call("ask_user", {"question": "Which database?"})),
            text_response("verified"),
        ])
        self.main.submit_query("/goal build app")
        self.assertEqual(self.goal.active.status, "waiting_user")
        self.evaluator.evaluate.assert_not_called()
        self.assertFalse(self.goal.can_run_automatically())
        self.evaluator.evaluate.return_value = GoalEvaluation("complete", "verified")
        self.main.submit_query("PostgreSQL")
        self.assertIsNone(self.goal.active)
        self.assertEqual(len(client.messages.calls), 2)
        self.evaluator.evaluate.assert_called_once()

    def test_entire_tool_batch_is_deferred_when_it_contains_ask_user(self):
        client = self.use_worker([tool_response(
            tool_call("write_file", {"path": "db.py", "content": "bad default"}, "write-1"),
            tool_call("ask_user", {"question": "Which database?"}, "ask-1"),
            tool_call("bash", {"command": "initialize db"}, "bash-1"),
        )])
        execute = self.stack.enter_context(patch.object(self.main, "execute_tool"))
        self.main.submit_query("/goal build app")
        execute.assert_not_called()
        results = self.main.session_history[-1]["content"]
        self.assertEqual([result["tool_use_id"] for result in results], ["write-1", "ask-1", "bash-1"])
        self.assertEqual([result["is_error"] for result in results], [True, False, True])
        self.assertEqual(json.loads(results[1]["content"])["status"], "waiting_user")
        self.assertEqual(len(client.messages.calls), 1)

    def test_skipped_operation_executes_only_when_reissued_after_answer(self):
        self.use_worker([
            tool_response(tool_call("ask_user", {"question": "Which database?"}, "ask-1"),
                          tool_call("write_file", {"path": "db.py", "content": "default"}, "skip-1")),
            tool_response(tool_call("write_file", {"path": "db.py", "content": "PostgreSQL"}, "write-2")),
            text_response("written"),
        ])
        execute = self.stack.enter_context(patch.object(self.main, "execute_tool", return_value="written"))
        self.main.submit_query("build app")
        execute.assert_not_called()
        self.main.submit_query("PostgreSQL")
        execute.assert_called_once()
        block = execute.call_args.args[0]
        self.assertEqual(block.id, "write-2")
        self.assertEqual(block.input["content"], "PostgreSQL")

    def test_gate_wait_user_displays_question_and_resumes_after_answer(self):
        client = self.use_worker([text_response("I need your preference"), text_response("verified")])
        self.evaluator.evaluate.side_effect = [
            GoalEvaluation("wait_user", "missing database preference", "Which database?", ("SQLite", "PostgreSQL")),
            GoalEvaluation("complete", "verified"),
        ]
        self.main.submit_query("/goal build app")
        self.assertEqual(len(client.messages.calls), 1)
        self.assertEqual(self.goal.active.status, "waiting_user")
        self.assertIn("Which database?", self.output.getvalue())
        self.main.submit_query("2")
        self.assertEqual(client.messages.calls[1]["messages"][-1]["content"], "PostgreSQL")
        self.assertIn("Which database?", str(client.messages.calls[1]["messages"]))
        self.assertIsNone(self.goal.active)

    def test_automatic_events_cannot_answer_pending_question(self):
        client = self.use_worker([])
        self.goal.set_goal("build app")
        self.goal.user_input.ask("Which database?")
        self.goal.wait_for_user("missing preference")
        self.main.agent_loop(self.main.session_history)
        self.assertEqual(client.messages.calls, [])
        self.main.consume_cron_queue.assert_not_called()
        self.main.team.consume_lead_inbox.assert_not_called()
        self.inject.assert_not_called()
        self.assertIsNotNone(self.goal.user_input.pending)

    def test_queue_processor_preserves_events_while_waiting(self):
        client = self.use_worker([])
        self.goal.user_input.ask("Which database?")
        self.stack.enter_context(patch.object(self.main, "has_cron_queue", return_value=True))
        self.stack.enter_context(patch.object(self.main.RUNTIME_STOP, "wait", side_effect=[False, True]))
        self.main.queue_processor_loop()
        self.main.consume_cron_queue.assert_not_called()
        self.assertEqual(client.messages.calls, [])
        self.assertTrue(self.main.agent_lock.acquire(blocking=False))
        self.main.agent_lock.release()

    def test_status_and_invalid_option_leave_question_pending(self):
        client = self.use_worker([tool_response(tool_call("ask_user", {
            "question": "Which database?", "options": ["SQLite", "PostgreSQL"],
        }))])
        self.main.submit_query("/goal build app")
        self.main.submit_query("/goal")
        self.main.submit_query("9")
        self.main.submit_query(" ")
        self.assertEqual(len(client.messages.calls), 1)
        self.assertIsNotNone(self.goal.user_input.pending)
        self.assertEqual(self.goal.active.status, "waiting_user")

    def test_clear_cancels_pending_tool_question_without_calling_worker(self):
        client = self.use_worker([tool_response(tool_call("ask_user", {"question": "Which database?"}))])
        self.main.submit_query("/goal build app")
        self.main.submit_query("/goal clear")
        self.assertIsNone(self.goal.active)
        self.assertIsNone(self.goal.user_input.pending)
        result = json.loads(self.main.session_history[-1]["content"][0]["content"])
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(len(client.messages.calls), 1)

    def test_replacing_goal_cancels_old_question(self):
        client = self.use_worker([
            tool_response(tool_call("ask_user", {"question": "Which database?"})),
            text_response("new task done"),
        ])
        self.main.submit_query("/goal build app")
        self.evaluator.evaluate.return_value = GoalEvaluation("complete", "new task verified")
        self.main.submit_query("/goal document the existing app")
        self.assertIsNone(self.goal.user_input.pending)
        history = client.messages.calls[1]["messages"]
        self.assertEqual(json.loads(history[-2]["content"][0]["content"])["status"], "cancelled")
        self.assertEqual(history[-1]["content"], "document the existing app")

    def test_invalid_ask_arguments_return_tool_error_and_allow_retry(self):
        client = self.use_worker([
            tool_response(tool_call("ask_user", {"question": " "}, "bad-ask")),
            tool_response(tool_call("ask_user", {"question": "Which database?"}, "good-ask")),
        ])
        self.main.submit_query("build app")
        self.assertTrue(client.messages.calls[1]["messages"][-1]["content"][0]["is_error"])
        self.assertEqual(self.goal.user_input.pending.tool_use_id, "good-ask")

    def test_multiple_ask_calls_are_rejected_with_paired_results(self):
        client = self.use_worker([
            tool_response(tool_call("ask_user", {"question": "db?"}, "ask-1"),
                          tool_call("ask_user", {"question": "region?"}, "ask-2")),
            tool_response(tool_call("ask_user", {"question": "Which database?"}, "ask-3")),
        ])
        self.main.submit_query("build app")
        results = client.messages.calls[1]["messages"][-1]["content"]
        self.assertEqual([result["tool_use_id"] for result in results], ["ask-1", "ask-2"])
        self.assertTrue(all(result["is_error"] for result in results))
        self.assertEqual(self.goal.user_input.pending.tool_use_id, "ask-3")


if __name__ == "__main__":
    unittest.main()
