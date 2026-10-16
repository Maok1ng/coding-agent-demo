import re
import threading
from coding_agent.integrations.mcp import get_mcp_policy
from coding_agent.runtime.workspace import current_workdir
#三道闸门 
#1.永远拒绝的操作
DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/","mkfs","dd if="]

def is_dangerous_command(command: str) -> str | None:
    for pattern in DENY_LIST:
        if pattern in command:
            return f"Error: Dangerous command blocked: {pattern}"
    return None

#2.用户进行判断的操作

DESTRUCTIVE_COMMAND_WORD = re.compile(
    r"(?i)(?:^|[;&|()\n])\s*(?:rm|del)(?=\s|$|[;&|()])"
)#正则表达式匹配del rm指令

def contains_destructive_command(command: str) -> bool:
    return bool(DESTRUCTIVE_COMMAND_WORD.search(command))

PERMISSION_RULES = [
    {
        "tools": ["read_file", "write_file", "edit_file"],
        "check": lambda args: not (current_workdir() / args.get("path", "")).resolve().is_relative_to(current_workdir()),
        "message": "Access outside workspace",
    },
    {
        "tools": ["bash"],
        "check": lambda args: contains_destructive_command(args.get("command", "")) or any(
            kw in args.get("command", "") for kw in ["rm ", "> /etc/", "chmod 777"]
        ),
        "message": "Potentially destructive command",##可能有危险的指令
    },
]

def check_rules(tool_name:str,args:dict)->str|None:
        #MCP 工具只信任宿主侧策略；未配置的外部工具默认要求确认。
        if tool_name.startswith("mcp__") and get_mcp_policy(tool_name) != "allow":
            return "Notice: External MCP tool requires confirmation"
        for pattern in PERMISSION_RULES:
            if tool_name in pattern["tools"] and pattern["check"](args):
                return f"Notice: {pattern['message']}"
        return None

#等待用户确认的操作
def ask_user_confirmation(tool_name:str,args:dict,reason:str)->str:
    #定时任务在后台线程执行，不能和主线程同时读取用户输入。
    if threading.current_thread() is not threading.main_thread():
        return "deny"
    print(f"{reason}. ")
    print(f"Tool: {tool_name}, Arguments: {args}")
    choice = input("Do you want to proceed? (y/n): ").strip().lower()
    return "allow" if choice in ("y", "yes") else "deny"

def check_permission(block, *, prompt_user: bool = True)->bool:
    if block.name == "bash":
        reason = is_dangerous_command(block.input.get("command", ""))#没有command以空字符串代替
        if reason:
            return False
    reason = check_rules(block.name, block.input)
    if reason:
        if not prompt_user:
            return False
        user_decision = ask_user_confirmation(block.name, block.input, reason)
        return user_decision == "allow"
    return True
     
