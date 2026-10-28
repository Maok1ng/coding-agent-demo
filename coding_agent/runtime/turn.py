"""一次执行的取消边界；Session 保留历史，Turn 只管理本次执行及其附属操作。"""

import asyncio
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
import threading
import uuid


class TurnCancelled(KeyboardInterrupt):
    """取消不是工具失败；继承 KeyboardInterrupt，避免被 except Exception 吞掉后续跑。"""


class SessionExit(BaseException):
    """工具确认时收到 EOF：退出整个 CLI，不能当成普通工具错误后继续执行。"""


class TurnContext:
    def __init__(self):
        self.id = uuid.uuid4().hex
        self.cancelled = threading.Event()
        self.done = threading.Event()
        self.reason = "User interrupted the current turn"
        self.started_tools: set[str] = set()
        self._lock = threading.RLock()
        self._callbacks = []
        self.cleanup_errors: list[str] = []
        #回合级指标累加（模型调用次数与 token 用量）；由使用方写入，收尾时汇总展示。
        self.metrics: dict[str, int] = {}
        #回合运行中的用户纠偏队列；CLI 线程写入，runner 在轮次接缝消费，子角色共享。
        self.steering: deque = deque()
        self.followups: deque = deque()
        self._steering_records = []
        self.protected_input_ids: set[str] = set()
        self._input_sequence = 0
        self._async = None
        self._async_lock = threading.Lock()
        self._closed = False

    def enqueue_steering(self, text: str):
        return self._enqueue_input(text, self.steering)

    def enqueue_followup(self, text: str):
        return self._enqueue_input(text, self.followups)

    def _enqueue_input(self, text, queue):
        #与取消、关闭共用锁；不能向已经结束或取消的回合再提交，调用方须保留原稿。
        if not isinstance(text, str) or not text.strip():
            return False
        with self._lock:
            if self.cancelled.is_set() or self.done.is_set() or self._closed:
                return False
            if len(self.steering) + len(self.followups) + len(self._steering_records) >= 128:
                return False
            self._input_sequence += 1
            queue.append({"text": text, "order": self._input_sequence})
            return True

    def inject_steering(self, history: list) -> bool:
        """仅执行线程在完整批次边界调用；每个角色各接收一次，父角色也不能漏掉子角色收到的纠偏。"""
        with self._lock:
            if self.cancelled.is_set():
                raise TurnCancelled(self.reason)
            while self.steering:
                item = self.steering.popleft()
                identifier = uuid.uuid4().hex
                self.protected_input_ids.add(identifier)
                self._steering_records.append({**item, "seen": False, "message": {
                    "role": "user", "tags": ["USER_PROMPT", "STEERING"], "input_id": identifier,
                    "content": ("[User steering: update the current task; this does not change host "
                                "mode, approval settings or Goal state.]\n" + item["text"]),
                }})
            existing = {item.get("input_id") for item in history}
            added = [dict(item["message"]) for item in self._steering_records
                     if item["message"]["input_id"] not in existing]
            history.extend(added)
            return bool(added)

    def acknowledge_steering(self, history: list):
        """完整模型响应才确认消费；压缩、连接失败或取消前未送达的输入仍可退还。"""
        with self._lock:
            if self.cancelled.is_set():
                return
            identifiers = {item.get("input_id") for item in history}
            for item in self._steering_records:
                if item["message"]["input_id"] in identifiers:
                    item["seen"] = True

    def retain_seen_steering(self, history: list):
        """父会话保存子角色真正收到的输入；未送达的消息从运行历史移除，回到编辑器。"""
        with self._lock:
            unseen = {item["message"]["input_id"] for item in self._steering_records if not item["seen"]}
            history[:] = [item for item in history if item.get("input_id") not in unseen]
            existing = {item.get("input_id") for item in history}
            history.extend(dict(item["message"]) for item in self._steering_records
                           if item["seen"] and item["message"]["input_id"] not in existing)

    def finish_inputs(self, *, followups=False) -> tuple[list[str], list[str]]:
        """监听线程停止后取回队列；只有正常完成才允许 follow-up 作为新回合提交。"""
        with self._lock:
            returned = [item for item in self._steering_records if not item["seen"]]
            returned.extend(self.steering)
            ready = list(self.followups) if followups and not self.cancelled.is_set() else []
            if not ready:
                returned.extend(self.followups)
            self.steering.clear()
            self.followups.clear()
            self._steering_records = [item for item in self._steering_records if item["seen"]]
            return ([item["text"] for item in sorted(returned, key=lambda item: item["order"])],
                    [item["text"] for item in ready])

    def async_runtime(self):
        """本回合常驻事件循环；惰性创建，请求提交到同一线程可复用连接池。"""
        with self._async_lock:
            if self._closed:
                raise RuntimeError("Turn resources are already closed")
            if self._async is None:
                self._async = _TurnAsyncRuntime(self)
            return self._async

    def close(self):
        """回合收尾：先执行关闭回调（如模型连接池，需要循环仍在运行），再停止循环线程。"""
        with self._lock:
            self._closed = True
        with self._async_lock:
            runtime, self._async = self._async, None
        if runtime is not None:
            runtime.close()

    def _cleanup(self, callback):
        try:
            callback()
        except Exception as error:
            #这里只保存类型，不把可能含凭据的网络异常原文写入历史。
            self.cleanup_errors.append(type(error).__name__)

    def add_cancel_callback(self, callback):
        """登记本回合资源的停止函数；取消后才登记的资源也必须立即清理。"""
        with self._lock:
            cancelled = self.cancelled.is_set()
            if not cancelled:
                self._callbacks.append(callback)
        if cancelled:
            self._cleanup(callback)

        def remove():
            with self._lock:
                if callback in self._callbacks:
                    self._callbacks.remove(callback)
        return remove

    def cancel(self, reason="User interrupted the current turn"):
        """可从键盘线程调用；先设置标记，再按后注册先清理的顺序停止附属操作。"""
        with self._lock:
            if self.cancelled.is_set():
                return
            self.reason = reason
            self.cancelled.set()
            callbacks = list(reversed(self._callbacks))
            self._callbacks.clear()
        #不能持锁调用外部清理函数，否则清理中的注销操作可能造成死锁。
        for callback in callbacks:
            self._cleanup(callback)


