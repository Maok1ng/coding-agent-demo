"""三级操作审批；只决定是否询问人类，不能授予角色能力或解除 Plan。"""

from dataclasses import dataclass
from pathlib import Path
import shlex

APPROVAL_MODES = {"ask", "auto", "full"}
APPROVAL_LABELS = {
    "ask": "逐次确认：每次工具调用询问你（直接提问除外）",
    "auto": "规则审批：常规操作自动通过，高风险或不确定操作询问你",
    "full": "完全自动：跳过操作确认；本机 Shell 可影响工作区外，角色、Plan、文件工具边界及硬性拒绝仍有效",
}


#1.永远拒绝的操作
DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/", "mkfs", "dd if="]


def is_dangerous_command(command: str) -> str | None:
    for pattern in DENY_LIST:
        if pattern in command:
            return f"Error: Dangerous command blocked: {pattern}"
    return None


def validate_approval_mode(value):
    if not isinstance(value, str) or value not in APPROVAL_MODES:
        raise ValueError("审批模式必须是 ask、auto 或 full")
    return value


@dataclass(frozen=True)
class ApprovalDecision:
    action: str  #allow / confirm / deny；这是宿主策略结果，不由模型填写。
    reason: str


def _routine_shell(command: str, workspace: Path) -> bool:
    """只自动批准有限的项目检查命令；不把任意 Python、Shell 或脚本当成安全命令。"""
    #不尝试用 shlex 解析完整 Shell 语言。重定向、替换、连接命令等一律交还用户确认。
    if any(char in command for char in ";|&<>`$\n\r\\(){}"):
        return False
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    if not words:
        return False
    if any(word.split("=", 1)[0] in {"--basetemp", "-exec", "-toolexec", "--registry", "--prefix"}
           for word in words):
        return False
    #禁止把工作区外路径、环境赋值或 ~ 路径夹在常规命令里自动批准。
    for word in words:
        value = word.split("=", 1)[-1]
        if value.startswith("~") or ".." in Path(value).parts:
            return False
        if Path(value).is_absolute() and not Path(value).resolve().is_relative_to(workspace):
            return False
    if words == ["pwd"]:
        return True
    prefixes = (
        ("pytest",), ("python", "-m", "pytest"), ("python3", "-m", "pytest"),
        ("python", "-m", "unittest"), ("python3", "-m", "unittest"),
        ("ruff", "check"), ("python", "-m", "ruff", "check"),
        ("python3", "-m", "ruff", "check"),
        ("npm", "test"), ("npm", "run", "test"), ("npm", "run", "build"),
        ("pnpm", "test"), ("pnpm", "run", "test"), ("pnpm", "build"),
        ("cargo", "test"), ("cargo", "check"), ("go", "test"),
        ("make", "test"), ("make", "check"), ("make", "lint"),
    )
    #测试/构建本身会运行仓库代码；“常规”是审批便利，不是无副作用保证或 OS 沙箱。
    #make 的额外 target 可以执行发布等动作，不能只匹配开头的 make test。
    if words[0] == "make":
        return tuple(words) in prefixes
    return any(tuple(words[:len(prefix)]) == prefix for prefix in prefixes)


def _sensitive_path(path: str, workspace: Path) -> bool:
    target = (workspace / path).resolve()
    if not target.is_relative_to(workspace):
        return True
    parts = target.relative_to(workspace).parts
    return (any(part in {".git", ".github", ".codex", ".agents", ".sessions", ".memory", ".task_outputs", ".transcripts"} for part in parts)
            or target.name in {"AGENTS.md", "CLAUDE.md", "config.json", "mcp.json", "sandbox.json"}
            or target.name.startswith(".env")
            or target.suffix.lower() in {".pem", ".key", ".p12", ".pfx"})


def approval_decision(mode, name, arguments, workspace, *, mcp_metadata=None, mcp_launch=False):
    """权限检查和参数校验通过后调用；未知操作默认确认，不信任工具文本中的批准。"""
    validate_approval_mode(mode)
    if name == "bash" and (reason := is_dangerous_command(arguments["command"])):
        return ApprovalDecision("deny", reason)
    #full 跳过人类审批，不跳过工具白名单、角色限制、路径检查和上面的硬性拒绝。
    if mode == "full":
        return ApprovalDecision("allow", "用户选择 full，跳过本次操作确认")
    if name == "ask_user":
        return ApprovalDecision("allow", "直接向用户提问，不再先询问是否允许提问")
    if mode == "ask":
        return ApprovalDecision("confirm", "ask 模式：本次工具调用需要你同意")
    if name == "connect_mcp":
        return ApprovalDecision("confirm" if mcp_launch else "allow",
                                "启动宿主上的 MCP 外部程序" if mcp_launch else "连接宿主已配置的远程 MCP")
    if name.startswith("mcp__"):
        automatic = (mcp_metadata or {}).get("policy") == "allow"
        return ApprovalDecision("allow" if automatic else "confirm",
                                "宿主 MCP 白名单已批准" if automatic else "外部 MCP 操作需要确认")
    if name == "bash":
        routine = _routine_shell(arguments["command"], workspace)
        return ApprovalDecision("allow" if routine else "confirm",
                                "常规项目检查命令（仍可能执行仓库代码）" if routine else
                                "Shell 操作可能修改 Git、远端或系统；不在自动批准范围")
    if name in {"write_file", "edit_file"}:
        sensitive = _sensitive_path(arguments["path"], workspace)
        return ApprovalDecision("confirm" if sensitive else "allow",
                                "修改敏感文件、规则或运行记录" if sensitive else "当前工作区内的常规文件修改")
    if name in {"read_file", "read_files", "list_files", "glob", "search_code", "search_many", "git_status", "git_diff", "load_skill",
                "read_url", "list_symbols", "find_symbol", "verification_status",
                "dispatch_agent", "report_result", "todo_write", "compact", "wait_process", "process_status"}:
        return ApprovalDecision("allow", "常规读取、调度或本轮状态管理")
    return ApprovalDecision("confirm", "操作未被规则分类，需要你确认")
