"""Turn 所属的命令进程；没有跨回合通知队列，也不会自动唤醒模型。"""

import json
from pathlib import Path
import threading
import time

from coding_agent.runtime.environment import run_bash_evidence
from coding_agent.runtime.turn import TurnCancelled, TurnContext, turn_context
from coding_agent.runtime.verification import capture_source_versions
from coding_agent.runtime.workspace import current_workdir, use_workdir, use_workspace


CLEANUP_TIMEOUT = 5.0


class TurnProcessManager:
    """后台线程只保存结果；调用者通过 wait/status 主动领取执行证据。"""

    def __init__(self, workspace: Path, turn: TurnContext):
        self.workspace = Path(workspace).resolve()
        self.turn = turn
        self.output_dir = self.workspace / ".task_outputs" / "processes"
        self._records: dict[str, dict] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._cancellers: dict[str, threading.Thread] = {}
        self._children: dict[str, TurnContext] = {}
        self._lock = threading.RLock()
        self._closed = False

    @property
    def records(self) -> list[dict]:
        """返回副本，避免工具调用者在锁外修改任务事实。"""
        with self._lock:
            return [dict(record) for record in self._records.values()]

    def start(self, command: str, cwd: Path | None = None, *, source_versions=None) -> str:
        """登记后立即返回进程 ID；启动成功不代表命令或测试通过。"""
        if not isinstance(command, str) or not command.strip():
            raise ValueError("Bash command cannot be empty")
        with use_workspace(self.workspace):
            directory = current_workdir(cwd or self.workspace)
        if not directory.is_relative_to(self.workspace):
            raise ValueError("Working directory escapes this Turn's workspace")
        with self._lock:
            if self._closed:
                raise RuntimeError("This Turn's process manager is closed")
            if self.turn.cancelled.is_set():
                raise TurnCancelled(self.turn.reason)
            task_id = f"process_{self.turn.id[:8]}_{len(self._records) + 1:04d}"
            output_path = self.output_dir / f"{task_id}.log"
            child = TurnContext()
            self._children[task_id] = child
            self._records[task_id] = {
                "id": task_id, "turn_id": self.turn.id, "command": command,
                "cwd": str(directory), "status": "running", "exit_code": None,
                "cancelled": False, "timed_out": False, "cleanup_confirmed": True,
                "full_output_path": str(output_path), "output": "",
                "source_versions": dict(source_versions or {}),
            }
            #新线程不自动继承 ContextVar，因此显式绑定附属上下文。正常 close 只取消 child。
            remove = self.turn.add_cancel_callback(lambda: self._cancel_child(task_id, self.turn.reason))
            thread = threading.Thread(target=self._run, args=(task_id, child, directory, remove), daemon=True)
            self._threads[task_id] = thread
            try:
                thread.start()
            except Exception:
                remove()
                self._threads.pop(task_id, None)
                self._children.pop(task_id, None)
                self._records.pop(task_id, None)
                raise
        return task_id

    def _cancel_child(self, task_id: str, reason: str):
        """清理 Docker 可能等待 daemon；并行发起取消，不能让多个任务串行阻塞按键处理。"""
        with self._lock:
            if task_id in self._cancellers:
                return
            child = self._children[task_id]
            thread = threading.Thread(target=child.cancel, args=(reason,), daemon=True)
            self._cancellers[task_id] = thread
            thread.start()

    def _run(self, task_id: str, child: TurnContext, cwd: Path, remove):
        with self._lock:
            record = dict(self._records[task_id])
        facts = {}
        try:
            with turn_context(child), use_workspace(self.workspace), use_workdir(cwd):
                facts = run_bash_evidence(record["command"], cwd, output_path=Path(record["full_output_path"]))
                facts["source_versions_after"] = capture_source_versions(self.workspace, record["source_versions"])
        except TurnCancelled as error:
            facts = getattr(error, "execution_evidence", {})
            facts.update(status="cancelled", cancelled=True, exit_code=None)
        except Exception as error:
            #工具执行失败不是线程启动成功；保留错误类别，并避免泄露外部异常中的敏感原文。
            facts = {"status": "failed", "exit_code": None, "output": f"Error: {type(error).__name__}"}
        finally:
            remove()
            with self._lock:
                record = self._records[task_id]
                cleanup_failed = not record["cleanup_confirmed"]
                record.update(facts)
                if child.cancelled.is_set():
                    record.update(status="cancelled", cancelled=True, exit_code=None)
                if cleanup_failed or child.cleanup_errors or not record["cleanup_confirmed"]:
                    record.update(status="cleanup_failed", cleanup_confirmed=False)
                child.done.set()

    def _get(self, task_id: str) -> dict:
        with self._lock:
            if task_id not in self._records:
                raise ValueError(f"Unknown process ID for this Turn: {task_id}")
            return dict(self._records[task_id])

    def status(self, task_id: str) -> str:
        """JSON 结果含完整输出路径；运行中不猜测退出码。"""
        return json.dumps(self._get(task_id), ensure_ascii=False)

    def wait(self, task_id: str, timeout: float = 30) -> str:
        """等待超时只表示本次等待结束，不终止命令，也不冒充命令本身超时。"""
        self._get(task_id)
        if not isinstance(timeout, (int, float)) or not 0 <= timeout <= 60:
            raise ValueError("Wait timeout must be between 0 and 60 seconds")
        deadline = time.monotonic() + timeout
        child = self._children[task_id]
        while not child.done.is_set():
            if self.turn.cancelled.is_set():
                raise TurnCancelled(self.turn.reason)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            child.done.wait(min(remaining, 0.05))
        result = self._get(task_id)
        result["wait_timed_out"] = not child.done.is_set()
        return json.dumps(result, ensure_ascii=False)

    def close(self) -> list[dict]:
        """Turn 结束时回收残留任务；不改变父 Turn 的取消状态，且不无限等待线程。"""
        with self._lock:
            self._closed = True
            running = [(task_id, self._children[task_id], thread)
                       for task_id, thread in self._threads.items()
                       if thread.is_alive() and not self._children[task_id].done.is_set()]
        for task_id, _, _ in running:
            self._cancel_child(task_id, "Owning Turn ended; unfinished command was cancelled")
        deadline = time.monotonic() + CLEANUP_TIMEOUT
        for task_id, _, thread in running:
            thread.join(timeout=max(0, deadline - time.monotonic()))
            canceller = self._cancellers.get(task_id)
            if canceller is not None:
                canceller.join(timeout=max(0, deadline - time.monotonic()))
            if thread.is_alive() or (canceller is not None and canceller.is_alive()):
                with self._lock:
                    self._records[task_id].update(status="cleanup_failed", cancelled=True,
                                                 cleanup_confirmed=False, exit_code=None)
                self.turn.cleanup_errors.append("BackgroundThreadCleanupUnconfirmed")
                print(f"  [shell] Background cleanup unconfirmed: {task_id}; result is not successful", flush=True)
        return self.records
