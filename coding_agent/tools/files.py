from copy import deepcopy
from pathlib import Path

from coding_agent.tools.skills import SKILL_LOADER
from coding_agent.runtime.environment import format_bash_result, run_bash_process
from coding_agent.runtime.permissions import is_dangerous_command
from coding_agent.runtime.workspace import current_workdir, safe_path as workspace_safe_path

#主 Agent、子 Agent 和队友共用工具 schema；只有主 Agent 支持异步 Bash 分发。
BASE_TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in a file once; the match must be unique.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "glob", "description": "Find files matching a glob pattern; ** matches recursively.",
     "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}},
]


def base_tools(background: bool = False) -> list[dict]:
    #deepcopy 复制嵌套字典，添加主 Agent 专用参数时不会污染队友的 schema。
    tools = deepcopy(BASE_TOOLS)
    if background:
        tools[0]["input_schema"]["properties"]["run_in_background"] = {"type": "boolean"}
    return tools


LOAD_SKILL_TOOL = {
    "name": "load_skill", "description": "Load the full instructions for a skill by name.",
    "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
}

def run_load_skill(name: str) -> str:
    return SKILL_LOADER.load(name)

def run_bash(command: str, cwd: Path | None = None) -> str:
    if reason := is_dangerous_command(command):
        return reason
    return format_bash_result(*run_bash_process(command, cwd))

def safe_path(p: str, cwd: Path | None = None) -> Path:
    #pathlib.Path 专门重载了 /，让你可以方便地拼接路径。
    return workspace_safe_path(p, cwd) #抛出异常

def run_read(path: str, limit: int | None = None, cwd: Path | None = None) -> str: #limit 可以是 int，也可以是 None
    try:
        lines = safe_path(path, cwd).read_text(encoding="utf-8").splitlines() #读取文件内容，并按行分割成列表
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:  
        return f"Error: {e}"

def run_write(path: str, content: str, cwd: Path | None = None) -> str:
    try:
        file_path = safe_path(path, cwd) #这里可能会抛出异常
        file_path.parent.mkdir(parents=True, exist_ok=True) #创建父目录 exist_ok=True，如果目录已经存在，不要报错。
        # parents=True  如果父目录不存在，则创建父目录
        file_path.write_text(content, encoding="utf-8") #写入文件内容
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"

def run_edit(path: str, old_text: str, new_text: str, cwd: Path | None = None) -> str:
    try:
        file_path = safe_path(path, cwd)
        text = file_path.read_text(encoding="utf-8")
        count = text.count(old_text)
        if not old_text or count != 1:
            return f"Error: Expected 1 non-empty occurrence, found {count}"
        file_path.write_text(text.replace(old_text, new_text, 1), encoding="utf-8") #只替换第一个匹配的字符串
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"

def run_glob(pattern: str, cwd: Path | None = None) -> str:
    import glob as g
    try:
        base = current_workdir(cwd)
        paths = g.glob(str(base / pattern), recursive=True) #recursive=True 允许使用 ** 搜索子目录
        matches = sorted(str(Path(p).relative_to(base)) for p in paths
                         if Path(p).resolve().is_relative_to(base))
        shown = matches[:200]
        if len(matches) > 200:
            shown.append("... (more matches omitted; narrow the pattern)")
        return "\n".join(shown) or "No files found"
    except Exception as e:
        return f"Error: {e}"

def build_system_prompt() -> str:
    return (
        f"You are a coding agent at {current_workdir()}. Use tools to solve tasks. "
        "Act, don't explain.\n\n"
        f"Skills available:\n{SKILL_LOADER.catalog()}\n\n"
        "Use load_skill to read the full instructions when a skill applies."
    )
