"""共用的有界文件目录；文件树、路径匹配和内容检索使用同一套发现规则。"""

from dataclasses import dataclass, field
import os
from pathlib import Path
import stat

from pathspec import GitIgnoreSpec

from coding_agent.runtime.turn import check_cancelled


EXCLUDED_DIRS = {
    ".git", ".sessions", ".memory", ".agent_runs", ".agent_state", ".task_outputs", ".tasks", ".worktrees",
    ".mailboxes", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".transcripts",
    ".mypy_cache", ".ruff_cache", "dist", "build", "target", ".next", "vendor",
}
EXCLUDED_FILES = {".scheduled_tasks.json"}
IGNORE_FILES = (".gitignore", ".agentignore")
MAX_IGNORE_BYTES = 64 * 1024
MAX_SCAN_ENTRIES = 20_000
MAX_SCAN_DIRECTORIES = 2_000


@dataclass(frozen=True)
class FileEntry:
    path: Path
    is_dir: bool


@dataclass
class ScanResult:
    entries: list[FileEntry] = field(default_factory=list)
    truncated: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def files(self) -> list[Path]:
        return [entry.path for entry in self.entries if not entry.is_dir]


def _directory(root: Path, path: Path) -> int:
    """逐级用目录描述符打开，不沿扫描期间被换成符号链接的父目录走出工作区。"""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(root, flags)
    try:
        for part in path.relative_to(root).parts:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


class _IgnoreRules:
    """缓存只属于本次扫描；下一次调用重新读规则，不会漏掉用户或 Shell 的修改。"""

    def __init__(self, root: Path):
        self.root = root
        self.cache = {}

    def _spec(self, directory: Path, name: str):
        key = (directory, name)
        if key in self.cache:
            return self.cache[key]
        check_cancelled()
        parent = _directory(self.root, directory)
        descriptor = None
        try:
            try:
                #O_NONBLOCK 防止命名管道卡住；随后仍要求它是普通文件。
                descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            except FileNotFoundError:
                self.cache[key] = None
                return None
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("ignore rule is not a regular file")
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = None
                content = handle.read(MAX_IGNORE_BYTES + 1)
            if len(content) > MAX_IGNORE_BYTES:
                raise ValueError(f"ignore rule exceeds {MAX_IGNORE_BYTES} bytes")
            spec = GitIgnoreSpec.from_lines(content.decode("utf-8").splitlines())
        except (OSError, UnicodeError, ValueError) as error:
            #坏规则不能降级为“全部可见”，避免把本应排除的文件交给模型。
            relative = (directory / name).relative_to(self.root)
            raise ValueError(f"cannot safely read ignore rules: {relative} ({type(error).__name__})") from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
            os.close(parent)
        self.cache[key] = spec
        return spec

    def ignored(self, path: Path, is_dir: bool) -> bool:
        relative = path.relative_to(self.root)
        if any(part in EXCLUDED_DIRS for part in relative.parts) or relative.name in EXCLUDED_FILES:
            return True
        if path == self.root:
            return False
        #两类规则分别判断后取 OR；.agentignore 的 ! 不能解除 Git 排除或内置排除。
        chains = {name: [] for name in IGNORE_FILES}
        directory = self.root
        for index, part in enumerate(relative.parts):
            check_cancelled()
            for name in IGNORE_FILES:
                spec = self._spec(directory, name)
                if spec is not None:
                    chains[name].append((directory, spec))
            candidate = directory / part
            directory_entry = index < len(relative.parts) - 1 or is_dir
            for chain in chains.values():
                ignored = False
                for base, spec in chain:
                    candidate_name = candidate.relative_to(base).as_posix() + ("/" if directory_entry else "")
                    decision = spec.check_file(candidate_name).include
                    #None 表示没匹配，False 表示显式 ! 否定；不能把二者混为一谈。
                    if decision is not None:
                        ignored = decision
                if ignored:
                    #父目录已经排除就不继续加载它的子规则，符合 Git 的剪枝语义。
                    return True
            directory = candidate
        return False


