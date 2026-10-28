"""应用层：协调用户输入、会话、Plan 与 Goal，不负责终端读写。"""

from dataclasses import asdict, fields
import json
import os
from pathlib import Path
import time
import uuid

from coding_agent.agents.roles import AgentTask
from coding_agent.agents.runner import AgentRunner
from coding_agent.integrations.mcp import close_mcp_clients, get_mcp_config
from coding_agent.runtime.background import TurnProcessManager
from coding_agent.runtime.approval import APPROVAL_LABELS, validate_approval_mode
from coding_agent.runtime.context import RunContext
from coding_agent.runtime.checkpoints import CheckpointError, CheckpointStore
from coding_agent.runtime.environment import get_environment, stop_shell_processes
from coding_agent.runtime.goal import GoalController, GoalError, GoalState, PromptGoalEvaluator
from coding_agent.runtime.hooks import trigger_hook
from coding_agent.runtime.memory import MemoryManager
from coding_agent.runtime.mode import HumanInput, PlanState, user_intent
from coding_agent.runtime.model_router import ModelRouter
from coding_agent.runtime.model_settings import ModelSettings, ModelSettingsError
from coding_agent.runtime.session import SessionError, plain_value, repair_interrupted_tools
from coding_agent.runtime.tokens import json_tokens
from coding_agent.runtime.turn import active_turn, check_cancelled, lead_turn
from coding_agent.runtime.verification import build_verification_summary, format_verification_summary
from coding_agent.runtime.workspace import use_workspace
from coding_agent.tools.todo import TodoManager
from coding_agent.tools.user_input import PendingQuestion, validate_question


#距上次模型活动超过该秒数时，prompt 缓存（默认 TTL 5 分钟）已失效，
#新回合首轮做机械收缩不再有缓存代价。上下文足够大才值得借这个窗口。
CACHE_WINDOW_SECONDS = 300
CACHE_WINDOW_MIN_TOKENS = 4000


