import copy
import json
import threading
from datetime import datetime

from anthropic import Anthropic

from coding_agent.agents import team
from coding_agent.runtime.background import BACKGROUND, execute_tool, inject_background_results
from coding_agent.runtime.compact import ContextCompact
from coding_agent.runtime.environment import get_environment
from coding_agent.runtime.cron import (CRON_HANDLERS, CRON_TOOLS, acknowledge_cron_jobs,
                     consume_cron_queue, has_cron_queue, load_durable_jobs,
                     poll_due_jobs, restore_cron_jobs)
from coding_agent.runtime.goal import GoalController, GoalError, PromptGoalEvaluator
from coding_agent.runtime.hooks import register_hook, trigger_hook
from coding_agent.integrations.mcp import assemble_system_prompt, assemble_tool_pool
from coding_agent.agents.subagent import configure_subagent_runtime, run_subagent
from coding_agent.tools.todo import run_todo_write
from coding_agent.tools.files import LOAD_SKILL_TOOL, base_tools, build_system_prompt, run_load_skill
from coding_agent.tools.user_input import ASK_USER_TOOL
from coding_agent.runtime.workspace import CONFIG_PATH, WORKDIR

with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    config = json.load(f)
client = Anthropic(
    auth_token=config["api_key"],
    base_url=config["base_url"]
)
#队友使用与主 Agent 相同的客户端和模型，但每个队友保留自己的 messages。
team.configure_team_runtime(client, config["model"])
configure_subagent_runtime(client, config["model"])
COMPACTOR = ContextCompact(
    client, config['model'], WORKDIR / ".transcripts",
    WORKDIR / ".task_outputs" / "tool-results"
)
MAX_REACTIVE_RETRIES = 1
GOAL_MAX_TURNS = int(config.get("goal_max_turns", 40))
if GOAL_MAX_TURNS < 1:
    raise GoalError("goal_max_turns 必须大于 0")
GOAL = GoalController(
    PromptGoalEvaluator(client, config.get("goal_evaluator_model") or config["model"]),
    block_cap=int(config.get("goal_block_cap", 8)),
    background_pending=BACKGROUND.has_pending,
)
#只有主会话经过 begin_query 绑定；子 agent 共用 Stop hook 时不会进入此 gate。
register_hook("Stop", GOAL.stop_hook)
SYSTEM = build_system_prompt() + (
    "\n\n"
    "Before starting any multi-step task, use todo_write to plan your steps. "
    "Update status as you go. The task tool runs a one-shot subagent. "
    "Set run_in_background to true only for independent Bash commands. "
    "Use schedule_cron for prompts that should run at a future local time. "
    "Call connect_mcp before using tools from an MCP server. "
    "Use reasonable defaults for routine implementation details. When missing "
    "information, consequential user preferences, or additional authorization "
    "require a human answer, call ask_user and wait. Ask one self-contained question "
    "per response, with optional suggested answers. Do not call other tools in the "
    "same response or assume an answer before the human replies."
) + "\n\n" + team.PROMPT_SECTIONS["tasks"] + "\n\n" + team.PROMPT_SECTIONS["teams"]
TOOLS = [#"type": "object"表示模型调用工具时，input 必须是一个对象，也就是 Python 里的 dict。required 表示 input 必须包含哪些字段。properties 定义了每个字段的类型和描述。
    *base_tools(background=True),
    {"name": "todo_write", "description": "Create and manage a task list for your current coding session.",
     "input_schema": {"type": "object", "properties": {"todos": {"type": "array", "maxItems": 10, "items": {"type": "object", "properties": {"content": {"type": "string", "minLength": 1}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}}, "required": ["content", "status"]}}}, "required": ["todos"]}},
     {
    "name": "task",
    "description": "Run a subagent with fresh conversation context and return its final text.",
    "input_schema": {
        "type": "object",
        "properties": {"prompt": {"type": "string", "minLength": 1}},
        "required": ["prompt"],
    },
},
    LOAD_SKILL_TOOL,
    ASK_USER_TOOL,
    {"name": "compact", "description": "Summarize earlier conversation to free context space.",
     "input_schema": {"type": "object", "properties": {}}},
    *team.TASK_TOOLS,
    *CRON_TOOLS,
    *team.TEAM_TOOLS,
]
TOOL_HANDLERS = {
    #文件与 Shell 工具从当前 Task assignment 选择工作目录；没有 assignment 时使用 WORKDIR。
    "bash": team.run_agent_bash, "read_file": team.run_agent_read,
    "write_file": team.run_agent_write, "edit_file": team.run_agent_edit,
    "glob": team.run_agent_glob, "todo_write": run_todo_write,
    "task": lambda prompt: run_subagent(prompt, cwd=team.assignment_cwd("agent")),
    "load_skill": run_load_skill,
    #主 Agent 和队友统一使用 team.py 的任务板，避免两套 .tasks 状态互相覆盖。
    **team.TASK_HANDLERS,
    **team.TEAM_HANDLERS,
    **CRON_HANDLERS,
}

