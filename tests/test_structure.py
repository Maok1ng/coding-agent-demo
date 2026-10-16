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

from coding_agent.agents import subagent, team
from coding_agent.runtime import background, environment, hooks, permissions, workspace
from coding_agent.tools import files


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

    def test_team_and_subagent_reuse_file_tool_implementations(self):
        for name in ("run_bash", "run_read", "run_write", "run_edit", "run_glob"):
            self.assertIs(getattr(team, name), getattr(files, name))
            self.assertIs(getattr(subagent, name), getattr(files, name))
        self.assertIs(background.format_bash_result, environment.format_bash_result)

    def test_background_schema_does_not_change_teammate_schema(self):
        lead = files.base_tools(background=True)
        self.assertIn("run_in_background", lead[0]["input_schema"]["properties"])
        self.assertNotIn("run_in_background", files.BASE_TOOLS[0]["input_schema"]["properties"])
        self.assertIs(team.BASE_TOOLS, files.BASE_TOOLS)
        self.assertEqual(len({tool["name"] for tool in subagent.SUB_TOOLS}), len(subagent.SUB_TOOLS))

    def test_importing_cli_and_subagent_does_not_read_model_credentials(self):
        result = subprocess.run(
            [sys.executable, "-c", "import coding_agent.cli; import coding_agent.agents.subagent; print('ok')"],
            cwd=workspace.APP_DIR,
            env={**os.environ, "AGENT_CONFIG": "/nonexistent-model-config.json"},
            text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "ok")

    def test_noninteractive_permission_check_never_prompts(self):
        block = types.SimpleNamespace(name="bash", input={"command": "rm old-file"})
        with patch("builtins.input", side_effect=AssertionError("must not prompt")):
            self.assertFalse(permissions.check_permission(block, prompt_user=False))
            self.assertIsNotNone(team.check_permission(block, prompt_user=False))

    def test_skipping_duplicate_permission_still_runs_other_hooks(self):
        logger = Mock()
        registry = {"PreToolUse": [hooks.permission_check_hook, logger]}
        with patch.dict(hooks.HOOKS, registry), \
                patch.object(permissions, "check_permission", side_effect=AssertionError("already checked")):
            hooks.trigger_hook("PreToolUse", object(), skip_permission=True)
        logger.assert_called_once()


class SharedFileToolTests(unittest.TestCase):
    def test_file_tools_use_explicit_task_directory_and_unique_edits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            task = root / "task"
            task.mkdir()
            with patch.object(workspace, "WORKDIR", root):
                self.assertTrue(files.run_write("answer.txt", "old old", cwd=task).startswith("Wrote"))
                self.assertTrue(files.run_edit("answer.txt", "old", "new", cwd=task).startswith("Error"))
                self.assertEqual(files.run_read("answer.txt", cwd=task), "old old")
                self.assertTrue(files.run_edit("answer.txt", "old old", "new", cwd=task).startswith("Edited"))
                self.assertEqual(files.run_read("answer.txt", cwd=task), "new")
                self.assertTrue(files.run_edit("answer.txt", "", "new", cwd=task).startswith("Error"))
                self.assertEqual(files.run_glob("*.txt", cwd=task), "answer.txt")
                self.assertTrue(files.run_write("../escape.txt", "no", cwd=task).startswith("Error"))
                self.assertFalse((root / "answer.txt").exists())


if __name__ == "__main__":
    unittest.main()
