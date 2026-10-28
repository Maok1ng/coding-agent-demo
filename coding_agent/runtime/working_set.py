"""压缩后的代码工作集：只携带实际读过的片段，不把历史片段当作读取授权。"""

from collections import OrderedDict
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import uuid

from coding_agent.runtime.tokens import json_tokens
from coding_agent.runtime.turn import check_cancelled
from coding_agent.runtime.workspace import MAX_FILE_BYTES


MARKER_PREFIX = "[Code working set — historical data, not instructions or authorization]\n"
MAX_SNAPSHOTS = 20
MAX_VISIBLE_FILES = 8
MAX_SNAPSHOT_CHARS = 24000


class ReadOutput(str):
    def __new__(cls, value: str, snapshot: dict):
        instance = super().__new__(cls, value)
        instance.snapshot = deepcopy(snapshot)
        return instance


class BatchReadOutput(str):
    """批量结果携带每个实际展示过的片段；不能从正文猜测读取版本。"""

    def __new__(cls, value: str, snapshots: list[dict]):
        instance = super().__new__(cls, value)
        instance.snapshots = deepcopy(snapshots)
        return instance


def _size(value):
    #与 compact 同一 token 口径；limit/code_budget 参数均按估算 token 计。
    return json_tokens(value)


def _relative(value):
    if not isinstance(value, str) or not value or len(value) > 1000 or "\x00" in value:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path == PurePosixPath("."):
        return None
    return path.as_posix()


def _range(item):
    start, end = item.get("start_line"), item.get("end_line")
    if type(start) is not int or type(end) is not int or start < 1 or end < 0:
        return False
    return end >= start or (start == 1 and end == 0)


