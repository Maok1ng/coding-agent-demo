"""统一程序目录、目标工作区和一次工具调用的工作目录。"""

import argparse
import os
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path


#__file__ 指向本模块自身；程序目录不再随终端的 cd 改变。
#parents 是逐级向上的父目录序列；[2] 从 runtime/、coding_agent/ 回到项目根目录。
APP_DIR = Path(__file__).resolve().parents[2]
#源码/可编辑安装继续使用原项目目录；wheel 安装后不能把 site-packages 当工作区。
SOURCE_LAYOUT = (APP_DIR / "pyproject.toml").is_file() and (APP_DIR / "main.py").is_file()
if not SOURCE_LAYOUT:
    APP_DIR = Path.home() / ".coding-agent"
CONFIG_PATH = Path(os.getenv("AGENT_CONFIG", str(APP_DIR / "config.json"))).expanduser().resolve()
WORKDIR = Path(os.getenv("AGENT_WORKSPACE", str(APP_DIR if SOURCE_LAYOUT else Path.cwd()))).expanduser().resolve()
#ContextVar 为每个执行上下文保存宿主工作区和任务目录，不修改进程级 cwd。
#工作区只由 Application/Runner 绑定，模型提供的 cwd 只能选其内部目录。
_active_workspace = ContextVar("agent_workspace", default=None)
_active_workdir = ContextVar("agent_workdir", default=None)

#读取、写入和归档共用的单文件上限；同一来源避免各模块私自放宽。
MAX_FILE_BYTES = 2 * 1024 * 1024


def configure_cli():
    """只由入口调用，必须早于其他业务模块的导入。"""
    parser = argparse.ArgumentParser(description="Run the coding agent in a chosen workspace.")
    parser.add_argument("--workspace", type=Path, help="目标项目目录；源码运行默认程序目录，wheel 安装默认当前目录")
    parser.add_argument("--config", type=Path, help="模型配置；源码默认程序目录/config.json，wheel 默认 ~/.coding-agent/config.json")
    parser.add_argument("--mcp-config", type=Path, help="宿主 MCP 配置；默认是程序目录的 config/mcp.json")
    parser.add_argument("--models-config", type=Path, help="系统模型档案；默认 ~/.coding-agent/models.json")
    parser.add_argument("--models", nargs=argparse.REMAINDER, metavar="COMMAND",
                        help="管理系统模型档案后退出，无需启动模型；参数放在本选项之前")
    parser.add_argument("--execution", choices=("local", "docker"), help="覆盖 sandbox.json 的执行后端")
    parser.add_argument("--sandbox-config", type=Path, help="宿主沙箱配置；也可使用 AGENT_SANDBOX_CONFIG")
    parser.add_argument("--memory-home", type=Path, help="用户规则与长期记忆目录；默认 ~/.coding-agent")
    parser.add_argument("--approval-mode", choices=("ask", "auto", "full"),
                        help="逐次确认 / 规则自动审批 / 完全自动；默认 auto")
    parser.add_argument("--no-stream", action="store_true", help="关闭模型文字的实时流式展示")
    sessions = parser.add_mutually_exclusive_group()
    #nargs='?' 允许 -r 后不带 ID；const='' 与未传参数时的 None 区分。
    sessions.add_argument("-r", "--resume", nargs="?", const="", metavar="SESSION_ID", help="选择历史会话，或恢复指定 ID/唯一前缀")
    sessions.add_argument("-c", "--continue", dest="continue_session", action="store_true", help="恢复当前工作区最近的会话")
    sessions.add_argument("--list-sessions", action="store_true", help="只列出当前工作区历史会话，不启动模型")
    args = parser.parse_args()
    global WORKDIR, CONFIG_PATH
    if args.workspace is not None:
        WORKDIR = args.workspace.expanduser().resolve()
    if args.config is not None:
        CONFIG_PATH = args.config.expanduser().resolve()
    if args.mcp_config is not None:
        os.environ["AGENT_MCP_CONFIG"] = str(args.mcp_config.expanduser().resolve())
    if args.models_config is not None:
        os.environ["AGENT_MODELS_CONFIG"] = os.path.abspath(args.models_config.expanduser())
    if args.execution is not None:
        os.environ["AGENT_EXECUTION"] = args.execution
    if args.sandbox_config is not None:
        os.environ["AGENT_SANDBOX_CONFIG"] = str(args.sandbox_config.expanduser().resolve())
    if args.memory_home is not None:
        os.environ["AGENT_MEMORY_HOME"] = os.path.abspath(args.memory_home.expanduser())
    if args.approval_mode is not None:
        os.environ["AGENT_APPROVAL_MODE"] = args.approval_mode
    if not WORKDIR.is_dir():
        parser.error(f"工作目录不存在或不是目录: {WORKDIR}")
    return args


def workspace_root() -> Path:
    """返回宿主绑定的边界；CLI 和旧调用者未绑定时仍使用 WORKDIR。"""
    return Path(_active_workspace.get() or WORKDIR).resolve()


@contextmanager
def use_workspace(root: Path):
    """由宿主绑定一次运行的工作区；嵌套调用退出后恢复原边界和目录。"""
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Workspace does not exist: {root}")
    root_token = _active_workspace.set(root)
    #切换宿主边界时不能继承另一个 Application 遗留的子目录。
    cwd_token = _active_workdir.set(None)
    try:
        yield
    finally:
        _active_workdir.reset(cwd_token)
        _active_workspace.reset(root_token)


def current_workdir(cwd: Path | None = None) -> Path:
    """返回显式目录、当前子任务目录或默认工作区，并检查目录边界。"""
    root = workspace_root()
    base = Path(cwd if cwd is not None else (_active_workdir.get() or root)).resolve()
    if not base.is_relative_to(root):
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