#主循环和定时线程共用会话；锁保证同一时间只有一个回合修改 history。
RUNTIME_STOP = threading.Event()
runtime_threads: list[threading.Thread] = []
agent_lock = threading.Lock()
session_history: list = []


#定时任务交给模型前不算完成；首次模型调用失败时恢复消息和队列。
def agent_loop(messages: list, active_request: str | None = None):
    try:
        return _agent_loop(messages, active_request)
    finally:
        #仅释放已完成 Task 的目录绑定；未完成任务在等待用户或异常时继续保留。
        team.release_completed_assignment("agent")


def _agent_loop(messages: list, active_request: str | None = None):
    #定时任务、队友消息不等于用户回答；等待期间保留队列，不启动模型。
    if GOAL.user_input.pending is not None:
        return
    if active_request is None and not GOAL.can_run_automatically():
        return
    rounds_since_todo = 0
    reactive_retries = 0
    inject_background_results(messages)
    waiting_for_ack = consume_cron_queue()
    #Lead 是邮箱的唯一消费者；事件和定时任务都在模型成功接收前保留重试能力。
    team_events = team.consume_lead_inbox()
    before_delivery = copy.deepcopy(messages) if waiting_for_ack or team_events else None
    if active_request is None and not waiting_for_ack and not team_events:
        return
    if waiting_for_ack:
        scheduled_text = "\n".join(f"[Scheduled] {job.prompt}" for job in waiting_for_ack)
        messages.append({"role": "user", "content": scheduled_text})
        active_request = "\n".join(filter(None, (active_request, scheduled_text)))
        for job in waiting_for_ack:
            print(f"  [cron] delivered {job.id}: {job.prompt[:60]}")
    if team_events:
        event_text = team.format_team_events(team_events)
        messages.append({"role": "user", "content": event_text})
        active_request = "\n".join(filter(None, (active_request, event_text)))
        print(f"  [team] delivered {len(team_events)} event(s) to Lead")

    #模型调用失败时恢复消息，再把尚未交付的任务/事件放回各自队列。
    def restore_undelivered():
        if not waiting_for_ack and not team_events:
            return
        messages[:] = before_delivery
        if waiting_for_ack:
            restore_cron_jobs(waiting_for_ack)
        if team_events:
            team.restore_lead_inbox(team_events)

    first_round = True
    goal_turns = 0
    GOAL.begin_query(messages)
    while True:
        #工具调用也计入上限，避免 agent 一直调用工具而绕过 Stop 拦截上限。
        if GOAL.active is not None and goal_turns >= GOAL_MAX_TURNS:
            GOAL.active.status = "limit"
            GOAL.last_decision = "limit"
            GOAL.active.last_reason = "Model turn limit reached; goal remains active"
            GOAL.last_status = GOAL.active.last_reason
            print(f"[goal] {GOAL.last_status}")
            return
        #每轮先收集完成通知，再让压缩器估算加入通知后的上下文大小。
        if not first_round:
            inject_background_results(messages)
        first_round = False
        try:
            messages[:] = COMPACTOR.prepare(messages, active_request)
            #每轮重新组装：connect_mcp 成功后，新发现的工具在下一轮才进入模型可见列表。
            tools, handlers = assemble_tool_pool(TOOLS, TOOL_HANDLERS)
            response = client.messages.create(
                model = config['model'], system=assemble_system_prompt(SYSTEM + GOAL.worker_context()), messages=messages,
                tools=tools, max_tokens=config["max_tokens"],
            )
            reactive_retries = 0
            goal_turns += 1
        except Exception as e:
            too_long = any(text in str(e).lower()
                           for text in ("prompt_too_long", "too many tokens"))
            if too_long and reactive_retries < MAX_REACTIVE_RETRIES:
                print("[reactive compact]")
                try:
                    messages[:] = COMPACTOR.reactive_compact(messages, active_request)
                except Exception:
                    restore_undelivered()
                    raise
                reactive_retries += 1
                continue
            restore_undelivered()
            raise

        messages.append({"role": "assistant", "content": response.content}) 
        #首次模型调用成功后才确认交付；后续回合失败不会重复投递同一事件。
        team_events = []
        if waiting_for_ack:
            try:
                acknowledge_cron_jobs(waiting_for_ack)
            except Exception as error:
                print(f"  [cron] acknowledgement failed: {error}")
            waiting_for_ack = []

        tool_calls = [
                    block for block in response.content if block.type == "tool_use"
                ]
        ask_calls = [block for block in tool_calls if block.name == "ask_user"]
        if ask_calls:
            #整批先检查提问，不执行同批任何其他工具，并为每个 tool_use 配对结果。
            #等待的 tool_result 先记录占位；真实回答到来后更新为 answered。
            waiting = False
            ask_id = ask_calls[0].id
            try:
                if len(ask_calls) != 1:
                    raise ValueError("每次响应只能调用一次 ask_user")
                block = ask_calls[0]
                blocked = trigger_hook("PreToolUse", block)
                if blocked is not None:
                    raise ValueError(str(blocked))
                output = GOAL.user_input.ask(tool_use_id=block.id, **block.input)
                GOAL.wait_for_user(block.input["question"])
                waiting = True
                trigger_hook("PostToolUse", block, output)
            except (TypeError, ValueError) as error:
                output = f"Error: {error}"
            results = []
            for block in tool_calls:
                is_ask = block.id == ask_id
                results.append({
                    "type": "tool_result", "tool_use_id": block.id,
                    "content": output if is_ask else (
                        "Not executed: ask_user is a batch boundary. "
                        "Reissue this operation after the user has answered."
                    ),
                    "is_error": not (waiting and is_ask),
                })
            messages.append({"role": "user", "content": results})
            if waiting:
                return  #不经过 gate，明确让终端读取真实用户回答。
            continue
        if not tool_calls:
            #响应生成期间可能有后台结果完成，先让 worker 读取结果，再验收。
            if GOAL.active is not None and inject_background_results(messages):
                continue
            force = trigger_hook("Stop", messages) 
            if force :
                messages.append({"role": "user", "content": force})
                continue
            return
        result = []
        use_todo = False;
        compact_requested = False
        for block in tool_calls:
            print(f"工具调用{block.name}")
            if block.name == "compact":
                try:
                    blocked = trigger_hook("PreToolUse", block)
                    if blocked is not None:
                        output = str(blocked)
                    else:
                        output = "Compaction requested after this tool batch."
                        compact_requested = True
                        trigger_hook("PostToolUse", block, output)
                except Exception as e:  # noqa: BLE001
                    output = f"Error: {e}"
            else:
                #内置工具和 MCP 工具都经过同一条 hook/权限/结果分发路径。
                output = execute_tool(
                    block, handlers, background_cwd=lambda: team.assignment_cwd("agent")
                )
            # print(f"output: {output[:200]}")
            if block.name == "todo_write":
                use_todo = True
            result.append({"type": "tool_result", "tool_use_id": block.id, "content": output or "(no output)"})
        rounds_since_todo = 0 if use_todo else rounds_since_todo + 1
        if rounds_since_todo >= 3:
            result.append({"type": "text","text": "<reminder>Update your todos.</reminder>"})

        messages.append({"role": "user", "content": result})
        if compact_requested:
            messages[:] = COMPACTOR.compact_history(messages, active_request)


