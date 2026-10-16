"""无模型请求：目录隔离、启动位置、Shell 回收和 Docker 参数边界。"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from coding_agent.runtime import environment, workspace


class WorkspaceTests(unittest.TestCase):
    def test_default_paths_do_not_follow_launch_directory(self):
        with tempfile.TemporaryDirectory() as other_dir:
            env = {key: value for key, value in os.environ.items()
                   if key not in {"AGENT_WORKSPACE", "AGENT_CONFIG"}}
            env["PYTHONPATH"] = str(workspace.APP_DIR)
            result = subprocess.run(
                [sys.executable, "-c", "from coding_agent.runtime import workspace; print(workspace.WORKDIR); print(workspace.CONFIG_PATH)"],
                cwd=other_dir, env=env, text=True, capture_output=True, check=True,
            )
            self.assertEqual(result.stdout.splitlines(), [str(workspace.APP_DIR), str(workspace.APP_DIR / "config.json")])

    def test_cli_uses_target_workspace_and_external_config(self):
        with tempfile.TemporaryDirectory() as other_dir:
            root = Path(other_dir)
            target = root / "project with spaces"
            target.mkdir()
            config = root / "model.json"
            config.write_text(json.dumps({"api_key": "fake", "base_url": "https://example.invalid",
                                          "model": "worker", "max_tokens": 100}), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(workspace.APP_DIR / "main.py"), "--workspace", str(target),
                 "--config", str(config), "--execution", "local"],
                cwd=root, input="exit\n", text=True, capture_output=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"Workspace: {target.resolve()}", result.stdout)
            self.assertIn("Execution: local", result.stdout)

    def test_relative_paths_and_symlinks_cannot_escape_task(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            task = root / "task"
            task.mkdir()
            (task / "outside").symlink_to(root, target_is_directory=True)
            with patch.object(workspace, "WORKDIR", root), workspace.use_workdir(task):
                self.assertEqual(workspace.safe_path("hello.py"), task / "hello.py")
                for path in ("../secret", "outside/secret", str(root / "secret")):
                    with self.subTest(path=path), self.assertRaises(ValueError):
                        workspace.safe_path(path)

    def test_parallel_task_contexts_do_not_change_process_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tasks = [root / "one", root / "two"]
            for task in tasks:
                task.mkdir()
            barrier = threading.Barrier(2)
            original = Path.cwd()

            def resolve(task):
                with workspace.use_workdir(task):
                    barrier.wait(timeout=5)
                    return workspace.safe_path("result.txt")

            with patch.object(workspace, "WORKDIR", root), ThreadPoolExecutor(max_workers=2) as pool:
                self.assertEqual(list(pool.map(resolve, tasks)), [task / "result.txt" for task in tasks])
                self.assertEqual(workspace.current_workdir(), root)
            self.assertEqual(Path.cwd(), original)


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.root_patch = patch.object(workspace, "WORKDIR", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def test_local_execution_uses_chosen_directory_and_preserves_exit_code(self):
        target = self.root / "project with spaces"
        target.mkdir()
        env = environment.LocalEnvironment(environment.ExecutionConfig())
        output, code = env.execute('printf "%s" "$PWD"; exit 7', target)
        self.assertEqual(code, 7)
        self.assertEqual(output, str(target))
        self.assertIn("status 7", environment.format_bash_result(output, code))

    def test_local_timeout_reaps_process_group(self):
        env = environment.LocalEnvironment(environment.ExecutionConfig(timeout=1))
        output, code = env.execute("sleep 30", self.root)
        self.assertIsNone(code)
        self.assertIn("Timeout", output)
        self.assertEqual(environment._shell_processes, set())

    def test_docker_passes_command_as_one_argument_and_cleans_up(self):
        task = self.root / "task with spaces"
        task.mkdir()
        env = environment.DockerEnvironment(environment.ExecutionConfig(backend="docker"))
        command = "printf '%s' '$(should-not-run-on-host)'"
        with patch.object(environment, "WORKDIR", self.root), \
                patch.object(environment, "_containers", set()), \
                patch.object(environment, "_run_process", return_value=("container output", 0)) as run, \
                patch.object(environment, "_remove_container") as cleanup:
            self.assertEqual(env.execute(command, task), ("container output", 0))
        args = run.call_args.args[0]
        self.assertEqual(args[:2], ["docker", "run"])
        self.assertEqual(args[-1], command)
        self.assertFalse(run.call_args.kwargs["shell"])
        self.assertEqual(args[args.index("--workdir") + 1], str(task))
        self.assertEqual(args[args.index("--network") + 1], "none")
        self.assertIn(f"type=bind,source={self.root},target={self.root}", args)
        self.assertIn("--read-only", args)
        self.assertIn("no-new-privileges", args)
        self.assertNotIn("--env-file", args)
        cleanup.assert_called_once_with(args[args.index("--name") + 1])

    def test_docker_rejects_mount_that_contains_model_credentials(self):
        credentials = self.root / "model.json"
        credentials.write_text("{}", encoding="utf-8")
        env = environment.DockerEnvironment(environment.ExecutionConfig(backend="docker"))
        with patch.object(environment, "WORKDIR", self.root), \
                patch.object(environment, "CONFIG_PATH", credentials), self.assertRaises(ValueError):
            env.command_args("pwd", self.root, "test")

    def test_docker_startup_fails_before_accepting_tasks_if_cli_missing(self):
        env = environment.DockerEnvironment(environment.ExecutionConfig(backend="docker"))
        with patch.object(environment, "WORKDIR", self.root), \
                patch.object(environment.shutil, "which", return_value=None), \
                self.assertRaisesRegex(ValueError, "找不到 docker"):
            env.prepare()

    def test_missing_docker_does_not_execute_a_local_shell(self):
        env = environment.DockerEnvironment(environment.ExecutionConfig(backend="docker"))
        with patch.object(environment, "WORKDIR", self.root), \
                patch.object(environment, "_containers", set()), \
                patch.object(environment.subprocess, "Popen", side_effect=FileNotFoundError("docker missing")) as popen, \
                patch.object(environment, "_remove_container"):
            output, code = env.execute("touch must-not-exist", self.root)
        self.assertIsNone(code)
        self.assertIn("docker missing", output)
        popen.assert_called_once()
        self.assertFalse(popen.call_args.kwargs["shell"])
        self.assertFalse((self.root / "must-not-exist").exists())


if __name__ == "__main__":
    unittest.main()
