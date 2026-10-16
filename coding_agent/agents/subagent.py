from pathlib import Path

from coding_agent.runtime.hooks import trigger_hook
from coding_agent.runtime.workspace import use_workdir
from coding_agent.tools.files import (
    BASE_TOOLS,
    LOAD_SKILL_TOOL,
    build_system_prompt,
    run_bash,
    run_edit,
    run_glob,
    run_load_skill,
    run_read,
    run_write,
)

#完整提示词在调用时组装，才能带上本次继承的 Task/worktree 目录。
SUB_SYSTEM = "Complete the given task, then return a concise final answer."
SUB_HANDLERS = {
    "bash": run_bash,
    "read_file": run_read,
    "write_file": run_write,
    "edit_file": run_edit,
    "glob": run_glob,
    "load_skill": run_load_skill,
}

#子 Agent 复用宿主客户端，不重复读取配置或创建连接池。
client = None
MODEL = None
SUB_TOOLS = [*BASE_TOOLS, LOAD_SKILL_TOOL]


def configure_subagent_runtime(llm_client, model: str):
    global client, MODEL
    client = llm_client
    MODEL = model

def extract_text(content) -> str:
    return "\n".join(
        getattr(block, "text", "")
        for block in content
        if getattr(block, "type", None) == "text"
    )

def execute_tool(block, handlers: dict) -> str:
    blocked = trigger_hook("PreToolUse", block)
    if blocked:
        return str(blocked)

    try:
        handle = handlers.get(block.name)
        output = handle(**block.input) if handle else f"Unknown: {block.name}"
        trigger_hook("PostToolUse", block, output)
    except Exception as e:  # noqa: BLE001
        output = f"Error: {e}"
    return str(output)

def run_subagent(prompt: str, cwd: Path | None = None) -> str:
    #一次性子 Agent 继承 Lead 当前的 Task/worktree，不修改进程级工作目录。
    with use_workdir(cwd):
        return _run_subagent(prompt)


def _run_subagent(prompt: str) -> str:
    print("\n\033[35m[Subagent started]\033[0m")
    messages = [{"role": "user", "content": prompt}]

    for _ in range(30):
        response = client.messages.create(
            model=MODEL,
            system=build_system_prompt() + "\n\n" + SUB_SYSTEM,
            messages=messages,
            tools=SUB_TOOLS,
            max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})

        tool_calls = [
            block for block in response.content if block.type == "tool_use"
        ]
        if not tool_calls:
            force = trigger_hook("Stop", messages)
            if force:
                messages.append({"role": "user", "content": force})
                continue
            print("\033[35m[Subagent done]\033[0m") #在终端中输出紫色的 [Subagent done]
            return extract_text(response.content) or "(no summary)"

        results = []
        for block in tool_calls:
            output = execute_tool(block, SUB_HANDLERS)
            print(f"  \033[90m[sub] {block.name}: {output[:100]}\033[0m") #在终端中输出紫色的工具名字
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": output,
            })
        messages.append({"role": "user", "content": results})

    print("\033[35m[Subagent stopped]\033[0m")
    return "Subagent stopped after 30 turns without a final answer."
