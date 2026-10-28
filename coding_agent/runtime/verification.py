"""宿主检查结果整理：记录退出事实，并核对它们对应的代码版本。"""

from collections import OrderedDict
import hashlib
import os
from pathlib import Path
import re
import stat

from coding_agent.runtime.turn import check_cancelled
from coding_agent.runtime.workspace import MAX_FILE_BYTES


MAX_COMMANDS = 20
MAX_TRACKED_FILES = 200
MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
_DIGEST = re.compile(r"[0-9a-f]{64}")


def _relative(root, value):
    """历史记录只能提供工作区内路径；这里不 resolve，避免先跟随链接再做检查。"""
    if not isinstance(value, str) or not value or len(value) > 2000 or "\x00" in value:
        return None
    path = Path(value)
    if path.is_absolute():
        try:
            path = path.relative_to(root)
        except ValueError:
            return None
    if ".." in path.parts or not path.parts:
        return None
    return path.as_posix()


def _snapshot(root, relative, budget):
    """只哈希已登记的普通文件；逐层拒绝链接、管道和超大文件，不读取正文到报告。"""
    descriptor = None
    try:
        check_cancelled()
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        parts = Path(relative).parts
        for part in parts[:-1]:
            check_cancelled()
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        child = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        os.close(descriptor)
        descriptor = child
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE_BYTES:
            return "unavailable"
        if before.st_size > budget[0]:
            return "budget_limited"
        digest, total = hashlib.sha256(), 0
        while True:
            check_cancelled()
            chunk = os.read(descriptor, min(65536, MAX_FILE_BYTES + 1 - total, budget[0] + 1))
            if not chunk:
                break
            total += len(chunk)
            budget[0] -= len(chunk)
            if total > MAX_FILE_BYTES or budget[0] < 0:
                return "budget_limited"
            digest.update(chunk)
        after = os.fstat(descriptor)
        #文件在哈希期间改动时不能把混合内容当作某个稳定版本。
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(before, field) != getattr(after, field) for field in fields):
            return "changed_during_snapshot"
        return digest.hexdigest()
    except FileNotFoundError:
        return "missing"
    except (OSError, RuntimeError):
        return "unavailable"
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _versions(root, value):
    if not isinstance(value, dict):
        return {}
    return {relative: digest for filename, digest in value.items()
            if (relative := _relative(root, filename)) is not None and isinstance(digest, str)}


def capture_source_versions(workspace: Path, filenames) -> dict[str, str]:
    """命令边界复核已跟踪文件；返回摘要/不可用标记，绝不增加新的正文或编辑权限。"""
    check_cancelled()
    root = Path(workspace)
    budget = [MAX_SNAPSHOT_BYTES]
    snapshots = {}
    for filename in filenames:
        check_cancelled()
        relative = _relative(root, str(filename) if isinstance(filename, Path) else filename)
        if relative is None:
            continue
        absolute = str(root / relative)
        if absolute in snapshots:
            continue
        snapshots[absolute] = (_snapshot(root, relative, budget) if len(snapshots) < MAX_TRACKED_FILES
                               else "budget_limited")
    return snapshots


def _outcome(record):
    """退出码为 0 只是必要条件；取消、超时、清理异常优先于它。"""
    status = record.get("status")
    if record.get("cleanup_confirmed", True) is not True or status == "cleanup_failed":
        return "incomplete"
    if record.get("cancelled") is True or status == "cancelled":
        return "cancelled"
    if record.get("timed_out") is True or status == "timed_out":
        return "timed_out"
    if status == "running":
        return "running"
    if status == "completed" and type(record.get("exit_code")) is int and record["exit_code"] == 0:
        return "successful"
    return "failed" if status in {"failed", "completed"} else "incomplete"


