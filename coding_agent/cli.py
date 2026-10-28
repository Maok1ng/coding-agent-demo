"""CLI 适配器：终端属于交互层，核心 Application 不直接读取 stdin。"""

import json
import shlex
from collections import deque

from coding_agent.cli_ui import _safe_text, answer_prompt, format_change_preview, format_question
from coding_agent.runtime.workspace import configure_cli


class TerminalInteraction:
    def __init__(self):
        self._stream_id = None
        self._stream_text = ""
        self._stream_role = None
        self._last_streamed_answer = None

    def _finish_stream(self):
        if self._stream_id is not None:
            print(flush=True)
            self._stream_id = None

    def emit(self, event):
        kind = event["type"]
        if kind == "text_delta":
            if event["request_id"] != self._stream_id:
                self._finish_stream()
                self._stream_id = event["request_id"]
                self._stream_text = ""
                self._stream_role = event["role"]
                print(f"[{self._stream_role}] ", end="", flush=True)
            self._stream_text += event["text"]
            print(_safe_text(event["text"]), end="", flush=True)
            return
        if kind == "text_end":
            if self._stream_id == event["request_id"]:
                if self._stream_role == "orchestrator" and event["status"] == "completed":
                    self._last_streamed_answer = self._stream_text
                self._finish_stream()
            return
        self._finish_stream()
        if kind == "agent_started" and event["role"] == "orchestrator":
            self._last_streamed_answer = None
        if kind == "answer" and self._last_streamed_answer:
            streamed = self._last_streamed_answer
            self._last_streamed_answer = None
            if event["text"].strip() == streamed.strip():
                return  #完整回复已经实时显示，不能在回合结束后再打印一遍。
            if event["text"].startswith(streamed):
                print(_safe_text(event["text"][len(streamed):]).lstrip("\n"), flush=True)
                return  #清理阶段追加的未验证提醒仍必须显示。
        if kind == "change_preview":
            print(format_change_preview(
                event["path"], event["diff"], truncated=event.get("truncated", False),
                reference=event.get("reference"), approval_action=event.get("approval_action", "confirm"),
            ), flush=True)
        elif kind == "question":
            print(format_question(event.get("question", event["text"]), event.get("options", ())), flush=True)
        elif "text" in event:
            #检查点摘要也来自历史用户文本，不能让标签中的控制字符影响终端。
            print(_safe_text(event["text"]), flush=True)
        elif event["type"] == "agent_started":
            print(f"[{event['role']}] started", flush=True)
        elif event["type"] == "tool_result":
            state = "error" if event["is_error"] else "done"
            print(f"  [{event['role']}] {event['tool']}: {state}", flush=True)
            for image in event.get("images", []):
                print("    截图：" + _safe_text(image), flush=True)

    def confirm(self, name, arguments, reason):
        from coding_agent.runtime.terminal import read_user_input
        from coding_agent.runtime.turn import SessionExit

        #只在交互层读取确认；MCP 参数可能含敏感数据，不重复打印整个参数对象。
        self._finish_stream()
        scope = ("仅批准本次候选修改，文件有变化时需要重新读取。"
                 if name in {"write_file", "edit_file"} else
                 "仅批准本次调用，不代表后续操作也获准。")
        if name == "restore_checkpoint":
            scope = "确认后将回退这些文件；聊天历史保留，不会自动继续任务。"
        print(format_question(f"操作确认：{name}\n{reason}\n{scope}"), flush=True)
        if name == "restore_checkpoint":
            print("目标检查点：" + _safe_text(str(arguments.get("checkpoint", ""))).replace("\n", " "), flush=True)
            print("涉及文件：", flush=True)
            for path in arguments.get("paths", []):
                print("  · " + _safe_text(str(path)).replace("\n", " "), flush=True)
        elif name == "bash":
            print(_safe_text(arguments.get("command", "")), flush=True)
        elif name in {"read_file", "write_file", "edit_file", "git_diff", "search_code"}:
            print("路径：" + _safe_text(str(arguments.get("path", "."))), flush=True)
        elif name in {"read_files", "search_many"}:
            items = arguments.get("files" if name == "read_files" else "queries", [])
            print("本次批量调用：", flush=True)
            for item in items:
                detail = _safe_text(str(item.get("path", "."))).replace("\n", " ")
                if name == "search_many":
                    detail += " · " + _safe_text(str(item.get("query", ""))).replace("\n", " ")
                else:
                    detail += f" · 行 {item.get('start_line', 1)}–{item.get('end_line', '默认上限')}"
                print("  · " + detail, flush=True)
        elif name in {"connect_mcp", "dispatch_agent", "load_skill"}:
            print(_safe_text(str(arguments.get("name", arguments.get("role", "")))), flush=True)
        elif name == "glob":
            print("匹配：" + _safe_text(arguments.get("pattern", "")), flush=True)
        elif name == "read_url":
            print("网页：" + _safe_text(arguments.get("url", "")), flush=True)
        elif name in {"list_symbols", "find_symbol"}:
            print("路径：" + _safe_text(arguments.get("path", ".")), flush=True)
            if name == "find_symbol":
                print("符号：" + _safe_text(arguments.get("symbol", "")), flush=True)
        try:
            return read_user_input("Do you want to proceed? (y/n): ").strip().lower() in {"y", "yes"}
        except EOFError:
            raise SessionExit from None


