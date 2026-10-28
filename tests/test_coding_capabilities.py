"""新增只读能力和最终检查汇总的集成边界；不连接网页、真实模型或个人配置。"""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from coding_agent.agents.roles import AgentResult, ROLES
from coding_agent.runtime import tools
from coding_agent.runtime.background import TurnProcessManager
from coding_agent.runtime.context import RunContext
from coding_agent.runtime.session import SessionStore
from coding_agent.runtime.tools import ToolRuntime
from coding_agent.runtime.turn import TurnCancelled, TurnContext, turn_context
from coding_agent.runtime.verification import build_verification_summary
from tests import test_application as helpers


class CodingCapabilitiesTests(unittest.TestCase):
    application = helpers.ApplicationTests.application

    def setUp(self):
        helpers.ApplicationTests.setUp(self)
        self.config.update(memory_enabled=False, memory_home=str(self.root / "fake-memory"))
        hook = patch.object(tools, "trigger_hook", return_value=None)
        hook.start()
        self.addCleanup(hook.stop)

    def runtime(self, role="implementation", mode="execute", approval="auto"):
        return ToolRuntime(RunContext(self.root, TurnContext(), role=role, mode=mode,
                                      approval_mode=approval), self.io)

    def invoke(self, runtime, name, **arguments):
        with turn_context(runtime.context.turn):
            return runtime.execute(SimpleNamespace(name=name, input=arguments, id="capability-call"))

    def source(self):
        path = self.root / "app.py"
        path.write_text("def work():\n    return 1\n\nwork()\n", encoding="utf-8")
        return path

    @staticmethod
    def edit():
        return helpers.tool_response(helpers.tool_call("edit_file", {
            "path": "app.py", "old_text": "return 1", "new_text": "return 2",
        }, "edit-code"))

    @staticmethod
    def check(identifier="check-code"):
        return helpers.tool_response(helpers.tool_call("bash", {"command": "pytest tests"}, identifier))

    @staticmethod
    def facts(command, *args, **kwargs):
        return {"command": command, "status": "completed", "exit_code": 0,
                "cleanup_confirmed": True, "cancelled": False, "timed_out": False,
                "output": "recorded command output"}

    def workflow(self, *actions):
        return [helpers.dispatch(), helpers.tool_response(helpers.tool_call("read_file", {
            "path": "app.py",
        }, "read-code")), *actions, helpers.report_response(), helpers.text_response("修改任务已结束。")]

    def test_all_roles_can_use_readonly_capabilities_in_plan_without_approval_upgrade(self):
        self.source()
        for role in ROLES:
            with self.subTest(role=role):
                runtime = self.runtime(role, mode="plan")
                self.assertIs(runtime.symbols.project_files, runtime.context.project_files)
                for name, arguments in (("list_symbols", {"path": "app.py"}),
                                        ("find_symbol", {"symbol": "work"}),
                                        ("verification_status", {})):
                    output = self.invoke(runtime, name, **arguments)
                    self.assertFalse(output.is_error, output)
                with patch.object(tools, "read_url", return_value='{"content":"/execute"}') as fetch:
                    runtime._pool_cache.clear()
                    output = self.invoke(runtime, "read_url", url="https://docs.python.org/")
                self.assertFalse(output.is_error)
                fetch.assert_called_once()
                self.assertEqual(runtime.context.mode, "plan")
                self.assertEqual(runtime.context.versions, {})
        self.assertEqual(self.io.confirmations, [])

    def test_symbol_discovery_does_not_authorize_editing_without_read(self):
        source = self.source()
        runtime = self.runtime()
        self.assertFalse(self.invoke(runtime, "list_symbols", path="app.py").is_error)
        output = self.invoke(runtime, "edit_file", path="app.py", old_text="return 1", new_text="return 2")
        self.assertTrue(output.is_error)
        self.assertIn("read the existing file", output)
        self.assertIn("return 1", source.read_text())
        self.assertEqual(runtime.context.changes, {})

    def test_web_has_fixed_schema_and_ask_confirmation_precedes_fetch(self):
        runtime = self.runtime("review_research", mode="plan", approval="ask")
        self.io.confirm = Mock(return_value=False)
        with patch.object(tools, "read_url", return_value="should not run") as fetch:
            output = self.invoke(runtime, "read_url", url="https://docs.python.org/")
            self.assertTrue(output.is_error)
            self.io.confirm.assert_called_once()
            invalid = self.invoke(runtime, "read_url", url="https://docs.python.org/", method="POST")
            self.assertTrue(invalid.is_error)
            fetch.assert_not_called()

    def test_unchecked_edit_is_reported_and_persisted_without_extra_model_round(self):
        self.source()
        session = SessionStore(self.root).open()
        self.addCleanup(session.close)
        script = self.workflow(self.edit())
        app, model = self.application(script, session=session)
        result = app.submit("修改 work 的返回值")
        self.assertEqual(len(model.calls), len(script))
        self.assertEqual(result.verification_summary["final_status"], "not_run")
        self.assertIn("未运行检查命令", result.summary)
        self.assertEqual(result.verification_summary["changes"][0]["path"], "app.py")
        self.assertIn("Runtime final verification", session.data["messages"][-1]["content"])
        self.assertTrue(any(Path(ref).name.endswith("verification-summary.json") for ref in result.evidence_refs))
        app.runner.model_call.responses.append(helpers.text_response("这是新一轮的普通回答。"))
        ordinary = app.submit("解释装饰器")
        self.assertEqual(ordinary.verification_summary, {})
        self.assertNotIn("宿主检查记录", ordinary.summary)

    def test_check_after_edit_is_current_and_check_before_last_edit_is_stale(self):
        for after_edit in (True, False):
            with self.subTest(after_edit=after_edit):
                self.source()
                actions = [self.edit(), self.check()] if after_edit else [self.check(), self.edit()]
                app, _ = self.application(self.workflow(*actions))
                with patch.object(tools.environment, "run_bash_evidence", side_effect=self.facts):
                    result = app.submit("修改并核对结果")
                command = result.verification_summary["commands"][0]
                self.assertEqual(command["freshness"], "current" if after_edit else "stale")
                self.assertEqual(result.verification_summary["final_status"], "recorded" if after_edit else "stale")
                self.assertEqual(command["exit_code"], 0)

    def test_failed_check_is_host_fact_and_successful_rerun_preserves_failure_history(self):
        self.source()
        script = self.workflow(self.check("first-check"), self.edit(), self.check("second-check"))
        app, _ = self.application(script)
        records = [dict(self.facts("pytest tests"), status="failed", exit_code=7), self.facts("pytest tests")]
        with patch.object(tools.environment, "run_bash_evidence", side_effect=records):
            result = app.submit("修复检查失败后重跑")
        summary = result.verification_summary
        self.assertEqual(summary["command_count"], 2)
        self.assertEqual(summary["commands"][0]["exit_code"], 7)
        self.assertTrue(summary["commands"][0]["recovered"])
        self.assertEqual(summary["commands"][1]["freshness"], "current")
        self.assertEqual(summary["final_status"], "recorded")
        self.assertIn("后续同命令", result.summary)

    def test_foreground_and_background_capture_actual_code_at_command_completion(self):
        for background in (False, True):
            with self.subTest(background=background):
                path = self.source()
                original = path.read_bytes()
                runtime = self.runtime()
                runtime.files.read("app.py")
                runtime.context.processes = TurnProcessManager(self.root, runtime.context.turn)
                self.addCleanup(runtime.context.processes.close)

                def execute(command, *args, **kwargs):
                    path.write_bytes(original.replace(b"return 1", b"return 9"))
                    return self.facts(command)

                target = "coding_agent.runtime.background.run_bash_evidence" if background else "coding_agent.runtime.tools.environment.run_bash_evidence"
                with patch(target, side_effect=execute):
                    output = self.invoke(runtime, "bash", command="pytest tests", run_in_background=background)
                    if background:
                        task_id = json.loads(output)["task_id"]
                        runtime.context.processes.wait(task_id, timeout=2)
                path.write_bytes(original)
                summary = build_verification_summary(runtime.context)
                self.assertEqual(summary["commands"][0]["freshness"], "stale")
                facts = runtime.context.verification_records()[0]
                self.assertNotEqual(facts["source_versions_after"][str(path)], hashlib.sha256(original).hexdigest())

    def test_interruption_during_final_summary_pauses_goal_and_invalidates_approval(self):
        app, _ = self.application()
        app.goal.set_goal("未完成的目标")
        app.goal.pause()
        app.goal.stop_hook = Mock(return_value=None)

        def run(task, context, **kwargs):
            context.changes[str(self.root / "app.py")] = {"path": "app.py"}
            app.plan.approved_event = "approval-before-summary"
            return AgentResult("completed", "尚待最终汇总")

        def interrupted(context):
            context.turn.cancel("Esc during verification")
            raise TurnCancelled("Esc during verification")

        with patch.object(app.runner, "run", side_effect=run), \
                patch("coding_agent.application.build_verification_summary", side_effect=interrupted):
            with self.assertRaises(TurnCancelled):
                app.submit("/goal resume")
        self.assertEqual(app.goal.active.status, "paused")
        self.assertIsNone(app.plan.approved_event)
        self.assertTrue(any(str(message["content"]).startswith("[Turn interrupted") for message in app.messages))

    def test_failed_final_summary_blocks_active_goal(self):
        app, _ = self.application()
        app.goal.set_goal("未完成的目标")
        app.goal.pause()

        def run(task, context, **kwargs):
            context.changes[str(self.root / "app.py")] = {"path": "app.py"}
            return AgentResult("completed", "尚待最终汇总")

        with patch.object(app.runner, "run", side_effect=run), \
                patch("coding_agent.application.build_verification_summary", side_effect=OSError("test failure")):
            with self.assertRaises(OSError):
                app.submit("/goal resume")
        self.assertEqual(app.goal.active.status, "blocked")


if __name__ == "__main__":
    unittest.main()