class ProjectFiles:
    """按需建立文件目录；没有常驻线程、向量索引，也不把忽略规则当作写入授权。"""

    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()

    def _path(self, path: Path | None) -> Path:
        target = self.workspace if path is None else Path(path)
        if not target.is_absolute():
            target = self.workspace / target
        target = Path(os.path.abspath(target))
        if not target.is_relative_to(self.workspace):
            raise ValueError("file discovery path is outside the workspace")
        #不能先 resolve：那样会丢掉用户传入的链接别名，误把链接当作普通文件。
        current = self.workspace
        for part in target.relative_to(self.workspace).parts:
            check_cancelled()
            current = current / part
            if current.is_symlink():
                raise ValueError("file discovery does not follow symbolic links")
        return target

    def is_ignored(self, path: Path, *, is_dir: bool = False) -> bool:
        check_cancelled()
        return _IgnoreRules(self.workspace).ignored(self._path(path), is_dir)

    def scan(self, target: Path | None = None, *, max_depth: int | None = None,
             max_entries: int = 10_000) -> ScanResult:
        """直接子项深度为 1；预算耗尽或遇到不可读目录时返回明确的不完整标记。"""
        check_cancelled()
        if type(max_entries) is not int or not 1 <= max_entries <= 10_000:
            raise ValueError("max_entries must be an integer between 1 and 10000")
        if max_depth is not None and (type(max_depth) is not int or not 1 <= max_depth <= 100):
            raise ValueError("max_depth must be an integer between 1 and 100")
        target = self._path(target)
        parent = None
        try:
            if target == self.workspace:
                mode = target.stat(follow_symlinks=False).st_mode
            else:
                parent = _directory(self.workspace, target.parent)
                mode = os.stat(target.name, dir_fd=parent, follow_symlinks=False).st_mode
        except FileNotFoundError as error:
            raise ValueError("file discovery path does not exist") from error
        finally:
            if parent is not None:
                os.close(parent)
        if stat.S_ISLNK(mode):
            raise ValueError("file discovery does not follow symbolic links")
        target_is_dir = stat.S_ISDIR(mode)
        rules = _IgnoreRules(self.workspace)
        if rules.ignored(target, target_is_dir):
            return ScanResult()
        if not target_is_dir:
            return ScanResult([FileEntry(target, False)]) if stat.S_ISREG(mode) else ScanResult()

        result = ScanResult()
        inspected, directories = 0, 0
        pending = [(target, 0)]

        def incomplete(message):
            result.truncated = True
            if message not in result.warnings and len(result.warnings) < 10:
                result.warnings.append(message)

        while pending:
            check_cancelled()
            directory, depth = pending.pop()
            if directories >= MAX_SCAN_DIRECTORIES:
                incomplete("directory scan limit reached; narrow the path")
                break
            directories += 1
            descriptor = None
            children = []
            try:
                descriptor = _directory(self.workspace, directory)
                with os.scandir(descriptor) as entries:
                    for entry in entries:
                        check_cancelled()
                        if inspected >= MAX_SCAN_ENTRIES:
                            incomplete("entry scan limit reached; narrow the path")
                            break
                        inspected += 1
                        try:
                            mode = entry.stat(follow_symlinks=False).st_mode
                        except OSError:
                            incomplete(f"entry changed or unreadable: {(directory / entry.name).relative_to(self.workspace)}")
                            continue
                        #链接、设备和管道都不属于供模型自动发现的源文件。
                        if stat.S_ISDIR(mode) or stat.S_ISREG(mode):
                            children.append((entry.name, stat.S_ISDIR(mode)))
            except OSError:
                incomplete(f"directory changed or unreadable: {directory.relative_to(self.workspace)}")
                continue
            finally:
                if descriptor is not None:
                    os.close(descriptor)

            next_directories = []
            for name, is_dir in sorted(children):
                check_cancelled()
                path = directory / name
                try:
                    ignored = rules.ignored(path, is_dir)
                except OSError:
                    incomplete(f"entry changed or unreadable: {path.relative_to(self.workspace)}")
                    continue
                if ignored:
                    continue
                if len(result.entries) >= max_entries:
                    incomplete("file listing limit reached; narrow the path or increase max_entries")
                    return result
                result.entries.append(FileEntry(path, is_dir))
                if is_dir:
                    if max_depth is None or depth + 1 < max_depth:
                        next_directories.append((path, depth + 1))
                    else:
                        incomplete("depth limit reached; narrow the path or increase max_depth")
            #倒序入栈，实际仍按名称顺序遍历，截断结果可重复。
            pending.extend(reversed(next_directories))
            if inspected >= MAX_SCAN_ENTRIES:
                if pending:
                    incomplete("entry scan limit reached; narrow the path")
                break
        return result