class _TurnAsyncRuntime:
    """回合专用的常驻事件循环线程；同一线程内复用连接池，不跨回合共享。"""

    def __init__(self, turn):
        self._turn = turn
        self._closers = []
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, daemon=True,
                                        name=f"agent-turn-{turn.id[:8]}")
        self._thread.start()

    def add_close_callback(self, callback):
        """callback(loop) 在循环仍在运行时执行，可用于等待异步连接关闭完成。"""
        self._closers.append(callback)

    def close(self):
        interrupted = None
        for closer in reversed(self._closers):
            try:
                closer(self.loop)
            except BaseException as error:
                #只记录类型，不把可能含凭据的网络异常原文写入历史。
                self._turn.cleanup_errors.append(type(error).__name__)
                if not isinstance(error, Exception):
                    interrupted = interrupted or error
        self._closers.clear()

        async def finish_pending():
            #主线程 Ctrl+C 可能提前离开 Future.result；先取消并等待遗留任务，再停循环。
            tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
            for task in tasks:
                task.cancel()
            if tasks:
                done, pending = await asyncio.wait(tasks, timeout=1)
                for task in done:
                    if not task.cancelled():
                        task.exception()  #取回异常，避免关闭循环时残留未消费的异常通知。
                return not pending
            return True

        cleanup = asyncio.run_coroutine_threadsafe(finish_pending(), self.loop)
        try:
            if not cleanup.result(timeout=2):
                self._turn.cleanup_errors.append("AsyncTasksStopUnconfirmed")
        except BaseException as error:
            cleanup.cancel()
            self._turn.cleanup_errors.append(type(error).__name__)
            if not isinstance(error, Exception):
                interrupted = interrupted or error
        self.loop.call_soon_threadsafe(self.loop.stop)
        #一次 Ctrl+C 不能跳过线程回收；保留中断，完成清理后再交给应用保存暂停状态。
        while True:
            try:
                self._thread.join(timeout=5)
                break
            except KeyboardInterrupt as error:
                interrupted = interrupted or error
        if self._thread.is_alive():
            self._turn.cleanup_errors.append("AsyncLoopStopUnconfirmed")
        else:
            self.loop.close()
        if interrupted is not None:
            raise interrupted


_current_turn = ContextVar("agent_turn", default=None)
_active_turn = None
_active_lock = threading.Lock()


def current_turn() -> TurnContext | None:
    return _current_turn.get()


def active_turn() -> TurnContext | None:
    """键盘在主线程、自动回合可能在队列线程；用此入口找到当前唯一的 Lead 回合。"""
    with _active_lock:
        return _active_turn


def check_cancelled():
    turn = current_turn()
    if turn is not None and turn.cancelled.is_set():
        raise TurnCancelled(turn.reason)


@contextmanager
def turn_context(turn):
    """ContextVar 自动传给同线程子 Agent，不传给独立新建的后台／队友线程。"""
    token = _current_turn.set(turn)
    try:
        check_cancelled()
        yield turn
    finally:
        _current_turn.reset(token)


@contextmanager
def lead_turn():
    """agent_lock 外部保证串行；回合附属资源（如事件循环）全部关闭后才设置 done。"""
    global _active_turn
    turn = TurnContext()
    with _active_lock:
        if _active_turn is not None:
            raise RuntimeError("Another Lead turn is still running")
        _active_turn = turn
    try:
        with turn_context(turn):
            yield turn
    finally:
        try:
            #收尾期间 Esc 仍应找到本回合；done 必须连关闭异常也可靠落下。
            turn.close()
        finally:
            with _active_lock:
                _active_turn = None
            turn.done.set()


def cancel_active_turn(reason="User interrupted the current turn"):
    turn = active_turn()
    if turn is not None:
        turn.cancel(reason)
    return turn


class _TurnCancelSignal:
    """在 Task 边界内传递取消；asyncio Task 会把 KeyboardInterrupt 直接抛进循环，
    不能作为 future 结果跨线程返回，所以用普通对象携带原因，由调用方转回 TurnCancelled。"""

    __slots__ = ("reason",)

    def __init__(self, reason: str):
        self.reason = reason


def run_cancellable_async(operation):
    """把请求提交到本回合常驻事件循环；取消 Task 后等待连接清理，不遗留会回写历史的线程。"""
    turn = current_turn()
    check_cancelled()

    async def run():
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()

        def cancel():
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass  #请求恰好清理完毕；后续 check_cancelled 仍会丢弃结果。

        remove = turn.add_cancel_callback(cancel) if turn is not None else lambda: None
        try:
            check_cancelled()
            result = await operation()
            check_cancelled()
            return result
        except asyncio.CancelledError:
            if turn is None or not turn.cancelled.is_set():
                raise
        except TurnCancelled as signal:
            return _TurnCancelSignal(str(signal))
        finally:
            remove()
        return _TurnCancelSignal(turn.reason)

    if turn is None:
        result = asyncio.run(run())
    else:
        runtime = turn.async_runtime()
        result = asyncio.run_coroutine_threadsafe(run(), runtime.loop).result()
    if isinstance(result, _TurnCancelSignal):
        raise TurnCancelled(result.reason) from None
    return result
