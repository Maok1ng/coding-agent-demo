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

from coding_agent.runtime.workspace import APP_DIR, CONFIG_PATH, WORKDIR, current_workdir


_shell_processes: set[subprocess.Popen] = set()
_shell_process_lock = threading.RLock()
_containers: set[str] = set()


def _stop_process_group(process: subprocess.Popen):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except (ProcessLookupError, OSError):
            return
        time.sleep(0.05)


def _remove_container(name: str):
    try:
        subprocess.run(["docker", "rm", "--force", name], capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass
    with _shell_process_lock:
        _containers.discard(name)


def _stop_all_shell_processes():
    with _shell_process_lock:
        processes = list(_shell_processes)
        containers = list(_containers)
    for process in processes:
        _stop_process_group(process)
    for name in containers:
        _remove_container(name)


def _handle_termination_signal(signum, _frame):
    _stop_all_shell_processes()
    raise SystemExit(128 + signum)


atexit.register(_stop_all_shell_processes)
if threading.current_thread() is threading.main_thread():
    signal.signal(signal.SIGTERM, _handle_termination_signal)


def _run_process(command, cwd: Path, timeout: int, *, shell: bool) -> tuple[str, int | None]:
    """本机和 docker CLI 共用超时、进程组回收、输出截断规则。"""
    process = None
    try:
        process = subprocess.Popen(
            command, shell=shell, cwd=cwd, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, errors="replace", start_new_session=True,
        )
        with _shell_process_lock:
            _shell_processes.add(process)
        output, _ = process.communicate(timeout=timeout)
        return (output.strip()[:50000] or "(no output)"), process.returncode
    except subprocess.TimeoutExpired:
        return f"Error: Timeout ({timeout}s)", None
    except OSError as error:
        return f"Error: {type(error).__name__}: {error}", None
    finally:
        if process is not None:
            _stop_process_group(process)
            try:
                process.communicate(timeout=0.2)
            except subprocess.TimeoutExpired:
                pass
            with _shell_process_lock:
                _shell_processes.discard(process)


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

    def execute(self, command: str, cwd: Path) -> tuple[str, int | None]:
        return _run_process(command, current_workdir(cwd), self.config.timeout, shell=True)


class DockerEnvironment(LocalEnvironment):
    def validate(self):
        super().validate()
        #绑定整个工作区，任务 worktree 的 .git 指针仍可访问同一工作区内的主仓库。
        #配置中的 API key 留在宿主；工作区包含本程序配置时拒绝启动，而不是泄露给容器。
        for path in (CONFIG_PATH, APP_DIR / "config.json"):
            if path.is_file() and path.resolve().is_relative_to(WORKDIR):
                raise ValueError("Docker 工作区包含 Agent 的 config.json；请用 --workspace 指向独立项目")
        if "," in str(WORKDIR):
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
        return [
            "docker", "run", "--rm", "--name", name, "--pull", "never", "--init",
            "--network", self.config.network, "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--pids-limit", str(self.config.pids_limit),
            "--cpus", str(self.config.cpus), "--memory", self.config.memory,
            "--user", f"{os.getuid()}:{os.getgid()}",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m,mode=1777",
            "--mount", f"type=bind,source={WORKDIR},target={WORKDIR}",
            "--workdir", str(cwd), "--env", "HOME=/tmp",
            "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", "GIT_TERMINAL_PROMPT=0",
            self.config.image, "/bin/bash", "-c", command,
        ]

    def execute(self, command: str, cwd: Path) -> tuple[str, int | None]:
        #每条命令一个容器；文件通过 bind mount 保留，后台进程不跨命令保留。
        name = f"coding-agent-{uuid.uuid4().hex}"
        args = self.command_args(command, cwd, name)
        with _shell_process_lock:
            _containers.add(name)
        try:
            return _run_process(args, current_workdir(cwd), self.config.timeout, shell=False)
        finally:
            #杀掉 docker CLI 不一定能停止容器，所以超时和退出还要按名称清理。
            _remove_container(name)


@lru_cache(maxsize=1)
def get_environment():
    """配置只读一次；模型配置和沙箱配置分开，Docker 不接收宿主环境变量。"""
    path = APP_DIR / "config" / "sandbox.json"
    with path.open(encoding="utf-8") as handle:
        values = json.load(handle)
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


def run_bash_process(command: str, cwd: Path | None = None) -> tuple[str, int | None]:
    if not isinstance(command, str) or not command.strip():
        return "Error: Bash command cannot be empty", None
    try:
        return get_environment().execute(command, current_workdir(cwd))
    except (OSError, TypeError, ValueError) as error:
        return f"Error: {error}", None


def format_bash_result(output: str, exit_code: int | None) -> str:
    return output if exit_code in (0, None) else f"Error: command exited with status {exit_code}\n{output}"
