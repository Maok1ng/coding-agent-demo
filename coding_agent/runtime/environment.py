"""Shell 的统一执行入口；Docker 后端失败时不会退回本机执行。"""

import atexit
import json
import os
import shutil
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from coding_agent.runtime.turn import TurnCancelled, check_cancelled, current_turn
from coding_agent.runtime.workspace import APP_DIR, CONFIG_PATH, SOURCE_LAYOUT, current_workdir, workspace_root


_shell_processes: set[subprocess.Popen] = set()
_shell_process_lock = threading.RLock()
_containers: set[str] = set()


def _stop_process_group(process: subprocess.Popen) -> bool:
    """先温和停止再强制结束；返回值表示直接子进程是否已确认退出。"""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            break
        except OSError:
            #macOS 沙箱可能在进程已成为僵尸时拒绝第二次 kill；poll 会收割并确认退出。
            return process.poll() is not None
        time.sleep(0.05)
    try:
        process.wait(timeout=0.2)
    except subprocess.TimeoutExpired:
        return False
    return True


def _remove_container(name: str):
    failure = None
    try:
        result = subprocess.run(["docker", "rm", "--force", name], capture_output=True, timeout=10)
        if result.returncode:
            #重复清理时“不存在”也算成功；其他非零返回不能证明容器已经停止。
            error_text = result.stderr.decode(errors="replace") if isinstance(result.stderr, bytes) else (result.stderr or "")
            if "no such container" not in error_text.lower():
                failure = f"exit status {result.returncode}"
    except (OSError, subprocess.TimeoutExpired) as error:
        failure = type(error).__name__
    if failure is not None:
        #保留名称让 CLI 退出时重试；只打印错误类型/退出码，不输出 Docker 的原始 stderr。
        print(f"  [shell] Container cleanup unconfirmed ({failure}): {name}; will retry on exit", flush=True)
        return False
    with _shell_process_lock:
        _containers.discard(name)
    return True


def _stop_all_shell_processes():
    with _shell_process_lock:
        processes = list(_shell_processes)
        containers = list(_containers)
    for process in processes:
        if _stop_process_group(process):
            with _shell_process_lock:
                _shell_processes.discard(process)
    for name in containers:
        _remove_container(name)


def stop_shell_processes():
    """退出整个 CLI 时兜底清理；正常工具取消仍只停止所属 Turn 的资源。"""
    _stop_all_shell_processes()


#供 rg/git 等有界读取方复用同一套温和停止再强杀的清理语义。
stop_process_group = _stop_process_group


def _handle_termination_signal(signum, _frame):
    _stop_all_shell_processes()
    raise SystemExit(128 + signum)


atexit.register(_stop_all_shell_processes)
if threading.current_thread() is threading.main_thread():
    signal.signal(signal.SIGTERM, _handle_termination_signal)


