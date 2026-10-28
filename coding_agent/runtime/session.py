"""主会话快照、历史列表与恢复；不保存凭据，也不重放历史工具调用。"""

import fcntl
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path


class SessionError(ValueError):
    pass


def plain_value(value):
    """递归转为 JSON 数据；SDK block 用 model_dump，不能用 str 丢失工具结构。"""
    if hasattr(value, "model_dump"):
        return plain_value(value.model_dump(mode="json", exclude_none=True))
    if isinstance(value, dict):
        return {key: plain_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise SessionError(f"不能保存会话数据类型: {type(value).__name__}")


def repair_interrupted_tools(messages: list) -> list:
    """只补齐末尾中断的工具结果；结果未知不等于失败，也绝不自动重新执行。"""
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("role") not in ("user", "assistant"):
            raise SessionError("会话包含无效消息")
        content = message.get("content")
        if not isinstance(content, (str, list)):
            raise SessionError("会话消息 content 必须是字符串或 block 列表")
        if isinstance(content, list) and any(not isinstance(block, dict) for block in content):
            raise SessionError("会话 block 必须是对象")
        if message["role"] == "user" and isinstance(content, list):
            result_ids = [block.get("tool_use_id") for block in content if block.get("type") == "tool_result"]
            previous = messages[index - 1] if index else {}
            previous_blocks = previous.get("content", [])
            previous_ids = [block.get("id") for block in previous_blocks
                            if isinstance(block, dict) and block.get("type") == "tool_use"] if isinstance(previous_blocks, list) else []
            if result_ids and (previous.get("role") != "assistant" or any(
                    not isinstance(tool_id, str) or tool_id not in previous_ids for tool_id in result_ids)):
                raise SessionError("会话包含没有对应调用的工具结果")
        if message["role"] != "assistant" or not isinstance(content, list):
            continue
        calls = [block for block in content if block.get("type") == "tool_use"]
        ids = [block.get("id") for block in calls]
        if any(not isinstance(tool_id, str) or not tool_id for tool_id in ids) or len(set(ids)) != len(ids):
            raise SessionError("会话工具调用 ID 无效或重复")
        if not calls:
            continue
        following = messages[index + 1] if index + 1 < len(messages) else None
        results = (following.get("content") if isinstance(following, dict)
                   and following.get("role") == "user" else None)
        result_ids = [block.get("tool_use_id") for block in results
                      if isinstance(block, dict) and block.get("type") == "tool_result"] if isinstance(results, list) else []
        if (any(not isinstance(tool_id, str) for tool_id in result_ids)
                or len(set(result_ids)) != len(result_ids) or any(tool_id not in ids for tool_id in result_ids)):
            raise SessionError("会话工具结果与调用不匹配")
        missing = [tool_id for tool_id in ids if tool_id not in result_ids]
        if not missing:
            continue
        #只允许最后一个工具批次缺结果；不能掩盖历史中间的格式损坏。
        if following is None:
            results = []
            messages.append({"role": "user", "content": results})
        elif index + 1 != len(messages) - 1 or not isinstance(results, list):
            raise SessionError("历史中间缺少工具结果，无法安全恢复")
        results.extend({
            "type": "tool_result", "tool_use_id": tool_id, "is_error": True,
            "content": ("Session interrupted before this tool result was saved. "
                        "Execution outcome is unknown; the operation may have taken effect. "
                        "Check the current files/processes before deciding whether to retry. "
                        "This operation has NOT been automatically replayed."),
        } for tool_id in missing)
    return messages


def display_text(value: str) -> str:
    #历史标题可能来自任意用户文本；过滤换行和终端控制字符，避免破坏选择列表。
    return "".join(char if char.isprintable() else " " for char in value)


class SessionStore:
    """每个显式工作区使用独立的 .sessions，不向父 Git 仓库扩大范围。"""

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).resolve()
        self.root = self.workspace / ".sessions"

    def checked_path(self, name: str) -> Path:
        path = self.root / name
        if self.root.is_symlink() or path.is_symlink() or not path.resolve().is_relative_to(self.workspace):
            raise SessionError("会话存储路径不能通过符号链接离开工作区")
        return path

    def load(self, session_id: str) -> dict:
        #仅接受自己生成的 UUID；不接受用户输入任意 JSON 文件路径。
        if not re.fullmatch(r"[0-9a-f]{32}", session_id):
            raise SessionError("无效的会话 ID")
        try:
            data = json.loads(self.checked_path(f"{session_id}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise SessionError(f"无法读取会话 {session_id}: {error}") from error
        if (not isinstance(data, dict) or data.get("version") != 1
                or data.get("id") != session_id or data.get("workspace") != str(self.workspace)
                or not isinstance(data.get("messages"), list) or not isinstance(data.get("state"), dict)
                or any(not isinstance(data.get(key), str) for key in ("title", "created_at", "updated_at"))):
            raise SessionError(f"会话格式或工作区不匹配: {session_id}")
        return data

    def list_sessions(self) -> list[dict]:
        self.checked_path("index")
        sessions = []
        for path in self.root.glob("*.json"):
            try:
                sessions.append(self.load(path.stem))
            except SessionError as error:
                print(f"[session] skipped: {error}")
        return sorted(sessions, key=lambda item: (item["updated_at"], item["id"]), reverse=True)

    def show_sessions(self, sessions: list[dict]):
        if not sessions:
            print("当前工作区没有历史会话。")
        for number, item in enumerate(sessions, 1):
            print(f"{number:>3}. {item['id']}  {display_text(item['updated_at'])}\n"
                  f"     {display_text(item['title'])}  ({len(item['messages'])} messages)")

    def select(self, query: str = "") -> str | None:
        """-r 无参数显示选择器；有参数按完整 ID 或唯一 ID 前缀查找。"""
        sessions = self.list_sessions()
        if query:
            matches = [item["id"] for item in sessions if item["id"].startswith(query)]
            if len(matches) != 1:
                raise SessionError("会话 ID 不存在或前缀不唯一；用 -r 查看完整列表")
            return matches[0]
        self.show_sessions(sessions)
        if not sessions:
            return None
        try:
            choice = input("选择会话编号或 ID（回车取消）: ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if not choice:
            return None
        if choice.isascii() and choice.isdigit() and 1 <= int(choice) <= len(sessions):
            return sessions[int(choice) - 1]["id"]
        return self.select(choice)

    def open(self, session_id: str | None = None):
        return Session(self, session_id)


class Session:
    """持有会话独占锁；同一个会话不能被两个进程同时恢复写入。"""

    def __init__(self, store: SessionStore, session_id: str | None):
        self.store = store
        self.resumed = session_id is not None
        self.id = session_id or uuid.uuid4().hex
        if not re.fullmatch(r"[0-9a-f]{32}", self.id):
            raise SessionError("无效的会话 ID")
        self.path = store.checked_path(f"{self.id}.json")
        store.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._lock = store.checked_path(f"{self.id}.lock").open("a+", encoding="utf-8")
        try:
            #LOCK_NB 表示不阻塞等待；已有进程占用时直接告诉用户。
            fcntl.flock(self._lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self._lock.close()
            raise SessionError("该会话正在另一个进程中使用，请先退出原进程") from error
        now = datetime.now(timezone.utc).isoformat()
        try:
            self.data = store.load(self.id) if self.resumed else {
                "version": 1, "id": self.id, "workspace": str(store.workspace),
                "title": "新会话", "created_at": now, "updated_at": now,
                "messages": [], "state": {},
            }
            repair_interrupted_tools(self.data["messages"])
        except Exception:
            self.close()
            raise

    def save(self, messages: list, state: dict):
        """原子替换当前可恢复上下文；压缩后保存摘要，不把旧全文重新塞回窗口。"""
        if not messages and not self.resumed and not self.path.exists():
            return  #只启动后退出，不制造空白历史会话。
        data = {**self.data, "messages": plain_value(messages), "state": plain_value(state),
                "updated_at": datetime.now(timezone.utc).isoformat()}
        self.store.checked_path(self.path.name)
        #mkstemp 生成唯一临时文件且默认仅当前用户可读写；replace 避免半份 JSON。
        descriptor, name = tempfile.mkstemp(prefix=f".{self.id}.", suffix=".tmp", dir=self.store.root)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            self.data = data
        except (OSError, TypeError, ValueError) as error:
            raise SessionError(f"保存会话失败，已停止继续执行: {error}") from error
        finally:
            temporary.unlink(missing_ok=True)

    def set_title(self, text: str):
        if self.data["title"] == "新会话":
            self.data["title"] = " ".join(text.split())[:80] or "新会话"

    def close(self):
        if not self._lock.closed:
            fcntl.flock(self._lock.fileno(), fcntl.LOCK_UN)
            self._lock.close()
