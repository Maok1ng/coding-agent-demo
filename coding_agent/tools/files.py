from copy import deepcopy
from dataclasses import dataclass
import difflib
from fnmatch import fnmatchcase
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from coding_agent.tools.skills import SkillLoader
from coding_agent.runtime.environment import stop_process_group
from coding_agent.runtime.project_files import ProjectFiles
from coding_agent.runtime.turn import check_cancelled, current_turn
from coding_agent.runtime.workspace import MAX_FILE_BYTES, current_workdir, safe_path as workspace_safe_path
from coding_agent.runtime.working_set import BatchReadOutput, ReadOutput

#所有角色共享工具 schema；ToolRuntime 根据可信角色与模式过滤实际能力。
BASE_TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read numbered file lines; line ranges are 1-based and inclusive. Read an existing file before changing it.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer", "minimum": 1}, "start_line": {"type": "integer", "minimum": 1}, "end_line": {"type": "integer", "minimum": 1}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in a file once; the match must be unique.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "glob", "description": "Find files matching a glob pattern; ** matches recursively.",
     "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}},
    {"name": "list_files", "description": "Inspect a bounded workspace directory tree before choosing candidate files. Shares ignore rules with glob and search_code; narrow the path when truncated.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "max_depth": {"type": "integer", "minimum": 1, "maximum": 10}, "max_entries": {"type": "integer", "minimum": 1, "maximum": 1000}}, "additionalProperties": False}},
    {"name": "search_code", "description": "Search literal text in workspace source files, returning paths, line numbers and nearby lines.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string", "minLength": 1}, "path": {"type": "string"}, "context_lines": {"type": "integer", "minimum": 0, "maximum": 5}, "max_results": {"type": "integer", "minimum": 1, "maximum": 200}}, "required": ["query"]}},
    {"name": "git_status", "description": "Show read-only Git status limited to the current workspace.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "git_diff", "description": "Show a read-only working-tree or staged Git diff, optionally limited to a workspace path.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "staged": {"type": "boolean"}}}},
]


def base_tools(background: bool = False) -> list[dict]:
    #deepcopy 复制嵌套字典，添加后台参数时不会污染共享 schema。
    tools = deepcopy(BASE_TOOLS)
    if background:
        tools[0]["input_schema"]["properties"]["run_in_background"] = {"type": "boolean"}
    return tools


LOAD_SKILL_TOOL = {
    "name": "load_skill", "description": "Load the full instructions for a skill by name.",
    "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
}


#工具返回给模型的是有界摘要；完整 Shell 输出由运行时另外落盘，不能混为一谈。
MAX_OUTPUT_CHARS = 24000
MAX_READ_LINES = 500
MAX_BATCH_ITEMS = 12


#批量操作仍是确定性的本地工具；没有额外的 Agent 循环，也不扩大读取和搜索权限。
BASE_TOOLS.extend([
    {"name": "read_files", "description": "Read up to 12 selected files or numbered ranges together. Duplicate ranges are removed; all results share a 24000-character budget. Narrow ranges when truncated.",
     "input_schema": {"type": "object", "properties": {"files": {
         "type": "array", "minItems": 1, "maxItems": MAX_BATCH_ITEMS, "items": {
             "type": "object", "properties": {
                 "path": {"type": "string", "minLength": 1, "maxLength": 1000},
                 "limit": {"type": "integer", "minimum": 1},
                 "start_line": {"type": "integer", "minimum": 1},
                 "end_line": {"type": "integer", "minimum": 1},
             }, "required": ["path"], "additionalProperties": False,
         }}}, "required": ["files"], "additionalProperties": False}},
    {"name": "search_many", "description": "Run up to 12 literal code searches together using the shared workspace file tree. Duplicate queries are removed; all results share a 24000-character budget.",
     "input_schema": {"type": "object", "properties": {"queries": {
         "type": "array", "minItems": 1, "maxItems": MAX_BATCH_ITEMS, "items": {
             "type": "object", "properties": {
                 "query": {"type": "string", "minLength": 1, "maxLength": 4096},
                 "path": {"type": "string", "minLength": 1, "maxLength": 1000},
                 "context_lines": {"type": "integer", "minimum": 0, "maximum": 5},
                 "max_results": {"type": "integer", "minimum": 1, "maximum": 200},
             }, "required": ["query"], "additionalProperties": False,
         }}}, "required": ["queries"], "additionalProperties": False}},
])


