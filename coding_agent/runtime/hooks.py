from coding_agent.runtime.workspace import WORKDIR

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

def trigger_hook(hook_name:str,*args, skip_permission: bool = False):#如果写成 **args，通常对应的是关键字参数字典 例如func(block=block, tool_name="bash")
    if hook_name not in HOOKS:
        raise ValueError(f"Unknown hook: {hook_name}")
    for func in HOOKS[hook_name]:
        #队友已执行非交互式权限检查，仍复用日志等其他 hook，避免再次询问用户。
        if skip_permission and func is permission_check_hook:
            continue
        result = func(*args)
        if result is not None:
            return result #返回值不为空 中止
    return None

def context_inject_hook(query:str)->str|None:
    print(f"[HOOKS] triggered before UserPromptSubmit: {WORKDIR}")
    return None

def permission_check_hook(block)->str|None:
    from coding_agent.runtime.permissions import check_permission
    result = check_permission(block)
    if result is False:
        return f"Error: Permission denied for {block.name}"
    else:
        return None

def log_hook(block):
    print(f"[HOOKS] triggered before tool use: {block.name} with input {block.input}")

def large_output_hook(block, output:str)->str|None:
    if len(str(output)) > 1000:
        print(f"[HOOKS] triggered after tool use: {block.name} output is too large ({len(str(output))} bytes)")

def summary_hook(messages:list)->str|None:
    tools_count = sum(
        1
        for m in messages
        for b in (m.get("content", []) if isinstance(m.get("content", []), list) else [])
        if (b.get("type") if isinstance(b, dict) else getattr(b, "type", None)) == "tool_use"
    )
    print(f"[HOOKS] triggered after agent loop: {tools_count} tool uses in history")
    return None

register_hook("UserPromptSubmit", context_inject_hook)
register_hook("PreToolUse", permission_check_hook)
register_hook("PreToolUse", log_hook)
register_hook("PostToolUse", large_output_hook)
register_hook("Stop", summary_hook)