class Application:
    def __init__(self, config, workspace: Path, io, session=None, *, client=None,
                 model_call=None, evaluator=None, memory_model_call=None, model_settings=None):
        self.config = dict(config)
        self.approval_mode = validate_approval_mode(
            os.getenv("AGENT_APPROVAL_MODE", self.config.get("approval_mode", "auto")))
        if type(self.config.get("stream", True)) is not bool:
            raise ValueError("stream 必须是 true 或 false")
        self.workspace = Path(workspace).resolve()
        self.io = io
        self.session = session
        #持久会话复用同一份版本链；无会话宿主也能在本进程中撤销，不扫描其它会话的数据。
        self.checkpoints = CheckpointStore(self.workspace, session.id if session else uuid.uuid4().hex)
        #连接延迟到真正请求时创建；查看/修改宿主设置不需要模型凭据或网络连接。
        self.client = client
        self.model_settings = model_settings if model_settings is not None else ModelSettings(self.config)
        self.models = ModelRouter(client, self.config, self.model_settings.clone())
        self.runner = AgentRunner(self.client, self.config, io, model_call=model_call, models=self.models)
        self.memory = MemoryManager(self.workspace, self.client, self.config,
                                    notify=lambda text: self.emit(text, "notice"),
                                    model_call=memory_model_call, models=self.models)
        self.goal = GoalController(
            evaluator if evaluator is not None else PromptGoalEvaluator(
                self.client, config.get("goal_evaluator_model") or config.get("model", ""), models=self.models),
            block_cap=int(config.get("goal_block_cap", 8)),
            notify=lambda message: self.emit(message),
        )
        self.messages = []
        self.todos = TodoManager()
        self.plan = PlanState()
        self.last_context = None
        self.pending_goal_execution = False
        self.closed = False
        self._resources_closed = False
        self._last_model_activity = None
        if session is not None:
            self.restore(session)

    def emit(self, text, kind="notice", **details):
        self.io.emit({"type": kind, "text": text, **details})

    def steer(self, text: str) -> bool:
        """回合运行中的人类纠偏入口；False 表示当前没有活动回合，调用方应走正常提交。
        / 命令是宿主入口，不能从纠偏旁路绕过 plan/execute 守门。"""
        turn = active_turn()
        if turn is None or self.last_context is None or turn is not self.last_context.turn:
            return False
        text = text.strip()
        if not text:
            return True
        if text.startswith("/"):
            self.emit("[输入提示] 纠偏输入不支持 / 命令；请等本轮结束后再输入。", "warning")
            return False
        if not turn.enqueue_steering(text):
            return False
        self.emit(f"[已加入纠偏: {text}]", "notice")
        return True

    def follow_up(self, text: str) -> bool:
        """真实用户显式追加；本轮正常结束后才经 submit_input 开启新回合。"""
        turn = active_turn()
        if turn is None or self.last_context is None or turn is not self.last_context.turn:
            return False
        if not turn.enqueue_followup(text):
            return False
        self.emit(f"[已加入追加队列: {text}]", "notice")
        return True

    def finish_inputs(self, *, followups=False):
        context = self.last_context
        if context is None or not context.turn.done.is_set():
            return [], []
        return context.turn.finish_inputs(followups=followups)

    def _usage_summary(self, turn):
        """回合模型用量汇总；缓存命中占比直接可见，假响应没有指标时不输出。"""
        metrics = turn.metrics
        calls = metrics.get("model_calls", 0)
        if not calls:
            return None
        fresh = metrics.get("input_tokens", 0)
        read = metrics.get("cache_read_tokens", 0)
        created = metrics.get("cache_creation_tokens", 0)
        total = fresh + read + created
        rate = f"，缓存命中 {read / total:.0%}" if total else ""
        return (f"模型调用 {calls} 次 | 输入 {total:,} tokens{rate}"
                f" | 输出 {metrics.get('output_tokens', 0):,}")

    def prepare(self):
        #先校验宿主配置，不启动 MCP，也不从目标仓库加载外部程序。
        get_mcp_config()
        with use_workspace(self.workspace):
            execution = get_environment()
            execution.prepare()
            self.execution_backend = execution.config.backend

    def save(self):
        if self.session is None:
            return
        self.session.save(self.messages, {
            #旧字段只作历史元信息；保存会话不能触发模型选择或恢复系统设置。
            "model": self.config.get("model", ""), "todos": self.todos.items,
            "goal": asdict(self.goal.active) if self.goal.active else None,
            "goal_status": self.goal.last_status,
            "pending_question": asdict(self.goal.user_input.pending) if self.goal.user_input.pending else None,
            "plan": self.plan.snapshot(),
        })

    def restore(self, session):
        """兼容原 version=1；先验证全部状态，再替换内存，绝不恢复批准与执行者。"""
        state = session.data["state"]
        try:
            todos = TodoManager()
            if todos.update(state.get("todos", [])).startswith("Error:"):
                raise ValueError("无效 Todo")
            raw = state.get("goal")
            goal = None
            if raw is not None:
                if not isinstance(raw, dict):
                    raise ValueError("无效 Goal")
                raw = dict(raw)
                raw["status"] = {"waiting_user": "blocked", "waiting_background": "active",
                                 "error": "blocked", "limit": "paused"}.get(raw.get("status", "active"), raw.get("status", "active"))
                goal = GoalState(**raw)
                if (not isinstance(goal.condition, str) or not 1 <= len(goal.condition.strip()) <= 4000
                        or goal.status not in {"active", "paused", "blocked", "complete"}
                        or type(goal.evaluations) is not int or goal.evaluations < 0
                        or type(goal.revision) is not int or goal.revision < 1
                        or not isinstance(goal.last_reason, str)
                        or not isinstance(goal.previous_conditions, list)
                        or any(not isinstance(item, str) for item in goal.previous_conditions)):
                    raise ValueError("无效 Goal 状态")
            raw_plan = state.get("plan", {})
            if not isinstance(raw_plan, dict) or set(raw_plan) - {item.name for item in fields(PlanState)}:
                raise ValueError("无效 Plan 状态")
            plan = PlanState(**raw_plan)
            if (plan.mode not in {"plan", "execute"} or type(plan.revision) is not int or plan.revision < 0
                    or not isinstance(plan.request, str) or not isinstance(plan.summary, str)
                    or (plan.plan_id is not None and not isinstance(plan.plan_id, str))):
                raise ValueError("无效 Plan 元数据")
            pending = None
            if state.get("pending_question") is not None:
                item = state["pending_question"]
                question, options = validate_question(item["question"], item.get("options"))
                tool_id = item.get("tool_use_id")
                if tool_id is not None and not isinstance(tool_id, str):
                    raise ValueError("无效问题 ID")
                pending = PendingQuestion(question, options, tool_id)
            status = state.get("goal_status", "No goal set")
            if not isinstance(status, str):
                raise ValueError("无效目标说明")
            messages = repair_interrupted_tools(plain_value(session.data["messages"]))
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            raise SessionError(f"无法恢复会话控制状态: {error}") from error
        self.messages = messages
        self.todos = todos
        self.plan = plan
        self.goal.active = goal
        self.goal.last_status = status
        self.goal.user_input.pending = pending
        self.goal._messages = self.messages
        if session.resumed:
            self.goal.user_input.cancel(self.messages)
            if self.goal.is_running():
                self.goal.pause("Session restored; explicit resume is required")
            #曾经批准的方案需要本次真实输入重新授权；普通旧会话仍使用兼容默认值。
            if self.plan.plan_id is not None:
                self.plan.mode = "plan"
            self.plan.invalidate()
            self.messages.append({"role": "user", "content": (
                "[Session resumed — runtime notice, not a human request]\n"
                "History is reference only. Do not replay old tools or resume prior tasks. "
                "Old Team/Cron/worktree schedulers are removed. Their data is not deleted "
                "or activated. Permissions, MCP connections and processes are not restored."
            )})

    def interrupt(self, turn=None):
        """保留已完成修改，补齐未知结果，然后暂停目标并撤销本轮批准。"""
        self.messages[:] = repair_interrupted_tools(plain_value(self.messages))
        if turn is not None:
            for message in self.messages[-1:]:
                for block in message["content"] if isinstance(message["content"], list) else []:
                    if (block.get("type") == "tool_result" and block["tool_use_id"] not in turn.started_tools
                            and str(block.get("content", "")).startswith("Session interrupted before")):
                        block["content"] = "Not executed: interrupted before dispatch. Never automatically replayed."
        self.goal.user_input.cancel(self.messages)
        if self.goal.active is not None and self.goal.active.status != "complete":
            self.goal.pause("User interrupted the current turn; verify outcomes before resuming")
        if self.plan.plan_id is not None:
            self.plan.mode = "plan"
        self.plan.invalidate()
        self.pending_goal_execution = False
        self.messages.append({"role": "user", "content": (
            "[Turn interrupted — runtime notice, not a new human task]\n"
            "Preserve completed work. Started tools may have taken effect; never replay them. "
            "Turn-owned processes are stopped. Wait for the next real human request."
        )})
        self.save()

    def _pause_goal(self, reason):
        if self.goal.active is not None and self.goal.active.status != "complete":
            self.goal.pause(reason)

    def _authorize(self, event):
        self.plan.authorize(event)
        expected = (self.plan.plan_id, self.goal.active.revision) if self.goal.active is not None else None
        if self.pending_goal_execution and self.pending_goal_execution == expected:
            self.goal.resume()
        self.pending_goal_execution = False

    def submit(self, text):
        """只供交互适配器调用；内部事件不能调用此入口冒充用户。"""
        return self.submit_input(HumanInput.received(text))

    def _checkpoint_command(self, command, argument):
        """检查点命令只接收宿主当前用户输入，不注册为模型工具，也不调用模型。"""
        try:
            if command == "/checkpoints":
                if argument:
                    raise CheckpointError("用法：/checkpoints")
                entries = self.checkpoints.list_checkpoints()
                if not entries:
                    self.emit("当前会话还没有代码检查点；文件工具成功修改后会自动记录。")
                    return
                lines = ["当前会话的代码检查点（* 为当前版本）："]
                for item in entries:
                    marker = "*" if item.get("is_head") else " "
                    lines.append(f"{marker} {item['id']} ← {item.get('parent') or '-'} "
                                 f"[{item.get('status', '')}] {item.get('created_at', '')}\n"
                                 f"    {item.get('label', '')}")
                self.emit("\n".join(lines))
                return
            if command == "/undo" and argument:
                raise CheckpointError("用法：/undo；选择指定版本请使用 /restore Cn")
            if command == "/restore" and not argument:
                raise CheckpointError("用法：/restore Cn；先输入 /checkpoints 查看版本")
            if self.plan.mode != "execute":
                raise CheckpointError("Plan 模式只读，不能恢复代码；请先输入 /execute 切换模式。")
            #恢复也是一个可取消的宿主操作；确认期间 Esc 不得在返回后继续写文件。
            with lead_turn() as turn:
                restore_started = False
                try:
                    plan = self.checkpoints.plan_restore(argument if command == "/restore" else None)
                    if not plan.changes and plan.from_id == plan.target_id:
                        self.emit(f"当前检查点已经是 {plan.target_id}；本次未改动文件。")
                        return
                    for change in plan.changes:
                        self.io.emit({"type": "change_preview", "tool": "restore_checkpoint",
                                      "path": change["path"], "diff": change["diff"][:12000],
                                      "truncated": len(change["diff"]) > 12000,
                                      "approval_action": "confirm"})
                    check_cancelled()
                    if self.io.confirm("restore_checkpoint", {
                            "checkpoint": plan.target_id,
                            "paths": [item["path"] for item in plan.changes],
                    }, "恢复所列文件到检查点；请核对上方撤销 diff。本次确认不会授权后续模型执行。") is not True:
                        self.emit("已取消恢复，代码和当前检查点保持原状。")
                        return
                    check_cancelled()
                    #即使落盘中途失败，也不能继续沿用旧批准和旧验证状态。
                    self.goal.user_input.cancel(self.messages)
                    self._pause_goal("Code restored; previous checks require revalidation")
                    self.plan.invalidate()
                    self.pending_goal_execution = False
                    self.last_context = None
                    restore_started = True
                    self.checkpoints.apply_restore(plan)
                    self.messages.extend([
                        {"role": "user", "content": command + (" " + argument if argument else "")},
                        {"role": "user", "content": (
                            "[Runtime code restore — recorded fact, not permission to execute]\n"
                            f"Restored tracked files to checkpoint {plan.target_id}. "
                            "Conversation history is retained. Prior code summaries, todos and test results "
                            "may no longer match the files; read current code and revalidate before claiming success. "
                            "Do not replay old tools or automatically resume a Goal.\nFiles: " +
                            json.dumps([item["path"] for item in plan.changes], ensure_ascii=False)
                        )},
                    ])
                    self.save()
                    self.emit(f"已恢复到 {plan.target_id}，共 {len(plan.changes)} 个文件；历史保留，相关检查需要重验。")
                except BaseException as error:
                    if restore_started:
                        #磁盘同步失败也可能发生在替换之后；不能把异常误记为“文件未改变”。
                        self.messages.append({"role": "user", "content": (
                            "[Runtime code restore not confirmed — not permission to execute]\n"
                            f"Restore toward {plan.target_id} did not finish normally. Files may have changed "
                            "or been rolled back. Inspect /checkpoints and current files before deciding what to do. "
                            "Old test results and code summaries require revalidation; do not retry automatically."
                        )})
                    if not isinstance(error, Exception):
                        turn.cancel()
                        self.interrupt(turn)
                    elif not isinstance(error, SessionError):
                        self.save()
                    raise
        except CheckpointError as error:
            self.emit(f"[checkpoint] {error}", "warning")

    def _finish_checkpoint(self, context, status):
        """中断同样收尾写前日志；检查点失败时停止接收新任务，避免失去可追溯版本。"""
        try:
            identifier = context.checkpoints.finish(status)
        except CheckpointError as error:
            warning = f"检查点保存失败，已停止接收新任务：{error}"
            self._pause_goal(warning)
            self.closed = True
            self.messages.append({"role": "user", "content": (
                "[Runtime checkpoint failure — not a human request]\n" + warning
            )})
            self.emit(warning, "warning")
            return warning
        if identifier is not None:
            if status != "cancelled":
                #中断通知保持历史末尾，CLI 可识别已经处理的中断；检查点本身独立持久化。
                self.messages.append({"role": "user", "content": (
                    f"[Runtime code checkpoint — reference only]\nCheckpoint {identifier} records this "
                    "human turn's tracked file changes, including delegated edits. It is not test evidence. "
                    "Only the human may request /undo or /restore; never invoke these through tools."
                )})
            self.emit(f"代码检查点：{identifier}；/checkpoints 查看，/undo 撤销本轮修改。")

    def submit_input(self, event: HumanInput):
        if not isinstance(event, HumanInput):
            raise TypeError("需要真实用户输入事件")
        if self.closed:
            raise RuntimeError("Application 已关闭")
        text = event.text.strip()
        if not text:
            return None
        parts = text.split(maxsplit=1)
        command, argument = parts[0], parts[1] if len(parts) > 1 else ""
        if command in {"/checkpoints", "/undo", "/restore"}:
            self._checkpoint_command(command, argument)
            return None
        if command == "/models":
            #只从本次真实用户输入管理系统设置；模型工具、历史和项目文件都没有此入口。
            try:
                self.emit(self.model_settings.command(argument))
            except ModelSettingsError as error:
                self.emit(str(error), "warning")
            return None
        if command == "/permissions":
            #只有当前真实输入可以切换审批模式；不从模型、工具、记忆或历史恢复它。
            if argument:
                try:
                    self.approval_mode = validate_approval_mode(argument)
                except ValueError as error:
                    self.emit(str(error), "warning")
                    return None
            self.emit(f"审批模式：{self.approval_mode} — {APPROVAL_LABELS[self.approval_mode]}")
            return None
        if command == "/memory":
            self.emit(self.memory.command(argument, self.plan.mode))
            return None
        if command == "/session":
            self.emit(f"Session: {self.session.id}" if self.session else "当前没有持久会话")
            return None
        if command == "/sessions":
            if self.session:
                for item in self.session.store.list_sessions():
                    self.emit(f"{item['id']}  {item['updated_at']}  {item['title']}")
            return None
        if command == "/plan":
            self.goal.user_input.cancel(self.messages)
            self.plan.enter(argument)
            self._pause_goal("Plan mode is read-only")
            self.pending_goal_execution = False
            self.save()
            if not argument:
                self.emit("已进入只读 Plan 模式。输入任务生成方案；确认后才能实施。")
                return None
            query = argument
        elif command == "/execute":
            self.goal.user_input.cancel(self.messages)
            query = argument or self.plan.request
            if argument and argument != self.plan.request:
                self.plan.enter(argument)
                self.pending_goal_execution = False
            self._authorize(event)
            self.save()
            if not query:
                self.emit("已切换 Execute 模式，等待你的任务。")
                return None
            query = self._execution_request(query)
        elif command == "/goal":
            if not argument:
                self.emit(self.goal.status())
                return None
            action = argument.split(maxsplit=1)
            lower = argument.lower()
            if lower in {"clear", "stop", "off", "reset", "none", "cancel"}:
                self.emit(self.goal.clear())
                self.pending_goal_execution = False
                self.save()
                return None
            if lower == "pause":
                self.pending_goal_execution = False
                self.emit(self.goal.pause())
                self.save()
                return None
            if action[0].lower() == "edit":
                if len(action) != 2:
                    raise GoalError("用法：/goal edit <完整的新目标描述>")
                query = self.goal.edit(action[1])
            elif lower == "resume":
                query = self.goal.resume()
            else:
                self.goal.set_goal(argument)
                query = argument
            if self.plan.mode == "plan":
                self.plan.enter(query)
                self.pending_goal_execution = (self.plan.plan_id, self.goal.active.revision)
                self.goal.pause("Goal updated, but Plan requires human execution confirmation")
                self.save()
                self.emit("Goal 已更新但仍暂停：当前为只读 Plan。先生成方案，再确认实施或输入 /execute。")
                return None
            #Goal 显式命令是在当前真实用户输入中发起，不使用历史审批。
            query = "执行当前 Goal；先核实工作区，不能重放旧工具。\n" + query
        else:
            intent = user_intent(event, self.plan)
            if intent == "plan":
                self.goal.user_input.cancel(self.messages)
                self.plan.enter(text)
                self.pending_goal_execution = False
                self._pause_goal("Human requested planning only")
                query = text
            elif self.plan.mode == "plan" and intent in {"confirm", "clarify"}:
                if intent == "clarify":
                    self.emit("你是要继续分析，还是按当前方案实施？请明确说明，或输入 /execute。", "question")
                    return None
                self.goal.user_input.cancel(self.messages)
                self._authorize(event)
                query = self._execution_request(self.plan.request or text)
            else:
                answering_question = self.goal.user_input.pending is not None
                query = self.goal.resume_from_user(self.messages, text)
                if self.plan.mode == "plan" and not answering_question:
                    #新的分析或修改要求会改变待审批方案，旧的简单“可以”不再适用。
                    self.plan.enter(text)
                    self.pending_goal_execution = False
        #距上次模型活动的间隔超过缓存 TTL 且历史足够大时，借窗口免费收缩一次。
        cache_window = (
            self._last_model_activity is not None
            and time.monotonic() - self._last_model_activity > CACHE_WINDOW_SECONDS
            and json_tokens(self.messages) >= CACHE_WINDOW_MIN_TOKENS)
        #一轮内主角色、子角色与辅助任务共用快照；下一次用户任务才采用刚修改的系统设置。
        self.models.settings = self.model_settings.clone()
        with lead_turn() as turn:
            context = RunContext(self.workspace, turn, mode=self.plan.mode,
                                 approval_mode=self.approval_mode,
                                 memory_home=self.memory.home,
                                 max_calls=int(self.config.get("max_model_calls", self.config.get("goal_max_turns", 40))),
                                 cache_window=cache_window)
            if context.max_calls < 1:
                raise ValueError("模型调用上限必须大于 0")
            context.processes = TurnProcessManager(self.workspace, turn)
            context.checkpoints = self.checkpoints.begin(turn.id, event.text)
            self.last_context = context
            self.goal.begin_query(self.messages)
            self.messages.append({"role": "user", "content": query})
            if self.session:
                self.session.set_title(query)
            result = None
            interrupted = False
            try:
                try:
                    check_cancelled()
                    trigger_hook("UserPromptSubmit", query)
                    self.save()
                    #召回与摘要分开：只按本次真实输入选择，不能把历史恢复通知当成用户请求。
                    context.memory_context = self.memory.recall(event.text, context)

                    def ask(question, options, tool_id=None):
                        output = self.goal.user_input.ask(question, options, tool_use_id=tool_id)
                        self.goal.wait_for_user(question)
                        return output

                    delivered_facts = set()

                    def gate():
                        if self.plan.mode == "plan" or not self.goal.is_running():
                            return None
                        #验收模型没有工具，不能打开子角色给出的引用；必须先展示宿主记录的真实执行事实。
                        facts = context.verification_records()
                        if any(not record.get("cleanup_confirmed", True) or record["status"] == "cleanup_failed" for record in facts):
                            self.goal.pause("Resource cleanup unconfirmed; cannot complete the goal")
                            self.closed = True
                            return None
                        fresh = []
                        for record in facts:
                            value = {key: item for key, item in record.items() if key != "output"}
                            value["output"] = str(record.get("output", ""))[:4000]
                            signature = json.dumps(value, sort_keys=True, ensure_ascii=False)
                            if signature not in delivered_facts and record["status"] != "running":
                                delivered_facts.add(signature)
                                fresh.append(value)
                        if fresh:
                            return ("[Runtime verification evidence — host recorded, not human authorization]\n" +
                                    json.dumps(fresh, ensure_ascii=False) +
                                    "\nReview actual failures/timeouts and code versions before claiming completion. "
                                    "A child's success summary cannot override these facts.")
                        running = [record for record in facts if record["status"] == "running"]
                        if running:
                            return ("[Runtime evidence] Background commands are still running. "
                                    "Delegate implementation to wait_process and inspect the exit status before goal evaluation.\n" +
                                    json.dumps(running, ensure_ascii=False))
                        return self.goal.stop_hook(self.messages)

                    result = self.runner.run(
                        AgentTask("orchestrator", query), context, messages=self.messages,
                        checkpoint=self.save, ask=ask, todo=self.todos.update,
                        before_finish=gate, extra_context=self.goal.worker_context,
                    )
                    check_cancelled()
                    if context.edit_recovery.stopped:
                        self.goal.block("重复编辑失败，当前回合已停止；等待用户决定下一步")
                    elif result.status == "blocked":
                        self.goal.block(result.summary)
                    if result.status == "limit":
                        self._pause_goal("Model call limit reached; explicit continuation is required")
                    if self.plan.mode == "plan" and result.status == "completed":
                        self.plan.publish(result.summary)
                    trigger_hook("Stop", self.messages)
                except BaseException as error:
                    if not isinstance(error, Exception):
                        turn.cancel()  #先停止异步响应，再等待进程收尾，不能让响应在清理期间继续输出。
                    raise
                finally:
                    turn.retain_seen_steering(self.messages)
                    records = context.processes.close()
                    context.evidence.extend(records)
                    if records:
                        reference = context.artifacts.save("process-results", records)
                        self.messages.append({"role": "user", "content": (
                            "[Runtime process evidence — not a human request]\n" +
                            json.dumps(records, ensure_ascii=False) + f"\nFull record: {reference}"
                        )})
                        incomplete = [record for record in records if record["status"] in {"cancelled", "cleanup_failed", "running"}]
                        if incomplete:
                            warning = "后台命令未完成验证，已停止或清理失败；不能据此宣称检查通过。"
                            self.emit(warning, "warning")
                            if result is not None:
                                result.summary += "\n" + warning
                            if self.goal.is_running():
                                self.goal.pause(warning)
                    self.save()
                #记忆提取可能发出本轮最后一次模型请求；此时仍复用同一连接池。
                if (result is not None and result.status == "completed" and not self.closed
                        and not turn.cleanup_errors and not self.goal.user_input.pending
                        and not any(not item.get("cleanup_confirmed", True)
                                    or item["status"] in {"running", "cancelled", "cleanup_failed"}
                                    for item in context.verification_records())):
                    self.memory.extract(event.text, context)
            except BaseException as error:
                if isinstance(error, KeyboardInterrupt) or not isinstance(error, Exception):
                    interrupted = True
                    turn.cancel()
                    self.interrupt(turn)
                elif isinstance(error, SessionError):
                    raise
                else:
                    self.goal.block(f"Runtime failure: {type(error).__name__}")
                raise
            finally:
                try:
                    #所有模型请求完成后才永久关闭；返回、展示和最终状态保存都在关闭之后。
                    turn.close()
                    if not interrupted:
                        check_cancelled()  #关闭期间 Esc 只设置取消标记，也必须撤销执行批准。
                except BaseException as error:
                    if not isinstance(error, Exception) and not interrupted:
                        turn.cancel()
                        self.interrupt(turn)
                    raise
                finally:
                    unsafe_cleanup = any(not item.get("cleanup_confirmed", True) or item["status"] == "cleanup_failed"
                                         for item in context.verification_records())
                    warning = None
                    if turn.cleanup_errors or unsafe_cleanup:
                        warning = "本轮有资源未确认清理，不能宣称成功；已停止接收新任务，请退出并检查进程。"
                        self._pause_goal(warning)
                        self.messages.append({"role": "user", "content": (
                            "[Runtime cleanup failure — not a human request]\n" + warning +
                            "\nCleanup error types: " + json.dumps(turn.cleanup_errors)
                        )})
                        if result is not None:
                            result.status = "blocked"
                            result.summary = warning
                        self.closed = True
                    self._last_model_activity = time.monotonic()
                    checkpoint_warning = self._finish_checkpoint(context, "cancelled" if turn.cancelled.is_set() else (
                        result.status if result is not None else "failed"))
                    if checkpoint_warning is not None and result is not None:
                        result.status, result.summary = "blocked", checkpoint_warning
                    self.save()
                    if warning is not None:
                        self.emit(warning, "warning")
            #进程和模型连接收尾后再整理，防止把“已启动”或清理前状态作为最终检查结果。
            try:
                if context.changes or context.verification_records():
                    summary = build_verification_summary(context)
                    reference = context.artifacts.save("verification-summary", summary)
                    result.verification_summary = summary
                    result.verification = [{**item, "output": str(item.get("output", ""))[:2000]}
                                           for item in context.verification_records()]
                    result.evidence_refs.append(reference)
                    self.messages.append({"role": "user", "content": (
                        "[Runtime final verification — recorded facts, not human authorization]\n" +
                        json.dumps(summary, ensure_ascii=False) + f"\nFull record: {reference}"
                    )})
                    if not self.goal.user_input.pending:
                        result.summary += "\n\n" + format_verification_summary(summary)
                    self.save()
            except BaseException as error:
                #汇总也在当前 Turn 的取消边界内；换成其他界面时不能依赖 CLI 补偿暂停。
                if not isinstance(error, Exception):
                    turn.cancel()
                    self.interrupt(turn)
                elif not isinstance(error, SessionError):
                    self.goal.block(f"Runtime verification failure: {type(error).__name__}")
                    self.save()
                raise
            if self.goal.user_input.pending:
                pending = self.goal.user_input.pending
                #保留原来的纯文本事件，同时让 CLI 等适配器直接使用结构化问题，不解析展示文字。
                self.emit(self.goal.user_input.render(), "question",
                          question=pending.question, options=list(pending.options))
            else:
                self.emit(result.summary, "answer")
            summary = self._usage_summary(turn)
            if summary is not None:
                self.emit(summary, "notice")
            return result

    def _execution_request(self, request):
        return (f"用户已明确授权执行当前任务：{request}\n"
                f"Current plan {self.plan.plan_id}, revision {self.plan.revision}:\n{self.plan.summary}\n"
                "方案只是上下文，先核实当前代码；不要重放历史工具，遵守用户限制并获得真实验证证据。")

    def close(self):
        """CLI 退出只关闭本应用拥有的资源；不会复活或删除旧工作区数据。"""
        if self._resources_closed:
            return
        try:
            if self.last_context is not None:
                self.last_context.processes.close()
            self.goal.user_input.cancel(self.messages)
            self._pause_goal("CLI exited; explicit resume is required")
            self.plan.invalidate()
            self.save()
        finally:
            close_mcp_clients()
            stop_shell_processes()
            try:
                self.models.close()
            finally:
                close = getattr(self.client, "close", None)
                if close is not None:
                    close()
                self.closed = True
                self._resources_closed = True
