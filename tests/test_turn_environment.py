"""不请求模型或启动 Docker：验证单轮取消只回收自己拥有的 Shell/容器。"""

from contextlib import redirect_stdout
import io
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from coding_agent.runtime import environment, workspace
from coding_agent.runtime.turn import TurnCancelled, TurnContext, turn_context


class TurnEnvironmentTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.root_patch = patch.object(workspace, "WORKDIR", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.local = environment.LocalEnvironment(environment.ExecutionConfig(timeout=20))

    def wait_for_processes(self, baseline, count):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with environment._shell_process_lock:
                processes = environment._shell_processes - baseline
            if len(processes) >= count:
                return processes
            time.sleep(0.01)
        self.fail(f"等待 Shell 启动超时，预期 {count} 个进程")

    def test_already_cancelled_turn_never_starts_a_process(self):
        turn = TurnContext()
        with turn_context(turn), patch.object(environment.subprocess, "Popen") as popen:
            turn.cancel()
            with self.assertRaises(TurnCancelled):
                self.local.execute("must-not-run", self.root)
        popen.assert_not_called()

    def test_cancel_foreground_reaps_its_group_but_leaves_independent_background(self):
        command = shlex.join([sys.executable, "-c", "import time; time.sleep(20)"])
        turn = TurnContext()
        outcomes = {}
        with environment._shell_process_lock:
            baseline = set(environment._shell_processes)

        def run(name, context=None):
            try:
                if context is None:
                    outcomes[name] = self.local.execute(command, self.root)
                else:
                    with turn_context(context):
                        outcomes[name] = self.local.execute(command, self.root)
            except BaseException as error:
                #TurnCancelled 继承 KeyboardInterrupt，不能用 except Exception 记录它。
                outcomes[name] = error

        background = threading.Thread(target=run, args=("background",), daemon=True)
        foreground = threading.Thread(target=run, args=("foreground", turn), daemon=True)
        background.start()
        try:
            background_process = next(iter(self.wait_for_processes(baseline, 1)))
            foreground.start()
            self.wait_for_processes(baseline, 2)
            turn.cancel()
            foreground.join(timeout=5)
            self.assertFalse(foreground.is_alive())
            self.assertIsInstance(outcomes.get("foreground"), TurnCancelled)
            self.assertIsNone(background_process.poll())
            self.assertTrue(background.is_alive())
        finally:
            turn.cancel()
            with environment._shell_process_lock:
                remaining = list(environment._shell_processes - baseline)
            for process in remaining:
                environment._stop_process_group(process)
            if foreground.ident is not None:
                foreground.join(timeout=5)
            background.join(timeout=5)
        self.assertFalse(background.is_alive())
        with environment._shell_process_lock:
            self.assertEqual(environment._shell_processes - baseline, set())

    def test_cancel_between_popen_and_callback_registration_is_not_lost(self):
        turn = TurnContext()
        real_popen = subprocess.Popen
        started = []

        def start_then_cancel(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            started.append(process)
            turn.cancel()
            return process

        command = shlex.join([sys.executable, "-c", "import time; time.sleep(20)"])
        with turn_context(turn), patch.object(environment.subprocess, "Popen", side_effect=start_then_cancel):
            with self.assertRaises(TurnCancelled):
                self.local.execute(command, self.root)
        self.assertEqual(len(started), 1)
        self.assertIsNotNone(started[0].poll())
        self.assertNotIn(started[0], environment._shell_processes)

    def test_cancel_at_communicate_completion_is_not_reported_as_success(self):
        turn = TurnContext()
        process = Mock()
        process.returncode = 0

        def complete(*args, **kwargs):
            turn.cancel()
            return "completed just before cancellation", None

        process.communicate.side_effect = complete
        with turn_context(turn), patch.object(environment.subprocess, "Popen", return_value=process), \
                patch.object(environment, "_stop_process_group"):
            with self.assertRaises(TurnCancelled):
                self.local.execute("fake command", self.root)
        self.assertNotIn(process, environment._shell_processes)

    def test_docker_cancel_cleans_same_container_again_after_startup_race(self):
        turn = TurnContext()
        docker = environment.DockerEnvironment(environment.ExecutionConfig(backend="docker"))
        running = set()
        removed = []

        def remove(name):
            removed.append(name)
            running.discard(name)
            with environment._shell_process_lock:
                environment._containers.discard(name)

        def run(args, *other, **kwargs):
            name = args[args.index("--name") + 1]
            #第一次 rm 早于容器创建；finally 必须再次按同名清理，不能遗留运行中的容器。
            turn.cancel()
            running.add(name)
            return "late container", 0

        with turn_context(turn), patch.object(docker, "validate"), \
                workspace.use_workspace(self.root), \
                patch.object(environment, "_run_process", side_effect=run) as execute, \
                patch.object(environment, "_remove_container", side_effect=remove):
            with self.assertRaises(TurnCancelled):
                docker.execute("sleep 20", self.root)
        self.assertEqual(running, set())
        self.assertEqual(len(removed), 2)
        self.assertEqual(removed[0], removed[1])
        self.assertFalse(execute.call_args.kwargs["shell"])
        self.assertNotIn(removed[0], environment._containers)

    def test_public_exit_cleanup_delegates_to_global_cleanup(self):
        with patch.object(environment, "_stop_all_shell_processes") as cleanup:
            environment.stop_shell_processes()
        cleanup.assert_called_once_with()

    def test_failed_container_cleanup_keeps_tracking_and_exit_retries(self):
        name = "coding-agent-cleanup-test"
        failed = subprocess.CompletedProcess([], 1, stdout=b"", stderr=b"private credential must not be printed")
        success = subprocess.CompletedProcess([], 0, stdout=name.encode(), stderr=b"")
        output = io.StringIO()
        with patch.object(environment, "_containers", {name}), \
                patch.object(environment, "_shell_processes", set()), \
                patch.object(environment.subprocess, "run", side_effect=[failed, success]) as remove, \
                redirect_stdout(output):
            self.assertFalse(environment._remove_container(name))
            self.assertIn(name, environment._containers)
            environment.stop_shell_processes()
            self.assertNotIn(name, environment._containers)
        self.assertEqual(remove.call_count, 2)
        self.assertIn("cleanup unconfirmed (exit status 1)", output.getvalue())
        self.assertNotIn("private credential", output.getvalue())

    def test_container_cleanup_timeout_or_oserror_does_not_drop_tracking(self):
        name = "coding-agent-cleanup-test"
        errors = [subprocess.TimeoutExpired(["docker", "rm"], 10), OSError("private error")]
        for error in errors:
            with self.subTest(error=type(error).__name__), \
                    patch.object(environment, "_containers", {name}), \
                    patch.object(environment.subprocess, "run", side_effect=error), \
                    redirect_stdout(io.StringIO()) as output:
                self.assertFalse(environment._remove_container(name))
                self.assertIn(name, environment._containers)
                self.assertIn(type(error).__name__, output.getvalue())
                self.assertNotIn("private error", output.getvalue())

    def test_missing_container_is_confirmed_removed_for_bytes_and_text_stderr(self):
        name = "coding-agent-cleanup-test"
        for error_text in (b"Error response from daemon: No such container: cleanup-test", "No such container: cleanup-test"):
            result = subprocess.CompletedProcess([], 1, stdout=b"", stderr=error_text)
            with self.subTest(error_text=error_text), patch.object(environment, "_containers", {name}), \
                    patch.object(environment.subprocess, "run", return_value=result):
                self.assertTrue(environment._remove_container(name))
                self.assertNotIn(name, environment._containers)

    def test_cancelled_docker_turn_preserves_unconfirmed_container_without_masking_cancel(self):
        turn = TurnContext()
        docker = environment.DockerEnvironment(environment.ExecutionConfig(backend="docker"))
        failed = subprocess.CompletedProcess([], 1, stdout=b"", stderr=b"daemon unavailable")
        names = []

        def cancel(args, *other, **kwargs):
            names.append(args[args.index("--name") + 1])
            turn.cancel()
            return "unconfirmed container", 0

        with turn_context(turn), patch.object(docker, "validate"), \
                workspace.use_workspace(self.root), \
                patch.object(environment, "_containers", set()), \
                patch.object(environment, "_run_process", side_effect=cancel), \
                patch.object(environment.subprocess, "run", return_value=failed) as remove, \
                redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(TurnCancelled):
                docker.execute("sleep 20", self.root)
            self.assertIn(names[0], environment._containers)
        self.assertEqual(remove.call_count, 2)
        self.assertIn("cleanup unconfirmed", output.getvalue())


if __name__ == "__main__":
    unittest.main()