def run_cli(application):
    from coding_agent.agents.runner import response_text
    from coding_agent.runtime.approval import APPROVAL_LABELS
    from coding_agent.runtime.goal import GoalError
    from coding_agent.runtime.model_settings import ModelSettingsError
    from coding_agent.runtime.session import SessionError
    from coding_agent.runtime.terminal import monitor_interrupts, read_user_input
    from coding_agent.runtime.turn import SessionExit

    application.prepare()
    application.emit(f"Workspace: {application.workspace}")
    application.emit(f"Execution: {getattr(application, 'execution_backend', 'local')}")
    application.emit(f"审批模式：{application.approval_mode} — {APPROVAL_LABELS[application.approval_mode]}")
    application.emit("/permissions ask|auto|full 切换审批；审批模式不能解除 /plan 的只读限制。")
    if application.session is not None:
        session = application.session
        application.emit(f"Session: {session.id}" + (" (resumed)" if session.resumed else " (new)"))
        if session.resumed:
            application.emit("历史已恢复，等待用户输入；旧工具、进程和权限批准不会自动恢复。")
            if application.goal.active is not None:
                application.emit(application.goal.status() + "\n使用 /goal resume 明确继续；Plan 限制仍然有效。")
            for message in reversed(application.messages):
                if message["role"] == "assistant":
                    application.emit(response_text(message["content"]))
                    break
    application.emit("Esc / Ctrl+C 中断本轮；运行中直接输入可纠偏；/followup 内容 排队到下一回合；/plan 只读规划；/execute 执行；exit / Ctrl+D 退出。")
    application.emit("/memory list 查看长期记忆；/memory show 或 forget <user|project>/名称.md。")
    application.emit("/models 查看系统模型设置；/models help 查看模型档案与任务绑定命令。")
    application.emit("/checkpoints 查看代码检查点；/undo 撤销最近一轮；/restore C编号 恢复指定版本。")
    drafts, followups = deque(), deque()

    def running_input(text):
        command, _, content = text.partition(" ")
        if command == "/followup":
            return application.follow_up(content.strip())
        if command == "/steer":
            return application.steer(content.strip())
        return application.steer(text)

    try:
        while not application.closed:
            keyboard, completed = None, False
            try:
                pending = application.goal.user_input.pending
                if pending and followups:
                    #追加消息不是对尚未展示的问题的回答，必须交还编辑器由用户确认。
                    drafts.extend(followups)
                    followups.clear()
                if followups and not drafts:
                    query = followups.popleft()
                    application.emit("[开始追加回合] " + query)
                else:
                    initial = drafts.popleft() if drafts else ""
                    query = (read_user_input(answer_prompt() if pending else ">> ", initial=initial)
                             if initial else read_user_input(answer_prompt() if pending else ">> "))
                if query.strip().lower() in {"q", "exit", "quit"}:
                    break
                if pending and not query.strip().startswith("/"):
                    try:
                        #这里只校验输入；仍提交原始编号，不能把选项文字提升为宿主命令或执行授权。
                        application.goal.user_input.validate_answer(query)
                    except ValueError as error:
                        application.emit(f"[输入提示] {error}", "warning")
                        continue
                with monitor_interrupts(on_line=running_input) as keyboard:
                    #提交前 Esc 已经到达时，不能再启动新的模型请求。
                    if keyboard is not None and keyboard.interrupted.is_set():
                        application.interrupt()
                        continue
                    result = application.submit(query)
                    completed = (getattr(result, "status", None) == "completed"
                                 and not application.goal.user_input.pending and not application.closed)
            except EOFError:
                break
            except SessionExit:
                break
            except KeyboardInterrupt:
                #Application 内已经保存中断；空闲时中断则在这里保存。
                context = application.last_context
                if context is None or context.turn.done.is_set():
                    if not application.messages or not str(application.messages[-1]["content"]).startswith("[Turn interrupted"):
                        application.interrupt()
                application.emit("本轮已中断，目标已暂停；等待你的下一条消息。")
            except SessionError:
                raise
            except GoalError as error:
                application.emit(f"[goal] {error}", "warning")
            except ModelSettingsError as error:
                application.emit(f"[models] {error}", "warning")
            except Exception as error:
                #不回显网络异常原文，避免配置凭据进入终端或历史。
                application.emit(f"[runtime] {type(error).__name__}；任务已停止，可查看历史后重试。", "warning")
            finally:
                #先退出监听器再取队列；中断和自然结束都不能与最后一次按键竞争。
                returned, ready = application.finish_inputs(followups=completed)
                drafts.extend(returned)
                followups.extend(ready)
                if keyboard is not None:
                    drafts.extend(keyboard.take_pending())
                if not completed and followups:
                    drafts.extend(followups)
                    followups.clear()
    finally:
        application.close()


