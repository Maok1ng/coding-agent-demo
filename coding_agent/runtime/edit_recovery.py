"""本轮编辑失败保护；宿主状态不放进模型历史，也不随上下文压缩重建。"""

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import json


RECOVERY_THRESHOLD = 3
STOP_THRESHOLD = 6
MAX_FAILURE_PATHS = 20
RECOVERY_MESSAGE = (
    "同一文件的相同修改已连续失败 3 次。请先重新读取当前文件，核对错误原因，"
    "再生成不同的修改候选；无法解决时向用户提问。重复原样重试不会产生进展。"
)
STOP_MESSAGE = (
    "同一文件的相同修改已连续失败 6 次，本轮已停止。请检查工具错误、当前文件或补充说明后再继续；"
    "本次失败的修改没有应用。"
)


@dataclass
class _Failure:
    fingerprint: str
    count: int = 1


class EditRecovery:
    """按文件保存最近失败候选；角色共享，读取成功和压缩不会清空计数。"""

    def __init__(self):
        self._failures: OrderedDict[str, _Failure] = OrderedDict()
        self._stopped = False

    @property
    def stopped(self):
        return self._stopped

    def failed(self, name: str, arguments: dict, reason: str, path: str):
        """同一候选及真实原因累计；只保留哈希，避免把整份候选代码常驻内存。"""
        if self._stopped:
            return "stop"
        #规范路径替换模型原路径，./main.py 与 main.py 不能绕过同一次失败计数。
        canonical = {**arguments, "path": path}
        payload = json.dumps([name, canonical, reason], sort_keys=True, ensure_ascii=True,
                             separators=(",", ":")).encode("utf-8")
        fingerprint = hashlib.sha256(payload).hexdigest()
        previous = self._failures.pop(path, None)
        failure = (_Failure(fingerprint, previous.count + 1)
                   if previous is not None and previous.fingerprint == fingerprint
                   else _Failure(fingerprint))
        self._failures[path] = failure
        #保留不同文件的独立进度，交替尝试文件不会清掉已有失败；限制常驻记录数量。
        while len(self._failures) > MAX_FAILURE_PATHS:
            self._failures.popitem(last=False)
        if failure.count >= STOP_THRESHOLD:
            self._stopped = True
            return "stop"
        if failure.count == RECOVERY_THRESHOLD:
            return "recover"
        return None

    def succeeded(self, path: str):
        """仅实际落盘成功才清理此文件；已经触发的整轮停止标记不可撤销。"""
        self._failures.pop(path, None)

    def snapshot(self):
        """供宿主证据记录使用；没有原参数、源码或可恢复的权限批准。"""
        return {"stopped": self._stopped, "failures": [
            {"path": path, "fingerprint": failure.fingerprint, "count": failure.count}
            for path, failure in self._failures.items()
        ]}
