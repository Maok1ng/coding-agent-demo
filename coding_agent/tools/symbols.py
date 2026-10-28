"""按需解析 Python 符号；结果只是语法位置线索，不执行源码，也不建立常驻索引。"""

import ast
import json
import os
from pathlib import Path
import stat

from coding_agent.runtime.project_files import ProjectFiles, _directory
from coding_agent.runtime.turn import check_cancelled
from coding_agent.runtime.workspace import MAX_FILE_BYTES


MAX_SYMBOL_FILES = 100
MAX_SYMBOL_BYTES = 8 * 1024 * 1024
MAX_AST_NODES = 100_000
MAX_AST_ENTRIES = 2_000
MAX_SYMBOL_OUTPUT = 24_000

SYMBOL_TOOLS = [
    {"name": "list_symbols", "description": (
        "List Python class/function definitions and syntactic calls in one source file. "
        "Optional query is a case-sensitive substring. Not a semantic reference graph; "
        "read_file is still required before editing."),
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string", "minLength": 1},
         "query": {"type": "string", "maxLength": 200},
         "max_results": {"type": "integer", "minimum": 1, "maximum": 100},
     }, "required": ["path"], "additionalProperties": False}},
    {"name": "find_symbol", "description": (
        "Find Python definitions and syntactic call clues by exact simple or dotted name "
        "within a bounded workspace scan. Receiver types, imports and dynamic dispatch "
        "are not resolved. Narrow path when incomplete; other languages use search_code."),
     "input_schema": {"type": "object", "properties": {
         "symbol": {"type": "string", "minLength": 1, "maxLength": 200},
         "path": {"type": "string", "minLength": 1},
         "max_results": {"type": "integer", "minimum": 1, "maximum": 100},
     }, "required": ["symbol"], "additionalProperties": False}},
]


def _limit(value):
    if type(value) is not int or not 1 <= value <= 100:
        raise ValueError("max_results must be an integer between 1 and 100")
    return value


def _call_name(node):
    """只拼接源码中实际写出的名称；self.run 不能据此推断为某个类的 run。"""
    names = []
    while isinstance(node, ast.Attribute) and len(names) < 64:
        names.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        names.append(node.id)
        return ".".join(reversed(names)), False
    #factory().run() 等复杂接收者只保留末尾名称，明确标记接收者无法解析。
    return (names[0], True) if names else (None, True)


def _parse(content, path):
    """ast.parse 接受 bytes 并遵循 Python 编码声明；迭代遍历避免 Python 递归栈增长。"""
    tree = ast.parse(content, filename=path)
    definitions, calls = [], []
    stack, visited = [(tree, ())], 0
    while stack:
        check_cancelled()
        node, scope = stack.pop()
        visited += 1
        if visited > MAX_AST_NODES:
            return definitions, calls, True
        next_scope = scope
        body = set()
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            class_node = isinstance(node, ast.ClassDef)
            method = bool(scope and scope[-1][1] == "class")
            kind = "class" if class_node else "method" if method else "function"
            if isinstance(node, ast.AsyncFunctionDef):
                kind = "async_" + kind
            definitions.append({
                "path": path, "name": node.name,
                "qualified_name": ".".join([*(name for name, _ in scope), node.name]),
                "kind": kind, "start_line": node.lineno, "end_line": node.end_lineno,
            })
            next_scope = (*scope, (node.name, "class" if class_node else "function"))
            body = {id(child) for child in node.body}
        elif isinstance(node, ast.Call):
            name, unresolved = _call_name(node.func)
            if name:
                calls.append({
                    "path": path, "name": name.rsplit(".", 1)[-1], "qualified_name": name,
                    "kind": "call", "start_line": node.lineno, "end_line": node.end_lineno,
                    "scope": ".".join(name for name, _ in scope),
                    "receiver_unresolved": unresolved or "." in name,
                })
        if len(definitions) + len(calls) >= MAX_AST_ENTRIES:
            return definitions, calls, True
        for child in reversed(list(ast.iter_child_nodes(node))):
            #装饰器、默认参数和基类在外层求值；只有定义的 body 属于新作用域。
            stack.append((child, next_scope if id(child) in body else scope))
    return definitions, calls, False


