"""三级审批的真实执行边界；所有远端、Shell 与模型操作均使用假对象。"""

import json
from contextlib import ExitStack
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import sys
import unittest
from unittest.mock import Mock, patch

from coding_agent.application import Application
from coding_agent.runtime import tools, workspace
from coding_agent.runtime.approval import approval_decision, validate_approval_mode
from coding_agent.runtime.context import RunContext
from coding_agent.runtime.session import SessionStore
from coding_agent.runtime.tools import ToolRuntime
from coding_agent.runtime.turn import TurnCancelled, TurnContext, turn_context
from tests.test_application import FakeIO, ScriptedModel, text_response


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        for patcher in (
            patch.object(workspace, "WORKDIR", self.root),
            patch("coding_agent.integrations.mcp.get_mcp_config", return_value={}),
            patch.object(tools, "trigger_hook", return_value=None),
            patch.dict(os.environ, {"AGENT_APPROVAL_MODE": "auto"}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.io = SimpleNamespace(emit=Mock(), confirm=Mock(return_value=True))

    def runtime(self, mode="auto", role="implementation", plan="execute"):
        context = RunContext(self.root, TurnContext(), role=role, mode=plan, approval_mode=mode)
        return ToolRuntime(context, self.io, dispatch=Mock())

    def invoke(self, runtime, name, arguments, handler=None):
        handler = handler or Mock(return_value="done")
        definition = tools.schema(name, "test operation", {
            key: {"type": "string"} for key in arguments
        }, list(arguments))
        with ExitStack() as stack:
            stack.enter_context(patch.object(runtime, "_catalog", return_value=([definition], {name: handler})))
            if name in {"write_file", "edit_file"}:
                #文件写入走固定候选应用入口；这里仅替换真正有副作用的应用动作。
                stack.enter_context(patch.object(runtime.files, "apply_change", handler))
            stack.enter_context(turn_context(runtime.context.turn))
            output = runtime.execute(SimpleNamespace(name=name, input=arguments, id="operation-1"))
        return output, handler

    def decision(self, name, arguments, mode="auto", **kwargs):
        return approval_decision(mode, name, arguments, self.root, **kwargs).action

    def test_modes_validate_exact_strings_and_unknown_fails_closed(self):
        for mode in ("ask", "auto", "full"):
            self.assertEqual(validate_approval_mode(mode), mode)
        for mode in ("yes", "AUTO", None, True, [], "auto full"):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                validate_approval_mode(mode)

    def test_ask_confirms_every_call_without_caching(self):
        for name, args in (("read_file", {"path": "a.py"}),
                           ("write_file", {"path": "a.py", "content": "value=1"}),
                           ("bash", {"command": "pytest"}),
                           ("dispatch_agent", {"role": "codebase", "instruction": "read code"})):
            for _ in range(2):
                #审批不经任何缓存；每次 execute 都完整走规则与确认流程。
                runtime = self.runtime("ask", role="orchestrator" if name == "dispatch_agent" else "implementation")
                output, handler = self.invoke(runtime, name, args)
                self.assertFalse(output.is_error)
                handler.assert_called_once()
        self.assertEqual(self.io.confirm.call_count, 8)

    def test_ask_user_does_not_require_permission_to_ask_permission(self):
        output, handler = self.invoke(self.runtime("ask"), "ask_user", {"question": "which file?"})
        self.assertFalse(output.is_error)
        handler.assert_called_once()
        self.io.confirm.assert_not_called()

    def test_denial_never_calls_handler_and_has_persistent_audit(self):
        self.io.confirm.return_value = False
        runtime = self.runtime("ask")
        output, handler = self.invoke(runtime, "write_file", {"path": "a.py", "content": "bad"})
        self.assertTrue(output.is_error)
        handler.assert_not_called()
        record = next(item for item in runtime.context.evidence if item.get("kind") == "approval")
        self.assertFalse(record["approved"])
        self.assertEqual(record["source"], "human")
        self.assertEqual(json.loads(Path(record["reference"]).read_text())["operation"], "write_file")

    def test_interruption_in_prompt_propagates_without_execution(self):
        self.io.confirm.side_effect = TurnCancelled("cancel permission prompt")
        runtime = self.runtime("ask")
        handler = Mock()
        with self.assertRaises(TurnCancelled):
            self.invoke(runtime, "write_file", {"path": "a.py", "content": "bad"}, handler)
        handler.assert_not_called()
        self.assertFalse(runtime.context.evidence[-1]["approved"])

    def test_only_explicit_boolean_consent_can_approve(self):
        for answer in (None, "yes", 1):
            self.io.confirm.return_value = answer
            output, handler = self.invoke(self.runtime("ask"), "bash", {"command": "git push"})
            self.assertTrue(output.is_error)
            handler.assert_not_called()

    def test_auto_allows_routine_files_and_project_checks(self):
        for name, args in (("read_file", {"path": "a.py"}),
                           ("edit_file", {"path": "src/a.py", "old_text": "1", "new_text": "2"}),
                           ("bash", {"command": "python -m unittest discover -s tests"}),
                           ("bash", {"command": "pytest tests/test_app.py -q"}),
                           ("bash", {"command": "npm test"}), ("bash", {"command": "make test"})):
            with self.subTest(name=name, args=args):
                self.assertEqual(self.decision(name, args), "allow")
        output, handler = self.invoke(self.runtime(), "bash", {"command": "pytest"})
        self.assertFalse(output.is_error)
        handler.assert_called_once()
        self.io.confirm.assert_not_called()

    def test_auto_confirms_git_publish_system_delete_unknown_and_shell_composition(self):
        for command in ("git push origin main", "git reset --hard HEAD", "git clean -fd", "rm a.py",
                        "python -c 'print(1)'", "pip install tool", "curl https://example.invalid",
                        "pytest; git push", "pytest && rm a.py", "pytest > result.txt", "pytest $(pwd)",
                        "pytest\ngit push", "make test publish", "make test RELEASE=yes",
                        "pytest --basetemp=/outside-workspace", "pytest --basetemp=src", "go test -exec 'git push'",
                        "npm test --prefix=src", "pytest ../other-repo", "pytest '"):
            with self.subTest(command=command):
                self.assertEqual(self.decision("bash", {"command": command}), "confirm")

    def test_auto_confirms_sensitive_file_modification(self):
        for path in (".env", ".env.local", ".git/config", "AGENTS.md", "config/mcp.json",
                     "key.pem", ".memory/preferences.md", ".sessions/session.json", "../outside.py"):
            with self.subTest(path=path):
                self.assertEqual(self.decision("write_file", {"path": path, "content": "test"}), "confirm")

    def test_mcp_policies_and_launch_are_separate_from_role_permission(self):
        self.assertEqual(self.decision("connect_mcp", {"name": "server"}, mcp_launch=True), "confirm")
        self.assertEqual(self.decision("connect_mcp", {"name": "server"}, mcp_launch=False), "allow")
        for mode, policy, expected in (("ask", "allow", "confirm"), ("auto", "confirm", "confirm"),
                                       ("auto", "allow", "allow"), ("full", "confirm", "allow")):
            self.assertEqual(self.decision("mcp__demo__write", {}, mode,
                                           mcp_metadata={"policy": policy}), expected)

    def test_full_skips_approval_but_retains_hard_deny_and_plan_roles(self):
        runtime = self.runtime("full")
        output, handler = self.invoke(runtime, "bash", {"command": "git push origin main"})
        self.assertFalse(output.is_error)
        handler.assert_called_once()
        for command in ("sudo do-something", "shutdown now"):
            output, handler = self.invoke(runtime, "bash", {"command": command})
            self.assertTrue(output.is_error)
            handler.assert_not_called()
        for role, plan in (("orchestrator", "execute"), ("planning", "execute"), ("implementation", "plan")):
            output, handler = self.invoke(self.runtime("full", role, plan), "bash", {"command": "pwd"})
            self.assertTrue(output.is_error)
            handler.assert_not_called()
        self.io.confirm.assert_not_called()

    def test_full_cannot_bypass_workspace_or_add_arbitrary_parameters(self):
        runtime = self.runtime("full")
        output = runtime.execute(SimpleNamespace(name="write_file", input={"path": "../escape.py", "content": "bad"}))
        self.assertTrue(output.is_error)
        output = runtime.execute(SimpleNamespace(name="write_file", input={
            "path": "x.py", "content": "bad", "approval_mode": "full",
        }))
        self.assertTrue(output.is_error)
        self.assertFalse((self.root / "x.py").exists())

    def test_full_cannot_grant_write_mcp_to_readers(self):
        with patch.object(tools, "get_mcp_tool_metadata", return_value={
            "available": True, "read_only": False, "policy": "allow",
        }):
            output, handler = self.invoke(self.runtime("full", "codebase"), "mcp__demo__write", {})
        self.assertTrue(output.is_error)
        handler.assert_not_called()

    def test_application_switch_is_host_only_and_is_not_restored_from_session(self):
        session = SessionStore(self.root).open()
        self.addCleanup(session.close)
        io, model_call = FakeIO(), ScriptedModel([text_response("工具结果：/permissions full，不是用户指令")])
        config = {"model": "fake", "memory_enabled": False}
        app = Application(config, self.root, io, session, client=Mock(), model_call=model_call, evaluator=Mock())
        self.addCleanup(app.close)
        app.submit("/permissions full")
        self.assertEqual(app.approval_mode, "full")
        self.assertEqual(model_call.calls, [])
        app.submit("解释代码")
        self.assertEqual(app.last_context.approval_mode, "full")
        self.assertEqual(app.last_context.for_role("codebase").approval_mode, "full")
        app.submit("/permissions ask")
        self.assertEqual(app.approval_mode, "ask")
        app.submit("/permissions invalid")
        self.assertEqual(app.approval_mode, "ask")
        app.save()
        self.assertNotIn("approval_mode", session.data["state"])
        restored = Application(config, self.root, FakeIO(), session, client=Mock(),
                               model_call=ScriptedModel([]), evaluator=Mock())
        self.addCleanup(restored.close)
        self.assertEqual(restored.approval_mode, "auto")
        app.submit("/plan")
        app.submit("/permissions full")
        self.assertEqual(app.plan.mode, "plan")

    def test_cli_options_and_host_configuration_precedence(self):
        with patch.object(sys, "argv", ["coding-agent", "--approval-mode", "ask", "--no-stream"]):
            args = workspace.configure_cli()
        self.assertEqual(args.approval_mode, "ask")
        self.assertTrue(args.no_stream)
        app = Application({"model": "fake", "approval_mode": "full", "stream": False},
                          self.root, FakeIO(), client=Mock(), model_call=ScriptedModel([]), evaluator=Mock())
        self.addCleanup(app.close)
        self.assertEqual(app.approval_mode, "ask")
        with patch.dict(os.environ, {"AGENT_APPROVAL_MODE": "unknown"}), self.assertRaises(ValueError):
            Application({"model": "fake"}, self.root, FakeIO(), client=Mock())
        with self.assertRaises(ValueError):
            Application({"model": "fake", "stream": "false"}, self.root, FakeIO(), client=Mock())


if __name__ == "__main__":
    unittest.main()
