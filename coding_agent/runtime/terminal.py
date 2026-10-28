"""终端输入与 Esc 监听；只在真实 TTY 使用按键模式，管道和测试仍使用 input。"""

import codecs
from contextlib import contextmanager
import os
import select
import sys
import termios
import threading
import tty

from coding_agent.runtime.turn import TurnCancelled, cancel_active_turn, current_turn


_monitor = None


def _interactive():
    return sys.stdin.isatty() and sys.stdout.isatty()


@contextmanager
def _keyboard():
    descriptor = sys.stdin.fileno()
    previous = termios.tcgetattr(descriptor)
    try:
        #cbreak 保留 Ctrl+C 的 SIGINT，但不再等 Enter 才把 Esc 交给程序。
        tty.setcbreak(descriptor, termios.TCSANOW)
        yield descriptor
    finally:
        termios.tcsetattr(descriptor, termios.TCSANOW, previous)


def _read_key(descriptor):
    key = os.read(descriptor, 1)
    if key != b"\x1b":
        return key
    #方向键同样以 Esc 开头；短暂等待后续序列，不能把方向键当成取消。
    if select.select([descriptor], [], [], 0.08)[0]:
        following = os.read(descriptor, 1)
        if following == b"\x1b":
            return key  #连续按两次 Esc 仍表示取消，不能把两个按键都当成 Alt 序列吞掉。
        if following in {b"[", b"O"}:
            for _ in range(16):
                if not select.select([descriptor], [], [], 0.02)[0]:
                    break
                if b"@" <= os.read(descriptor, 1) <= b"~":
                    break
        return b""  #不支持的方向键／Alt 组合不改变输入，也不触发取消。
    return key


class InterruptMonitor:
    """主线程执行模型／工具时，辅助线程读 Esc 与整行纠偏；历史由原执行线程单独修改。
    on_line 提供时，可打印字符攒行、Enter 提交给回调（Application.steer）；否则维持只读 Esc。"""

    def __init__(self, on_line=None):
        self.stop_event = threading.Event()
        self.interrupted = threading.Event()
        self.on_line = on_line
        self.thread = None
        self.mode = None
        self.characters = []
        self.returned_lines = []
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def start(self):
        self.stop_event.clear()
        self.mode = _keyboard()
        descriptor = self.mode.__enter__()

        def watch():
            characters = self.characters
            decoder = self.decoder
            while not self.stop_event.is_set():
                if select.select([descriptor], [], [], 0.05)[0]:
                    key = _read_key(descriptor)
                    if key == b"\x1b":
                        self.interrupted.set()
                        print("\n[正在中断当前回合…]", flush=True)
                        cancel_active_turn()
                        return
                    if self.on_line is None or not key:
                        continue
                    if key in {b"\r", b"\n"}:
                        line = "".join(characters).strip()
                        characters.clear()
                        print(flush=True)
                        if line:
                            #回调拒收时保留原文；回合刚结束、宿主命令或队列满都不能吞输入。
                            if not self.on_line(line):
                                self.returned_lines.append(line)
                        continue
                    if key in {b"\x7f", b"\x08"}:
                        if characters:
                            characters.pop()
                            print("\b \b", end="", flush=True)
                        continue
                    if key >= b" ":
                        text = decoder.decode(key)
                        if text:
                            characters.append(text)
                            print(text, end="", flush=True)

        self.thread = threading.Thread(target=watch, name="escape-monitor", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread is not None:
            #清理容器等回调有自己的超时；等监听线程退出再恢复终端，避免双读输入。
            if self.thread.ident is not None:
                self.thread.join()
            self.thread = None
        if self.mode is not None:
            self.mode.__exit__(None, None, None)
            self.mode = None

    def take_pending(self):
        """监听线程已停止后，把未提交整行及拒收的输入交还普通编辑器。"""
        pending = [*self.returned_lines]
        if self.characters:
            pending.append("".join(self.characters))
        self.returned_lines.clear()
        self.characters.clear()
        self.decoder.reset()
        return pending


@contextmanager
def monitor_interrupts(on_line=None):
    global _monitor
    if _monitor is not None:
        yield _monitor
        return
    if not _interactive() or threading.current_thread() is not threading.main_thread():
        yield None
        return
    monitor = InterruptMonitor(on_line)
    _monitor = monitor
    try:
        #初始化本身也可能被 Ctrl+C 打断；已经切换的终端模式仍必须恢复。
        monitor.start()
        yield monitor
    finally:
        monitor.stop()
        _monitor = None


def read_user_input(prompt, initial=""):
    """权限确认和普通输入共用入口；读人类答案时暂时交还键盘，不与 Esc 线程争抢。"""
    if not _interactive():
        #非 TTY 无法预填编辑缓冲；显示未发送原文，仍须显式按 Enter/输入确认。
        answer = input(prompt + initial)
        return initial + answer
    monitor = _monitor
    if monitor is not None:
        monitor.stop()
    try:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        characters = list(initial)
        with _keyboard() as descriptor:
            #先进入编辑模式再显示输入框，否则用户立刻按退格时会被 canonical 模式吞掉。
            print(prompt + initial, end="", flush=True)
            while True:
                key = _read_key(descriptor)
                if key == b"\x1b":
                    cancel_active_turn()
                    raise TurnCancelled("User pressed Escape")
                if key in {b"\r", b"\n"}:
                    print()
                    return "".join(characters)
                if key == b"\x04":
                    if not characters:
                        raise EOFError
                    continue
                if key in {b"\x7f", b"\x08"}:
                    if characters:
                        characters.pop()
                        print("\b \b", end="", flush=True)
                    continue
                if key and key >= b" ":
                    text = decoder.decode(key)
                    if text:
                        characters.append(text)
                        print(text, end="", flush=True)
    finally:
        if monitor is not None and not (current_turn() and current_turn().cancelled.is_set()):
            monitor.start()