class SymbolTools:
    def __init__(self, workspace: Path, project_files=None):
        self.workspace = Path(workspace).resolve()
        self.project_files = project_files if project_files is not None else ProjectFiles(self.workspace)

    def handlers(self):
        return {"list_symbols": self.list_symbols, "find_symbol": self.find_symbol}

    def _read(self, path):
        """逐级拒绝链接，并以非阻塞方式读取普通文件；FIFO 或扫描后的换链不能卡住工具。"""
        parent = descriptor = None
        try:
            parent = _directory(self.workspace, path.parent)
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("not a regular source file")
            if before.st_size > MAX_FILE_BYTES:
                raise ValueError("source file exceeds byte limit")
            chunks, count = [], 0
            while True:
                check_cancelled()
                chunk = os.read(descriptor, min(65536, MAX_FILE_BYTES + 1 - count))
                if not chunk:
                    break
                chunks.append(chunk)
                count += len(chunk)
                if count > MAX_FILE_BYTES:
                    raise ValueError("source file exceeds byte limit")
            after = os.fstat(descriptor)
            attributes = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
            if any(getattr(before, key) != getattr(after, key) for key in attributes):
                raise ValueError("source changed during parsing; retry")
            return b"".join(chunks)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if parent is not None:
                os.close(parent)

    def _scan(self, path, match, max_results, *, single=False):
        check_cancelled()
        if not isinstance(path, str) or not path:
            raise ValueError("path must be a non-empty string")
        target = self.project_files._path(Path(path))
        if single and target.suffix.lower() not in {".py", ".pyi"}:
            raise ValueError("list_symbols supports Python .py/.pyi files; use search_code for other languages")
        if single and target.is_dir():
            raise ValueError("list_symbols expects one Python source file; use find_symbol for directories")
        listing = self.project_files.scan(target)
        result = {
            "language": "python", "analysis": "syntax_only",
            "notice": "Locations and call names are syntax clues. Imports, receiver types and dynamic dispatch are unresolved; verify with read_file before editing.",
            "definitions": [], "calls": [], "files_scanned": 0, "files_parsed": 0,
            "unsupported_files": 0, "truncated": listing.truncated,
            "warnings": list(listing.warnings),
        }

        def warn(message):
            if len(result["warnings"]) < 10:
                result["warnings"].append(message)

        total_bytes = hits = 0
        for source in listing.files:
            check_cancelled()
            if source.suffix.lower() not in {".py", ".pyi"}:
                result["unsupported_files"] += 1
                continue
            if result["files_scanned"] >= MAX_SYMBOL_FILES or total_bytes >= MAX_SYMBOL_BYTES:
                result["truncated"] = True
                warn("Python scan budget reached; narrow path")
                break
            result["files_scanned"] += 1
            relative = source.relative_to(self.workspace).as_posix()
            try:
                content = self._read(source)
                if total_bytes + len(content) > MAX_SYMBOL_BYTES:
                    result["truncated"] = True
                    warn("Python source byte budget reached; narrow path")
                    break
                total_bytes += len(content)
                definitions, calls, partial = _parse(content, relative)
            except (OSError, ValueError, SyntaxError, UnicodeError, RecursionError) as error:
                result["truncated"] = True
                #只报告位置和异常类型，不回显源码或工作区外信息。
                warn(f"Skipped {relative}: {type(error).__name__}; results are incomplete")
                continue
            result["files_parsed"] += 1
            if partial:
                result["truncated"] = True
                warn(f"AST node budget or symbol entry budget reached: {relative}; results are incomplete")
            for field, items in (("definitions", definitions), ("calls", calls)):
                for item in items:
                    if not match(item):
                        continue
                    if hits >= max_results:
                        result["truncated"] = True
                        warn("Symbol result limit reached; narrow symbol or path")
                        break
                    result[field].append(item)
                    hits += 1
            if hits >= max_results and not single:
                #后续文件尚未解析，不能将当前结果当作所有定义或调用。
                result["truncated"] = True
                warn("Symbol result limit reached; remaining files were not scanned")
                break
        if single and not listing.files:
            warn("Source file is ignored or unavailable to shared file discovery")
        if result["unsupported_files"]:
            warn("Non-Python files were not parsed; use search_code for their symbols")
        if not result["files_parsed"] and not result["warnings"]:
            warn("No Python source files were parsed in this path")
        #长标识符或目录名也可能突破条数预算；只裁剪结果，保留不完整标记。
        encoded = json.dumps(result, ensure_ascii=False)
        while len(encoded) > MAX_SYMBOL_OUTPUT and (result["definitions"] or result["calls"]):
            result["truncated"] = True
            field = "calls" if result["calls"] else "definitions"
            result[field].pop()
            encoded = json.dumps(result, ensure_ascii=False)
        return encoded

    def list_symbols(self, path, query="", max_results=50):
        try:
            _limit(max_results)
            if not isinstance(query, str) or len(query) > 200:
                raise ValueError("query must be a string up to 200 characters")
            return self._scan(path, lambda item: query in item["qualified_name"], max_results, single=True)
        except (OSError, ValueError) as error:
            return f"Error: {error}"

    def find_symbol(self, symbol, path=".", max_results=30):
        try:
            _limit(max_results)
            if (not isinstance(symbol, str) or len(symbol) > 200
                    or not all(part.isidentifier() for part in symbol.split("."))):
                raise ValueError("symbol must be an exact simple or dotted Python identifier")
            return self._scan(path, lambda item: symbol in {item["name"], item["qualified_name"]}, max_results)
        except (OSError, ValueError) as error:
            return f"Error: {error}"
