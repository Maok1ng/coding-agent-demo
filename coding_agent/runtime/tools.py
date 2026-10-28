"""唯一工具执行入口；权限检查是内核逻辑，不能由 Hook 或子角色绕过。"""

from dataclasses import asdict
import base64
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid

from jsonschema import Draft202012Validator
from referencing import Registry

from coding_agent.agents.roles import AgentTask, ROLES, dispatch_role_brief
from coding_agent.agents.contracts import BRIEF_PROPERTIES, report_tool, validate_report
from coding_agent.integrations.mcp import (
    assemble_tool_pool, get_mcp_tool_metadata, mcp_catalog_version, requires_mcp_launch_confirmation,
)
from coding_agent.runtime import environment
from coding_agent.runtime.approval import approval_decision
from coding_agent.runtime.context import RunContext
from coding_agent.runtime.edit_recovery import RECOVERY_MESSAGE, STOP_MESSAGE
from coding_agent.runtime.hooks import trigger_hook
from coding_agent.runtime.media import IMAGE_SUFFIXES
from coding_agent.runtime.turn import TurnCancelled, check_cancelled
from coding_agent.runtime.verification import build_verification_summary, capture_source_versions
from coding_agent.runtime.workspace import MAX_FILE_BYTES, use_workdir
from coding_agent.runtime.working_set import BatchReadOutput, ReadOutput
from coding_agent.tools.files import FileTools, LOAD_SKILL_TOOL, base_tools
from coding_agent.tools.symbols import SymbolTools, SYMBOL_TOOLS
from coding_agent.tools.web import READ_URL_TOOL, read_url
from coding_agent.tools.user_input import ASK_USER_TOOL, validate_question


class ToolOutput(str):
    def __new__(cls, value, *, is_error=False, edit_recovery=None, content_blocks=None):
        instance = super().__new__(cls, value)
        instance.is_error = is_error
        instance.edit_recovery = edit_recovery
        instance.content_blocks = content_blocks
        return instance


def schema(name, description, properties=None, required=None):
    return {"name": name, "description": description, "input_schema": {
        "type": "object", "properties": properties or {}, "required": required or [],
        "additionalProperties": False,
    }}


DISPATCH_TOOL = schema("dispatch_agent",
                       "Delegate a focused coding task to a trusted specialist. Children cannot "
                       "delegate. Pick one role by its scope: " + dispatch_role_brief(),
                       {
    "role": {"type": "string", "enum": [name for name in ROLES if name != "orchestrator"]},
    "instruction": {"type": "string", "minLength": 1, "maxLength": 20000},
    "context_refs": {"type": "array", "maxItems": 30, "items": {"type": "string"}},
    **BRIEF_PROPERTIES,
}, ["role", "instruction"])
PROCESS_TOOLS = [
    schema("wait_process", "Wait for this turn's background command; inspect its actual exit status.", {
        "task_id": {"type": "string"}, "timeout": {"type": "number", "minimum": 0, "maximum": 60},
    }, ["task_id"]),
    schema("process_status", "Inspect this turn's process evidence; running does not mean passed.", {
        "task_id": {"type": "string"},
    }, ["task_id"]),
]
TODO_TOOL = schema("todo_write", "Update the current session's lightweight coding checklist.", {
    "todos": {"type": "array", "maxItems": 10, "items": {"type": "object", "properties": {
        "content": {"type": "string", "minLength": 1},
        "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
    }, "required": ["content", "status"], "additionalProperties": False}},
}, ["todos"])
READ_TOOLS = {"read_file", "read_files", "list_files", "glob", "search_code", "search_many", "git_status", "git_diff", "load_skill", "ask_user",
              "read_url", "list_symbols", "find_symbol", "verification_status"}
WRITE_TOOLS = {"write_file", "edit_file", "bash", "wait_process", "process_status"}


