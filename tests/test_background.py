"""不请求模型或 Docker，验证命令证据与 Turn 所属进程的生命周期。"""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import shlex
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from coding_agent.runtime import background, environment, workspace
from coding_agent.runtime.background import TurnProcessManager
from coding_agent.runtime.turn import TurnCancelled, TurnContext


class TurnProcessTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.root_patch = patch.object(workspace, "WORKDIR", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.local = environment.LocalEnvironment(environment.ExecutionConfig(timeout=20))
        self.env_patch = patch.object(environment, "get_environment", return_value=self.local)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.turn = TurnContext()
        self.manager = TurnProcessManager(self.root, self.turn)
        self.addCleanup(self.manager.close)

    @staticmethod
    def command(code):
        return shlex.join([sys.executable, "-c", code])

    def wait_for_shell(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with environment._shell_process_lock:
                if environment._shell_processes:
                    return
            time.sleep(0.01)
        self.fail("等待 Shell 启动超时")

    def test_wait_saves_full_output_and_preserves_exit_code(self):
        task_id = self.manager.start(self.command("print('a' * 80000)"))
        result = json.loads(self.manager.wait(task_id))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["exit_code"], 0)
        self.assertFalse(result["wait_timed_out"])
        self.assertEqual(result["turn_id"], self.turn.id)
        self.assertEqual(len(Path(result["full_output_path"]).read_text()), 80001)
        self.assertEqual(len(result["output"]), 50000)
        self.assertEqual(self.manager.close()[0]["status"], "completed")

    def test_nonzero_command_is_failed_not_completed(self):
        task_id = self.manager.start(self.command("print('check failed'); raise SystemExit(7)"))
        result = json.loads(self.manager.wait(task_id))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["exit_code"], 7)
        self.assertIn("check failed", result["output"])
        self.assertFalse(result["cancelled"])

    def test_wait_timeout_does_not_claim_command_timeout_or_success(self):
        task_id = self.manager.start(self.command("import time; time.sleep(20)"))
        result = json.loads(self.manager.wait(task_id, timeout=0))
        self.assertTrue(result["wait_timed_out"])
        self.assertEqual(result["status"], "running")
        self.assertIsNone(result["exit_code"])
        self.assertFalse(result["timed_out"])

    def test_command_timeout_has_distinct_identity_and_partial_output(self):
        self.local.config.timeout = 0.1
        task_id = self.manager.start(self.command("import time; print('started', flush=True); time.sleep(20)"))
        result = json.loads(self.manager.wait(task_id))
        self.assertEqual(result["status"], "timed_out")
        self.assertTrue(result["timed_out"])
        self.assertFalse(result["cancelled"])
        self.assertIsNone(result["exit_code"])
        self.assertIn("started", Path(result["full_output_path"]).read_text())

    def test_parent_cancel_reaches_explicitly_bound_worker_context(self):
        task_id = self.manager.start(self.command("import time; time.sleep(20)"))
        self.wait_for_shell()
        self.turn.cancel()
        results = self.manager.close()
        self.assertEqual(results[0]["id"], task_id)
        self.assertEqual(results[0]["status"], "cancelled")
        self.assertTrue(results[0]["cancelled"])
        self.assertTrue(results[0]["cleanup_confirmed"])
        self.assertIsNone(results[0]["exit_code"])
        self.assertEqual(environment._shell_processes, set())

    def test_close_stops_only_remaining_tasks_without_cancelling_parent(self):
        completed_id = self.manager.start(self.command("print('done')"))
        self.manager.wait(completed_id)
        pending_id = self.manager.start(self.command("import time; time.sleep(20)"))
        self.wait_for_shell()
        records = {item["id"]: item for item in self.manager.close()}
        self.assertEqual(records[completed_id]["status"], "completed")
        self.assertEqual(records[pending_id]["status"], "cancelled")
        self.assertFalse(self.turn.cancelled.is_set())
        self.assertEqual(environment._shell_processes, set())
        with self.assertRaisesRegex(RuntimeError, "closed"):
            self.manager.start("must not run")

    def test_cancellation_does_not_cross_turns_or_share_ids(self):
        second = TurnProcessManager(self.root, TurnContext())
        self.addCleanup(second.close)
        first_id = self.manager.start(self.command("import time; time.sleep(20)"))
        second_id = second.start(self.command("import time; time.sleep(20)"))
        self.turn.cancel()
        self.manager.close()
        self.assertEqual(json.loads(self.manager.status(first_id))["status"], "cancelled")
        self.assertEqual(json.loads(second.status(second_id))["status"], "running")
        self.assertFalse(second.turn.cancelled.is_set())
        self.assertNotEqual(first_id, second_id)
        with self.assertRaisesRegex(ValueError, "Unknown process ID"):
            second.status(first_id)

    def test_cancelled_turn_cannot_start_new_process(self):
        self.turn.cancel()
        with patch.object(environment.subprocess, "Popen") as popen, self.assertRaises(TurnCancelled):
            self.manager.start("must not run")
        popen.assert_not_called()

    def test_records_are_copies_and_invalid_wait_timeout_is_rejected(self):
        task_id = self.manager.start(self.command("print('done')"))
        self.manager.wait(task_id)
        self.manager.records[0]["status"] = "changed"
        self.assertEqual(json.loads(self.manager.status(task_id))["status"], "completed")
        for timeout in (-1, 61, "30"):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                self.manager.wait(task_id, timeout=timeout)

    def test_unconfirmed_cleanup_is_never_reported_as_success(self):
        with patch.object(environment, "_shell_processes", set()), \
                patch.object(environment, "_stop_process_group", return_value=False), \
                redirect_stdout(io.StringIO()) as output:
            task_id = self.manager.start(self.command("print('done')"))
            result = json.loads(self.manager.wait(task_id))
            self.assertEqual(result["status"], "cleanup_failed")
            self.assertFalse(result["cleanup_confirmed"])
            self.assertIn("cleanup unconfirmed", output.getvalue())

    def test_foreground_evidence_also_retains_output_and_timeout_identity(self):
        self.local.config.timeout = 0.1
        result = environment.run_bash_evidence(self.command("import time; print('checking', flush=True); time.sleep(20)"), self.root)
        self.assertEqual(result["status"], "timed_out")
        self.assertIsNone(result["exit_code"])
        self.assertTrue(Path(result["full_output_path"]).is_file())
        self.assertIn("checking", Path(result["full_output_path"]).read_text())

    def test_close_is_bounded_and_unfinished_thread_is_reported_unconfirmed(self):
        release = threading.Event()
        started = threading.Event()

        def stalled(*args):
            started.set()
            release.wait(timeout=5)

        with patch.object(self.manager, "_run", side_effect=stalled), \
                patch.object(background, "CLEANUP_TIMEOUT", 0.05), \
                redirect_stdout(io.StringIO()) as output:
            task_id = self.manager.start("fake stalled worker")
            self.assertTrue(started.wait(timeout=1))
            try:
                began = time.monotonic()
                record = self.manager.close()[0]
                self.assertLess(time.monotonic() - began, 1)
                self.assertEqual(record["status"], "cleanup_failed")
                self.assertFalse(record["cleanup_confirmed"])
                self.assertIn("BackgroundThreadCleanupUnconfirmed", self.turn.cleanup_errors)
                self.assertIn("cleanup unconfirmed", output.getvalue())
                self.assertFalse(self.turn.cancelled.is_set())
            finally:
                release.set()
                self.manager._threads[task_id].join(timeout=1)

    def test_output_directory_symlink_cannot_redirect_host_writes(self):
        with tempfile.TemporaryDirectory() as outside:
            (self.root / ".task_outputs").symlink_to(outside, target_is_directory=True)
            with patch.object(environment.subprocess, "Popen") as popen:
                result = environment.run_bash_evidence("must not run", self.root)
            self.assertEqual(result["status"], "failed")
            popen.assert_not_called()
            self.assertEqual(list(Path(outside).iterdir()), [])

    def test_existing_output_file_or_link_is_never_overwritten(self):
        original = self.root / "original.txt"
        original.write_text("user data")
        link = self.root / "output.log"
        link.symlink_to(original)
        for output_path in (original, link):
            with self.subTest(path=output_path), patch.object(environment.subprocess, "Popen") as popen:
                result = environment.run_bash_evidence("must not run", self.root, output_path=output_path)
                self.assertEqual(result["status"], "failed")
                popen.assert_not_called()
                self.assertEqual(original.read_text(), "user data")


if __name__ == "__main__":
    unittest.main()