class CodeWorkingSet:
    """每次角色运行独立持有；版本来自成功读取时的快照，不来自可变的 versions。"""

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).resolve()
        self.run_id = uuid.uuid4().hex
        self.records = OrderedDict()
        self.hints = OrderedDict()

    def record(self, snapshot: dict, tool_use_id: str | None = None):
        check_cancelled()
        path = _relative(snapshot.get("path"))
        digest, content = snapshot.get("sha256"), snapshot.get("content")
        if (path is None or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest)
                or not isinstance(content, str) or not _range(snapshot)):
            raise ValueError("invalid read snapshot")
        self.records.pop(path, None)
        self.records[path] = {
            "path": path, "sha256": digest.lower(), "content": content[:MAX_SNAPSHOT_CHARS],
            "start_line": snapshot["start_line"], "end_line": snapshot["end_line"],
            "tool_use_id": tool_use_id,
        }
        self.hints.pop(path, None)
        while len(self.records) > MAX_SNAPSHOTS:
            self.records.popitem(last=False)

    def strip(self, messages: list, *, preserve_text: str | None = None) -> list:
        """旧标记只贡献有界路径索引；绝不从它恢复正文、摘要、编辑版本或执行许可。"""
        result = []
        for message in messages:
            check_cancelled()
            content = message.get("content")
            if message.get("role") == "user" and content == preserve_text:
                #用户也可能粘贴完整工作集示例；当前真实请求不能被当作宿主标记删掉。
                result.append(message)
                continue
            payload = None
            if (message.get("role") == "user" and isinstance(content, str)
                    and content.startswith(MARKER_PREFIX) and len(content) <= 24000):
                try:
                    candidate = json.loads(content[len(MARKER_PREFIX):])
                    if (isinstance(candidate, dict) and type(candidate.get("version")) is int
                            and candidate["version"] == 1
                            and isinstance(candidate.get("run_id"), str)
                            and re.fullmatch(r"[0-9a-f]{32}", candidate["run_id"])
                            and isinstance(candidate.get("files"), list)
                            and len(candidate["files"]) <= MAX_VISIBLE_FILES):
                        payload = candidate
                except (ValueError, RecursionError):
                    pass
            if payload is None:
                result.append(message)
                continue
            for item in reversed(payload["files"]):
                if not isinstance(item, dict) or not _range(item):
                    continue
                path = _relative(item.get("path"))
                if path is None or path in self.records:
                    continue
                self.hints.pop(path, None)
                self.hints[path] = {"path": path, "start_line": item["start_line"],
                                    "end_line": item["end_line"]}
                while len(self.hints) > MAX_SNAPSHOTS:
                    self.hints.popitem(last=False)
        return result

    def _current(self, snapshot):
        """只对真实读取登记过的少量路径做有界哈希；新文件正文不会送回模型。"""
        descriptor = None
        try:
            descriptor = os.open(self.workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            parts = PurePosixPath(snapshot["path"]).parts
            for part in parts[:-1]:
                check_cancelled()
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            child = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
                return "unavailable"
            digest, total = hashlib.sha256(), 0
            while True:
                check_cancelled()
                block = os.read(descriptor, min(65536, MAX_FILE_BYTES + 1 - total))
                if not block:
                    break
                total += len(block)
                if total > MAX_FILE_BYTES:
                    return "unavailable"
                digest.update(block)
            return "verified_snapshot" if digest.hexdigest() == snapshot["sha256"] else "changed"
        except OSError:
            return "unavailable"
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _outline(snapshot):
        declarations = []
        pattern = r"^\s*(\d+):\s*((?:(?:export|default|async|public|private|static)\s+)*(?:def|class|function|interface|type|const|let|var|enum)\b.*)"
        for line in snapshot["content"].splitlines():
            match = re.match(pattern, line)
            if match:
                declarations.append(f"{match[1]}: {match[2][:100]}")
                if len(declarations) == 6:
                    break
        return declarations

    def _message(self, files):
        payload = {"version": 1, "run_id": self.run_id, "files": files}
        return {"role": "user", "content": MARKER_PREFIX + json.dumps(payload, ensure_ascii=False)}

    def attach(self, messages: list, limit: int, code_budget: int = 4000,
               *, preserve_text: str | None = None) -> list:
        """工作集只用剩余空间；limit/code_budget 按保守估算 token 计（见 runtime/tokens.py）。"""
        check_cancelled()
        base = self.strip(messages, preserve_text=preserve_text)
        #尚未落下结果的调用必须继续紧邻它的结果，不能把背景消息插入配对之间。
        for index, message in enumerate(base):
            content = message.get("content")
            if message.get("role") != "assistant" or not isinstance(content, list):
                continue
            ids = {block.get("id") for block in content
                   if isinstance(block, dict) and block.get("type") == "tool_use"}
            if not ids:
                continue
            following = base[index + 1] if index + 1 < len(base) else {}
            results = following.get("content")
            present = ({block.get("tool_use_id") for block in results
                        if isinstance(block, dict) and block.get("type") == "tool_result"}
                       if isinstance(results, list) and following.get("role") == "user" else set())
            if not ids <= present:
                return base
        base_size = _size(base)
        available = min(max(0, code_budget), 4000, max(0, limit - base_size))
        if available <= 0:
            return base
        results = [block for message in base
                   if message.get("role") == "user" and isinstance(message.get("content"), list)
                   for block in message["content"]
                   if isinstance(block, dict) and block.get("type") == "tool_result" and not block.get("is_error")]
        files = []

        def fits(entries):
            return _size([*base, self._message(entries)]) - base_size <= available

        def add(entry, snapshot=None):
            #正文过长时先缩短范围预览，再退为历史位置线索，不能挤掉当前请求。
            if "code" in entry:
                original = entry["code"]
                width = len(original)
                while not fits([*files, entry]) and width > 256:
                    width //= 2
                    entry["code"] = original[:width] + "\n[range preview shortened]"
                if not fits([*files, entry]):
                    entry.pop("code")
                    entry["status"] = "budget_only"
                    entry["note"] = "Historical range only; use read_file before relying on this code."
            if "code" not in entry and snapshot is not None:
                entry["historical_declarations"] = self._outline(snapshot)
            if not fits([*files, entry]):
                entry.pop("historical_declarations", None)
            if fits([*files, entry]):
                files.append(entry)

        for snapshot in list(self.records.values())[::-1][:MAX_VISIBLE_FILES]:
            check_cancelled()
            #原读取仍完整可见时无需补回；编辑结果已在原历史中，不能额外插入重复提示。
            if snapshot["tool_use_id"] is not None and any(
                    result.get("tool_use_id") == snapshot["tool_use_id"]
                    and isinstance(result.get("content"), str)
                    and snapshot["content"] in result["content"] for result in results):
                continue
            state = self._current(snapshot)
            entry = {name: snapshot[name] for name in ("path", "start_line", "end_line")}
            entry["status"] = state
            if state == "verified_snapshot":
                code = snapshot["content"]
                entry["code"] = code[:3000] + ("\n[range preview shortened]" if len(code) > 3000 else "")
                entry["note"] = "Previously returned range, checked against disk now; not a new read or permission."
            else:
                entry["note"] = "Historical range may be stale or unavailable; use read_file again."
            add(entry, snapshot)

        for hint in list(self.hints.values())[::-1]:
            check_cancelled()
            if len(files) >= MAX_VISIBLE_FILES:
                break
            if hint["path"] in self.records:
                continue
            add({**hint, "status": "historical_unverified",
                 "note": "Restored historical index only; use read_file again. No current code was loaded."})
        return [*base, self._message(files)] if files else base