#每秒按本地时间检查一次；同一分钟内重复检查由 last_fired 去重。
def cron_scheduler_loop():
    while not RUNTIME_STOP.wait(1.0):
        poll_due_jobs(datetime.now())


#主 Agent 空闲时处理定时任务和队友消息，避免后台线程与用户回合同时改会话。
def queue_processor_loop():
    while not RUNTIME_STOP.wait(0.2):
        #定时任务和队友邮箱共用 Lead 的唤醒线程，不需要再启动第二个主 Agent。
        if (not has_cron_queue() and not team.BUS.peek("lead")) or not agent_lock.acquire(blocking=False):
            continue
        retry_failed = False
        try:
            if not GOAL.can_run_automatically():
                continue
            if has_cron_queue() or team.BUS.peek("lead"):
                agent_loop(session_history)
                print_latest_assistant_text(session_history)
        except Exception as error:
            print(f"  [runtime] automatic turn failed: {error}")
            retry_failed = True
        finally:
            agent_lock.release()
        if retry_failed:
            #失败的任务已放回队列，短暂等待后再试，避免不停请求模型。
            RUNTIME_STOP.wait(5.0)


#CLI 启动时恢复磁盘任务，再启动轮询和队列处理线程。
def start_runtime_threads():
    if runtime_threads:
        return
    load_durable_jobs()
    RUNTIME_STOP.clear()
    runtime_threads.extend([
        threading.Thread(target=cron_scheduler_loop, name="cron-scheduler", daemon=True),
        threading.Thread(target=queue_processor_loop, name="agent-event-processor", daemon=True),
    ])
    for thread in runtime_threads:
        thread.start()


