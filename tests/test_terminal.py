"""真实伪终端回归：不访问模型网络，不读取本机配置，不启动后台命令。"""

import contextlib
import fcntl
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import termios
import textwrap
import time
import unittest
from unittest.mock import patch

from coding_agent.runtime import terminal
from coding_agent.cli import TerminalInteraction
from coding_agent.runtime.turn import SessionExit


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class TerminalTests(unittest.TestCase):
    def test_permission_eof_requests_session_exit_not_tool_failure(self):
        with patch.object(terminal, "read_user_input", side_effect=EOFError), \
                patch("builtins.print"):
            with self.assertRaises(SessionExit):
                TerminalInteraction().confirm("bash", {}, "Needs confirmation")

    def test_double_escape_is_still_cancellation(self):
        reading, writing = os.pipe()
        try:
            os.write(writing, b"\x1b\x1b")
            self.assertEqual(terminal._read_key(reading), b"\x1b")
        finally:
            os.close(reading)
            os.close(writing)

    def test_monitor_assembles_lines_and_reports_through_callback(self):
        reading, writing = os.pipe()
        lines = []
        monitor = terminal.InterruptMonitor(on_line=lines.append)

        @contextlib.contextmanager
        def keyboard():
            yield reading

        try:
            with patch.object(terminal, "_keyboard", keyboard), \
                    patch.object(terminal, "_interactive", return_value=True), \
                    patch("builtins.print"):
                monitor.start()
                os.write(writing, "ab\x7fc".encode() + "你好".encode() + b"\n")
                deadline = time.monotonic() + 5
                while not lines and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertEqual(lines, ["ac你好"])
        finally:
            monitor.stop()
            os.close(reading)
            os.close(writing)

    def test_monitor_without_callback_keeps_legacy_esc_only_behavior(self):
        reading, writing = os.pipe()
        monitor = terminal.InterruptMonitor()

        @contextlib.contextmanager
        def keyboard():
            yield reading

        try:
            with patch.object(terminal, "_keyboard", keyboard), \
                    patch.object(terminal, "_interactive", return_value=True), \
                    patch("builtins.print"):
                monitor.start()
                os.write(writing, b"ignored\n")
                deadline = time.monotonic() + 0.5
                while time.monotonic() < deadline and not monitor.stop_event.is_set():
                    time.sleep(0.02)
                self.assertFalse(monitor.interrupted.is_set())
        finally:
            monitor.stop()
            os.close(reading)
            os.close(writing)

    def test_start_interrupted_restores_keyboard_and_global_monitor(self):
        events = []

        @contextlib.contextmanager
        def keyboard():
            events.append("entered")
            try:
                yield 0
            finally:
                events.append("restored")

        with patch.object(terminal, "_interactive", return_value=True), \
                patch.object(terminal, "_keyboard", keyboard), \
                patch.object(terminal.threading.Thread, "start", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                with terminal.monitor_interrupts():
                    self.fail("Interrupted initialization must not enter its body")
        self.assertEqual(events, ["entered", "restored"])
        self.assertIsNone(terminal._monitor)

    @contextlib.contextmanager
    def process(self, source):
        master, slave = pty.openpty()

        def controlling_terminal():
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        child = subprocess.Popen(
            [sys.executable, "-u", "-c", textwrap.dedent(source)],
            stdin=slave, stdout=slave, stderr=slave, cwd=PROJECT_ROOT,
            start_new_session=True, preexec_fn=controlling_terminal,
        )
        os.close(slave)
        self.terminal_output = b""

        def expect(text):
            target = text.encode()
            deadline = time.monotonic() + 10
            while target not in self.terminal_output and time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    self.terminal_output += chunk
            self.assertIn(target, self.terminal_output, self.terminal_output.decode(errors="replace"))
            before, self.terminal_output = self.terminal_output.split(target, 1)
            return before

        try:
            yield child, lambda value: os.write(master, value), expect
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
            os.close(master)

    def test_unicode_arrow_keys_and_terminal_mode_restoration(self):
        source = '''
            import termios
            from coding_agent.runtime.terminal import read_user_input
            before = termios.tcgetattr(0)
            print("RESULT=" + read_user_input("INPUT>"), flush=True)
            after = termios.tcgetattr(0)
            #macOS 恢复 canonical 模式时，内核可能自行设置待重放输入的 PENDIN 标记。
            before[3] &= ~getattr(termios, "PENDIN", 0)
            after[3] &= ~getattr(termios, "PENDIN", 0)
            print("RESTORED=" + str(before == after), flush=True)
        '''
        with self.process(source) as (child, send, expect):
            expect("INPUT>")
            send("你好".encode() + b"\x1b[A!\x7f?\n")
            expect("RESULT=你好?")
            expect("RESTORED=True")
            self.assertEqual(child.wait(timeout=5), 0)

    def test_cli_question_card_invalid_answer_free_text_and_escape(self):
        #真实终端走完整提问链路；只替换模型与宿主配置，校验错误不能消耗模型轮次。
        source = '''
            import tempfile
            from contextlib import ExitStack
            from pathlib import Path
            from types import SimpleNamespace
            from unittest.mock import Mock, patch
            from coding_agent.application import Application
            from coding_agent.cli import TerminalInteraction, run_cli
            from coding_agent.runtime import hooks
            calls = []
            def model(client, **kwargs):
                calls.append(kwargs)
                if len(calls) in {1, 3, 5}:
                    arguments = {"question": "选择数据库？", "options": ["SQLite", "PostgreSQL"]}
                    if len(calls) == 3:
                        arguments = {"question": "还有哪些要求？"}
                    content = [{"type": "tool_use", "id": "ask-" + str(len(calls)),
                                "name": "ask_user", "input": arguments}]
                else:
                    content = [{"type": "text", "text": "ANSWER=" + kwargs["messages"][-1]["content"]}]
                return SimpleNamespace(content=content)
            with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                root = Path(directory).resolve()
                app = Application({"model": "fake", "max_tokens": 100}, root,
                    TerminalInteraction(), client=SimpleNamespace(close=Mock()),
                    model_call=model, evaluator=Mock())
                stack.enter_context(patch.object(app, "prepare"))
                stack.enter_context(patch("coding_agent.integrations.mcp.get_mcp_config", return_value={}))
                stack.enter_context(patch.dict(hooks.HOOKS, {name: [] for name in hooks.HOOKS}))
                run_cli(app)
                print("CALLS=" + str(len(calls)), flush=True)
                print("PENDING=" + str(app.goal.user_input.pending), flush=True)
        '''
        with self.process(source) as (child, send, expect):
            expect(">> ")
            send("选择数据库\n".encode())
            expect("等待你的回答")
            expect("[2]  PostgreSQL")
            expect("回答 › ")
            send(b"9\n")
            expect("[输入提示] 选项编号超出范围")
            expect("回答 › ")
            send(b"\n")
            expect("[输入提示] 回答不能为空")
            expect("回答 › ")
            send(b"2\n")
            expect("ANSWER=PostgreSQL")
            expect(">> ")
            send("补充要求\n".encode())
            expect("还有哪些要求？")
            expect("回答 › ")
            send("保留现有数据\n".encode())
            expect("ANSWER=保留现有数据")
            expect(">> ")
            send("再提问\n".encode())
            expect("回答 › ")
            send(b"\x1b")
            expect("本轮已中断")
            expect(">> ")
            send(b"exit\n")
            expect("CALLS=5")
            expect("PENDING=None")
            self.assertEqual(child.wait(timeout=5), 0)

    def test_cli_manual_approval_denial_and_escape_never_execute_tool(self):
        source = '''
            import tempfile
            from contextlib import ExitStack
            from pathlib import Path
            from types import SimpleNamespace
            from unittest.mock import Mock, patch
            from coding_agent.application import Application
            from coding_agent.cli import TerminalInteraction, run_cli
            from coding_agent.tools.files import FileTools
            calls, reads = [], []
            original_read = FileTools.read
            def read(instance, *args, **kwargs):
                reads.append(1)
                return original_read(instance, *args, **kwargs)
            def model(client, **kwargs):
                calls.append(kwargs)
                if len(calls) in {1, 3}:
                    return SimpleNamespace(content=[{"type": "tool_use", "id": "read-" + str(len(calls)),
                        "name": "read_file", "input": {"path": "a.py"}}])
                return SimpleNamespace(content=[{"type": "text", "text": "DENIED=工具没有获准执行"}])
            with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                root = Path(directory).resolve()
                (root / "a.py").write_text("private project content")
                app = Application({"model": "fake", "max_tokens": 100, "memory_enabled": False},
                    root, TerminalInteraction(), client=SimpleNamespace(close=Mock()), model_call=model, evaluator=Mock())
                stack.enter_context(patch.object(app, "prepare"))
                stack.enter_context(patch("coding_agent.integrations.mcp.get_mcp_config", return_value={}))
                stack.enter_context(patch.object(FileTools, "read", read))
                run_cli(app)
                print("CALLS=" + str(len(calls)), flush=True)
                print("READS=" + str(len(reads)), flush=True)
        '''
        with self.process(source) as (child, send, expect):
            expect(">> ")
            send(b"/permissions ask\n")
            expect("审批模式：ask")
            expect(">> ")
            send("读取代码\n".encode())
            expect("操作确认：read_file")
            expect("Do you want to proceed? (y/n): ")
            send(b"n\n")
            expect("DENIED=工具没有获准执行")
            expect(">> ")
            send("重新读取\n".encode())
            expect("操作确认：read_file")
            expect("Do you want to proceed? (y/n): ")
            send(b"\x1b")
            expect("本轮已中断")
            expect(">> ")
            send(b"exit\n")
            expect("CALLS=3")
            expect("READS=0")
            self.assertEqual(child.wait(timeout=5), 0)

    def test_cli_escape_then_ordinary_question_resume_control_c_and_eof(self):
        #直接组合新应用层，只替换网络与外部资源；终端、Turn、中断和持久会话均走真实代码。
        source = '''
            import asyncio
            import tempfile
            from pathlib import Path
            from types import SimpleNamespace
            from unittest.mock import Mock, patch
            from contextlib import ExitStack
            from coding_agent.application import Application
            from coding_agent.cli import TerminalInteraction, run_cli
            from coding_agent.runtime import hooks, workspace
            from coding_agent.runtime.session import SessionStore
            from coding_agent.runtime.turn import run_cancellable_async
            async def waiting():
                print("MODEL_WAIT", flush=True)
                try:
                    await asyncio.sleep(30)
                finally:
                    print("NETWORK_CLOSED", flush=True)
            def model(client, **kwargs):
                if "Active session goal (user-defined completion condition):" in kwargs["system"]:
                    return run_cancellable_async(waiting)
                return SimpleNamespace(content=[{"type": "text", "text": "ORDINARY_ANSWER"}])
            with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                root = Path(directory).resolve()
                stack.enter_context(patch.object(workspace, "WORKDIR", root))
                session = SessionStore(root).open()
                stack.callback(session.close)
                application = Application({"model": "fake", "max_tokens": 100}, root,
                    TerminalInteraction(), session, client=SimpleNamespace(close=Mock()),
                    model_call=model, evaluator=Mock())
                stack.enter_context(patch.object(application, "prepare"))
                stack.enter_context(patch("coding_agent.integrations.mcp.get_mcp_config", return_value={}))
                stack.enter_context(patch.dict(hooks.HOOKS, {name: [] for name in hooks.HOOKS}))
                run_cli(application)
                saved = session.store.load(session.id)
                print("SAVED=" + saved["state"]["goal"]["status"], flush=True)
                print("INTERRUPTS=" + str(sum(
                    isinstance(message["content"], str) and
                    message["content"].startswith("[Turn interrupted")
                    for message in saved["messages"])), flush=True)
        '''
        with self.process(source) as (child, send, expect):
            expect(">> ")
            send(b"/goal build feature\n")
            expect("MODEL_WAIT")
            send(b"\x1b")
            expect("NETWORK_CLOSED")
            expect(">> ")
            send(b"ordinary question\n")
            expect("ORDINARY_ANSWER")
            expect(">> ")
            send(b"/goal\n")
            expect("Goal paused: build feature")
            expect(">> ")
            send(b"/goal resume\n")
            expect("MODEL_WAIT")
            send(b"\x03")
            expect("NETWORK_CLOSED")
            expect(">> ")
            send(b"\x04")
            expect("SAVED=paused")
            expect("INTERRUPTS=2")
            self.assertEqual(child.wait(timeout=5), 0)


    def test_cli_mid_turn_steering_enters_next_model_request(self):
        #模型调用在飞行中等待纠偏入队；轮次接缝把它带进下一次请求，不新开回合。
        source = '''
            import faulthandler, tempfile
            import time
            faulthandler.dump_traceback_later(15, exit=True)
            from types import SimpleNamespace
            from unittest.mock import Mock, patch
            from coding_agent.application import Application
            from coding_agent.cli import TerminalInteraction, run_cli
            from coding_agent.runtime import hooks
            calls = []
            def model(client, **kwargs):
                calls.append([m.get("content") for m in kwargs["messages"]])
                print("CALL" + str(len(calls)) + " turn_done=" + str(app.last_context.turn.done.is_set())
                      + " queue=" + str(len(app.last_context.turn.steering)), flush=True)
                #纠偏在轮次接缝进入历史；模型轮询自己的请求内容，看到纠偏后才结束。
                if any("改用方案 B" in str(m.get("content")) for m in kwargs["messages"]):
                    print("LAST=" + str(calls[-1][-1]), flush=True)
                    return SimpleNamespace(content=[{"type": "text", "text": "STEERED_OK"}])
                print("WAITING", flush=True)
                deadline = time.monotonic() + 5
                while not app.last_context.turn.steering and time.monotonic() < deadline:
                    time.sleep(0.01)
                #第一条完整文本回复期间收到纠偏，也必须在同一回合继续请求。
                return SimpleNamespace(content=[{"type": "text", "text": "OLD_ANSWER"}])
            with tempfile.TemporaryDirectory() as directory:
                from pathlib import Path
                root = Path(directory).resolve()
                (root / "app.py").write_text("value = 1\\n", encoding="utf-8")
                app = Application({"model": "fake", "max_tokens": 100, "max_model_calls": 400}, root,
                    TerminalInteraction(), client=SimpleNamespace(close=Mock()),
                    model_call=model, evaluator=Mock())
                original_steer = app.steer
                def loud_steer(line):
                    result = original_steer(line)
                    print("STEER_CALL result=" + str(result), flush=True)
                    return result
                app.steer = loud_steer
                from contextlib import ExitStack
                with ExitStack() as stack:
                    stack.enter_context(patch.object(app, "prepare"))
                    stack.enter_context(patch("coding_agent.integrations.mcp.get_mcp_config", return_value={}))
                    stack.enter_context(patch.dict(hooks.HOOKS, {name: [] for name in hooks.HOOKS}))
                    run_cli(app)
                print("HISTORY_HAS_STEERING=" + str(any(
                    "改用方案 B" in str(m.get("content")) for m in app.messages)), flush=True)
        '''
        with self.process(source) as (child, send, expect):
            expect(">> ")
            send("读取代码\n".encode())
            expect("WAITING")
            send("改用方案 B\n".encode())
            expect("已加入纠偏: 改用方案 B")
            expect("LAST=")
            expect("STEERED_OK")
            send(b"exit\n")
            expect("HISTORY_HAS_STEERING=True")
            self.assertEqual(child.wait(timeout=5), 0)

    def test_abort_returns_queued_message_and_editable_unicode_draft(self):
        source = '''
            import time
            from coding_agent.runtime.terminal import monitor_interrupts, read_user_input
            from coding_agent.runtime.turn import lead_turn, check_cancelled
            def enqueue(line):
                accepted = turn.enqueue_followup(line)
                print("QUEUED=" + str(accepted), flush=True)
                return accepted
            try:
                with lead_turn() as turn:
                    with monitor_interrupts(on_line=enqueue) as monitor:
                        print("READY", flush=True)
                        while True:
                            check_cancelled()
                            time.sleep(0.01)
            except KeyboardInterrupt:
                turn.cancel()
            returned, ready = turn.finish_inputs(followups=True)
            print("RETURNED=" + str(returned) + ";READY=" + str(ready), flush=True)
            drafts = monitor.take_pending()
            print("EDITED=" + read_user_input("DRAFT>", initial=drafts[0]), flush=True)
        '''
        for abort in (b"\x1b", b"\x03"):
            with self.subTest(abort=abort), self.process(source) as (child, send, expect):
                expect("READY")
                send("排队任务\n".encode())
                expect("QUEUED=True")
                send("未发送中文".encode())
                expect("未发送中文")
                send(abort)
                expect("RETURNED=['排队任务'];READY=[]")
                expect("DRAFT>未发送中文")
                send(b"\x7f" + "稿\n".encode())
                expect("EDITED=未发送中稿")
                self.assertEqual(child.wait(timeout=5), 0)

    def test_confirmation_owns_keyboard_without_consuming_existing_draft(self):
        source = '''
            import time
            from coding_agent.runtime.terminal import monitor_interrupts, read_user_input
            with monitor_interrupts(on_line=lambda line: True) as monitor:
                print("READY", flush=True)
                deadline = time.monotonic() + 5
                while len(monitor.characters) < 3 and time.monotonic() < deadline:
                    time.sleep(0.01)
                answer = read_user_input("CONFIRM>")
                print("ANSWER=" + answer, flush=True)
            print("DRAFTS=" + str(monitor.take_pending()), flush=True)
        '''
        with self.process(source) as (child, send, expect):
            expect("READY")
            send("草稿文".encode())
            expect("CONFIRM>")
            send(b"y\n")
            expect("ANSWER=y")
            expect("DRAFTS=['草稿文']")
            self.assertEqual(child.wait(timeout=5), 0)


if __name__ == "__main__":
    unittest.main()