def build_verification_summary(context):
    """从本轮真实事实生成有界报告；不执行命令，也不推断用户要求都已被测试覆盖。"""
    check_cancelled()
    root = Path(context.workspace)
    records = OrderedDict()
    for number, record in enumerate(context.verification_records()):
        check_cancelled()
        #同一后台进程可能被多次 wait/status，再于收尾登记；保留最终状态而非重复计数。
        identifier = record.get("id")
        key = ("id", identifier) if isinstance(identifier, str) and identifier else ("row", number)
        previous = records.get(key, {})
        records[key] = {**previous, **record}

    ordered_records = list(records.values())
    if ordered_records and all(type(record.get("execution_order")) is int for record in ordered_records):
        #后台晚结束不代表启动得晚；以宿主启动顺序判断哪一次才是最终重跑。
        ordered_records.sort(key=lambda record: record["execution_order"])

    changes = {}
    for filename, change in getattr(context, "changes", {}).items():
        if (relative := _relative(root, filename)) is not None:
            changes[relative] = change
    normalized = []
    tracked = OrderedDict((path, None) for path in changes)
    for record in ordered_records:
        before = _versions(root, record.get("source_versions"))
        after = _versions(root, record.get("source_versions_after"))
        normalized.append((record, before, after))
        tracked.update((path, None) for path in before)
        tracked.update((path, None) for path in after)
    current = {_relative(root, path): digest for path, digest in capture_source_versions(root, tracked).items()}
    commands = []
    command_keys = []
    for record, before, after in normalized:
        check_cancelled()
        stale, unknown = [], []
        for path in dict.fromkeys([*before, *after, *changes]):
            start, end, actual = before.get(path), after.get(path), current.get(path)
            if path in changes and start is None:
                #修改后新增的文件从未进入该命令的快照；旧检查不能替新文件背书。
                stale.append(path)
            elif not isinstance(start, str) or not _DIGEST.fullmatch(start):
                unknown.append(path)
            elif actual == "missing" or (_DIGEST.fullmatch(actual or "") and actual != start):
                stale.append(path)
            elif end is not None and end != start:
                stale.append(path)
            elif actual != start:
                unknown.append(path)
        freshness = "stale" if stale else "unverified" if unknown or not before else "current"
        command = str(record.get("command", ""))
        #精确分组使用完整命令；展示截断后的相同前缀不能让另一个命令消除失败。
        command_keys.append(command if command else ("unnamed", len(commands)))
        commands.append({
            "id": record.get("id"), "command": command[:2000],
            "attempt": len(commands) + 1,
            "execution_order": record.get("execution_order"),
            "command_truncated": len(command) > 2000,
            "status": record.get("status"), "exit_code": record.get("exit_code"),
            "outcome": _outcome(record), "freshness": freshness,
            "stale_paths": stale[:20], "unverified_paths": unknown[:20],
            "reference": record.get("reference"), "full_output_path": record.get("full_output_path"),
        })
    latest = {key: index for index, key in enumerate(command_keys)}
    for index, command in enumerate(commands):
        final_attempt = latest[command_keys[index]]
        replacement = commands[final_attempt]
        recovered = (index != final_attempt and command["outcome"] in {"failed", "timed_out"}
                     and replacement["outcome"] == "successful" and replacement["freshness"] == "current")
        command["recovered"] = recovered
        if recovered:
            command["superseded_by"] = replacement.get("id") or replacement["attempt"]
    notices = ["退出码只说明命令执行结果，不证明运行了测试或覆盖全部需求。"] if commands else []
    if any(value == "budget_limited" for value in current.values()):
        notices.append("代码版本核对达到预算；未核对的路径不能视为已验证。")
    if any(item["freshness"] == "unverified" for item in commands):
        notices.append("部分命令缺少可核实的代码快照，只保留执行事实。")
    if any(item["freshness"] == "stale" for item in commands):
        notices.append("部分命令对应旧代码，或执行期间改动了代码；请查看各条记录。")
    historical_failures = sum(item["outcome"] in {"failed", "timed_out"} for item in commands)
    recovered_failures = sum(item["recovered"] for item in commands)
    if historical_failures:
        notices.append(f"记录中有 {historical_failures} 次失败或超时；" + (
            f"{recovered_failures} 次先前失败，后续同命令已在当前代码版本重跑成功。"
            if recovered_failures else "尚无同命令在当前代码版本重跑成功的记录。"))
    #恢复只改变最终结论，不改写旧事实。取消/仍在运行/清理异常不能靠重跑同命令掩盖。
    outcomes = {item["outcome"] for item in commands if not item["recovered"]}
    latest_attempts = [commands[index] for index in latest.values()]
    if not commands:
        final_status = "not_run"
        if changes:
            notices.append("本轮有文件修改，但没有实际命令检查记录。")
    elif outcomes & {"failed", "timed_out"}:
        final_status = "failed"
    elif outcomes & {"cancelled", "running", "incomplete"}:
        final_status = "incomplete"
    elif any(item["freshness"] == "stale" for item in latest_attempts):
        final_status = "stale"
    else:
        final_status = "recorded"
    changed_files = [{"path": path, "before_sha256": value.get("before_sha256"),
                      "after_sha256": value.get("after_sha256"), "reference": value.get("reference"),
                      "current_sha256": current[path]}
                     for path, value in list(changes.items())[:MAX_TRACKED_FILES]]
    return {"version": 1, "final_status": final_status, "command_count": len(commands),
            "latest_command_count": len(latest), "historical_failure_count": historical_failures,
            "recovered_failure_count": recovered_failures,
            "commands": commands[-MAX_COMMANDS:], "omitted_commands": max(0, len(commands) - MAX_COMMANDS),
            "changes": changed_files, "omitted_changes": max(0, len(changes) - MAX_TRACKED_FILES),
            "notices": notices}