def _batch_items(items, *, search=False):
    """先校验整批结构，再开始读取；直接调用 Python 方法也不能绕过工具 schema。"""
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_BATCH_ITEMS:
        raise ValueError(f"batch must contain between 1 and {MAX_BATCH_ITEMS} items")
    normalized, seen = [], set()
    for item in items:
        check_cancelled()
        allowed = ({"query", "path", "context_lines", "max_results"} if search else
                   {"path", "start_line", "end_line", "limit"})
        required = "query" if search else "path"
        if not isinstance(item, dict) or set(item) - allowed or required not in item:
            raise ValueError("invalid batch item arguments")
        path = item.get("path", ".")
        if not isinstance(path, str) or not path or len(path) > 1000 or "\x00" in path:
            raise ValueError("path must be non-empty text of at most 1000 characters")
        #只折叠词法上的 ./，不 resolve 符号链接；搜索器仍须识别原路径中的链接。
        path = Path(path).as_posix()
        if search:
            query = item["query"]
            if not isinstance(query, str) or not query or len(query) > 4096 or "\n" in query:
                raise ValueError("query must be non-empty single-line literal text of at most 4096 characters")
            options = {"query": query, "path": path,
                       "context_lines": _integer(item.get("context_lines", 2), "context_lines", 0, 5),
                       "max_results": _integer(item.get("max_results", 100), "max_results", 1, 200)}
        else:
            start = _integer(item.get("start_line", 1), "start_line", 1)
            end = _integer(item["end_line"], "end_line", 1) if "end_line" in item else None
            limit = _integer(item.get("limit", MAX_READ_LINES), "limit", 1)
            if end is not None and end < start:
                raise ValueError("end_line must not be smaller than start_line")
            #统一默认值和有效上限，相同读取不因显式写出默认参数而重复执行。
            options = {"path": path, "start_line": start, "limit": min(limit, MAX_READ_LINES)}
            if end is not None:
                options["end_line"] = end
        key = tuple(sorted(options.items()))
        if key not in seen:
            seen.add(key)
            normalized.append(options)
    return normalized


def _batch_piece(header: str, output: str, limit: int) -> tuple[str, str]:
    """标题、正文、截断说明共用预算；返回的正文也正是可登记的读取片段。"""
    prefix = f"--- {header} ---\n"
    available = max(0, limit - len(prefix))
    notice = "\n... (batch output truncated; request a smaller range)"
    visible = output
    if len(visible) > available:
        visible = visible[:max(0, available - len(notice))] + notice[:available]
    return prefix + visible, visible


def _bounded(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) > limit:
        return text[:limit] + "\n... (truncated; narrow the query or read a smaller range)"
    return text


def _integer(value: int, name: str, minimum: int, maximum: int | None = None) -> int:
    #bool 是 int 的子类，所以这里用 type(...) is int 拒绝 true/false 充当行号。
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum or 'unbounded'}")
    return value


def _indentation_variants(text: str):
    """模型复制代码时常丢失统一缩进；按空格 1-12 层、tab 1-6 层逐级尝试整体缩进。"""
    lines = text.split("\n")
    for unit, maximum in ((" ", 12), ("\t", 6)):
        for level in range(1, maximum + 1):
            prefix = unit * level
            yield prefix, "\n".join(f"{prefix}{line}" if line else line for line in lines)


@dataclass(frozen=True)
class FileChange:
    """宿主生成的不可变修改候选；批准绑定到路径、原版本和这份具体内容。"""

    path: str
    file_path: Path
    content: str
    expected: str | None
    diff: str
    tool: str


