"""Markdown 记忆存储：正文是源数据，MEMORY.md 只是可以重建的短索引。"""

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

import yaml

from coding_agent.runtime.turn import check_cancelled


MEMORY_TYPES = {"user", "feedback", "project", "reference"}
MAX_RECORDS = 200
MAX_DOCUMENT_CHARS = 12000


def memory_slug(name):
    """只从名称生成文件名；模型不能用 ../、绝对路径或索引名选择写入目标。"""
    slug = re.sub(r"[^\w-]+", "-", name.lower()).strip("-_")
    if len(slug) > 80:
        slug = slug[:68] + "-" + hashlib.sha256(name.encode()).hexdigest()[:10]
    if not slug or slug == "memory":
        raise ValueError("记忆名称为空或与 MEMORY.md 索引冲突")
    return slug


def validate_record(record):
    """拒绝把列表、布尔值等强转成字符串；长度上限同时约束文件和模型输入。"""
    limits = {"name": 100, "description": 300, "body": 6000, "type": 20}
    if not isinstance(record, dict):
        raise ValueError("记忆必须是对象")
    clean = {}
    for key, limit in limits.items():
        value = record.get(key)
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= limit:
            raise ValueError(f"无效记忆字段: {key}")
        clean[key] = value.strip()
    if clean["type"] not in MEMORY_TYPES:
        raise ValueError("未知记忆类型")
    memory_slug(clean["name"])
    return clean


def memory_document(record):
    metadata = {key: record[key] for key in ("name", "description", "type")}
    metadata["updated_at"] = datetime.now(timezone.utc).isoformat()
    #safe_dump / safe_load 不允许 YAML 构造任意 Python 对象。
    header = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{header}\n---\n\n{record['body']}\n"


