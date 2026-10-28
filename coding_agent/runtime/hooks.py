"""轻量生命周期扩展；日志走 logging，核心不直接依赖终端。"""

import logging

logger = logging.getLogger(__name__)

HOOKS = {
    "UserPromptSubmit":[],
    "PreToolUse": [],
    "PostToolUse": [],
    "Stop": [],
}
def register_hook(hook_name: str, func):
    if hook_name not in HOOKS:
        raise ValueError(f"Unknown hook: {hook_name}")
    HOOKS[hook_name].append(func)

def trigger_hook(hook_name:str,*args):#如果写成 **args，通常对应的是关键字参数字典 例如func(block=block, tool_name="bash")
    if hook_name not in HOOKS:
        raise ValueError(f"Unknown hook: {hook_name}")
    for func in HOOKS[hook_name]:
        result = func(*args)
        if result is not None:
            return result #返回值不为空 中止
    return None

def context_inject_hook(query:str)->str|None:
    logger.debug("UserPromptSubmit")
    return None

def log_hook(block):
    #不要默认输出工具参数，避免用户代码或 MCP 参数出现在终端日志中。
    logger.debug("PreToolUse: %s", block.name)

def large_output_hook(block, output:str)->str|None:
    if len(str(output)) > 1000:
        logger.debug("PostToolUse: %s, large output (%d characters)", block.name, len(str(output)))

def summary_hook(messages:list)->str|None:
    tools_count = sum(
        1
        for m in messages
        for b in (m.get("content", []) if isinstance(m.get("content", []), list) else [])
        if (b.get("type") if isinstance(b, dict) else getattr(b, "type", None)) == "tool_use"
    )
    logger.debug("Stop: %d tool uses in history", tools_count)
    return None

register_hook("UserPromptSubmit", context_inject_hook)
#权限由 ToolRuntime 强制执行；Hook 不能代替权限检查，也不能重复读取终端确认。
register_hook("PreToolUse", log_hook)
register_hook("PostToolUse", large_output_hook)
register_hook("Stop", summary_hook)
