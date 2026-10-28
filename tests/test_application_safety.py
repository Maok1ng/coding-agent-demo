"""应用层安全回归：审批不能串任务，异常不能破坏历史，验收必须看到真实执行事实。"""

import copy
import json
import shlex
import sys
import unittest
from unittest.mock import Mock, patch

from coding_agent.runtime import background as background_runtime, environment, hooks
from coding_agent.runtime.goal import GoalEvaluation, plain_content
from coding_agent.runtime.session import SessionStore, repair_interrupted_tools
from coding_agent.runtime.turn import TurnCancelled, active_turn, current_turn
from coding_agent.tools.files import FileTools
from tests import test_application as helpers


class ApplicationSafetyTests(unittest.TestCase):
    setUp = helpers.ApplicationTests.setUp
    application = helpers.ApplicationTests.application

    @staticmethod
    def command(code):
        return shlex.join([sys.executable, "-c", code])

    def test_plain_answer_after_plan_does_not_become_a_new_approval_prompt(self):
        for confirmation in ("好的", "可以"):
            with self.subTest(confirmation=confirmation):
                app, model = self.application([
                    helpers.text_response("方案：先定位 app.py，再做最小修改。"),
                    helpers.text_response("装饰器是包装已有函数的可调用对象。"),
                    helpers.text_response("不应因为用户肯定普通解释而进入执行。"),
                ])
                app.submit("/plan 修改 app.py")
                app.submit("解释一下 Python 装饰器")
                calls_before_confirmation = len(model.calls)

                result = app.submit(confirmation)

                self.assertIsNone(result)
                self.assertEqual(app.plan.mode, "plan")
                self.assertIsNone(app.plan.approved_event)
                self.assertEqual(len(model.calls), calls_before_confirmation)
                self.assertEqual(self.io.events[-1]["type"], "question")

    def test_explicit_pause_clears_pending_goal_execution_before_execute(self):
        app, model = self.application([
            helpers.text_response("原任务方案。"),
            helpers.text_response("当前只执行已授权任务，不恢复暂停目标。"),
        ])
        app.goal.set_goal("旧长期目标")
        app.goal.pause()
        app.submit("/plan 原任务")
        app.submit("/goal edit 修改后的长期目标")
        self.assertTrue(app.pending_goal_execution)

        app.submit("/goal pause")
        self.assertFalse(app.pending_goal_execution)
        app.submit("/execute")

        self.assertEqual(app.plan.mode, "execute")
        self.assertEqual(app.goal.active.status, "paused")
        self.assertEqual(len(model.calls), 2)
        self.evaluator.evaluate.assert_not_called()

    def test_new_topic_clears_old_goal_execution_before_confirming_new_plan(self):
        app, model = self.application([
            helpers.text_response("原任务方案。"),
            helpers.text_response("新方案：只为 app.py 增加类型标注，不继续旧目标。"),
            helpers.text_response("按当前类型标注方案执行，旧目标保持暂停。"),
        ])
        app.goal.set_goal("旧长期目标")
        app.goal.pause()
        app.submit("/plan 原任务")
        app.submit("/goal edit 修改后的长期目标")
        self.assertTrue(app.pending_goal_execution)

        app.submit("换个任务，为 app.py 设计类型标注改造方案")
        self.assertFalse(app.pending_goal_execution)
        app.submit("按这个方案开始实施")

        self.assertEqual(app.plan.mode, "execute")
        self.assertEqual(app.goal.active.condition, "修改后的长期目标")
        self.assertEqual(app.goal.active.status, "paused")
        self.assertEqual(len(model.calls), 3)
        self.evaluator.evaluate.assert_not_called()

    def test_io_failure_after_tool_result_repairs_history_without_replaying_tool(self):
        (self.root / "app.py").write_text("value = 1\n", encoding="utf-8")
        session = SessionStore(self.root).open()
        self.addCleanup(session.close)
        app, model = self.application([
            helpers.tool_response(helpers.tool_call("read_file", {"path": "app.py"}, "read-once")),
            helpers.text_response("这是新的普通回答，没有重放旧工具。"),
        ], session=session)
        emitted = self.io.emit
        failed = False
        reads = []
        read = FileTools.read

        def fail_once(event):
            nonlocal failed
            if event["type"] == "tool_result" and not failed:
                failed = True
                raise RuntimeError("test UI notification failure")
            emitted(event)

        def counted_read(tools, *args, **kwargs):
            reads.append((args, kwargs))
            return read(tools, *args, **kwargs)

        with patch.object(self.io, "emit", side_effect=fail_once), \
                patch.object(FileTools, "read", counted_read):
            with self.assertRaises(RuntimeError):
                app.submit("读取 app.py")
            #下一条用户消息进入前就应补齐尾部结果，不能把未配对调用留在历史中间。
            self.assertEqual(app.messages, repair_interrupted_tools(copy.deepcopy(app.messages)))
            result = app.submit("换个问题，解释装饰器")

        self.assertEqual(result.status, "completed")
        self.assertEqual(len(reads), 1)
        self.assertEqual(len(model.calls), 2)
        saved = session.store.load(session.id)["messages"]
        self.assertEqual(saved, repair_interrupted_tools(copy.deepcopy(saved)))
        self.assertEqual(model.calls[-1]["messages"], repair_interrupted_tools(
            copy.deepcopy(model.calls[-1]["messages"])))

    def verification_script(self, command, *, background=False):
        responses = [helpers.dispatch(), helpers.tool_response(helpers.tool_call("bash", {
            "command": command, "run_in_background": background,
        }, "verification-command"))]
        if background:
            def wait_for_process(**kwargs):
                result = json.loads(kwargs["messages"][-1]["content"][0]["content"])
                return helpers.tool_response(helpers.tool_call("wait_process", {
                    "task_id": result["task_id"], "timeout": 2,
                }, "wait-for-verification"))
            responses.append(wait_for_process)
        #故意让子角色和第一条主回复谎称通过，宿主仍必须注入真实证据后再验收。
        responses.extend([
            helpers.report_response(summary="子角色声称：检查全部通过。"),
            helpers.text_response("主 Agent 第一条回复也声称全部通过。"),
            helpers.text_response("读过宿主记录，检查实际失败或超时，不能声称通过。"),
        ])
        return responses

    def test_goal_judge_receives_real_foreground_and_background_failure_timeout_facts(self):
        environment.get_environment().config.timeout = 0.15
        for background in (False, True):
            for status in ("failed", "timed_out"):
                with self.subTest(background=background, status=status):
                    marker = f"HOST_CHECK_{'BACKGROUND' if background else 'FOREGROUND'}_{status.upper()}"
                    code = f"print({marker!r}, flush=True); "
                    code += "raise SystemExit(7)" if status == "failed" else "import time; time.sleep(2)"
                    snapshots = []
                    app, model = self.application(self.verification_script(self.command(code), background=background))

                    def judge(condition, messages):
                        snapshots.append((copy.deepcopy(messages), len(model.calls)))
                        return GoalEvaluation("blocked", "检查未通过，需要后续处理")

                    self.evaluator.evaluate.reset_mock()
                    self.evaluator.evaluate.side_effect = judge
                    with patch.dict(hooks.HOOKS, {name: [] for name in hooks.HOOKS}):
                        app.submit("/goal 运行检查并根据真实结果完成修改")

                    self.evaluator.evaluate.assert_called_once()
                    self.assertEqual(model.responses, [])
                    self.assertEqual(len(snapshots), 1)
                    messages, call_count = snapshots[0]
                    #验收必须晚于宿主反馈之后的主 Agent 回复，而非直接相信子角色摘要。
                    self.assertEqual(call_count, 6 if background else 5)
                    transcript = "\n".join(plain_content(message["content"]) for message in messages)
                    self.assertIn(marker, transcript)
                    self.assertIn(f'"status": "{status}"', transcript)
                    self.assertIn('"exit_code": 7' if status == "failed" else '"timed_out": true', transcript)
                    self.assertIn("宿主记录", transcript)
                    self.assertNotEqual(app.goal.active.status, "complete")

    def test_cleanup_failed_never_reaches_goal_judge_and_stops_application(self):
        for in_background in (False, True):
            with self.subTest(background=in_background):
                app, _ = self.application(self.verification_script("fake cleanup failure", background=in_background))
                record = {
                    "command": "fake cleanup failure", "status": "cleanup_failed", "exit_code": 0,
                    "cleanup_confirmed": False, "cancelled": False, "timed_out": False,
                    "output": "resource cleanup not confirmed",
                }
                self.evaluator.evaluate.reset_mock()
                target = background_runtime if in_background else environment
                with patch.object(target, "run_bash_evidence", return_value=record), \
                        patch.dict(hooks.HOOKS, {name: [] for name in hooks.HOOKS}):
                    app.submit("/goal 完成修改并验证资源清理")

                self.evaluator.evaluate.assert_not_called()
                self.assertNotEqual(app.goal.active.status, "complete")
                self.assertTrue(app.closed)
                with self.assertRaises(RuntimeError):
                    app.submit("新的普通问题")

    def test_ordinary_task_cleanup_failure_also_stops_application(self):
        for in_background in (False, True):
            with self.subTest(background=in_background):
                app, _ = self.application(self.verification_script("fake cleanup failure", background=in_background))
                record = {
                    "command": "fake cleanup failure", "status": "cleanup_failed", "exit_code": 0,
                    "cleanup_confirmed": False, "cancelled": False, "timed_out": False,
                    "output": "resource cleanup not confirmed",
                }
                target = background_runtime if in_background else environment
                with patch.object(target, "run_bash_evidence", return_value=record):
                    result = app.submit("运行普通检查")
                self.assertIsNone(app.goal.active)
                self.assertTrue(app.closed)
                self.assertEqual(result.status, "blocked")
                self.assertIn("不能宣称成功", result.summary)
                with self.assertRaises(RuntimeError):
                    app.submit("下一轮不得执行")

    def test_async_close_failure_blocks_result_and_saves_failure_before_answer(self):
        session = SessionStore(self.root).open()
        self.addCleanup(session.close)
        runtimes = []

        def response(**kwargs):
            runtime = current_turn().async_runtime()
            runtime.add_close_callback(Mock(side_effect=RuntimeError("private close failure")))
            runtimes.append(runtime)
            return helpers.text_response("模型声称完成。")

        app, _ = self.application([response], session=session)
        with patch.object(app.memory, "extract"):
            result = app.submit("解释本项目")
        self.assertEqual(result.status, "blocked")
        self.assertTrue(app.closed)
        self.assertEqual(app.last_context.turn.cleanup_errors, ["RuntimeError"])
        self.assertTrue(app.last_context.turn.done.is_set())
        self.assertFalse(runtimes[0]._thread.is_alive())
        self.assertTrue(runtimes[0].loop.is_closed())
        saved = session.store.load(session.id)
        self.assertIn("Runtime cleanup failure", saved["messages"][-1]["content"])
        self.assertNotIn("private close failure", json.dumps(saved))
        answers = [event["text"] for event in self.io.events if event["type"] == "answer"]
        self.assertEqual(answers, [result.summary])
        self.assertIn("不能宣称成功", answers[0])

    def test_memory_extraction_reuses_runtime_before_success_is_emitted(self):
        timeline, runtimes = [], []

        def response(**kwargs):
            runtime = current_turn().async_runtime()
            runtime.add_close_callback(lambda loop: timeline.append("closed"))
            runtimes.append(runtime)
            return helpers.text_response("完成。")

        app, _ = self.application([response])

        def extract(*args):
            self.assertIs(current_turn().async_runtime(), runtimes[0])
            timeline.append("memory")

        emit = self.io.emit

        def observed(event):
            if event["type"] == "answer":
                self.assertTrue(runtimes[0].loop.is_closed())
                timeline.append("answer")
            emit(event)

        with patch.object(app.memory, "extract", side_effect=extract), patch.object(self.io, "emit", side_effect=observed):
            result = app.submit("解释本项目")
        self.assertEqual(result.status, "completed")
        self.assertEqual(timeline, ["memory", "closed", "answer"])

    def test_async_close_interrupt_pauses_goal_revokes_plan_and_saves_history(self):
        for interrupt in ("ctrl_c", "escape"):
            with self.subTest(interrupt=interrupt):
                session = SessionStore(self.root).open()
                self.addCleanup(session.close)
                runtimes = []

                def response(**kwargs):
                    turn = current_turn()
                    runtime = turn.async_runtime()

                    def close(loop):
                        self.assertIs(active_turn(), turn)
                        if interrupt == "ctrl_c":
                            raise KeyboardInterrupt
                        turn.cancel("Esc while closing")

                    runtime.add_close_callback(close)
                    runtimes.append(runtime)
                    return helpers.text_response("准备结束。")

                app, _ = self.application([response], session=session)
                app.goal.set_goal("未完成的目标")
                self.evaluator.evaluate.return_value = GoalEvaluation("blocked", "缺少验证")
                app.plan.enter("当前执行任务")
                app.plan.publish("当前方案")
                expected = TurnCancelled if interrupt == "escape" else KeyboardInterrupt
                with patch.object(app.memory, "extract"), self.assertRaises(expected):
                    app.submit("/execute")
                self.assertEqual(app.goal.active.status, "paused")
                self.assertEqual(app.plan.mode, "plan")
                self.assertIsNone(app.plan.approved_event)
                self.assertTrue(app.last_context.turn.done.is_set())
                self.assertIsNone(active_turn())
                self.assertFalse(runtimes[0]._thread.is_alive())
                self.assertTrue(runtimes[0].loop.is_closed())
                saved = session.store.load(session.id)
                self.assertTrue(any("Turn interrupted" in str(message["content"]) for message in saved["messages"]))
                self.assertEqual(saved["state"]["goal"]["status"], "paused")
                self.assertEqual(saved["state"]["plan"]["mode"], "plan")
                self.assertFalse(any(event["type"] == "answer" for event in app.io.events))

    def test_revising_an_approved_plan_requires_new_execution_confirmation(self):
        app, model = self.application([
            helpers.text_response("原方案。"), helpers.text_response("按原方案执行。"),
            helpers.text_response("调整后的方案。"),
        ])
        app.submit("/plan 修改 app.py")
        app.submit("按这个方案开始实施")
        old_approval = app.plan.approved_event
        self.assertEqual(app.plan.mode, "execute")
        app.submit("调整方案，减少修改范围")
        self.assertTrue(old_approval)
        self.assertEqual(app.plan.mode, "plan")
        self.assertIsNone(app.plan.approved_event)
        self.assertEqual(len(model.calls), 3)
        self.assertNotIn("write_file", {tool["name"] for tool in model.calls[-1]["tools"]})


if __name__ == "__main__":
    unittest.main()
