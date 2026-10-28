"""工程整理后的导入、公共工具和共享配置回归测试。"""

import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from coding_agent.application import Application
from coding_agent.agents.runner import AgentRunner
from coding_agent.runtime import environment, hooks, workspace
from coding_agent.runtime.approval import approval_decision
from coding_agent.tools import files
from tests.test_application import report_response


class StructureTests(unittest.TestCase):
    def test_program_root_and_sandbox_config_stay_explicit(self):
        self.assertEqual(workspace.APP_DIR, Path(__file__).resolve().parents[1])
        self.assertTrue((workspace.APP_DIR / "config" / "sandbox.json").is_file())
        with patch.dict(os.environ, {"AGENT_EXECUTION": "local"}), \
                patch.object(environment, "get_environment", environment.get_environment.__wrapped__):
            self.assertIsInstance(environment.get_environment(), environment.LocalEnvironment)

    def test_both_cli_help_paths_work_without_model_config(self):
        for entry in ([str(workspace.APP_DIR / "main.py")], ["-m", "coding_agent"]):
            with self.subTest(entry=entry):
                result = subprocess.run(
                    [sys.executable, *entry, "--help"], cwd=workspace.APP_DIR,
                    env={**os.environ, "AGENT_CONFIG": "/nonexistent-model-config.json"},
                    text=True, capture_output=True, timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("--workspace", result.stdout)

    def test_package_cli_uses_selected_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project = root / "project with spaces"
            project.mkdir()
            config = root / "fake-model.json"
            config.write_text(json.dumps({"api_key": "fake", "base_url": "https://example.invalid",
                                          "model": "worker", "max_tokens": 100}), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-m", "coding_agent", "--workspace", str(project),
                 "--config", str(config), "--execution", "local"],
                cwd=workspace.APP_DIR, input="exit\n", text=True, capture_output=True, timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"Workspace: {project}", result.stdout)

    def test_application_and_specialists_share_one_runner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            roles = []

            def model(client, **kwargs):
                role = next(line.removeprefix("Role: ") for line in kwargs["system"].splitlines()
                            if line.startswith("Role: "))
                roles.append(role)
                if roles == ["orchestrator"]:
                    content = [{"type": "tool_use", "id": "delegate", "name": "dispatch_agent", "input": {
                        "role": "codebase", "instruction": "Locate the entry point",
                    }}]
                elif role == "codebase":
                    return report_response("codebase", "codebase result")
                else:
                    content = [{"type": "text", "text": f"{role} result"}]
                return types.SimpleNamespace(content=content)

            io = types.SimpleNamespace(emit=Mock(), confirm=Mock(return_value=True))
            with patch.object(workspace, "WORKDIR", root), \
                    patch("coding_agent.integrations.mcp.get_mcp_config", return_value={}), \
                    patch.dict(hooks.HOOKS, {name: [] for name in hooks.HOOKS}):
                application = Application({"model": "fake"}, root, io,
                                          client=types.SimpleNamespace(close=Mock()),
                                          model_call=model, evaluator=Mock())
                self.assertIsInstance(application.runner, AgentRunner)
                with patch.object(application.runner, "run", wraps=application.runner.run) as run:
                    result = application.submit("Explain the entry point")
                self.assertEqual(result.status, "completed")
                self.assertEqual(roles, ["orchestrator", "codebase", "orchestrator"])
                self.assertEqual([call.args[0].role for call in run.call_args_list], ["orchestrator", "codebase"])
                contexts = [call.args[1] for call in run.call_args_list]
                self.assertIs(contexts[0].turn, contexts[1].turn)
                self.assertIs(contexts[0].versions, contexts[1].versions)
                application.close()

    def test_background_schema_copy_does_not_change_shared_schema(self):
        lead = files.base_tools(background=True)
        self.assertIn("run_in_background", lead[0]["input_schema"]["properties"])
        self.assertNotIn("run_in_background", files.BASE_TOOLS[0]["input_schema"]["properties"])
        names = [tool["name"] for tool in files.BASE_TOOLS]
        self.assertEqual(len(set(names)), len(names))
        self.assertTrue({"read_file", "search_code", "git_status", "git_diff"} <= set(names))

    def test_importing_application_and_runner_has_no_credentials_threads_or_legacy_dependencies(self):
        source = """
import sys
import threading
from unittest.mock import patch
with patch.object(threading.Thread, 'start', side_effect=AssertionError('import started a thread')):
    import coding_agent.cli
    from coding_agent.application import Application
    from coding_agent.agents.runner import AgentRunner
    assert not {
        'coding_agent.agents.loop', 'coding_agent.agents.team',
        'coding_agent.agents.subagent', 'coding_agent.runtime.cron',
    }.intersection(sys.modules)
print('ok')
"""
        result = subprocess.run(
            [sys.executable, "-c", source],
            cwd=workspace.APP_DIR,
            env={**os.environ, "AGENT_CONFIG": "/nonexistent-model-config.json"},
            text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "ok")

    def test_approval_policy_returns_decision_without_reading_terminal(self):
        with patch("coding_agent.runtime.terminal.read_user_input", side_effect=AssertionError("must not prompt")):
            decision = approval_decision("auto", "bash", {"command": "rm old-file"}, workspace.APP_DIR)
        self.assertEqual(decision.action, "confirm")

    def test_hooks_do_not_register_legacy_permission_or_duplicate_confirmation(self):
        self.assertEqual(hooks.HOOKS["PreToolUse"], [hooks.log_hook])
        logger = Mock()
        registry = {"PreToolUse": [logger]}
        with patch.dict(hooks.HOOKS, registry), \
                patch("coding_agent.runtime.terminal.read_user_input", side_effect=AssertionError("must not prompt")):
            hooks.trigger_hook("PreToolUse", object())
        logger.assert_called_once()


class SharedFileToolTests(unittest.TestCase):
    def test_file_tools_use_explicit_task_directory_and_unique_edits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            task = root / "task"
            task.mkdir()
            with patch.object(workspace, "WORKDIR", root):
                tools = files.FileTools(task)
                self.assertTrue(tools.write("answer.txt", "old old").startswith("Wrote"))
                self.assertTrue(tools.edit("answer.txt", "old", "new").startswith("Error"))
                self.assertIn("old old", tools.read("answer.txt"))
                self.assertTrue(tools.edit("answer.txt", "old old", "new").startswith("Edited"))
                self.assertIn("new", tools.read("answer.txt"))
                self.assertTrue(tools.edit("answer.txt", "", "new").startswith("Error"))
                self.assertEqual(tools.glob("*.txt"), "answer.txt")
                self.assertTrue(tools.write("../escape.txt", "no").startswith("Error"))
                self.assertFalse((root / "answer.txt").exists())


if __name__ == "__main__":
    unittest.main()