class MemoryStore:
    def __init__(self, anchor: Path, relative=".memory"):
        self.anchor = Path(anchor).resolve()
        self.relative = Path(relative)
        if self.relative.is_absolute() or ".." in self.relative.parts or not self.relative.parts:
            raise ValueError("记忆目录必须位于宿主指定的边界内")
        self.root = self.anchor / self.relative

    @contextmanager
    def directory(self, create=False):
        """逐层用目录描述符打开；O_NOFOLLOW 防止目录在检查后被换成符号链接。"""
        descriptor = os.open(self.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in self.relative.parts:
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

    @staticmethod
    def read_text(parent, name, limit=MAX_DOCUMENT_CHARS):
        """只读普通文件，既不跟随链接，也不阻塞在命名管道上。"""
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("记忆和规则必须是普通文件")
            with os.fdopen(descriptor, encoding="utf-8") as handle:
                descriptor = None
                text = handle.read(limit + 1)
                if len(text) > limit:
                    raise ValueError("记忆文件超过长度上限")
                return text
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _atomic_write(parent, name, text):
        """600 权限的唯一临时文件 + replace，避免读到半份正文或索引。"""
        temporary = f".memory-{uuid.uuid4().hex}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass

    @contextmanager
    def _locked(self):
        with self.directory(create=True) as parent:
            descriptor = os.open(".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 0o600, dir_fd=parent)
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise ValueError("无效记忆锁")
                #LOCK_NB 不等待另一会话的长事务；本轮可以跳过记忆，不阻塞编码工作。
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                yield parent
            finally:
                os.close(descriptor)

    def _documents(self, parent):
        names = sorted(name for name in os.listdir(parent) if name.endswith(".md") and name != "MEMORY.md")
        if len(names) > MAX_RECORDS:
            raise ValueError("记忆条数超过上限，请先手动整理")
        return {name: self.read_text(parent, name) for name in names}

    @staticmethod
    @contextmanager
    def _read_lock(parent):
        """已有存储使用共享锁读取，避免撞上另一进程正在执行的批量整理。"""
        try:
            descriptor = os.open(".lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        except FileNotFoundError:
            #手工创建的记忆目录可以没有锁；只读操作不为它制造文件。
            yield
            return
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("无效记忆锁")
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
            yield
        finally:
            os.close(descriptor)

    @staticmethod
    def _records(documents):
        records = []
        for filename, text in documents.items():
            if not text.startswith("---\n"):
                continue
            parts = text.split("\n---\n", 1)
            if len(parts) != 2:
                continue
            try:
                metadata = yaml.safe_load(parts[0][4:])
                record = validate_record({**metadata, "body": parts[1].strip()})
            except (TypeError, ValueError, yaml.YAMLError):
                continue
            updated = metadata.get("updated_at")
            if isinstance(updated, str) and len(updated) <= 40:
                try:
                    datetime.fromisoformat(updated)
                    record["updated_at"] = updated
                except ValueError:
                    pass  #兼容没有时间戳的手工记录，但不捏造它的新旧顺序。
            records.append({**record, "filename": filename})
        return records

    @staticmethod
    def signature(documents):
        return hashlib.sha256(json.dumps(documents, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def snapshot(self):
        try:
            with self.directory() as parent:
                with self._read_lock(parent):
                    documents = self._documents(parent)
        except FileNotFoundError:
            documents = {}
        return self._records(documents), self.signature(documents)

    @staticmethod
    def _index(documents):
        lines = ["# Memory catalog", "", "<!-- Generated; edit individual records, not this index. -->", ""]
        for record in MemoryStore._records(documents):
            name = " ".join(record["name"].split()).replace("[", "(").replace("]", ")")
            description = " ".join(record["description"].split())
            lines.append(f"- [{name}]({record['filename']}) ({record['type']}) — {description}")
        return "\n".join(lines) + "\n"

    def _commit(self, parent, old, new):
        """替换前留快照；异常（包括 Ctrl+C）时恢复。模型请求不在锁内执行。"""
        backup = f".snapshot-{uuid.uuid4().hex}.json"
        check_cancelled()
        self._atomic_write(parent, backup, json.dumps(old, ensure_ascii=False))
        try:
            for name, text in new.items():
                check_cancelled()
                if old.get(name) != text:
                    self._atomic_write(parent, name, text)
            for name in old.keys() - new.keys():
                check_cancelled()
                os.unlink(name, dir_fd=parent)
            self._atomic_write(parent, "MEMORY.md", self._index(new))
            check_cancelled()
        except BaseException:
            for name in new.keys() - old.keys():
                try:
                    os.unlink(name, dir_fd=parent)
                except FileNotFoundError:
                    pass
            for name, text in old.items():
                self._atomic_write(parent, name, text)
            self._atomic_write(parent, "MEMORY.md", self._index(old))
            raise

    def save(self, candidates, expected):
        """已存在的同名记录可以纠正；同正文不重复写。版本变了则拒绝覆盖并发修改。"""
        clean = [validate_record(item) for item in candidates]
        with self._locked() as parent:
            old = self._documents(parent)
            if self.signature(old) != expected:
                raise ValueError("记忆已被另一会话修改，本次提取跳过")
            new = dict(old)
            changed = 0
            for record in clean:
                filename = memory_slug(record["name"]) + ".md"
                normalized = " ".join(record["body"].split())
                if any(" ".join(item["body"].split()) == normalized
                       for item in self._records(new)):
                    continue
                #损坏或手工命名的文件不自动覆盖，保留给用户检查。
                if filename in new and not self._records({filename: new[filename]}):
                    raise ValueError("目标记忆文件格式无效，拒绝覆盖")
                new[filename] = memory_document(record)
                changed += 1
            if len(new) > MAX_RECORDS:
                raise ValueError("记忆条数超过上限")
            if changed:
                self._commit(parent, old, new)
            return changed

    def replace(self, records, expected):
        """整理必须完整校验，不能把模型返回的半份列表当作成功。"""
        clean = [validate_record(item) for item in records]
        names = [memory_slug(item["name"]) + ".md" for item in clean]
        if not names or len(names) != len(set(names)):
            raise ValueError("整理结果为空或包含重名记录")
        with self._locked() as parent:
            old = self._documents(parent)
            if self.signature(old) != expected or len(self._records(old)) != len(old):
                raise ValueError("记忆已变化或含损坏文件，拒绝批量整理")
            self._commit(parent, old, dict(zip(names, map(memory_document, clean))))

    def read(self, filename):
        if Path(filename).name != filename or not filename.endswith(".md") or filename == "MEMORY.md":
            raise ValueError("无效记忆文件名")
        with self.directory() as parent:
            with self._read_lock(parent):
                return self.read_text(parent, filename)

    def forget(self, filename):
        """只删除用户指定的一条记录；原文仍可从本地 .snapshot 文件恢复。"""
        with self._locked() as parent:
            old = self._documents(parent)
            if filename not in old:
                raise ValueError("没有这条记忆")
            new = {name: text for name, text in old.items() if name != filename}
            self._commit(parent, old, new)
