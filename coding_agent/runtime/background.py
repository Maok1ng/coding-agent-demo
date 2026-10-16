#后台命令的流程：bash 先返回 bg_id，线程执行命令，后续轮次把完成结果作为通知加入 messages。
import threading
from pathlib import Path

from coding_agent.runtime.hooks import trigger_hook
from coding_agent.runtime.environment import format_bash_result, run_bash_process as _run_bash_process
from coding_agent.runtime.workspace import WORKDIR, use_workdir

#Shell 进程组清理与超时处理统一移到 environment.py，前台、后台共用同一执行后端。


class BackgroundManager:
    #tasks 记录运行状态，results 保存完成输出，_ready 只保存待通知的 bg_id。
    def __init__(self):
        self.tasks: dict[str, dict] = {}
        self.results: dict[str, str] = {}
        self._ready: list[str] = []
        self._counter = 0
        self._lock = threading.Lock()

    #先登记任务再启动 daemon 线程，让当前工具调用立即拿到 bg_id。
    def start(self, block, cwd: Path = WORKDIR) -> str:
        if block.name != "bash":
            raise ValueError("Only Bash commands can run in the background")
        command = block.input.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("Bash command cannot be empty")

        with self._lock:
            self._counter += 1
            task_id = f"bg_{self._counter:04d}"
            self.tasks[task_id] = {
                "tool_use_id": block.id,
                "command": command,
                "status": "running",
            }

        thread = threading.Thread(
            target=self._run,
            args=(task_id, command, cwd),
            #主程序退出时线程不阻止退出，Shell 进程交给上面的清理函数处理。
            daemon=True,
        )
        try:
            thread.start()
        except Exception:
            with self._lock:
                self.tasks.pop(task_id, None)
            raise
        print(f"  [background] started {task_id}: {command[:60]}")
        return task_id

    #线程里只执行命令并记录结果；不直接修改 Agent 的 messages。
    def _run(self, task_id: str, command: str, cwd: Path):
        try:
            output, exit_code = _run_bash_process(command, cwd)
            result = format_bash_result(output, exit_code)
            status = "completed" if exit_code == 0 else "failed"
        except Exception as error:
            result = f"Error: {type(error).__name__}: {error}"
            status = "failed"

        with self._lock:
            #完成结果进入 _ready，等主线程下次调用 collect 时再交给模型。
            task = self.tasks.get(task_id)
            if task is None:
                return
            task["status"] = status
            self.results[task_id] = result
            self._ready.append(task_id)

    #一次性取走已完成的任务，并整理成模型可读的通知文本。
    def has_pending(self) -> bool:
        #包括运行中和已经完成但尚未注入 messages 的任务。
        with self._lock:
            return bool(self.tasks)

    def collect(self) -> list[str]:
        with self._lock:
            ready = []
            for task_id in self._ready:
                task = self.tasks.pop(task_id, None)
                result = self.results.pop(task_id, "")
                if task is not None:
                    ready.append((task_id, task, result))
            self._ready.clear()

        notifications = []
        for task_id, task, result in ready:
            #通知只展示前500字符；原始 tool_use 已收到“启动成功”的 tool_result。
            notifications.append(
                f"<task_notification>\n"
                f"  <task_id>{task_id}</task_id>\n"
                f"  <status>{task['status']}</status>\n"
                f"  <command>{task['command']}</command>\n"
                f"  <summary>{result[:500]}</summary>\n"
                f"</task_notification>"
            )
            print(f"  [background] collected {task_id}: {task['status']}")
        return notifications


#模块级管理器在同一个进程里共享；bg_id 与 .tasks/ 中的 task_id 是两套编号。
BACKGROUND = BackgroundManager()


#必须是 bash 且显式传入 true；普通 bash 仍按原来的同步方式执行。
def should_run_background(tool_name: str, tool_input: dict) -> bool:
    return (
        tool_name == "bash"
        and tool_input.get("run_in_background") is True
    )


#给主循环使用的入口，实际状态都保存在 BACKGROUND 中。
def start_background_task(block, cwd: Path = WORKDIR) -> str:
    return BACKGROUND.start(block, cwd)


#取出已经完成但尚未通知模型的后台结果。
def collect_background_results() -> list[str]:
    return BACKGROUND.collect()


#在下一次调用模型前注入通知；已有 user 消息时追加内容，避免连续创建相同角色的消息。
def inject_background_results(messages: list) -> int:
    notifications = collect_background_results()
    if not notifications:
        return 0

    blocks = [{"type": "text", "text": item} for item in notifications]
    if messages and messages[-1].get("role") == "user":
        content = messages[-1].get("content", "")
        if isinstance(content, list):
            #上一条可能是 tool_result 列表，把通知作为 text block 接在后面。
            content.extend(blocks)
        else:
            messages[-1]["content"] = [
                {"type": "text", "text": str(content)},
                *blocks,
            ]
    else:
        messages.append({"role": "user", "content": blocks})
    return len(notifications)


#先过原有权限 hook，再决定走后台线程还是普通工具 handler。
def execute_tool(block, handlers: dict, background_cwd=WORKDIR) -> str:
    try:
        cwd = background_cwd() if callable(background_cwd) else background_cwd
        #权限 hook、一次性子 Agent 和后台线程都使用本次 Task 的目录。
        with use_workdir(cwd):
            return _execute_tool(block, handlers, cwd)
    except Exception as error:
        return f"Error: {error}"


def _execute_tool(block, handlers: dict, cwd: Path) -> str:
    blocked = trigger_hook("PreToolUse", block)
    if blocked is not None:
        return str(blocked)

    try:
        if should_run_background(block.name, block.input):
            #后台 Bash 使用本轮 Agent 的任务工作目录，与同步 Bash 保持一致。
            task_id = start_background_task(block, cwd)
            output = (
                f"[Background task {task_id} started] "
                "The result will be collected on a later turn."
            )
        else:
            handler = handlers.get(block.name)
            tool_input = dict(block.input)
            if block.name == "bash":
                #run_in_background 只控制分发，原来的 run_bash 不接收这个参数。
                tool_input.pop("run_in_background", None)
            output = handler(**tool_input) if handler else f"Unknown: {block.name}"
        trigger_hook("PostToolUse", block, output)
    except Exception as error:
        output = f"Error: {error}"

    return str(output)