class FileTools:
    """一次运行共享的文件工具：工作区边界、读取版本和修改前置条件集中处理。"""

    def __init__(self, cwd: Path, versions: dict | None = None, *, skill_loader=None,
                 project_files=None):
        self.cwd = current_workdir(cwd)
        #用 is not None，避免调用者传入的空字典被 `versions or {}` 意外替换。
        self.versions = versions if versions is not None else {}
        self.skills = skill_loader if skill_loader is not None else SkillLoader(self.cwd / "skills")
        self.project_files = project_files if project_files is not None else ProjectFiles(self.cwd)

    def handlers(self) -> dict:
        return {
            "read_file": self.read, "write_file": self.write, "edit_file": self.edit,
            "list_files": self.list_files, "glob": self.glob, "search_code": self.search_code,
            "read_files": self.read_files, "search_many": self.search_many,
            "git_status": self.git_status, "git_diff": self.git_diff,
            "load_skill": self.load_skill,
        }

    def _path(self, path: str) -> Path:
        #resolve 同时拒绝 .. 和符号链接越界；异常向上交给 handlers 统一转成 Error 文本。
        return workspace_safe_path(path, self.cwd)

    @staticmethod
    def _contents(file_path: Path) -> bytes:
        if not file_path.is_file():
            raise ValueError("path is not a regular file")
        with file_path.open("rb") as stream:
            content = stream.read(MAX_FILE_BYTES + 1)
        if len(content) > MAX_FILE_BYTES:
            raise ValueError(f"file exceeds {MAX_FILE_BYTES} bytes; use a narrower source file")
        return content

    @staticmethod
    def _hash(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    def read(self, path: str, limit: int | None = None, start_line: int | None = None,
             end_line: int | None = None) -> str:
        """按 1 起始的闭区间读取，并记录整份文件的摘要以检测后续并发修改。"""
        output = self._read_output(path, limit, start_line, end_line)
        if isinstance(output, ReadOutput):
            self.versions[str(self.cwd / output.snapshot["path"])] = output.snapshot["sha256"]
        return output

    def _read_output(self, path, limit=None, start_line=None, end_line=None):
        """只准备读取结果；批量读取尚未展示的文件不能先获得修改前置版本。"""
        try:
            file_path = self._path(path)
            content = self._contents(file_path)
            lines = content.decode("utf-8").splitlines()
            start = _integer(start_line if start_line is not None else 1, "start_line", 1)
            end = _integer(end_line, "end_line", 1) if end_line is not None else len(lines)
            count = _integer(limit, "limit", 1) if limit is not None else MAX_READ_LINES
            if end_line is not None and end < start:
                raise ValueError("end_line must not be smaller than start_line")
            if start > max(1, len(lines)):
                raise ValueError("start_line exceeds file line count")
            stop = min(end, start + min(count, MAX_READ_LINES) - 1, len(lines))
            shown = [f"{number}: {lines[number - 1]}" for number in range(start, stop + 1)]
            if stop < min(end, len(lines)):
                shown.append("... (truncated; request the next line range)")
            digest = self._hash(content)
            output = _bounded("\n".join(shown)) or "(empty file)"
            #快照只记录实际给模型的片段及当时版本；后续写入更新 versions 也不会改掉它。
            displayed = [int(line.partition(":")[0]) for line in output.splitlines()
                         if line.partition(":")[0].isdigit()]
            return ReadOutput(output, {
                "path": file_path.relative_to(self.cwd).as_posix(), "sha256": digest,
                "content": output, "start_line": start,
                "end_line": displayed[-1] if displayed else 0,
            })
        except Exception as error:
            return f"Error: {error}"

    def read_files(self, files: list[dict]) -> str:
        """按顺序读取、去重并分摊总预算；失败项不影响其余文件，也不获得编辑版本。"""
        try:
            items = _batch_items(files)
            parts, snapshots = [], []
            remaining = MAX_OUTPUT_CHARS
            for index, item in enumerate(items):
                check_cancelled()
                output = self._read_output(**item)
                check_cancelled()
                header = f"read {index + 1}: {json.dumps(item['path'][:160], ensure_ascii=False)}"
                budget = (remaining - 2 * (len(items) - index - 1)) // (len(items) - index)
                piece, visible = _batch_piece(header, str(output), budget)
                parts.append(piece)
                remaining -= len(piece) + (2 if index < len(items) - 1 else 0)
                if isinstance(output, ReadOutput):
                    numbers = [int(line.partition(":")[0]) for line in visible.splitlines()
                               if line.partition(":")[0].isdigit()]
                    if numbers or visible == "(empty file)":
                        snapshots.append({**output.snapshot, "content": visible,
                                          "end_line": numbers[-1] if numbers else 0})
            check_cancelled()
            for snapshot in snapshots:
                self.versions[str(self.cwd / snapshot["path"])] = snapshot["sha256"]
            return BatchReadOutput("\n\n".join(parts), snapshots)
        except Exception as error:
            return f"Error: {error}"

    def search_many(self, queries: list[dict]) -> str:
        """同一目录只发现一次候选文件；每个查询仍有独立命中和失败结果。"""
        try:
            items = _batch_items(queries, search=True)
            parts, listings = [], {}
            remaining = MAX_OUTPUT_CHARS
            for index, item in enumerate(items):
                check_cancelled()
                output = self._search_code(**item, listings=listings)
                check_cancelled()
                #完整参数仍在工具调用和审计记录中，标题只放有界的查询提示。
                header = (f"search {index + 1}: {json.dumps(item['query'][:160], ensure_ascii=False)} "
                          f"in {json.dumps(item['path'][:160], ensure_ascii=False)}")
                budget = (remaining - 2 * (len(items) - index - 1)) // (len(items) - index)
                piece, _ = _batch_piece(header, output, budget)
                parts.append(piece)
                remaining -= len(piece) + (2 if index < len(items) - 1 else 0)
            return "\n\n".join(parts)
        except Exception as error:
            return f"Error: {error}"

    def _expected_version(self, file_path: Path) -> tuple[bytes | None, str | None]:
        if not file_path.exists():
            if str(file_path) in self.versions:
                raise ValueError("file disappeared since it was read; read it again before writing")
            return None, None
        content = self._contents(file_path)
        expected = self.versions.get(str(file_path))
        if expected is None:
            raise ValueError("read the existing file before modifying it")
        if self._hash(content) != expected:
            raise ValueError("file changed since it was read; read it again before modifying it")
        return content, expected

    def _save(self, file_path: Path, content: str, expected: str | None) -> None:
        """先写同目录临时文件，替换前再次核对版本；不直接截断用户的原文件。"""
        check_cancelled()
        payload = content.encode("utf-8")
        if len(payload) > MAX_FILE_BYTES:
            raise ValueError(f"content exceeds {MAX_FILE_BYTES} bytes")
        file_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=file_path.parent, prefix=f".{file_path.name}.",
                                             suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(payload)
                if expected is not None:
                    os.fchmod(stream.fileno(), stat.S_IMODE(file_path.stat().st_mode))
            #这属于乐观并发检查；它防止使用旧读取结果覆盖后续修改，不是跨程序文件锁。
            if expected is None:
                if file_path.exists():
                    raise ValueError("file was created concurrently; read it before modifying it")
            elif not file_path.exists() or self._hash(self._contents(file_path)) != expected:
                raise ValueError("file changed before writing; read it again before modifying it")
            check_cancelled()
            os.replace(temporary, file_path)
            self.versions[str(file_path)] = self._hash(payload)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def prepare_change(self, name: str, **arguments) -> FileChange:
        """只计算候选和 diff；展示或等待确认时不创建目录、不写入、不更新读取版本。"""
        parameters = {"write_file": {"path", "content"},
                      "edit_file": {"path", "old_text", "new_text"}}
        if name not in parameters or set(arguments) != parameters[name]:
            raise ValueError("invalid file change operation or arguments")
        check_cancelled()
        path = arguments["path"]
        file_path = self._path(path)
        before, expected = self._expected_version(file_path)
        if name == "write_file":
            replacement = arguments["content"]
        else:
            if before is None:
                raise ValueError("file does not exist")
            text = before.decode("utf-8")
            old_text, new_text = arguments["old_text"], arguments["new_text"]
            count = text.count(old_text) if old_text else 0
            if count == 1:
                replacement = text.replace(old_text, new_text, 1)
            elif count > 1:
                raise ValueError(f"Expected 1 non-empty occurrence, found {count}")
            else:
                #精确匹配失败先试整体缩进变体；仍要求唯一命中，替换文本加同样缩进。
                matched = None
                for indent, candidate in _indentation_variants(old_text):
                    if text.count(candidate) == 1:
                        matched = (candidate, indent)
                        break
                if matched is None:
                    raise ValueError("Expected 1 non-empty occurrence, found 0")
                candidate, indent = matched
                indented_new = "\n".join(
                    f"{indent}{line}" if line else line for line in new_text.split("\n"))
                replacement = text.replace(candidate, indented_new, 1)
        if len(replacement.encode("utf-8")) > MAX_FILE_BYTES:
            raise ValueError(f"content exceeds {MAX_FILE_BYTES} bytes")
        diff = self._diff(path, before.decode("utf-8") if before is not None else "", replacement,
                          new_file=before is None)
        return FileChange(path, file_path, replacement, expected, diff, name)

    def apply_change(self, change: FileChange) -> str:
        """只应用展示过的候选；确认期间路径或原文件变化时，必须重新生成并确认。"""
        check_cancelled()
        if self._path(change.path) != change.file_path:
            raise ValueError("file target changed after preview; prepare the change again")
        #直接使用候选中的 expected，不接受等待期间 read() 更新后的新版本替它背书。
        self._save(change.file_path, change.content, change.expected)
        if change.tool == "write_file":
            return f"Wrote {len(change.content.encode('utf-8'))} bytes to {change.path}\n{change.diff}"
        return f"Edited {change.path}\n{change.diff}"

    def write(self, path: str, content: str) -> str:
        try:
            return self.apply_change(self.prepare_change("write_file", path=path, content=content))
        except Exception as error:
            return f"Error: {error}"

    def edit(self, path: str, old_text: str, new_text: str) -> str:
        try:
            return self.apply_change(self.prepare_change("edit_file", path=path,
                                                         old_text=old_text, new_text=new_text))
        except Exception as error:
            return f"Error: {error}"

    @staticmethod
    def _diff(path, before, after, *, new_file=False):
        """即使文件尚未加入 Git，也返回真实修改对比；完整结果由 ToolRuntime 保存。"""
        lines = difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                     fromfile="/dev/null" if new_file else f"a/{path}",
                                     tofile=f"b/{path}")
        parts = []
        for line in lines:
            parts.append(line)
            if not line.endswith("\n"):
                parts.append("\n\\ No newline at end of file\n")
        return "".join(parts).rstrip("\n") or "(no line content changes)"

    def _discovery_target(self, path: str) -> Path:
        #先检查工作区边界，再保留原路径供扫描器识别符号链接；不能只传 resolve 后的路径。
        self._path(path)
        return self.cwd / path

    @staticmethod
    def _scan_notice(listing) -> str:
        messages = list(listing.warnings)
        if listing.truncated:
            messages.insert(0, "truncated file discovery; narrow the path/pattern or increase tree depth")
        return "\n".join(f"... ({message})" for message in messages)

    def list_files(self, path: str = ".", max_depth: int = 3, max_entries: int = 200) -> str:
        """按需显示有界目录树；文件名只是定位线索，代码行为仍须读取后确认。"""
        try:
            _integer(max_depth, "max_depth", 1, 10)
            _integer(max_entries, "max_entries", 1, 1000)
            target = self._discovery_target(path)
            listing = self.project_files.scan(target, max_depth=max_depth, max_entries=max_entries)
            label = target.relative_to(self.cwd).as_posix()
            is_directory = target.is_dir()
            lines = [f"{label}/"] if is_directory else []
            #公共扫描先收集同层条目；展示时按路径分组，使子项紧跟所属目录。
            for entry in sorted(listing.entries, key=lambda entry: entry.path.parts):
                relative = entry.path.relative_to(target) if is_directory else Path(entry.path.name)
                lines.append("  " * len(relative.parts) + relative.name + ("/" if entry.is_dir else ""))
            if not listing.entries:
                lines.append("(no discoverable entries)")
            if notice := self._scan_notice(listing):
                lines.append(notice)
            return _bounded("\n".join(lines))
        except Exception as error:
            return f"Error: {error}"

    @staticmethod
    def _glob_matches(parts, pattern):
        #逐段匹配，* 不跨目录；** 可匹配零层或多层，根目录的 *.py 也能匹配 **/*.py。
        #用迭代而不是递归，深目录不会耗尽 Python 的递归栈。
        positions = {0}
        for segment in pattern:
            if segment == "**":
                positions = set(range(min(positions), len(parts) + 1)) if positions else set()
            else:
                positions = {position + 1 for position in positions if position < len(parts)
                             and fnmatchcase(parts[position], segment)}
        return len(parts) in positions

    def glob(self, pattern: str) -> str:
        try:
            #先解析固定前缀以拒绝 ../ 和绝对路径逃逸，而不是先搜索再静默丢弃结果。
            self._path(pattern)
            parts = Path(pattern).parts
            if not parts or Path(pattern).is_absolute() or ".." in parts or len(parts) > 64:
                raise ValueError("pattern must be a relative workspace glob with at most 64 segments")
            prefix = []
            for part in parts[:-1]:
                if any(character in part for character in "*?["):
                    break
                prefix.append(part)
            target = self.cwd.joinpath(*prefix)
            if not target.exists():
                return "No files found"
            depth = None if "**" in parts else len(parts) - len(prefix)
            listing = self.project_files.scan(target, max_depth=depth)
            if depth is not None and any(item.startswith("depth limit") for item in listing.warnings):
                #非递归模式不会匹配更深的路径；这里的深度边界不代表 glob 结果不完整。
                listing.warnings = [item for item in listing.warnings if not item.startswith("depth limit")]
                listing.truncated = bool(listing.warnings)
            matches = [entry.path.relative_to(self.cwd).as_posix() for entry in listing.entries
                       if (not pattern.endswith("/") or entry.is_dir)
                       and self._glob_matches(entry.path.relative_to(self.cwd).parts, parts)]
            text = "\n".join(sorted(matches[:200])) or "No files found"
            if len(matches) > 200:
                text += "\n... (truncated; narrow the pattern)"
            if notice := self._scan_notice(listing):
                text += "\n" + notice
            return _bounded(text) or "No files found"
        except Exception as error:
            return f"Error: {error}"

    def search_code(self, query: str, path: str = ".", context_lines: int = 2,
                    max_results: int = 100) -> str:
        return self._search_code(query, path, context_lines, max_results)

    def _search_code(self, query, path=".", context_lines=2, max_results=100, *, listings=None):
        try:
            if not isinstance(query, str) or not query or "\n" in query:
                raise ValueError("query must be non-empty single-line literal text")
            _integer(context_lines, "context_lines", 0, 5)
            _integer(max_results, "max_results", 1, 200)
            target = self._discovery_target(path)
            if not target.exists():
                raise ValueError("search path does not exist")
            if self.project_files.is_ignored(target, is_dir=target.is_dir()):
                raise ValueError("search path is an excluded state or dependency directory")
            key = str(target)
            if listings is not None and key in listings:
                listing = listings[key]
            else:
                listing = self.project_files.scan(target)
                if listings is not None:
                    listings[key] = listing
            candidates = listing.files
            executable = shutil.which("rg")
            #macOS 的 argv 长度有限；很大的候选集合直接使用同一列表做 Python 搜索。
            if executable and sum(len(os.fsencode(item)) + 1 for item in candidates) < 48000:
                result = self._search_rg(executable, query, candidates, context_lines, max_results)
            else:
                result = self._search_python(query, candidates, context_lines, max_results)
            if notice := self._scan_notice(listing):
                result += "\n" + notice
            return _bounded(result)
        except Exception as error:
            return f"Error: {error}"

    def _search_rg(self, executable: str, query: str, candidates: list[Path],
                   context_lines: int, max_results: int) -> str:
        if not candidates:
            return "No matches found"
        #argv 列表和 -- 分隔符确保查询中的引号、$()、以 - 开头的文本都只是数据。
        args = [executable, "--no-config", "--json", "--fixed-strings", "--hidden", "--no-follow",
                "--no-ignore",
                "--max-filesize", str(MAX_FILE_BYTES), "--max-count", str(max_results + 1),
                "--context", str(context_lines)]
        #忽略规则只由 ProjectFiles 解释，rg 不再额外应用用户全局或父仓库的规则。
        args.extend(["--", query, *(str(item) for item in candidates)])
        #rg 原样回显命令行里传入的候选路径；用精确集合匹配做守卫。
        #不能先 resolve 再比较：符号链接源文件的 resolve 结果不在候选集合里，匹配会被静默丢弃。
        allowed_paths = set(candidates)
        check_cancelled()
        process = subprocess.Popen(args, cwd=self.cwd, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                   start_new_session=True)
        turn = current_turn()
        remove_cancel = turn.add_cancel_callback(lambda: stop_process_group(process)) if turn is not None else lambda: None
        timed_out = threading.Event()

        def expire():
            timed_out.set()
            stop_process_group(process)

        timer = threading.Timer(10, expire)
        timer.daemon = True
        timer.start()
        lines, errors, matches, characters = [], [], 0, 0
        truncated = False
        try:
            for raw in process.stdout:
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    errors.append(raw[:1000])
                    continue
                if event.get("type") not in {"match", "context"}:
                    continue
                data = event["data"]
                raw_path = data["path"].get("text")
                if raw_path is None or "text" not in data["lines"]:
                    continue
                file_path = Path(raw_path)
                if file_path not in allowed_paths:
                    continue
                if event["type"] == "match":
                    matches += 1
                    if matches > max_results:
                        truncated = True
                        break
                line = f"{file_path.relative_to(self.cwd)}:{data['line_number']}: {data['lines']['text'].rstrip()}"
                lines.append(line)
                characters += len(line) + 1
                if characters > MAX_OUTPUT_CHARS:
                    truncated = True
                    break
            if timed_out.is_set():
                return "Error: search timed out; narrow the search path"
            if truncated and process.poll() is None:
                stop_process_group(process)
            code = process.wait(timeout=2)
            check_cancelled()
            if timed_out.is_set():
                return "Error: search timed out; narrow the search path"
            if code not in (0, 1) and not truncated:
                return "Error: " + _bounded("".join(errors).strip() or f"rg exited with status {code}")
            if truncated:
                lines.append("... (truncated; narrow the query or path)")
            return _bounded("\n".join(lines)) or "No matches found"
        finally:
            remove_cancel()
            timer.cancel()
            stop_process_group(process)
            process.wait()
            process.stdout.close()

    def _search_python(self, query: str, candidates: list[Path], context_lines: int, max_results: int) -> str:
        lines, matches, characters = [], 0, 0
        truncated = False
        deadline = time.monotonic() + 10
        for file_path in candidates:
            check_cancelled()
            if time.monotonic() > deadline:
                return "Error: search timed out; narrow the search path"
            try:
                if file_path.is_symlink() or not file_path.resolve().is_relative_to(self.cwd):
                    continue
                if file_path.stat().st_size > MAX_FILE_BYTES:
                    continue
                content = self._contents(file_path)
                if b"\x00" in content:
                    continue
                text_lines = content.decode("utf-8").splitlines()
            except (OSError, UnicodeError, ValueError):
                continue
            shown = set()
            for index, text in enumerate(text_lines):
                if query not in text:
                    continue
                matches += 1
                if matches > max_results:
                    truncated = True
                    break
                for neighbor in range(max(0, index - context_lines), min(len(text_lines), index + context_lines + 1)):
                    if neighbor not in shown:
                        line = f"{file_path.relative_to(self.cwd)}:{neighbor + 1}: {text_lines[neighbor]}"
                        lines.append(line)
                        characters += len(line) + 1
                        shown.add(neighbor)
                if characters > MAX_OUTPUT_CHARS:
                    truncated = True
                    break
            if truncated:
                break
        if truncated:
            lines.append("... (truncated; narrow the query or path)")
        return _bounded("\n".join(lines)) or "No matches found"

    @staticmethod
    def _git_environment() -> dict:
        env = dict(os.environ)
        #不继承可把 Git 指向别处的目录变量；GIT_OPTIONAL_LOCKS=0 阻止 status 刷新索引。
        for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                     "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_PREFIX"):
            env.pop(name, None)
        env.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat"})
        return env

    def _git_process(self, args: list[str], env: dict) -> tuple[bytes, int, bool]:
        """固定 Git 命令的有界执行器；配置查询与实际查询均服从本轮取消。"""
        process = None
        timer = None
        remove_cancel = lambda: None
        timed_out = threading.Event()
        try:
            check_cancelled()
            process = subprocess.Popen(args, cwd=self.cwd, env=env, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, start_new_session=True)
            turn = current_turn()
            remove_cancel = turn.add_cancel_callback(lambda: stop_process_group(process)) if turn is not None else lambda: None

            def expire():
                timed_out.set()
                stop_process_group(process)

            timer = threading.Timer(10, expire)
            timer.daemon = True
            timer.start()
            output = process.stdout.read(MAX_OUTPUT_CHARS * 4 + 1)
            truncated = len(output) > MAX_OUTPUT_CHARS * 4
            if truncated and process.poll() is None:
                stop_process_group(process)
            code = process.wait(timeout=2)
            check_cancelled()
            if timed_out.is_set():
                raise TimeoutError("Git query timed out")
            return output, code, truncated
        finally:
            remove_cancel()
            if timer is not None:
                timer.cancel()
            if process is not None:
                stop_process_group(process)
                process.wait()
                process.stdout.close()

    def _git_filter_overrides(self, env: dict) -> list[str]:
        """每次查询实际生效的内容过滤器；父仓库、worktree 和 include 配置都不能沿用旧缓存。"""
        base = ["git", "--no-pager", "--literal-pathspecs", "-c", "core.pager=cat",
                "-c", "core.fsmonitor=false", "-c", f"core.hooksPath={os.devnull}"]
        #--no-ext-diff/--no-textconv 不会阻止 clean/process 过滤器；status/diff 都可能执行它们。
        #config 内置命令只查询键名，不读取内容转换，不会启动这些过滤器。
        raw, code, truncated = self._git_process([
            *base, "config", "--null", "--name-only", "--get-regexp",
            r"^filter\..*\.(clean|process|smudge|required)$",
        ], env)
        if truncated or code not in (0, 1):
            raise ValueError("cannot safely inspect Git content filters")
        drivers = set()
        for item in raw.split(b"\x00"):
            if not item:
                continue
            key = item.decode("utf-8")
            #`-c key=value` 用第一个等号拆分；异常键名必须失败关闭，不能覆盖错键后继续执行。
            if not key.startswith("filter.") or any(char in key for char in "=\r\n"):
                raise ValueError("unsupported Git filter configuration key")
            driver, setting = key.rsplit(".", 1)
            if setting not in {"clean", "process", "smudge", "required"} or driver == "filter.":
                raise ValueError("unsupported Git filter configuration key")
            drivers.add(driver)
        overrides = []
        for driver in sorted(drivers):
            for setting in ("clean", "process", "smudge", "required"):
                overrides.extend(["-c", f"{driver}.{setting}={'false' if setting == 'required' else ''}"])
        return overrides

    def _git(self, arguments: list[str]) -> str:
        args = ["git", "--no-pager", "--literal-pathspecs", "-c", "core.pager=cat", "-c", "core.fsmonitor=false",
                "-c", f"core.hooksPath={os.devnull}"]
        env = self._git_environment()
        try:
            args.extend(self._git_filter_overrides(env))
            output, code, truncated = self._git_process([*args, *arguments], env)
            text = output.decode("utf-8", errors="replace").rstrip()
            if code and not truncated:
                return "Error: " + _bounded(text or f"git exited with status {code}")
            if truncated:
                text += "\n... (truncated; request a narrower path)"
            return _bounded(text) or "No changes"
        except (OSError, UnicodeError, ValueError, subprocess.SubprocessError) as error:
            return f"Error: {error}"

    def git_status(self) -> str:
        return self._git(["status", "--porcelain=v1", "--untracked-files=normal", "--ignore-submodules=all", "--", "."])

    def git_diff(self, path: str | None = None, staged: bool = False) -> str:
        try:
            if type(staged) is not bool:
                raise ValueError("staged must be a boolean")
            target = self._path(path if path is not None else ".")
            arguments = ["diff", "--no-ext-diff", "--no-textconv", "--no-color", "--ignore-submodules=all"]
            if staged:
                arguments.append("--cached")
            arguments.extend(["--", str(target.relative_to(self.cwd))])
            return self._git(arguments)
        except Exception as error:
            return f"Error: {error}"

    def load_skill(self, name: str) -> str:
        try:
            #完整说明交给 ToolRuntime 落盘；其预览会给出证据引用，不能提前丢掉后半部分。
            return self.skills.load(name)
        except Exception as error:
            return f"Error: {error}"