def _open_output_file(path: Path):
    """输出只能新建在工作区内；不跟随预先放置的目录链接或覆盖已有文件。"""
    root = workspace_root()
    absolute = Path(os.path.abspath(path))
    if not absolute.is_relative_to(root):
        raise ValueError("Shell output path escapes workspace")
    parent = root
    for part in absolute.parent.relative_to(root).parts:
        parent = parent / part
        if parent.is_symlink():
            raise ValueError("Shell output directories cannot be symbolic links")
        parent.mkdir(mode=0o700, exist_ok=True)
    #O_EXCL 同时拒绝已有普通文件、符号链接与硬链接，避免证据路径覆盖用户数据。
    descriptor = os.open(absolute, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(descriptor, "w+", encoding="utf-8", errors="replace")


def _run_process(command, cwd: Path, timeout: int, *, shell: bool,
                 output_path: Path | None = None, evidence: dict | None = None) -> tuple[str, int | None]:
    """本机和 docker CLI 共用执行规则；有路径时直接落盘，避免完整输出常驻内存。"""
    check_cancelled()
    process = None
    remove_cancel = None
    output_handle = None
    output = ""
    timed_out = False
    execution_error = None
    facts = evidence if evidence is not None else {}
    facts.update(exit_code=None, cancelled=False, timed_out=False, cleanup_confirmed=True)
    try:
        if output_path is not None:
            output_path = Path(output_path)
            facts["full_output_path"] = str(output_path)
            output_handle = _open_output_file(output_path)
        process = subprocess.Popen(
            command, shell=shell, cwd=cwd, stdout=output_handle or subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, errors="replace", start_new_session=True,
        )
        with _shell_process_lock:
            _shell_processes.add(process)
        turn = current_turn()
        if turn is not None:
            #回调只持有本次进程组；后台线程显式绑定自己的附属 TurnContext。
            remove_cancel = turn.add_cancel_callback(lambda: _stop_process_group(process))
        #取消可能恰好发生在 Popen 与登记回调之间，登记后必须再次检查。
        check_cancelled()
        output, _ = process.communicate(timeout=timeout)
        output = output or ""
        #进程被取消回调杀死时，不能把非零退出码当成一次普通工具结果继续执行。
        check_cancelled()
        facts["exit_code"] = process.returncode
    except subprocess.TimeoutExpired:
        check_cancelled()
        timed_out = True
        facts["timed_out"] = True
    except OSError as error:
        check_cancelled()
        execution_error = f"Error: {type(error).__name__}: {error}"
    finally:
        if remove_cancel is not None:
            remove_cancel()
        if process is not None:
            stopped = _stop_process_group(process)
            try:
                remainder, _ = process.communicate(timeout=0.2)
                if remainder is not None:
                    output = remainder
            except subprocess.TimeoutExpired:
                stopped = False
            facts["cleanup_confirmed"] = bool(stopped)
            if stopped:
                with _shell_process_lock:
                    _shell_processes.discard(process)
            else:
                print(f"  [shell] Process cleanup unconfirmed: {process.pid}; will retry on exit", flush=True)
                if turn is not None:
                    turn.cleanup_errors.append("ProcessCleanupUnconfirmed")
        if output_handle is not None:
            #只把预览装入工具结果；完整原文始终保留在证据文件。
            #从同一描述符读取，避免完成后按路径重新打开时跟随新放置的符号链接。
            try:
                output_handle.seek(0)
                output = output_handle.read(50000)
            finally:
                output_handle.close()
        if current_turn() is not None and current_turn().cancelled.is_set():
            facts["cancelled"] = True
        facts["output"] = output.strip()[:50000] or "(no output)"
    if timed_out:
        return f"Error: Timeout ({timeout}s)\n{facts['output']}", None
    if execution_error is not None:
        return execution_error, None
    return facts["output"], facts["exit_code"]


@dataclass
class ExecutionConfig:
    backend: str = "local"
    image: str = "coding-agent-sandbox:local"
    timeout: int = 120
    cpus: float = 2.0
    memory: str = "2g"
    pids_limit: int = 128
    network: str = "none"


class LocalEnvironment:
    def __init__(self, config: ExecutionConfig):
        self.config = config

    def validate(self):
        current_workdir()

    def prepare(self):
        self.validate()

    def execute(self, command: str, cwd: Path, *, output_path: Path | None = None,
                evidence: dict | None = None) -> tuple[str, int | None]:
        options = {"output_path": output_path, "evidence": evidence} if output_path is not None or evidence is not None else {}
        return _run_process(command, current_workdir(cwd), self.config.timeout, shell=True, **options)


class DockerEnvironment(LocalEnvironment):
    def validate(self):
        super().validate()
        root = workspace_root()
        #绑定整个工作区，任务 worktree 的 .git 指针仍可访问同一工作区内的主仓库。
        #配置中的 API key 留在宿主；工作区包含本程序配置时拒绝启动，而不是泄露给容器。
        for path in (CONFIG_PATH, APP_DIR / "config.json"):
            if path.is_file() and path.resolve().is_relative_to(root):
                raise ValueError("Docker 工作区包含 Agent 的 config.json；请用 --workspace 指向独立项目")
        if "," in str(root):
            raise ValueError("Docker bind mount 暂不支持路径中的逗号")

    def prepare(self):
        self.validate()
        if shutil.which("docker") is None:
            raise ValueError("找不到 docker 命令；请先准备 Docker，再启用 --execution docker")
        try:
            result = subprocess.run(
                ["docker", "image", "inspect", "--format", "{{.Id}}", self.config.image],
                capture_output=True, text=True, timeout=10,
            )
        except subprocess.TimeoutExpired as error:
            raise ValueError("连接 Docker daemon 超时，请确认 Docker 已启动") from error
        if result.returncode:
            raise ValueError(f"Docker daemon 或镜像未就绪，请先启动 Docker 并构建 {self.config.image}")

    def command_args(self, command: str, cwd: Path, name: str) -> list[str]:
        self.validate()
        cwd = current_workdir(cwd)
        root = workspace_root()
        return [
            "docker", "run", "--rm", "--name", name, "--pull", "never", "--init",
            "--network", self.config.network, "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--pids-limit", str(self.config.pids_limit),
            "--cpus", str(self.config.cpus), "--memory", self.config.memory,
            "--user", f"{os.getuid()}:{os.getgid()}",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m,mode=1777",
            "--mount", f"type=bind,source={root},target={root}",
            "--workdir", str(cwd), "--env", "HOME=/tmp",
            "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", "GIT_TERMINAL_PROMPT=0",
            self.config.image, "/bin/bash", "-c", command,
        ]

    def execute(self, command: str, cwd: Path, *, output_path: Path | None = None,
                evidence: dict | None = None) -> tuple[str, int | None]:
        #每条命令一个容器；文件通过 bind mount 保留，后台进程不跨命令保留。
        check_cancelled()
        name = f"coding-agent-{uuid.uuid4().hex}"
        args = self.command_args(command, cwd, name)
        remove_cancel = None
        with _shell_process_lock:
            _containers.add(name)
        turn = current_turn()

        def cleanup_container():
            confirmed = _remove_container(name)
            if confirmed is False:
                if evidence is not None:
                    evidence["cleanup_confirmed"] = False
                if turn is not None:
                    turn.cleanup_errors.append("ContainerCleanupUnconfirmed")
            return confirmed

        try:
            if turn is not None:
                #docker CLI 和容器是两个资源；取消时必须按本次名称停止容器。
                remove_cancel = turn.add_cancel_callback(cleanup_container)
            check_cancelled()
            options = {"output_path": output_path, "evidence": evidence} if output_path is not None or evidence is not None else {}
            result = _run_process(args, current_workdir(cwd), self.config.timeout, shell=False, **options)
            check_cancelled()
            return result
        finally:
            if remove_cancel is not None:
                remove_cancel()
            #杀掉 docker CLI 不一定能停止容器，所以超时和退出还要按名称清理。
            #取消回调可能早于容器实际创建；收尾再次清理同名容器，操作可重复执行。
            cleanup_container()


@lru_cache(maxsize=1)
def get_environment():
    """配置只读一次；模型配置和沙箱配置分开，Docker 不接收宿主环境变量。"""
    path = Path(os.getenv("AGENT_SANDBOX_CONFIG", str(APP_DIR / "config" / "sandbox.json"))).expanduser()
    #wheel 不携带宿主配置；未配置时使用 ExecutionConfig 的同一套默认值。
    #源码配置或显式指定的文件若缺失仍报错，不能把拼错路径悄悄当成默认值。
    if path.exists() or SOURCE_LAYOUT or "AGENT_SANDBOX_CONFIG" in os.environ:
        with path.open(encoding="utf-8") as handle:
            values = json.load(handle)
    else:
        values = {}
    config = ExecutionConfig(**values)
    config.backend = os.getenv("AGENT_EXECUTION", config.backend)
    config.image = os.getenv("AGENT_DOCKER_IMAGE", config.image)
    if config.backend not in {"local", "docker"}:
        raise ValueError("Execution backend must be local or docker")
    if config.timeout <= 0 or config.cpus <= 0 or config.pids_limit <= 0:
        raise ValueError("Execution timeout/resource limits must be positive")
    if config.network not in {"none", "bridge"}:
        raise ValueError("Docker network must be none or bridge")
    return DockerEnvironment(config) if config.backend == "docker" else LocalEnvironment(config)


def run_bash_process(command: str, cwd: Path | None = None, *,
                     output_path: Path | None = None, evidence: dict | None = None) -> tuple[str, int | None]:
    if not isinstance(command, str) or not command.strip():
        return "Error: Bash command cannot be empty", None
    try:
        options = {"output_path": output_path, "evidence": evidence} if output_path is not None or evidence is not None else {}
        return get_environment().execute(command, current_workdir(cwd), **options)
    except (OSError, TypeError, ValueError) as error:
        return f"Error: {error}", None


def run_bash_evidence(command: str, cwd: Path | None = None, *, output_path: Path | None = None) -> dict:
    """前台和后台共用结构化执行证据；取消仍向上抛出，不能伪装成普通失败或成功。"""
    if output_path is None:
        output_path = current_workdir(cwd) / ".task_outputs" / "processes" / f"command_{uuid.uuid4().hex}.log"
    facts = {"command": command, "exit_code": None, "cancelled": False,
             "timed_out": False, "cleanup_confirmed": True,
             "full_output_path": str(output_path) if output_path is not None else None}
    try:
        output, code = run_bash_process(command, cwd, output_path=output_path, evidence=facts)
    except BaseException as error:
        #取消属于控制流；随异常附上已保存的证据，供管理器收尾而不是吞掉取消。
        facts["cancelled"] = isinstance(error, TurnCancelled) or facts["cancelled"]
        facts["status"] = "cancelled" if facts["cancelled"] else "failed"
        if facts["cancelled"]:
            facts["exit_code"] = None
        if not facts["cleanup_confirmed"]:
            facts["status"] = "cleanup_failed"
        error.execution_evidence = facts
        raise
    facts.update(output=output, exit_code=code)
    if not facts["cleanup_confirmed"]:
        facts["status"] = "cleanup_failed"
    elif facts["timed_out"]:
        facts["status"] = "timed_out"
    else:
        facts["status"] = "completed" if code == 0 else "failed"
    return facts