def _display(value, limit=160):
    #命令和路径中可能有控制字符；摘要不得通过它们移动终端光标或伪造换行。
    text = "".join(char if char.isprintable() else " " for char in str(value or ""))
    return text[:limit] + ("…" if len(text) > limit else "")


def format_verification_summary(summary):
    """面向 CLI/普通文本界面的宿主摘要；没有改动和命令时不占用回答空间。"""
    if not summary.get("changes") and not summary.get("command_count"):
        return ""
    labels = {"successful": "执行成功", "failed": "执行失败", "timed_out": "超时",
              "cancelled": "已取消", "running": "仍在运行", "incomplete": "未完成/清理未确认"}
    overall = {"not_run": "没有命令检查记录", "failed": "存在未被有效重跑解除的执行失败或超时",
               "incomplete": "存在未完成或清理未确认的命令", "stale": "已有检查对应旧代码",
               "recorded": "已记录命令结果；请结合各条版本状态判断"}
    lines = ["宿主检查记录：", "汇总：" + overall.get(summary.get("final_status"), "状态未知")]
    changes = summary.get("changes", [])
    if changes:
        paths = "、".join(_display(item["path"], 80) for item in changes[:8])
        lines.append(f"修改文件：{paths}" + ("等" if len(changes) > 8 else ""))
    if summary.get("command_count") == 0:
        lines.append("未运行检查命令；本轮修改尚未验证。")
    for command in summary.get("commands", []):
        label = labels.get(command["outcome"], "状态未知")
        if command.get("recovered"):
            label += "；先前失败，后续同命令已重跑"
        code = command.get("exit_code")
        code = str(code) if type(code) is int else "未知"
        freshness = {"current": "已核对记录内代码版本", "stale": "代码已变化/快照未覆盖修改",
                     "unverified": "代码版本未核实"}.get(command.get("freshness"), "代码版本未核实")
        lines.append(f"- {label}（退出码 {code}；{freshness}）：{_display(command.get('command'))}")
    if summary.get("omitted_commands"):
        lines.append(f"另有 {summary['omitted_commands']} 条较早命令，详见本轮运行记录。")
    lines.extend(summary.get("notices", []))
    return "\n".join(lines)
