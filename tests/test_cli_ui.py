"""提问卡片和输入预校验；不访问模型、真实配置或外部服务。"""

import contextlib
import io
import os
import re
import unittest
from unittest.mock import patch

from coding_agent.cli import TerminalInteraction
from coding_agent.cli_ui import answer_prompt, format_change_preview, format_question, _width
from coding_agent.tools.user_input import UserInput


class QuestionCardTests(unittest.TestCase):
    def test_chinese_long_options_and_combining_characters_fit_terminal(self):
        for columns in (2, 18, 31, 32, 40, 80, 180):
            with self.subTest(columns=columns):
                card = format_question("使用哪个数据库？\n" + "schema_name_" * 12 + " e\u0301",
                                       ["SQLite", "PostgreSQL：" + "支持多人协作" * 10],
                                       columns=columns, color=False)
                self.assertNotIn("\033", card)
                self.assertTrue(all(_width(line) <= min(columns, 88) for line in card.splitlines()), card)
                if columns >= 32:
                    self.assertTrue(all(_width(line) == min(columns, 88)
                                        for line in card.splitlines() if line), card)
                    self.assertIn("[1]  SQLite", card)

    def test_free_text_question_and_control_characters_are_safe(self):
        card = format_question("请输入路径\033[2J\r\x07\u202e", columns=60, color=False)
        self.assertIn("直接输入你的回答。", card)
        self.assertNotIn("输入编号选择", card)
        for char in ("\033", "\r", "\x07", "\u202e"):
            self.assertNotIn(char, card)

    def test_color_does_not_change_alignment(self):
        plain = format_question("选择数据库？", ["SQLite", "PostgreSQL"], columns=60, color=False)
        colored = format_question("选择数据库？", ["SQLite", "PostgreSQL"], columns=60, color=True)
        self.assertIn("\033[", colored)
        self.assertEqual(re.sub(r"\x1b\[[0-9;]*m", "", colored), plain)

    def test_pipe_no_color_and_dumb_terminal_have_no_ansi(self):
        for tty, variables in ((False, {}), (True, {"NO_COLOR": ""}), (True, {"TERM": "dumb"})):
            with self.subTest(tty=tty, variables=variables), \
                    patch.dict(os.environ, variables, clear=True), \
                    patch("coding_agent.cli_ui.sys.stdout.isatty", return_value=tty):
                self.assertNotIn("\033", format_question("选择目录？", ["src", "lib"]))
                self.assertEqual(answer_prompt(), "回答 › ")

    def test_terminal_renders_structured_and_plain_question_events(self):
        terminal = TerminalInteraction()
        for event in ({"type": "question", "text": "legacy text", "question": "选择目录？",
                       "options": ["src", "lib"]},
                      {"type": "question", "text": "请明确是否按当前方案实施。"}):
            with self.subTest(event=event), contextlib.redirect_stdout(io.StringIO()) as output:
                terminal.emit(event)
            rendered = output.getvalue()
            self.assertIn("等待你的回答", rendered)
            self.assertIn(event.get("question", event["text"]), rendered)
            if "options" in event:
                self.assertIn("[2]  lib", rendered)
                self.assertNotIn("legacy text", rendered)


