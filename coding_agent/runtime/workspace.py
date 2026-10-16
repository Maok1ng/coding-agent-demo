"""统一程序目录、目标工作区和一次工具调用的工作目录。"""

import argparse
import os
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path


#__file__ 指向本模块自身；程序目录不再随终端的 cd 改变。
#parents 是逐级向上的父目录序列；[2] 从 runtime/、coding_agent/ 回到项目根目录。
APP_DIR = Path(__file__).resolve().parents[2]
CONFIG_PATH = Path(os.getenv("AGENT_CONFIG", str(APP_DIR / "config.json"))).expanduser().resolve()
WORKDIR = Path(os.getenv("AGENT_WORKSPACE", str(APP_DIR))).expanduser().resolve()
#ContextVar 为每个执行上下文保存目录，避免并发队友互相修改进程级 cwd。
_active_workdir = ContextVar("agent_workdir", default=None)


def configure_cli():
    """只由入口调用，必须早于其他业务模块的导入。"""
    parser = argparse.ArgumentParser(description="Run the coding agent in a chosen workspace.")
    parser.add_argument("--workspace", type=Path, help="目标项目目录；默认是 Agent 程序目录")
    parser.add_argument("--config", type=Path, help="模型配置文件；默认是程序目录的 config.json")
    parser.add_argument("--execution", choices=("local", "docker"), help="覆盖 sandbox.json 的执行后端")
    args = parser.parse_args()
    global WORKDIR, CONFIG_PATH
    if args.workspace is not None:
        WORKDIR = args.workspace.expanduser().resolve()
    if args.config is not None:
        CONFIG_PATH = args.config.expanduser().resolve()
    if args.execution is not None:
        os.environ["AGENT_EXECUTION"] = args.execution
    if not WORKDIR.is_dir():
        parser.error(f"工作目录不存在或不是目录: {WORKDIR}")


def current_workdir(cwd: Path | None = None) -> Path:
    """返回显式目录、当前子任务目录或默认工作区，并检查目录边界。"""
    base = Path(cwd if cwd is not None else (_active_workdir.get() or WORKDIR)).resolve()
    if not base.is_relative_to(WORKDIR):
        raise ValueError(f"Working directory escapes workspace: {base}")
    if not base.is_dir():
        raise ValueError(f"Working directory does not exist: {base}")
    return base


def safe_path(path: str, cwd: Path | None = None) -> Path:
    """文件路径以当前任务目录为基准；resolve 同时检查 .. 和符号链接越界。"""
    base = current_workdir(cwd)
    resolved = (base / Path(path).expanduser()).resolve()
    if not resolved.is_relative_to(base):
        raise ValueError(f"Path escapes workspace: {path}")
    return resolved


@contextmanager
def use_workdir(cwd: Path | None = None):
    """子 Agent 临时继承调用者目录；finally 即使遇到异常也会恢复原上下文。"""
    token = _active_workdir.set(current_workdir(cwd))
    try:
        yield
    finally:
        _active_workdir.reset(token)