#退出 CLI 时停止轮询；已保存的 durable 任务下次启动仍会恢复。
def stop_runtime_threads():
    RUNTIME_STOP.set()
    for thread in runtime_threads:
        thread.join(timeout=1)
    runtime_threads.clear()


#输出最近一次 Agent 回复的文字，供用户回合和定时回合复用。
def print_latest_assistant_text(messages: list):
    if GOAL.user_input.pending is not None:
        print(GOAL.user_input.render())
        return
    if GOAL.active is not None and GOAL.active.status == "blocked":
        print(f"[等待外部条件变化] {GOAL.active.last_reason}")
        return
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        content = message.get("content", "")
        if isinstance(content, str):
            print(content)
        else:
            for block in content:
                if getattr(block, "type", None) == "text":
                    print(block.text)
                elif isinstance(block, dict) and block.get("type") == "text":
                    print(block.get("text", ""))
        return


def submit_query(query: str):
    """在 agent_lock 内调用；goal 控制命令不送给模型执行。"""
    stripped = query.strip()
    if not stripped:
        return
    if stripped == "/goal":
        print(GOAL.status())
        return
    if stripped.startswith("/goal "):
        condition = stripped[6:].strip()
        if condition.lower() in {"clear", "stop", "off", "reset", "none", "cancel"}:
            print(GOAL.clear())
            return
        GOAL.set_goal(condition)
        query = condition
    else:
        #仅终端提交的非空用户输入解除等待，后台队列不经过这条路径。
        try:
            query = GOAL.resume_from_user(session_history, query)
        except ValueError as error:
            print(f"[ask_user] {error}")
            print(GOAL.user_input.render())
            return
    trigger_hook("UserPromptSubmit", query)
    session_history.append({"role": "user", "content": query})
    agent_loop(session_history, query)
    print_latest_assistant_text(session_history)

def run_cli():
    #Docker 配置有误时在接收任务前报错，不会悄悄退回本机。
    try:
        get_environment().prepare()
    except (OSError, ValueError) as error:
        raise SystemExit(str(error)) from None
    print(f"Workspace: {WORKDIR}\nExecution: {get_environment().config.backend}")
    print("Agent is running. quit with 'exit' or Ctrl+C.\n")
    start_runtime_threads()
    try:
        while True:
            try:
                query = input(">> ")
            except (EOFError, KeyboardInterrupt):
                break
            if query.strip().lower() in ("q", "exit", "quit"):
                break
            with agent_lock:
                try:
                    submit_query(query)
                except GoalError as error:
                    print(f"[goal] {error}")
    finally:
        stop_runtime_threads()
        #程序退出时让队友释放未完成任务的认领；超时则明确提示仍在运行的线程。
        unfinished = team.stop_teammates()
        if unfinished:
            print(f"[team] Teammates still finishing: {', '.join(unfinished)}")
        