def main():
    args = configure_cli()
    from coding_agent.runtime import workspace
    from coding_agent.runtime.model_settings import ModelSettings, ModelSettingsError
    from coding_agent.runtime.session import SessionError, SessionStore

    session = None
    try:
        if args.models is not None:
            #纯设置命令先于会话、客户端、MCP 和 Docker；没有 API 配置也可以添加模型档案。
            config = {}
            if workspace.CONFIG_PATH.is_file():
                with workspace.CONFIG_PATH.open(encoding="utf-8") as handle:
                    config = json.load(handle)
            settings = ModelSettings(config, path=args.models_config, load=True)
            print(settings.command(shlex.join(args.models)))
            return
        store = SessionStore(workspace.WORKDIR)
        #历史查询先于模型模块导入，不需要模型凭据，也不会连接 MCP 或启动 Docker。
        if args.list_sessions:
            store.show_sessions(store.list_sessions())
            return
        session_id = None
        if args.resume is not None:
            session_id = store.select(args.resume)
            if session_id is None:
                return
        elif args.continue_session:
            history = store.list_sessions()
            if not history:
                raise SessionError("当前工作区没有可继续的会话")
            session_id = history[0]["id"]
        session = store.open(session_id)
        from coding_agent.application import Application

        with workspace.CONFIG_PATH.open(encoding="utf-8") as handle:
            config = json.load(handle)
        settings = ModelSettings(config, path=args.models_config, load=True)
        if args.no_stream:
            config["stream"] = False
        #唯一组合入口：先选工作区/会话，再构造客户端和业务对象，无导入即启动副作用。
        application = Application(config, workspace.WORKDIR, TerminalInteraction(), session,
                                  model_settings=settings)
        try:
            run_cli(application)
        finally:
            application.close()
    except SessionError as error:
        raise SystemExit(f"[session] {error}") from None
    except ModelSettingsError as error:
        raise SystemExit(f"[models] {error}") from None
    finally:
        if session is not None:
            session.close()
