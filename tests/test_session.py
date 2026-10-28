"""会话快照、无网络 CLI、工具中断和问答恢复回归测试。"""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from coding_agent.runtime.session import SessionError, SessionStore, plain_value, repair_interrupted_tools

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def call(tool_id, name="bash"):
    return {"type": "tool_use", "id": tool_id, "name": name, "input": {"command": "test"}}


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.store = SessionStore(self.root)

    def open_session(self, session_id=None):
        session = self.store.open(session_id)
        self.addCleanup(session.close)
        return session

    def test_round_trip_and_workspace_isolation(self):
        session = self.open_session()
        messages = [{"role": "user", "content": "请保留中文"}]
        session.set_title("请保留中文")
        session.save(messages, {"todos": []})
        session.close()
        restored = self.open_session(session.id)
        self.assertEqual(restored.data["messages"], messages)
        self.assertEqual(restored.data["title"], "请保留中文")
        self.assertEqual(self.store.list_sessions()[0]["id"], session.id)
        other = self.root / "other"
        other.mkdir()
        self.assertEqual(SessionStore(other).list_sessions(), [])
        self.assertEqual(session.path.stat().st_mode & 0o777, 0o600)

    def test_real_sdk_blocks_remain_protocol_objects(self):
        from anthropic.types import TextBlock, ToolUseBlock

        blocks = [TextBlock(type="text", text="检查文件"),
                  ToolUseBlock(type="tool_use", id="tool-1", name="read_file", input={"path": "a.py"})]
        saved = plain_value(blocks)
        self.assertIsInstance(saved[1], dict)
        self.assertEqual(saved[1]["input"], {"path": "a.py"})
        session = self.open_session()
        session.save([{"role": "assistant", "content": blocks}], {})
        self.assertEqual(self.store.load(session.id)["messages"][0]["content"], saved)

    def test_unsupported_objects_never_silently_become_strings(self):
        with self.assertRaises(SessionError):
            plain_value(object())

    def test_empty_start_does_not_create_history(self):
        self.open_session().save([], {})
        self.assertEqual(self.store.list_sessions(), [])

    def test_same_session_has_exclusive_process_lock(self):
        session = self.open_session()
        session.save([{"role": "user", "content": "one"}], {})
        with self.assertRaisesRegex(SessionError, "另一个进程"):
            self.store.open(session.id)
        session.close()
        self.open_session(session.id)

    def test_failed_replace_preserves_previous_snapshot(self):
        session = self.open_session()
        session.save([{"role": "user", "content": "old"}], {})
        with patch("coding_agent.runtime.session.os.replace", side_effect=OSError("disk failure")):
            with self.assertRaises(SessionError):
                session.save([{"role": "user", "content": "new"}], {})
        self.assertEqual(self.store.load(session.id)["messages"][0]["content"], "old")
        self.assertEqual(list(self.store.root.glob("*.tmp")), [])

    def test_invalid_files_and_cross_workspace_snapshot_are_not_selected(self):
        session = self.open_session()
        session.save([{"role": "user", "content": "one"}], {})
        original = session.data["workspace"]
        session.data["workspace"] = "/another/project"
        #模拟被移动/篡改的快照，不允许它跨工作区恢复。
        session.save(session.data["messages"], {})
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self.store.list_sessions(), [])
        with self.assertRaises(SessionError):
            self.store.load(session.id)
        session.data["workspace"] = original
        for invalid in ("../config", "../../secret.json", "not-a-uuid"):
            with self.assertRaises(SessionError):
                self.store.load(invalid)

    def test_store_rejects_symlink_escape(self):
        other = self.root / "outside"
        other.mkdir()
        self.store.root.symlink_to(other, target_is_directory=True)
        with self.assertRaises(SessionError):
            self.store.open()

    def test_resume_repairs_only_missing_tail_results(self):
        session = self.open_session()
        messages = [{"role": "assistant", "content": [call("done"), call("unknown")]},
                    {"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": "done", "content": "exit_code=0"}]}]
        session.save(messages, {})
        session.close()
        restored = self.open_session(session.id)
        results = restored.data["messages"][-1]["content"]
        self.assertEqual(results[0]["content"], "exit_code=0")
        self.assertEqual(results[1]["tool_use_id"], "unknown")
        self.assertTrue(results[1]["is_error"])
        self.assertIn("outcome is unknown", results[1]["content"])
        self.assertEqual(len(repair_interrupted_tools(restored.data["messages"])[-1]["content"]), 2)

    def test_interrupt_before_any_result_does_not_replay(self):
        messages = repair_interrupted_tools([{"role": "assistant", "content": [call("tool-1")]}])
        self.assertEqual(messages[-1]["content"][0]["tool_use_id"], "tool-1")
        self.assertIn("NOT been automatically replayed", messages[-1]["content"][0]["content"])

    def test_corrupt_middle_or_orphan_result_is_rejected(self):
        invalid = [
            [{"role": "assistant", "content": [call("a")]}, {"role": "user", "content": "unpaired"}],
            [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a"}]}],
            [{"role": "assistant", "content": [call("a"), call("a")]}],
            [{"role": "assistant", "content": [call("a")]},
             {"role": "user", "content": [{"type": "tool_result", "tool_use_id": []}]}],
        ]
        for messages in invalid:
            with self.subTest(messages=messages), self.assertRaises(SessionError):
                repair_interrupted_tools(messages)

    def test_picker_and_id_prefix(self):
        session = self.open_session()
        session.save([{"role": "user", "content": "hello"}], {})
        with patch("builtins.input", return_value="1"), redirect_stdout(io.StringIO()):
            self.assertEqual(self.store.select(), session.id)
        self.assertEqual(self.store.select(session.id[:12]), session.id)
        with patch("builtins.input", return_value=""), redirect_stdout(io.StringIO()):
            self.assertIsNone(self.store.select())

    def test_history_cli_needs_no_model_credentials(self):
        session = self.open_session()
        session.set_title("历史会话")
        session.save([{"role": "user", "content": "hello"}], {})
        result = subprocess.run(
            [sys.executable, "-m", "coding_agent", "--workspace", str(self.root), "--list-sessions"],
            cwd=PROJECT_ROOT, env={**os.environ, "AGENT_CONFIG": "/nonexistent-config.json"},
            text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(session.id, result.stdout)
        self.assertIn("历史会话", result.stdout)

    def test_resume_cli_restores_without_model_call_and_continue_uses_same_id(self):
        session = self.open_session()
        session.save([{"role": "user", "content": "old question"},
                      {"role": "assistant", "content": [{"type": "text", "text": "old answer"}]}], {})
        session.close()
        config = self.root / "fake-model.json"
        config.write_text(json.dumps({"api_key": "fake", "base_url": "https://example.invalid",
                                      "model": "worker", "max_tokens": 100}), encoding="utf-8")
        for args, text in [(["-r"], "1\nexit\n"), (["-r", session.id[:10]], "exit\n"), (["-c"], "exit\n")]:
            with self.subTest(args=args):
                result = subprocess.run(
                    [sys.executable, "-m", "coding_agent", "--workspace", str(self.root),
                     "--config", str(config), "--execution", "local", *args],
                    cwd=PROJECT_ROOT, input=text, text=True, capture_output=True, timeout=15,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"Session: {session.id} (resumed)", result.stdout)
                self.assertIn("old answer", result.stdout)
        self.assertEqual(len(self.store.list_sessions()), 1)

    def test_resume_cli_pauses_old_goal_without_calling_model(self):
        session = self.open_session()
        session.save([{"role": "user", "content": "finish login"}], {
            "goal": {"condition": "finish login", "status": "active", "evaluations": 2, "last_reason": "tests pending"},
        })
        session.close()
        config = self.root / "fake-model.json"
        config.write_text(json.dumps({"api_key": "fake", "base_url": "https://example.invalid",
                                      "model": "worker", "max_tokens": 100}), encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-m", "coding_agent", "--workspace", str(self.root),
             "--config", str(config), "--execution", "local", "-c"],
            cwd=PROJECT_ROOT, input="exit\n", text=True, capture_output=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Goal paused: finish login", result.stdout)
        self.assertIn("/goal resume", result.stdout)
        self.assertEqual(self.store.load(session.id)["state"]["goal"]["status"], "paused")


if __name__ == "__main__":
    unittest.main()