class AnswerValidationTests(unittest.TestCase):
    def test_prevalidation_does_not_consume_question_or_reinterpret_commands(self):
        user_input = UserInput()
        user_input.ask("选择下一步？", ["/execute", "普通回答"])
        pending = user_input.pending
        self.assertEqual(user_input.validate_answer("1"), "/execute")
        self.assertIs(user_input.pending, pending)
        self.assertEqual(user_input.answer([], "1"), "/execute")
        self.assertIsNone(user_input.pending)

    def test_invalid_answers_keep_pending_and_free_text_is_accepted(self):
        user_input = UserInput()
        user_input.ask("选择数据库？", ["SQLite", "PostgreSQL"])
        pending = user_input.pending
        for text in ("", "  ", "0", "9"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                user_input.validate_answer(text)
            self.assertIs(user_input.pending, pending)
        self.assertEqual(user_input.answer([], "使用本地文件"), "使用本地文件")
        self.assertIsNone(user_input.pending)


class ChangePreviewTests(unittest.TestCase):
    def test_preview_is_readable_in_narrow_and_chinese_terminals(self):
        diff = "--- a/代码.py\n+++ b/代码.py\n@@ -1 +1 @@\n-旧内容\n+新内容" + "很长" * 40
        for columns in (2, 18, 31, 32, 40, 80, 180):
            with self.subTest(columns=columns):
                shown = format_change_preview("代码.py", diff, columns=columns, color=False)
                self.assertTrue(all(_width(line) <= min(columns, 88) for line in shown.splitlines()), shown)
                self.assertNotIn("\033", shown)
                if columns >= 32:
                    self.assertIn("修改预览 · 未写入", shown)
                    self.assertIn("确认后才会写入以上修改。", shown)
                    self.assertTrue(all(_width(line) == min(columns, 88)
                                        for line in shown.splitlines() if line), shown)

    def test_diff_colors_only_use_host_styles_and_keep_text_identical(self):
        diff = "--- a/main.py\n+++ b/main.py\n@@ -1 +1 @@\n-old\n+new\n context"
        plain = format_change_preview("main.py", diff, columns=70, color=False)
        colored = format_change_preview("main.py", diff, columns=70, color=True)
        self.assertIn("\033[31m-old", colored)
        self.assertIn("\033[32m+new", colored)
        self.assertIn("\033[36m--- a/main.py", colored)
        self.assertEqual(re.sub(r"\x1b\[[0-9;]*m", "", colored), plain)

    def test_untrusted_paths_diffs_and_references_cannot_control_terminal(self):
        control = "\033[2J\r\x07\u202e"
        shown = format_change_preview("main.py" + control, "+new" + control,
                                      truncated=True, reference="/tmp/diff.json" + control,
                                      color=False)
        self.assertIn("完整 diff 记录：/tmp/diff.json", shown)
        for char in ("\033", "\r", "\x07", "\u202e"):
            self.assertNotIn(char, shown)

    def test_both_char_and_line_truncation_show_full_record(self):
        for diff, truncated in (("+partial", True), ("\n".join(f"+line {i}" for i in range(130)), False)):
            with self.subTest(truncated=truncated):
                shown = format_change_preview("main.py", diff, truncated=truncated,
                                              reference="/tmp/full-diff.json", color=False)
                self.assertIn("预览已截断", shown)
                self.assertIn("/tmp/full-diff.json", shown)
                self.assertNotIn("+line 120", shown)

    def test_auto_and_full_preview_explain_no_confirmation_is_required(self):
        shown = format_change_preview("main.py", "+new", approval_action="allow", color=False)
        self.assertIn("将按当前审批设置写入以上修改。", shown)
        self.assertNotIn("确认后才会", shown)
        self.assertNotIn("规则审批", shown)

    def test_preview_respects_pipe_and_no_color_preferences(self):
        for tty, variables in ((False, {}), (True, {"NO_COLOR": ""}), (True, {"TERM": "dumb"})):
            with self.subTest(tty=tty, variables=variables), \
                    patch.dict(os.environ, variables, clear=True), \
                    patch("coding_agent.cli_ui.sys.stdout.isatty", return_value=tty):
                self.assertNotIn("\033", format_change_preview("main.py", "-old\n+new"))

    def test_terminal_displays_candidate_before_confirming_without_duplicate_diff(self):
        terminal = TerminalInteraction()
        with contextlib.redirect_stdout(io.StringIO()) as output, \
                patch("coding_agent.runtime.terminal.read_user_input", return_value="y"):
            terminal.emit({"type": "text_delta", "request_id": "request", "role": "implementation",
                           "text": "准备修改"})
            terminal.emit({"type": "change_preview", "tool": "edit_file", "path": "main.py",
                           "diff": "-old\n+new", "approval_action": "confirm"})
            accepted = terminal.confirm("edit_file", {"path": "main.py", "old_text": "old",
                                                       "new_text": "new"}, "需要确认")
        shown = output.getvalue()
        self.assertTrue(accepted)
        self.assertIn("准备修改\n", shown)
        self.assertLess(shown.index("修改预览"), shown.index("操作确认"))
        self.assertEqual(shown.count("-old"), 1)
        self.assertEqual(shown.count("+new"), 1)
        self.assertIn("仅批准本次候选修改", shown)


if __name__ == "__main__":
    unittest.main()
