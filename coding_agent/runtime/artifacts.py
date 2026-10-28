"""运行证据存储；完整工具结果留在磁盘，传给模型的只是有界预览和引用。"""

import json
import os
from pathlib import Path
import tempfile

from coding_agent.runtime.session import plain_value


class ArtifactStore:
    def __init__(self, workspace: Path, turn_id: str):
        self.workspace = workspace.resolve()
        self.root = self.workspace / ".task_outputs" / "runs" / turn_id
        self.counter = 0

    def save(self, label: str, payload) -> str:
        """逐层拒绝符号链接，避免运行记录被重定向到工作区以外。"""
        encoded = json.dumps(plain_value(payload), ensure_ascii=False, indent=2).encode("utf-8")
        return self.save_bytes(label, encoded, suffix=".json")

    def save_bytes(self, label: str, payload: bytes, *, suffix: str) -> str:
        """图片与 JSON 共用原子写入；后缀由宿主提供，不接受外部文件路径。"""
        if suffix not in {".json", ".png", ".jpg"}:
            raise ValueError("不支持的运行记录文件类型")
        parent = self.workspace
        for part in self.root.relative_to(self.workspace).parts:
            parent = parent / part
            if parent.is_symlink():
                raise ValueError("运行记录目录不能是符号链接")
            parent.mkdir(mode=0o700, exist_ok=True)
        self.counter += 1
        label = "".join(c for c in label if c.isalnum() or c in "-_")[:60] or "result"
        path = self.root / f"{self.counter:04d}-{label}{suffix}"
        descriptor, name = tempfile.mkstemp(dir=self.root, suffix=".tmp")
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return str(path)
