"""保留同步调用风格，但真实模型请求在 Turn 内使用可取消的异步连接。"""

import asyncio
import weakref

from anthropic import Anthropic, AsyncAnthropic

from coding_agent.runtime.turn import check_cancelled, current_turn, run_cancellable_async


CACHE_CONTROL = {"type": "ephemeral"}


def provider_arguments(arguments):
    """宿主消息标签只留在本地历史；是否启用缓存都不能把它们传给模型 API。"""
    arguments = dict(arguments)
    if arguments.get("messages"):
        arguments["messages"] = [
            {key: value for key, value in message.items() if key not in {"tags", "input_id"}}
            for message in arguments["messages"]
        ]
    return arguments


def cached_arguments(arguments):
    """参考 freebuff 的做法，在最后一个工具、system 和最后一条消息末尾放缓存断点。
    断点按请求前缀内容命中；每次按同一规则重算副本，不改写调用方的历史，
    也不会累积超过 4 个断点。未达到最小缓存长度的请求由 API 自动忽略，不报错。"""
    arguments = provider_arguments(arguments)
    tools = arguments.get("tools")
    if tools:
        marked = list(tools)
        marked[-1] = {**marked[-1], "cache_control": CACHE_CONTROL}
        arguments["tools"] = marked
    system = arguments.get("system")
    if isinstance(system, str) and system:
        arguments["system"] = [{"type": "text", "text": system, "cache_control": CACHE_CONTROL}]
    messages = arguments.get("messages")
    if messages:
        #字符串 content 统一转为文本块；同一条消息在多次请求中保持字节一致，前缀才能持续命中。
        #tags/input_id 仅供宿主保护真实输入，供应商协议不接受这些额外字段。
        marked = [{**message, "content": [{"type": "text", "text": message["content"]}]}
                  if isinstance(message.get("content"), str) else message for message in messages]
        arguments["messages"] = marked
        last = marked[-1]
        content = last.get("content")
        if isinstance(content, list) and content and isinstance(content[-1], dict):
            blocks = list(content)
            blocks[-1] = {**blocks[-1], "cache_control": CACHE_CONTROL}
            marked[-1] = {**last, "content": blocks}
            arguments["messages"] = marked
    return arguments


def _stream_event(event, on_text, text_blocks):
    #只展示文字 delta，不展示工具 JSON、隐藏推理或 SDK 的重复文字辅助事件。
    check_cancelled()
    if event.type == "content_block_start" and event.content_block.type == "text":
        #与 response_text 的多文本块拼接方式一致，避免最终完整回答多出换行而重复显示。
        if text_blocks[0]:
            on_text("\n")
        text_blocks[0] += 1
        if event.content_block.text:
            on_text(event.content_block.text)
    elif event.type == "content_block_delta" and event.delta.type == "text_delta":
        on_text(event.delta.text)
    return event.type == "message_stop"


#同一回合内的请求复用一个异步客户端（TLS 连接池）；回合结束时随事件循环一起关闭。
_turn_clients: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


async def _turn_client(host, turn):
    """首用时在回合循环线程内创建；关闭回调登记到回合收尾，先于循环停止执行。"""
    clients = _turn_clients.setdefault(turn, {})
    asynchronous = clients.get(host)
    if asynchronous is None:
        asynchronous = AsyncAnthropic(
            api_key=host.api_key, auth_token=host.auth_token,
            base_url=host.base_url, timeout=host.timeout, max_retries=host.max_retries,
            default_headers=host.default_headers, default_query=host.default_query,
        )
        #一个回合可选多个端点/凭据，不能把第二个宿主的请求发进第一个连接池。
        clients[host] = asynchronous

        def close_client(loop):
            cached_clients = _turn_clients.get(turn)
            cached = cached_clients.pop(host, None) if cached_clients is not None else None
            if cached_clients is not None and not cached_clients:
                _turn_clients.pop(turn, None)
            if cached is not None:
                asyncio.run_coroutine_threadsafe(cached.close(), loop).result(timeout=10)

        turn.async_runtime().add_close_callback(close_client)
    return asynchronous


#Anthropic 的 input_tokens 不含缓存部分；总输入 = 新鲜输入 + 缓存读 + 缓存写。
_USAGE_FIELDS = (
    ("input_tokens", "input_tokens"),
    ("cache_read_input_tokens", "cache_read_tokens"),
    ("cache_creation_input_tokens", "cache_creation_tokens"),
    ("output_tokens", "output_tokens"),
)


def _usage_value(usage, name):
    if isinstance(usage, dict):
        return usage.get(name) or 0
    return getattr(usage, name, 0) or 0


def _record_usage(turn, response):
    """把本次响应的真实 token 用量累加进回合指标；假响应没有 usage 时跳过。"""
    usage = getattr(response, "usage", None)
    if turn is None or usage is None:
        return
    metrics = turn.metrics
    metrics["model_calls"] = metrics.get("model_calls", 0) + 1
    for name, key in _USAGE_FIELDS:
        metrics[key] = metrics.get(key, 0) + int(_usage_value(usage, name))


def call_model(client, *, on_text=None, cache=True, **arguments):
    """可选地发送文字增量，但只返回 SDK 完整组装后的 Message，不执行半份工具参数。
    cache=False 用于一次性调用（压缩摘要、记忆分析）：前缀永不再命中，
    设断点只会为无人读取的缓存条目白付 25% 写溢价（对齐 pi 的 cacheRetention: "none"）。"""
    check_cancelled()
    arguments = cached_arguments(arguments) if cache else provider_arguments(arguments)
    turn = current_turn()
    if turn is None or not isinstance(client, Anthropic):
        #独立诊断调用可以同步执行；测试假客户端也沿用原接口，不连接真实服务。
        if on_text is None:
            response = client.messages.create(**arguments)
        else:
            with client.messages.stream(**arguments) as stream:
                stopped = False
                text_blocks = [0]
                for event in stream:
                    stopped = _stream_event(event, on_text, text_blocks) or stopped
                if not stopped:
                    raise ConnectionError("Model stream ended before message_stop")
                response = stream.get_final_message()
        _record_usage(turn, response)
        check_cancelled()
        return response

    async def request():
        #凭据仅由已配置的宿主客户端传递，不读取目标工作区的配置，也不写入 Session。
        asynchronous = await _turn_client(client, turn)
        if on_text is None:
            response = await asynchronous.messages.create(**arguments)
        else:
            #流式响应仍按请求开关：Esc、Ctrl+C 或连接异常时由任务取消收尾，关闭 SSE 与 HTTP 连接。
            async with asynchronous.messages.stream(**arguments) as stream:
                stopped = False
                text_blocks = [0]
                async for event in stream:
                    stopped = _stream_event(event, on_text, text_blocks) or stopped
                #SDK 的 get_final_message 本身可能返回 EOF 前的累积快照，不能代替结束标记。
                if not stopped:
                    raise ConnectionError("Model stream ended before message_stop")
                response = await stream.get_final_message()
        _record_usage(turn, response)
        return response

    return run_cancellable_async(request)
