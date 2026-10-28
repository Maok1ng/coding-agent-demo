"""Goal 控制器与验收协议；应用编排集成回归见 test_application。"""

import copy
import io
import json
import types
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock

from coding_agent.runtime.goal import GoalController, GoalError, GoalEvaluation, PromptGoalEvaluator, transcript_text

class FakeBlock(types.SimpleNamespace):
    def model_dump(self, **kwargs):
        return vars(self)


def text_response(text):
    return types.SimpleNamespace(content=[FakeBlock(type="text", text=text)])


def tool_call(name, arguments, tool_id="tool-1"):
    return FakeBlock(type="tool_use", id=tool_id, name=name, input=arguments)


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
        payload = json.loads(call["messages"][0]["content"][0]["text"])
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

    def test_pause_retains_goal_progress_but_cancels_pending_question(self):
        controller = GoalController(Mock())
        controller.set_goal("finish login")
        messages = []
        controller.begin_query(messages)
        result = controller.user_input.ask("Which database?", tool_use_id="ask-1")
        messages.append({"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": "ask-1", "content": result,
        }]})
        controller.wait_for_user("missing database preference")
        controller.active.evaluations = 3
        state = controller.active

        controller.pause()

        self.assertIs(controller.active, state)
        self.assertEqual(state.condition, "finish login")
        self.assertEqual(state.evaluations, 3)
        self.assertEqual(state.status, "paused")
        self.assertIsNone(controller.user_input.pending)
        self.assertEqual(json.loads(messages[-1]["content"][0]["content"])["status"], "cancelled")

    def test_paused_goal_never_evaluates_or_runs_automatically(self):
        evaluator = Mock()
        controller = GoalController(evaluator)
        controller.set_goal("finish login")
        messages = []
        controller.begin_query(messages)
        controller.pause()

        self.assertFalse(controller.is_running())
        self.assertIsNone(controller.stop_hook(messages))
        evaluator.evaluate.assert_not_called()
        self.assertEqual(controller.active.status, "paused")

    def test_paused_context_does_not_reintroduce_old_goal(self):
        controller = GoalController(Mock())
        controller.set_goal("finish login")
        controller.pause()

        context = controller.worker_context()

        self.assertTrue(context)
        self.assertNotIn("finish login", context)
        self.assertNotIn("Work toward this goal", context)

    def test_normal_input_does_not_resume_a_paused_goal(self):
        controller = GoalController(Mock())
        controller.set_goal("finish login")
        controller.pause()

        self.assertEqual(controller.resume_from_user([], "解释装饰器"), "解释装饰器")
        self.assertEqual(controller.active.status, "paused")
        self.assertFalse(controller.is_running())

    def test_explicit_resume_reactivates_retained_goal(self):
        controller = GoalController(Mock())
        controller.set_goal("finish login")
        controller.active.evaluations = 3
        controller.pause()

        self.assertEqual(controller.resume(), "finish login")
        self.assertEqual(controller.active.status, "active")
        self.assertEqual(controller.active.evaluations, 3)
        self.assertTrue(controller.is_running())
        self.assertIn("finish login", controller.worker_context())

    def test_explicit_resume_without_a_goal_is_rejected(self):
        controller = GoalController(Mock())
        with self.assertRaises(GoalError):
            controller.resume()

    def test_edit_keeps_progress_and_records_the_previous_goal(self):
        controller = GoalController(Mock())
        controller.set_goal("finish login and registration")
        messages = []
        controller.begin_query(messages)
        controller.user_input.ask("Which database?")
        controller.active.evaluations = 3
        controller.active.last_reason = "registration tests pending"
        controller.active.status = "blocked"
        controller.consecutive_blocks = 2

        self.assertEqual(controller.edit("  finish login only  "), "finish login only")

        self.assertEqual(controller.active.condition, "finish login only")
        self.assertEqual(controller.active.status, "active")
        self.assertEqual(controller.active.evaluations, 3)
        self.assertEqual(controller.active.revision, 2)
        self.assertEqual(controller.active.previous_conditions, ["finish login and registration"])
        self.assertEqual(controller.active.last_reason, "")
        self.assertEqual(controller.consecutive_blocks, 0)
        self.assertIsNone(controller.user_input.pending)

        controller.edit("verify login tests")
        self.assertEqual(controller.active.revision, 3)
        self.assertEqual(controller.active.previous_conditions,
                         ["finish login and registration", "finish login only"])

    def test_invalid_edit_keeps_goal_and_pending_question_unchanged(self):
        controller = GoalController(Mock())
        with self.assertRaises(GoalError):
            controller.edit("new goal")
        self.assertIsNone(controller.active)
        controller.set_goal("finish login")
        controller.user_input.ask("Which database?")
        controller.active.status = "blocked"
        controller.active.last_reason = "missing database preference"
        before = copy.deepcopy(vars(controller.active))
        question = controller.user_input.pending
        for condition in ["", " ", "x" * 4001]:
            with self.subTest(condition=condition[:10]), self.assertRaises(GoalError):
                controller.edit(condition)
            self.assertEqual(vars(controller.active), before)
            self.assertIs(controller.user_input.pending, question)

    def test_completed_goal_is_retained_but_cannot_resume_without_editing(self):
        evaluator = Mock()
        evaluator.evaluate.return_value = GoalEvaluation("complete", "login tests passed")
        controller = GoalController(evaluator)
        controller.set_goal("finish login")
        messages = []
        controller.begin_query(messages)
        with redirect_stdout(io.StringIO()):
            self.assertIsNone(controller.stop_hook(messages))
        self.assertEqual(controller.active.status, "complete")
        self.assertEqual(controller.active.last_reason, "login tests passed")
        self.assertFalse(controller.is_running())
        self.assertIsNone(controller.stop_hook(messages))
        evaluator.evaluate.assert_called_once()
        with self.assertRaises(GoalError):
            controller.resume()
        self.assertEqual(controller.active.status, "complete")
        controller.pause()
        self.assertEqual(controller.active.status, "complete")
        controller.edit("finish login and registration")
        self.assertEqual(controller.active.status, "active")
        self.assertEqual(controller.active.evaluations, 1)

    def test_inactive_goal_preserves_reason_when_an_unrelated_turn_asks_for_input(self):
        for status in ["paused", "blocked", "complete"]:
            with self.subTest(status=status):
                controller = GoalController(Mock())
                controller.set_goal("finish login")
                controller.active.status = status
                controller.active.last_reason = "retained goal reason"
                controller.wait_for_user("Which explanation level?")
                self.assertEqual(controller.active.status, status)
                self.assertEqual(controller.active.last_reason, "retained goal reason")
                self.assertNotIn("finish login", controller.worker_context())
                self.assertFalse(controller.is_running())


if __name__ == "__main__":
    unittest.main()
