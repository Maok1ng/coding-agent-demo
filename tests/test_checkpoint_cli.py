"""代码恢复的 CLI 展示；不读取真实会话、配置或模型。"""

import contextlib
import io
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from coding_agent.cli import TerminalInteraction, run_cli
from coding_agent.runtime.turn import SessionExit, TurnCancelled
from scripts.check_package import check_members


class CheckpointCLITests(unittest.TestCase):
    def confirm(self, answer="y", *, arguments=None):
        output = io.StringIO()
        with patch("coding_agent.runtime.terminal.read_user_input", return_value=answer), \
                contextlib.redirect_stdout(output):
            accepted = TerminalInteraction().confirm("restore_checkpoint", arguments or {
                "checkpoint": "C1", "paths": ["src/app.py", "tests/test_app.py"],
            }, "将当前受跟踪文件恢复到选定检查点。")
        return accepted, output.getvalue()

    def test_restore_confirmation_shows_target_files_and_scope(self):
        accepted, shown = self.confirm()
        self.assertTrue(accepted)
        for text in ("操作确认：restore_checkpoint", "目标检查点：C1", "src/app.py",
                     "tests/test_app.py", "聊天历史保留", "不会自动继续任务"):
            self.assertIn(text, shown)

    def test_rejected_restore_returns_false(self):
        for answer in ("n", "", "no"):
            with self.subTest(answer=answer):
                accepted, _ = self.confirm(answer)
                self.assertFalse(accepted)

    def test_restore_confirmation_sanitizes_history_and_path_controls(self):
        controls = "\033[2J\r\b\u202e"
        _, shown = self.confirm(arguments={"checkpoint": "C1" + controls,
                                          "paths": ["代码.py" + controls, "src/\nfake.py"]})
        for character in ("\033", "\r", "\b", "\u202e"):
            self.assertNotIn(character, shown)
        self.assertIn("src/ fake.py", shown)

    def test_restore_eof_exits_and_cancel_propagates(self):
        for error in (EOFError, TurnCancelled):
            expected = SessionExit if error is EOFError else TurnCancelled
            with self.subTest(error=error), \
                    patch("coding_agent.runtime.terminal.read_user_input", side_effect=error), \
                    contextlib.redirect_stdout(io.StringIO()), self.assertRaises(expected):
                TerminalInteraction().confirm("restore_checkpoint", {"checkpoint": "C0", "paths": []}, "恢复")

    def test_checkpoint_notice_cannot_control_terminal(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            TerminalInteraction().emit({"type": "notice", "text": "C1 摘要\033[2J\r\u202e"})
        for character in ("\033", "\r", "\u202e"):
            self.assertNotIn(character, output.getvalue())
        self.assertIn("C1 摘要", output.getvalue())

    def test_cli_advertises_commands_without_starting_a_model(self):
        application = SimpleNamespace(
            prepare=Mock(), emit=Mock(), close=Mock(), workspace="/tmp/project",
            finish_inputs=Mock(return_value=([], [])),
            approval_mode="full", session=None, closed=False,
            goal=SimpleNamespace(user_input=SimpleNamespace(pending=None)),
        )
        with patch("coding_agent.runtime.terminal.read_user_input", return_value="exit"):
            run_cli(application)
        notices = "\n".join(call.args[0] for call in application.emit.call_args_list)
        for command in ("/checkpoints", "/undo", "/restore"):
            self.assertIn(command, notices)
        application.close.assert_called_once()

    def test_package_rejects_private_checkpoint_snapshots(self):
        with self.assertRaises(RuntimeError):
            check_members(["project/.task_outputs/checkpoints/session/state.json"])
        check_members(["project/docs/checkpoints.md", "project/coding_agent/runtime/checkpoints.py"])


if __name__ == "__main__":
    unittest.main()
