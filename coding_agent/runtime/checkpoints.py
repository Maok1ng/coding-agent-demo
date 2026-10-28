"""会话内的代码检查点；只恢复文件工具实际记录的文件，不操作 Git 或外部资源。"""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import base64
import binascii
import difflib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

from coding_agent.runtime.turn import check_cancelled
from coding_agent.runtime.workspace import MAX_FILE_BYTES


class CheckpointError(ValueError):
    pass


_HOST_PATHS = {".git", ".sessions", ".task_outputs", ".transcripts", ".memory", ".codex", ".agents"}
_MAX_JSON = 64 * 1024 * 1024
_MAX_FILES = 128
_ID = re.compile(r"C(?:0|[1-9][0-9]*)\Z")


def _now():
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class _Snapshot:
    exists: bool
    data: str | None
    mode: int | None
    sha256: str | None

    @classmethod
    def missing(cls):
        return cls(False, None, None, None)

    @classmethod
    def from_bytes(cls, payload, mode):
        return cls(True, base64.b64encode(payload).decode("ascii"), mode,
                   hashlib.sha256(payload).hexdigest())

    def payload(self):
        return base64.b64decode(self.data or "", validate=True)

    def json(self):
        return {"exists": self.exists, "data": self.data, "mode": self.mode, "sha256": self.sha256}

    @classmethod
    def checked(cls, value):
        if not isinstance(value, dict) or set(value) != {"exists", "data", "mode", "sha256"}:
            raise CheckpointError("检查点文件快照格式损坏")
        if type(value["exists"]) is not bool:
            raise CheckpointError("检查点文件存在标记损坏")
        if value == cls.missing().json():
            return cls.missing()
        if (value["exists"] is not True or not isinstance(value["data"], str)
                or type(value["mode"]) is not int or not 0 <= value["mode"] <= 0o7777
                or not isinstance(value["sha256"], str)
                or len(value["data"]) > 4 * ((MAX_FILE_BYTES + 2) // 3)):
            raise CheckpointError("检查点文件快照格式损坏或过大")
        try:
            payload = base64.b64decode(value["data"], validate=True)
        except (ValueError, binascii.Error):
            raise CheckpointError("检查点文件快照编码损坏") from None
        if len(payload) > MAX_FILE_BYTES or hashlib.sha256(payload).hexdigest() != value["sha256"]:
            raise CheckpointError("检查点文件快照校验失败")
        return cls(**value)


@dataclass(frozen=True)
class _RestoreFile:
    path: str
    before: _Snapshot
    after: _Snapshot
    diff: str


@dataclass(frozen=True)
class RestorePlan:
    from_id: str
    target_id: str
    _fingerprint: str
    _files: tuple[_RestoreFile, ...]
    _guards: tuple[tuple[str, _Snapshot], ...]

    @property
    def changes(self):
        #每次返回新字典；确认界面拿到的列表不能反过来改写已冻结的恢复目标。
        return [{"path": item.path, "diff": item.diff} for item in self._files]

    @property
    def diff(self):
        return "\n\n".join(item.diff for item in self._files)


class CheckpointStore:
    """一个 Session 一份增量版本树；构造时不读取磁盘，不自动恢复代码。"""

    def __init__(self, workspace: Path, session_id: str):
        self.workspace = Path(workspace).resolve()
        if not isinstance(session_id, str) or not re.fullmatch(r"[0-9a-f]{32}", session_id):
            raise CheckpointError("无效的检查点会话 ID")
        self.session_id = session_id
        self.root = self.workspace / ".task_outputs" / "checkpoints" / session_id

    @staticmethod
    def _parts(path):
        if (not isinstance(path, str) or not path or "\x00" in path or "\\" in path
                or Path(path).is_absolute() or any(part in {"", ".", ".."} for part in path.split("/"))
                or path.split("/")[0] in _HOST_PATHS):
            raise CheckpointError("检查点路径越界或属于宿主数据目录")
        return path.split("/")

    @contextmanager
    def _directory(self, parts, *, create=False):
        descriptor = os.open(self.workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts:
                if create:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            yield descriptor
        finally:
            os.close(descriptor)

    @contextmanager
    def _locked(self, *, create=False):
        try:
            directory = self._directory(self.root.relative_to(self.workspace).parts, create=create)
            with directory as parent:
                lock = os.open(".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=parent)
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    yield parent
                finally:
                    os.close(lock)
        except CheckpointError:
            raise
        except OSError as error:
            raise CheckpointError(f"检查点存储操作失败：{type(error).__name__}") from None

    @staticmethod
    def _read(parent, name):
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise CheckpointError("检查点记录必须是普通文件")
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = None
                raw = handle.read(_MAX_JSON + 1)
            if len(raw) > _MAX_JSON:
                raise CheckpointError("检查点记录过大")
            return json.loads(raw)
        except (ValueError, TypeError):
            raise CheckpointError("检查点记录不是有效 JSON") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _write(parent, name, data):
        raw = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(raw) > _MAX_JSON:
            raise CheckpointError("单轮检查点记录过大，请缩小修改范围")
        temporary = f".{uuid.uuid4().hex}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass

    def _state(self, parent):
        try:
            state = self._read(parent, "index.json")
        except FileNotFoundError:
            return {"version": 1, "workspace": str(self.workspace), "session_id": self.session_id,
                    "head": "C0", "next_id": 1, "nodes": {}, "active": None, "restore_pending": False}
        if (not isinstance(state, dict) or type(state.get("version")) is not int or state.get("version") != 1
                or state.get("workspace") != str(self.workspace) or state.get("session_id") != self.session_id
                or not isinstance(state.get("nodes"), dict) or type(state.get("next_id")) is not int
                or state["next_id"] < 1 or not isinstance(state.get("head"), str)
                or state["head"] not in {"C0", *state["nodes"]}
                or type(state.get("restore_pending")) is not bool
                or (state.get("active") is not None and not isinstance(state["active"], str))):
            raise CheckpointError("检查点索引格式或工作区不匹配")
        for identifier, node in state["nodes"].items():
            if (not isinstance(identifier, str) or not _ID.fullmatch(identifier) or identifier == "C0"
                    or len(identifier) > 18 or not isinstance(node, dict) or node.get("id") != identifier
                    or not isinstance(node.get("parent"), str)
                    or node.get("parent") not in {"C0", *state["nodes"]}
                    or any(not isinstance(node.get(key), str) for key in
                           ("turn_id", "label", "status", "created_at", "kind"))):
                raise CheckpointError("检查点索引节点损坏")
            if int(identifier[1:]) >= state["next_id"]:
                raise CheckpointError("检查点编号计数损坏")
        for identifier in state["nodes"]:
            self._ancestors(state, identifier)
        return state

    @staticmethod
    def _fingerprint(state):
        return hashlib.sha256(json.dumps(state, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    @staticmethod
    def _ancestors(state, identifier):
        result = []
        while identifier != "C0":
            if identifier in result or identifier not in state["nodes"]:
                raise CheckpointError("检查点父版本链损坏")
            result.append(identifier)
            identifier = state["nodes"][identifier]["parent"]
        return [*result, "C0"]

    def _entries(self, node):
        files = node.get("files")
        if not isinstance(files, dict) or len(files) > _MAX_FILES:
            raise CheckpointError("检查点文件列表损坏或过大")
        result = {}
        for path, entry in files.items():
            self._parts(path)
            if not isinstance(entry, dict) or set(entry) != {"before", "after"}:
                raise CheckpointError("检查点文件记录损坏")
            result[path] = (_Snapshot.checked(entry["before"]), _Snapshot.checked(entry["after"]))
        return result

    def _node(self, parent, state, identifier):
        value = self._read(parent, f"{identifier}.json")
        if not isinstance(value, dict) or {key: value.get(key) for key in state["nodes"][identifier]} != state["nodes"][identifier]:
            raise CheckpointError("检查点节点与索引不一致")
        self._entries(value)
        return value

    def _snapshot(self, path):
        parts = self._parts(path)
        try:
            with self._directory(parts[:-1]) as parent:
                descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                with os.fdopen(descriptor, "rb") as handle:
                    before = os.fstat(handle.fileno())
                    if not stat.S_ISREG(before.st_mode):
                        raise CheckpointError(f"不是可恢复的普通文件：{path}")
                    payload = handle.read(MAX_FILE_BYTES + 1)
                    after = os.fstat(handle.fileno())
                    if len(payload) > MAX_FILE_BYTES:
                        raise CheckpointError(f"文件超过检查点大小限制：{path}")
                    signature = lambda item: (item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
                    if signature(before) != signature(after):
                        raise CheckpointError(f"读取快照期间文件发生变化：{path}")
                    return _Snapshot.from_bytes(payload, stat.S_IMODE(after.st_mode))
        except FileNotFoundError:
            return _Snapshot.missing()
        except OSError:
            raise CheckpointError(f"文件路径不安全或无法读取：{path}") from None

    def _known(self, parent, state, identifier, path):
        for current in self._ancestors(state, identifier):
            if current == "C0":
                break
            entries = self._entries(self._node(parent, state, current))
            if path in entries:
                return entries[path][1]
        #回到 C0 或较早分支时，文件可能还没在该祖先中出现；它首次进入版本树时的
        #before 仍是已知懒基线，不能把后续手工修改误当成从未跟踪过的新文件。
        for current in sorted(state["nodes"], key=lambda value: int(value[1:])):
            entries = self._entries(self._node(parent, state, current))
            if path in entries:
                return entries[path][0]
        return None

    def begin(self, turn_id, label):
        return _Recorder(self, turn_id, label)

    def _commit_node(self, parent, state, *, parent_id, files, turn_id, label, status, kind):
        identifier = f"C{state['next_id']}"
        state["next_id"] += 1
        metadata = {"id": identifier, "parent": parent_id, "turn_id": turn_id, "label": label[:160],
                    "status": status, "created_at": _now(), "kind": kind, "file_count": len(files)}
        self._write(parent, f"{identifier}.json", {**metadata, "files": files})
        state["nodes"][identifier] = metadata
        state["head"] = identifier
        return identifier

    def _finish_pending(self, parent, state, status):
        pending = self._read(parent, "pending.json")
        if (not isinstance(pending, dict) or pending.get("turn_id") != state["active"]
                or pending.get("parent") != state["head"] or not isinstance(pending.get("label"), str)
                or not isinstance(pending.get("external"), dict)):
            raise CheckpointError("未完成检查点记录损坏")
        files = self._entries(pending)
        operation = pending.get("operation")
        uncertain = False
        if operation is not None:
            if not isinstance(operation, dict) or operation.get("path") not in files:
                raise CheckpointError("未完成文件修改记录损坏")
            path = operation["path"]
            previous = _Snapshot.checked(operation["before"])
            intended = _Snapshot.checked(operation["after"])
            actual = self._snapshot(path)
            if actual == intended:
                files[path] = (files[path][0], intended)
            elif actual != previous:
                uncertain = True
        cleaned = {}
        for path, (before, after) in files.items():
            actual = self._snapshot(path)
            if actual != after:
                uncertain = True
                after = actual
            if before != after:
                cleaned[path] = {"before": before.json(), "after": after.json()}
        external_node = {"files": pending["external"]}
        self._entries(external_node)
        if pending["external"]:
            self._commit_node(parent, state, parent_id=state["head"], files=pending["external"],
                              turn_id=pending["turn_id"], label="用户或其他程序的外部修改基线",
                              status="external", kind="external")
        identifier = None
        if cleaned:
            identifier = self._commit_node(parent, state, parent_id=state["head"], files=cleaned,
                                           turn_id=pending["turn_id"], label=pending["label"],
                                           status="uncertain" if uncertain else status, kind="agent")
        state["active"] = None
        self._write(parent, "index.json", state)
        return identifier

    def _ready(self, parent, *, recover=True):
        state = self._state(parent)
        if state["restore_pending"]:
            #崩溃后只核对状态；不在恢复会话或列历史时继续写入用户代码。
            journal = self._read(parent, "restore.json")
            entries = self._entries(journal)
            if all(self._snapshot(path) == before for path, (before, _) in entries.items()):
                state["restore_pending"] = False
                self._write(parent, "index.json", state)
            elif all(self._snapshot(path) == after for path, (_, after) in entries.items()):
                target = journal.get("target_id")
                if target not in {"C0", *state["nodes"]}:
                    raise CheckpointError("恢复日志的目标检查点损坏")
                state["head"], state["restore_pending"] = target, False
                self._write(parent, "index.json", state)
            else:
                raise CheckpointError("代码恢复曾被中断，部分文件状态未知；请先检查 restore.json 和当前文件，禁止继续自动恢复")
        if recover and state["active"] is not None:
            self._finish_pending(parent, state, "interrupted")
        return state

    def list_checkpoints(self):
        #无写入的旧会话也能列出 C0；不为只读查询新建存储目录。
        if not self.root.exists() and not self.root.is_symlink():
            return [{"id": "C0", "parent": None, "turn_id": "", "label": "文件工具首次修改前的基线",
                     "status": "baseline", "kind": "baseline", "is_head": True}]
        with self._locked() as parent:
            state = self._ready(parent)
            return [{"id": "C0", "parent": None, "turn_id": "", "label": "文件工具首次修改前的基线",
                     "status": "baseline", "kind": "baseline", "is_head": state["head"] == "C0"},
                    *[{**node, "is_head": identifier == state["head"]}
                      for identifier, node in state["nodes"].items()]]

    @staticmethod
    def _diff(path, before, after):
        text = "".join(difflib.unified_diff(
            before.payload().decode("utf-8", errors="replace").splitlines(keepends=True),
            after.payload().decode("utf-8", errors="replace").splitlines(keepends=True),
            fromfile=f"a/{path}" if before.exists else "/dev/null",
            tofile=f"b/{path}" if after.exists else "/dev/null"))
        if before.mode != after.mode:
            text += f"\nmode: {before.mode!r} -> {after.mode!r}\n"
        return text or f"{path}: (no content changes)"

    def plan_restore(self, target_id=None):
        with self._locked(create=True) as parent:
            state = self._ready(parent)
            current = state["head"]
            if target_id is None:
                if current == "C0":
                    raise CheckpointError("当前没有可撤销的 Agent 修改回合")
                metadata = state["nodes"][current]
                if metadata["kind"] == "external":
                    raise CheckpointError("当前是外部修改基线；/undo 不撤销你的手动修改，请显式选择 /restore Cn")
                target_id = metadata["parent"]
            if not isinstance(target_id, str) or not _ID.fullmatch(target_id) or target_id not in {"C0", *state["nodes"]}:
                raise CheckpointError("检查点不存在；使用 /checkpoints 查看可用版本")
            current_chain, target_chain = self._ancestors(state, current), self._ancestors(state, target_id)
            common = next(identifier for identifier in current_chain if identifier in target_chain)
            backward = current_chain[:current_chain.index(common)]
            forward = list(reversed(target_chain[:target_chain.index(common)]))
            original, simulated = {}, {}
            for identifier, reverse in [(value, True) for value in backward] + [(value, False) for value in forward]:
                node = self._node(parent, state, identifier)
                if node["status"] == "uncertain":
                    raise CheckpointError(f"检查点 {identifier} 含结果未知的文件变化，不能自动恢复")
                for path, (before, after) in self._entries(node).items():
                    if path not in simulated:
                        original[path] = simulated[path] = self._snapshot(path)
                    expected, replacement = (after, before) if reverse else (before, after)
                    if simulated[path] != expected:
                        raise CheckpointError(f"文件已被手动修改或版本链不连续，未恢复任何文件：{path}")
                    simulated[path] = replacement
            files = tuple(_RestoreFile(path, original[path], simulated[path],
                                       self._diff(path, original[path], simulated[path]))
                          for path in sorted(original) if original[path] != simulated[path])
            #净差为零的文件也参与版本路径校验，确认期间变动时不能悄悄切换 head。
            guards = tuple((path, original[path]) for path in sorted(original))
            return RestorePlan(current, target_id, self._fingerprint(state), files, guards)

    def _write_snapshot(self, path, target, expected):
        parts = self._parts(path)
        with self._directory(parts[:-1], create=target.exists) as parent:
            if self._snapshot(path) != expected:
                raise CheckpointError(f"恢复前文件再次变化：{path}")
            if not target.exists:
                if expected.exists:
                    os.unlink(parts[-1], dir_fd=parent)
                return
            temporary = f".{uuid.uuid4().hex}.checkpoint.tmp"
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=parent)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(target.payload())
                    os.fchmod(handle.fileno(), target.mode)
                    handle.flush()
                    os.fsync(handle.fileno())
                if self._snapshot(path) != expected:
                    raise CheckpointError(f"恢复前文件再次变化：{path}")
                os.replace(temporary, parts[-1], src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
            finally:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass

    def apply_restore(self, plan):
        if not isinstance(plan, RestorePlan):
            raise CheckpointError("需要宿主生成并确认的恢复方案")
        with self._locked(create=True) as parent:
            state = self._ready(parent)
            if state["head"] != plan.from_id or self._fingerprint(state) != plan._fingerprint:
                raise CheckpointError("检查点状态在预览后发生变化，请重新预览")
            for path, expected in plan._guards:
                if self._snapshot(path) != expected:
                    raise CheckpointError(f"文件在确认期间发生变化，未恢复任何文件：{path}")
            journal = {"from_id": plan.from_id, "target_id": plan.target_id,
                       "files": {item.path: {"before": item.before.json(), "after": item.after.json()}
                                 for item in plan._files}}
            self._write(parent, "restore.json", journal)
            state["restore_pending"] = True
            self._write(parent, "index.json", state)
            attempted = []
            committing = False
            try:
                for item in plan._files:
                    check_cancelled()
                    attempted.append(item)
                    self._write_snapshot(item.path, item.after, item.before)
                state["head"], state["restore_pending"] = plan.target_id, False
                committing = True
                self._write(parent, "index.json", state)
            except BaseException as error:
                if committing:
                    #replace 成功后 fsync 仍可能失败；此时不能再回滚代码，否则磁盘已是
                    #目标 head 而文件回到原版。保持全部目标文件：索引要么已提交，要么仍带
                    #restore_pending，后续只核对并补记元数据，不自动继续改写用户代码。
                    if not isinstance(error, Exception):
                        raise
                    raise CheckpointError(
                        "恢复文件已写入，但检查点提交或磁盘同步未确认；本次操作可能已生效，"
                        "请使用 /checkpoints 核对，不能把此错误当作未修改代码"
                    ) from None
                failed = False
                for item in reversed(attempted):
                    try:
                        actual = self._snapshot(item.path)
                        if actual == item.after:
                            self._write_snapshot(item.path, item.before, item.after)
                        elif actual != item.before:
                            failed = True
                    except BaseException:
                        failed = True
                if failed:
                    raise CheckpointError("代码恢复未全部完成，且无法安全恢复原状态；已锁定自动恢复，请检查 restore.json") from None
                state["head"], state["restore_pending"] = plan.from_id, False
                self._write(parent, "index.json", state)
                raise
            return f"已从 {plan.from_id} 恢复到 {plan.target_id}，恢复 {len(plan._files)} 个受跟踪文件。"


class _Recorder:
    def __init__(self, store, turn_id, label):
        if not isinstance(turn_id, str) or not turn_id or not isinstance(label, str):
            raise CheckpointError("无效的检查点回合")
        self.store, self.turn_id, self.label = store, turn_id, label[:160]
        self._finished = False
        self._identifier = None

    def prepare(self, change):
        store = self.store
        path = Path(change.file_path).relative_to(store.workspace).as_posix()
        store._parts(path)
        with store._locked(create=True) as parent:
            state = store._ready(parent, recover=False)
            if self._finished:
                raise CheckpointError("本轮检查点已结束")
            if state["active"] not in {None, self.turn_id}:
                store._finish_pending(parent, state, "interrupted")
            if state["active"] is None:
                pending = {"turn_id": self.turn_id, "label": self.label, "parent": state["head"],
                           "files": {}, "external": {}, "operation": None}
            else:
                pending = store._read(parent, "pending.json")
                store._entries(pending)
                if pending.get("operation") is not None:
                    raise CheckpointError("上一条文件修改结果尚未确认")
            actual = store._snapshot(path)
            if actual.sha256 != change.expected:
                raise CheckpointError("文件在检查点保存前发生变化，请重新读取")
            entry = pending["files"].get(path)
            if entry is not None and actual != _Snapshot.checked(entry["after"]):
                raise CheckpointError("文件在本轮被外部程序修改，请结束本轮并重新读取")
            if entry is None:
                known = store._known(parent, state, pending["parent"], path)
                if known is not None and known != actual:
                    pending["external"][path] = {"before": known.json(), "after": actual.json()}
                entry = {"before": actual.json(), "after": actual.json()}
                pending["files"][path] = entry
            if len(pending["files"]) > _MAX_FILES:
                raise CheckpointError("单轮修改文件过多，请拆分任务")
            payload = change.content.encode("utf-8")
            if len(payload) > MAX_FILE_BYTES:
                raise CheckpointError("文件内容超过检查点大小限制")
            intended = _Snapshot.from_bytes(payload, actual.mode if actual.exists else 0o600)
            token = uuid.uuid4().hex
            pending["operation"] = {"token": token, "path": path,
                                    "before": actual.json(), "after": intended.json()}
            store._write(parent, "pending.json", pending)
            state["active"] = self.turn_id
            store._write(parent, "index.json", state)
            return token

    def applied(self, token):
        store = self.store
        with store._locked(create=True) as parent:
            state = store._state(parent)
            pending = store._read(parent, "pending.json")
            operation = pending.get("operation")
            if state["active"] != self.turn_id or not operation or operation.get("token") != token:
                raise CheckpointError("文件修改与检查点记录不匹配")
            actual = store._snapshot(operation["path"])
            intended = _Snapshot.checked(operation["after"])
            if actual != intended:
                raise CheckpointError("文件写入后状态不匹配，保留待核实记录")
            pending["files"][operation["path"]]["after"] = intended.json()
            pending["operation"] = None
            store._write(parent, "pending.json", pending)

    def failed(self, token):
        """文件工具失败也核对磁盘；未生效才释放待写记录，已落盘则仍保留真实修改。"""
        store = self.store
        with store._locked(create=True) as parent:
            state = store._state(parent)
            pending = store._read(parent, "pending.json")
            operation = pending.get("operation")
            if state["active"] != self.turn_id or not operation or operation.get("token") != token:
                raise CheckpointError("失败文件修改与检查点记录不匹配")
            actual = store._snapshot(operation["path"])
            before, after = _Snapshot.checked(operation["before"]), _Snapshot.checked(operation["after"])
            if actual not in {before, after}:
                raise CheckpointError("文件修改失败后状态未知，请结束本轮核实")
            pending["files"][operation["path"]]["after"] = actual.json()
            pending["operation"] = None
            store._write(parent, "pending.json", pending)

    def finish(self, status):
        if self._finished:
            return self._identifier
        if not self.store.root.exists():
            self._finished = True
            return None
        with self.store._locked() as parent:
            state = self.store._state(parent)
            if state["active"] == self.turn_id:
                self._identifier = self.store._finish_pending(parent, state, str(status)[:32])
            elif state["active"] is not None:
                raise CheckpointError("另一个回合正在记录检查点")
        self._finished = True
        return self._identifier