class ToolRuntime:
    def __init__(self, context: RunContext, io, dispatch=None, ask=None, todo=None, *, reporting=False,
                 working_set=None):
        self.context = context
        self.io = io
        self.dispatch = dispatch
        self.ask = ask
        self.todo = todo
        self.reporting = reporting
        #格式修复阶段仅可重新提交报告，不能重复执行已经完成的修改。
        self.report_only = False
        self.working_set = working_set
        #bash 快照摘要缓存：文件身份、大小和时间均未变才复用；只服务证据快照，
        #写入前置的版本核对（_expected_version）仍直接重哈希，不走这里。
        self._digest_cache: dict[str, tuple[tuple, str]] = {}
        #工具池缓存：每次执行和主循环迭代都读池，重建只在键变化（模式、修复阶段、MCP 目录）时发生。
        self._pool_cache: dict[tuple, tuple[list, dict]] = {}
        self.files = FileTools(context.workspace, context.versions, skill_loader=context.skills,
                               project_files=context.project_files)
        self.symbols = SymbolTools(context.workspace, project_files=context.project_files)

    def allowed(self, name):
        """每次调用都重新检查；模型不可通过伪造工具名使用不可见的工具。"""
        if not isinstance(name, str):
            return False
        role = ROLES.get(self.context.role)
        if role is None:
            return False
        if self.report_only and name != "report_result":
            return False
        if name == "report_result":
            return self.reporting and self.context.role != "orchestrator"
        if name == "dispatch_agent":
            return role.can_dispatch and self.dispatch is not None
        if name in {"todo_write", "compact"}:
            return role.can_dispatch
        if name in READ_TOOLS or name == "connect_mcp":
            return True
        if name in WRITE_TOOLS:
            return role.can_write and self.context.mode == "execute"
        if name.startswith("mcp__"):
            metadata = get_mcp_tool_metadata(name)
            return metadata.get("available", False) and (
                metadata["read_only"] or (role.can_write and self.context.mode == "execute")
            )
        return False

    def _catalog(self):
        tools = [*base_tools(background=True), LOAD_SKILL_TOOL, ASK_USER_TOOL, READ_URL_TOOL, *SYMBOL_TOOLS,
                 DISPATCH_TOOL, *PROCESS_TOOLS, TODO_TOOL,
                 schema("verification_status", "Inspect host-recorded command outcomes and whether they still match the known current code. Exit codes do not prove all tests or requirements were covered."),
                 schema("compact", "Compact earlier history after the current tool batch.")]
        handlers = {**self.files.handlers(), **self.symbols.handlers(), "read_url": read_url, "bash": self._bash,
                    "dispatch_agent": self._dispatch, "ask_user": self._ask,
                    "verification_status": lambda: json.dumps(build_verification_summary(self.context), ensure_ascii=False),
                    "todo_write": self._todo, "compact": lambda: "Compaction requested."}
        if self.reporting and self.context.role != "orchestrator":
            tools.append(report_tool(self.context.role))
            handlers["report_result"] = self._report_result
        if self.context.processes is not None:
            handlers.update({"wait_process": self._wait_process,
                             "process_status": self._process_status})
        return assemble_tool_pool(tools, handlers)

    def pool(self):
        """可见工具与处理器按角色运行期状态缓存；连接 MCP 或进入报告修复阶段才会重建。"""
        key = (self.context.mode, self.report_only, self.context.processes is not None,
               mcp_catalog_version())
        cached = self._pool_cache.get(key)
        if cached is not None:
            return cached
        tools, handlers = self._catalog()
        visible = [deepcopy(tool) for tool in tools if self.allowed(tool["name"])]
        if self.context.mode == "plan":
            for tool in visible:
                if tool["name"] == "dispatch_agent":
                    tool["input_schema"]["properties"]["role"]["enum"] = [
                        role for role in ROLES if role not in {"implementation", "orchestrator"}
                    ]
        pool = (visible, {tool["name"]: handlers[tool["name"]] for tool in visible if tool["name"] in handlers})
        self._pool_cache[key] = pool
        return pool

    def _dispatch(self, role, instruction, context_refs=None, constraints=None,
                  acceptance_criteria=None, known_facts=None):
        if role not in ROLES or role == "orchestrator":
            raise ValueError("不允许的专业角色")
        if role == "implementation" and self.context.mode != "execute":
            raise ValueError("Plan 模式不能派发 Implementation；需要真实用户确认")
        #引用必须属于当前工作区；子角色不能借参数切换 cwd 或读取宿主配置目录。
        for ref in context_refs or []:
            if not (self.context.workspace / ref).resolve().is_relative_to(self.context.workspace):
                raise ValueError("上下文引用超出工作区")
        result = self.dispatch(AgentTask(
            role, instruction, context_refs or [], constraints or [], acceptance_criteria or [],
            known_facts or [], require_report=True,
        ))
        return ToolOutput(json.dumps(asdict(result), ensure_ascii=False),
                          is_error=result.status not in {"completed", "needs_input"})

    def _report_result(self, summary, details):
        """只接受当前可信角色的报告；退出码、权限和生命周期状态不能由模型填写。"""
        payload = validate_report(self.context.role, {"summary": summary, "details": details},
                                  self.context.workspace)
        return json.dumps(payload, ensure_ascii=False)

    def _ask(self, question, options=None):
        validate_question(question, options)
        if self.ask is not None:
            return self.ask(question, options)
        return json.dumps({"status": "needs_input", "question": question, "options": options or []}, ensure_ascii=False)

    def _todo(self, todos):
        return self.todo(todos) if self.todo is not None else "Error: No session checklist attached"

    @staticmethod
    def _process_output(output):
        #等待动作成功不等于命令成功；把实际失败、超时和取消同时标成协议错误结果。
        status = json.loads(output)["status"]
        return ToolOutput(output, is_error=status not in {"running", "completed"})

    def _wait_process(self, task_id, timeout=30):
        return self._process_output(self.context.processes.wait(task_id, timeout))

    def _process_status(self, task_id):
        return self._process_output(self.context.processes.status(task_id))

    def _file_digest(self, filename: str) -> str | None:
        """有界快照已读普通文件；不跟随链接，不等待 FIFO，取消后不再启动命令。"""
        check_cancelled()
        path = Path(filename)
        if not path.is_relative_to(self.context.workspace):
            return None
        descriptor = None

        def signature(info):
            return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

        try:
            if not path.resolve().is_relative_to(self.context.workspace):
                return "outside_workspace"
            parts = path.relative_to(self.context.workspace).parts
            if not parts or ".." in parts:
                return "unavailable"
            #逐级使用目录描述符和 O_NOFOLLOW，避免检查后再按路径打开时跟随替换的链接。
            descriptor = os.open(self.context.workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
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
            current = signature(info)
            cached = self._digest_cache.get(filename)
            if cached is not None and cached[0] == current:
                check_cancelled()
                return cached[1]
            digest, total = hashlib.sha256(), 0
            while True:
                check_cancelled()
                chunk = os.read(descriptor, min(65536, MAX_FILE_BYTES + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_FILE_BYTES:
                    return "unavailable"
                digest.update(chunk)
            #读到一半发生的改动不能被缓存为某个稳定版本；下次调用会重新核实。
            if signature(os.fstat(descriptor)) != current:
                return "changed_during_snapshot"
            check_cancelled()
            value = digest.hexdigest()
        except FileNotFoundError:
            return "missing"
        except (OSError, RuntimeError):
            return "unavailable"
        finally:
            if descriptor is not None:
                os.close(descriptor)
        self._digest_cache[filename] = (current, value)
        return value

    def _bash(self, command, run_in_background=False):
        execution_order = len(self.context.evidence)
        versions = {}
        for filename in self.context.versions:
            digest = self._file_digest(filename)
            if digest is not None:
                versions[filename] = digest
        if run_in_background:
            if self.context.processes is None:
                return "Error: No process manager attached"
            task_id = self.context.processes.start(command, self.context.workspace, source_versions=versions)
            self.context.evidence.append({"kind": "process_start", "task_id": task_id,
                                          "source_versions": versions, "execution_order": execution_order})
            return json.dumps({"task_id": task_id, "status": "running",
                               "notice": "Wait for actual results before claiming verification."})
        output_path = Path(self.context.artifacts.save("shell-output", {})).with_suffix(".txt")
        try:
            #独立副本与标识用于汇总去重，不能把两次前台执行误合成同一次检查。
            record = dict(environment.run_bash_evidence(command, self.context.workspace, output_path=output_path))
        except TurnCancelled as error:
            record = getattr(error, "execution_evidence", None)
            if record is not None:
                record["source_versions"] = versions
                record["execution_order"] = execution_order
                reference = self.context.artifacts.save("cancelled-shell", record)
                self.context.evidence.append({**record, "reference": reference})
            raise
        record["id"] = "shell_" + uuid.uuid4().hex
        self.context.evidence.append(record)
        record["source_versions"] = versions
        record["execution_order"] = execution_order
        #结束时立即固定快照；不能等最终报告时再采样，遗漏检查期间实际改过的版本。
        record["source_versions_after"] = capture_source_versions(self.context.workspace, versions)
        return ToolOutput(json.dumps(record, ensure_ascii=False), is_error=record["status"] != "completed")

    def execute(self, block):
        check_cancelled()
        name = block.name
        #固定本次校验的参数；交互回调和 Hook 不能改掉已经展示并批准的修改。
        arguments = deepcopy(block.input)
        if not self.allowed(name):
            return self._record(name, arguments, f"Error: Permission denied for {self.context.role}: {name} (mode={self.context.mode})", True)
        if self.context.edit_recovery.stopped:
            #执行层也拒绝已停止回合的新调用；不能靠重新派发角色获得一份新计数器。
            return self._record(name, arguments, ToolOutput(
                f"Error: {STOP_MESSAGE}", is_error=True, edit_recovery="stop"), True,
                tool_use_id=getattr(block, "id", None))
        try:
            tools, handlers = self.pool()
            definition = next((tool for tool in tools if tool["name"] == name), None)
            if definition is None or name not in handlers:
                raise ValueError("工具不可用")
            #空 Registry 禁止外部 JSON Schema 引用偷偷联网。
            validator = Draft202012Validator(definition["input_schema"], registry=Registry())
            if (name == "dispatch_agent" and self.context.mode == "plan"
                    and isinstance(arguments, dict) and arguments.get("role") == "implementation"):
                raise ValueError("Plan 模式不能派发 Implementation；需要真实用户确认")
            validator.validate(arguments)
            decision = approval_decision(
                self.context.approval_mode, name, arguments, self.context.workspace,
                mcp_metadata=get_mcp_tool_metadata(name) if name.startswith("mcp__") else None,
                mcp_launch=name == "connect_mcp" and requires_mcp_launch_confirmation(arguments["name"]),
            )
            change = None
            change_record = {}
            if name in {"write_file", "edit_file"} and decision.action != "deny":
                #只在内存生成候选；未通过读版本、唯一匹配或大小检查时不弹一个无效确认。
                try:
                    change = self.files.prepare_change(name, **arguments)
                except Exception as error:
                    return self._failed_change(name, arguments, error, getattr(block, "id", None))
                change_record = {
                    "kind": "file_change", "role": self.context.role, "operation": name,
                    "tool_use_id": getattr(block, "id", None), "path": change.path,
                    "before_sha256": change.expected,
                    "after_sha256": hashlib.sha256(change.content.encode("utf-8")).hexdigest(),
                }
                reference = self.context.artifacts.save("file-change", {**change_record, "diff": change.diff})
                change_record["change_reference"] = reference
                #完整 diff 留存；终端展示有界预览，不将候选内容伪装成新的模型工具参数。
                self.io.emit({
                    **change_record, "type": "change_preview", "tool": name,
                    "diff": change.diff[:12000], "truncated": len(change.diff) > 12000,
                    "reference": reference, "approval_action": decision.action,
                    "approval_mode": self.context.approval_mode,
                })
            approved = decision.action == "allow"
            source = "policy"
            try:
                if decision.action == "confirm":
                    source = "human"
                    approved = self.io.confirm(name, deepcopy(arguments), decision.reason) is True
            finally:
                #拒绝与中断也记录；一次同意只针对本次调用，不缓存为后续操作的许可。
                record = {"kind": "approval", "role": self.context.role, "operation": name,
                          "tool_use_id": getattr(block, "id", None), "action": decision.action,
                          "arguments_sha256": hashlib.sha256(json.dumps(
                              arguments, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest(),
                          "mode": self.context.approval_mode, "reason": decision.reason,
                          "approved": bool(approved), "source": source,
                          **{key: value for key, value in change_record.items()
                             if key in {"before_sha256", "after_sha256", "change_reference"}}}
                reference = self.context.artifacts.save("approval", record)
                self.context.evidence.append({**record, "reference": reference})
            if not approved:
                output = decision.reason if decision.action == "deny" else "Error: User denied operation"
                return self._record(name, arguments, output, True)
            check_cancelled()
            #Hook 只能进一步阻止，不能跳过上面已经执行的权限判定。
            with use_workdir(self.context.workspace):
                blocked = trigger_hook("PreToolUse", block)
                if blocked is not None:
                    output = ToolOutput(f"Error: Tool blocked by PreToolUse: {blocked}", is_error=True)
                else:
                    check_cancelled()
                    if not self.allowed(name):
                        raise ValueError("权限已改变；本次工具调用已取消")
                    #不能重新从参数生成另一份修改；只应用刚才展示并获准的不可变候选。
                    if change is not None:
                        #批准之后、真正写入之前保存原件；日志落盘失败就不允许开始写文件。
                        change_token = (self.context.checkpoints.prepare(change)
                                        if self.context.checkpoints is not None else None)
                        try:
                            output = self.files.apply_change(change)
                        except Exception as error:
                            if change_token is not None:
                                self.context.checkpoints.failed(change_token)
                            return self._failed_change(name, arguments, error, getattr(block, "id", None))
                        #落盘已经成功；之后的 Hook 报错不应伪装成编辑失败并累计。
                        self.context.edit_recovery.succeeded(str(change.file_path))
                    else:
                        output = handlers[name](**arguments)
                    if change is not None:
                        #先登记真实落盘，再运行扩展；PostToolUse 失败也不能隐去已经发生的修改。
                        self.context.changes[str(change.file_path)] = {
                            "path": change.file_path.relative_to(self.context.workspace).as_posix(),
                            "before_sha256": change.expected,
                            "after_sha256": change_record["after_sha256"],
                            "reference": change_record["change_reference"],
                        }
                        if change_token is not None:
                            #先记录真实落盘，再执行 Hook；写后中断也有可恢复的检查点。
                            self.context.checkpoints.applied(change_token)
                    trigger_hook("PostToolUse", block, output)
            check_cancelled()
            is_error = getattr(output, "is_error", False) or str(output).startswith("Error:")
        except Exception as error:
            #不将外部验证器/网络异常的原文（可能带凭据）送回模型。
            output = f"Error: {type(error).__name__}: invalid arguments or tool operation failed"
            if isinstance(error, (ValueError, TypeError)) and not name.startswith("mcp__"):
                output = f"Error: {error}"
            is_error = True
        return self._record(name, arguments, output, is_error, tool_use_id=getattr(block, "id", None))

    def _failed_change(self, name, arguments, error, tool_use_id):
        """仅真实候选生成/落盘失败进入保护；审批、参数、权限和扩展失败仍各自处理。"""
        deterministic = isinstance(error, (ValueError, FileNotFoundError, FileExistsError,
                                           IsADirectoryError, NotADirectoryError))
        parameters = {"write_file": {"path", "content"},
                      "edit_file": {"path", "old_text", "new_text"}}
        path = None
        if deterministic and set(arguments) == parameters[name]:
            try:
                path = str(self.files._path(arguments["path"]))
            except (ValueError, OSError, RuntimeError):
                #工作区越界、权限等拒绝不能被当作需要重试的代码匹配问题。
                pass
        recovery = self.context.edit_recovery.failed(
            name, arguments, f"{type(error).__name__}: {error}", path) if path else None
        #内建文件异常保留真实原因；其他意外异常沿用统一入口的脱敏规则。
        output = (f"Error: {error}" if isinstance(error, (ValueError, TypeError, OSError)) else
                  f"Error: {type(error).__name__}: invalid arguments or tool operation failed")
        if recovery is not None:
            message = STOP_MESSAGE if recovery == "stop" else RECOVERY_MESSAGE
            output += "\n[Runtime edit recovery — host notice, not user authorization]\n" + message
            self.io.emit({"type": "warning", "text": message, "role": self.context.role})
        return self._record(name, arguments, ToolOutput(
            output, is_error=True, edit_recovery=recovery), True,
            tool_use_id=tool_use_id)

    def _record(self, name, arguments, output, is_error, tool_use_id=None):
        """拒绝和失败也留证据；不能把没有执行的工具悄悄从审计记录中省略。"""
        content_blocks = getattr(output, "content_blocks", None)
        image_refs = []
        image_reference_positions = set()
        if content_blocks is not None:
            #MCP 已完成格式/大小校验；只保存它实际返回的字节，不能追随其路径或 URL。
            content_blocks = deepcopy(content_blocks)
            recorded_blocks = []
            for item in content_blocks:
                recorded_blocks.append(item)
                if item.get("type") == "image":
                    source = item["source"]
                    image_ref = self.context.artifacts.save_bytes(
                        "screenshot", base64.b64decode(source["data"], validate=True),
                        suffix=IMAGE_SUFFIXES[source["media_type"]],
                    )
                    image_refs.append(image_ref)
                    image_reference_positions.add(len(recorded_blocks))
                    recorded_blocks.append({"type": "text", "text": f"[Saved screenshot: {image_ref}]"})
            content_blocks = recorded_blocks
        snapshot = output.snapshot if name == "read_file" and not is_error and isinstance(output, ReadOutput) else None
        if snapshot is not None:
            #返回模型前还有一层预览上限；工作集只保存这层上限内实际可见的代码。
            snapshot = {**snapshot, "content": str(output)[:24000]}
            displayed = [int(line.partition(":")[0]) for line in snapshot["content"].splitlines()
                         if line.partition(":")[0].isdigit()]
            snapshot["end_line"] = displayed[-1] if displayed else 0
        snapshots = (output.snapshots if name == "read_files" and not is_error
                     and isinstance(output, BatchReadOutput) else [])
        recovery = getattr(output, "edit_recovery", None)
        recovery_record = ({"edit_recovery": recovery,
                            "edit_recovery_state": self.context.edit_recovery.snapshot()}
                           if recovery is not None else {})
        reference = self.context.artifacts.save("tool", {
            "role": self.context.role, "tool": name, "arguments": arguments,
            "output": str(output), "is_error": is_error,
            **({"images": image_refs} if image_refs else {}),
            **recovery_record,
            **({"read_snapshot": {key: value for key, value in snapshot.items() if key != "content"}}
               if snapshot is not None else {}),
            **({"read_snapshots": [{key: value for key, value in item.items() if key != "content"}
                                    for item in snapshots]} if snapshots else {}),
        })
        if snapshot is not None and self.working_set is not None:
            #只有执行成功且真正返回过的读取才登记；不从模型文字或当前 versions 猜测版本。
            self.working_set.record(snapshot, tool_use_id)
        if self.working_set is not None:
            for item in snapshots:
                self.working_set.record(item, tool_use_id)
        evidence = {"tool": name, "role": self.context.role, "reference": reference,
                    "is_error": is_error, **recovery_record,
                    **({"images": image_refs} if image_refs else {})}
        if name == "read_file" and not is_error:
            evidence["code_ref"] = f"{arguments['path']}:{arguments.get('start_line', 1)}"
        if snapshots:
            evidence["code_refs"] = [f"{item['path']}:{item['start_line']}" for item in snapshots]
        self.context.evidence.append(evidence)
        self.io.emit({"type": "tool_result", "role": self.context.role, "tool": name,
                      "is_error": is_error, "reference": reference,
                      **({"images": image_refs} if image_refs else {})})
        preview = str(output)
        if len(preview) > 24000:
            preview = preview[:24000] + f"\n[Truncated; full tool result: {reference}]"
        if content_blocks is not None:
            #混合结果的文字也遵守总预览上限；绝不能用字符串切片截坏图片或丢掉截图引用。
            remaining, truncated = 24000, False
            bounded = []
            for index, item in enumerate(content_blocks):
                if item.get("type") == "text" and index not in image_reference_positions:
                    text = item["text"]
                    if len(text) > remaining:
                        truncated = True
                    if remaining:
                        bounded.append({"type": "text", "text": text[:remaining]})
                    remaining = max(0, remaining - len(text))
                else:
                    bounded.append(item)
            if truncated:
                bounded.append({"type": "text", "text": f"[Text truncated; full tool result: {reference}]"})
            content_blocks = bounded
        return ToolOutput(preview or "(no output)", is_error=is_error, edit_recovery=recovery,
                          content_blocks=content_blocks)
