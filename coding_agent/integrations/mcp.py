"""用官方 MCP SDK 接入外部工具；对同步主循环保留原来的调用接口。"""

import asyncio
import atexit
from concurrent.futures import CancelledError as FutureCancelled, Future, InvalidStateError, TimeoutError as FutureTimeout
from contextlib import AsyncExitStack
from functools import lru_cache
import json
import logging
import math
import os
from pathlib import Path
import re
import threading
from urllib.parse import urlsplit

from coding_agent.runtime.workspace import APP_DIR
from coding_agent.runtime.turn import TurnCancelled, check_cancelled, current_turn


_DISALLOWED_CHARS = re.compile(r"[^a-zA-Z0-9_-]")
_ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_mcp_lock = threading.RLock()
_catalog_version = 0
logger = logging.getLogger(__name__)


class MCPConfigError(ValueError):
    """只包含安全的配置提示，不把请求头、令牌或配置原文交给模型。"""


class MCPToolResult(str):
    #str 子类仍能经过原有 hook 和压缩器，同时把工具失败状态带回主循环。
    def __new__(cls, text: str, *, is_error: bool = False, content_blocks=None):
        result = super().__new__(cls, text)
        result.is_error = is_error
        #文字预览供 Hook/CLI 使用；图片协议块单独携带，不能先 str 再交给模型。
        result.content_blocks = content_blocks
        return result


def normalize_mcp_name(name: str) -> str:
    """Replace characters outside the model tool-name alphabet."""
    #正则把不适合模型工具名的字符替换成下划线。
    normalized = _DISALLOWED_CHARS.sub("_", name)
    if not normalized:
        raise MCPConfigError("MCP names cannot normalize to an empty string")
    return normalized


def _expand_environment(value: str) -> str:
    """连接时才展开 ${变量名}；缺少变量时报错，绝不把占位符当凭据发送。"""
    def replace(match):
        name = match.group(1)
        if not os.environ.get(name):
            raise MCPConfigError(f"Missing environment variable: {name}")
        return os.environ[name]
    return _ENV_REFERENCE.sub(replace, value)


def _string_list(config: dict, key: str):
    value = config.get(key)
    if key in config and value is None and key != "allowed_tools":
        raise MCPConfigError(f"MCP {key} must be a list, not null")
    if value is not None and (not isinstance(value, list) or any(
            not isinstance(item, str) or not item for item in value)):
        raise MCPConfigError(f"MCP {key} must be a list of non-empty strings")


def load_mcp_config(path: Path) -> dict[str, dict]:
    """读取宿主配置；不扫描目标仓库、不自动执行仓库中携带的 MCP 命令。"""
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise MCPConfigError(f"Cannot read MCP JSON config: {path}") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("mcpServers"), dict):
        raise MCPConfigError("MCP config needs a top-level mcpServers object")
    servers = {}
    safe_names = set()
    for name, original in payload["mcpServers"].items():
        if not isinstance(original, dict) or not isinstance(original.get("enabled", True), bool):
            raise MCPConfigError("Each MCP server needs an object and a boolean enabled flag")
        if not original.get("enabled", True):
            continue
        safe_name = normalize_mcp_name(name)
        if safe_name in safe_names:
            raise MCPConfigError("MCP server names collide after normalization")
        safe_names.add(safe_name)
        config = dict(original)
        transport = config.get("type", "stdio" if "command" in config else "http")
        if transport == "streamable-http":
            transport = "http"
        if transport not in {"http", "stdio"}:
            raise MCPConfigError("MCP type must be http, streamable-http or stdio")
        config["type"] = transport
        for key, default in (("connect_timeout", 15), ("timeout", 60)):
            value = config.get(key, default)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise MCPConfigError(f"MCP {key} must be a positive finite number")
            config[key] = float(value)
        for key in ("allowed_tools", "auto_approve", "read_only_tools", "args"):
            _string_list(config, key)
        for key in ("headers", "env"):
            value = config.get(key, {})
            if not isinstance(value, dict) or any(
                    not isinstance(k, str) or not isinstance(v, str) for k, v in value.items()):
                raise MCPConfigError(f"MCP {key} must map strings to strings")
        required = "url" if transport == "http" else "command"
        if not isinstance(config.get(required), str) or not config[required].strip():
            raise MCPConfigError(f"MCP {transport} server needs {required}")
        if "cwd" in config and not isinstance(config["cwd"], str):
            raise MCPConfigError("MCP cwd must be a string")
        #相对 cwd 以 MCP 配置文件所在目录为基准，不改变整个 Agent 的 cwd。
        config["config_dir"] = path.resolve().parent
        servers[name] = config
    return servers


@lru_cache(maxsize=1)
def get_mcp_config() -> dict[str, dict]:
    #CLI 先设置环境变量，再延迟读取；默认不从不可信目标项目加载配置。
    path = Path(os.getenv("AGENT_MCP_CONFIG", str(APP_DIR / "config" / "mcp.json")))
    if os.getenv("AGENT_MCP_CONFIG") and not path.expanduser().is_file():
        raise MCPConfigError("The explicitly selected MCP config does not exist")
    return load_mcp_config(path.expanduser().resolve())


def format_mcp_result(result) -> MCPToolResult:
    """文本原样保留、结构化数据转 JSON；不把二进制内容伪装成可读文字。"""
    parts = []
    content_blocks = []
    image_count = 0
    has_images = False
    is_error = result.is_error

    def text_part(value):
        parts.append(value)
        content_blocks.append({"type": "text", "text": value})

    for block in result.content:
        if block.type == "text":
            text_part(block.text)
        elif block.type == "image":
            image_count += 1
            if image_count > 4:
                #只解码前四张，避免服务一次返回大量图片占满宿主内存。
                if image_count == 5:
                    text_part("[MCP image rejected: at most 4 images per tool result; remaining images omitted]")
                is_error = True
                continue
            from coding_agent.runtime.media import normalize_image
            try:
                image = normalize_image(block.data, block.mime_type)
            except ValueError as error:
                text_part(f"[MCP image rejected: {error}]")
                is_error = True
                continue
            text_part(f"[MCP image {image_count}: {image['source']['media_type']}]")
            content_blocks.append(image)
            has_images = True
        elif block.type == "resource_link":
            text_part(f"[MCP resource] {block.name}: {block.uri}")
        elif block.type == "resource" and hasattr(block.resource, "text"):
            text_part(f"[MCP resource: {block.resource.uri}]\n{block.resource.text}")
        else:
            #本轮先支持搜索等文本型工具；图片/音频明确提示，不灌入整段 base64。
            text_part(f"[MCP {block.type} content is not rendered by this agent]")
    if result.structured_content is not None:
        structured = json.dumps(result.structured_content, ensure_ascii=False)
        if structured not in parts:
            text_part(structured)
    return MCPToolResult("\n".join(parts) or "(no output)", is_error=is_error,
                         content_blocks=content_blocks if has_images else None)


def _safe_error(error: BaseException) -> str:
    #TaskGroup 可能把实际错误包装成 ExceptionGroup；只展开类型和 HTTP 状态码。
    if isinstance(error, BaseExceptionGroup):
        return "; ".join(dict.fromkeys(_safe_error(item) for item in error.exceptions))
    if isinstance(error, MCPConfigError):
        return str(error)
    status = getattr(getattr(error, "response", None), "status_code", None)
    if isinstance(status, int):
        return f"HTTP {status} (check endpoint, authentication and permissions)"
    if isinstance(error, ModuleNotFoundError):
        return "MCP SDK dependency missing; install requirements.txt in the agent environment"
    return type(error).__name__


class MCPClient:
    """一个服务一个异步线程；SDK 上下文始终由同一个协程进入和退出。"""

    def __init__(self, name: str, config: dict):
        check_cancelled()
        self.name = name
        self.config = config
        self.tools: list[dict] = []
        self._ready = Future()
        self._loop = None
        self._queue = None
        self._task = None
        #连接尚未建立时只取消本次初始化，不关闭其它回合共享的 MCP 连接。
        self._ready.add_done_callback(self._cancel_connect)
        self._thread = threading.Thread(target=self._run, name=f"mcp-{name}", daemon=True)
        self._thread.start()
        turn = current_turn()
        remove_cancel = turn.add_cancel_callback(self._ready.cancel) if turn else lambda: None
        try:
            self.tools = self._ready.result(timeout=config["connect_timeout"] + 5)
            check_cancelled()
        except FutureCancelled:
            self.close()
            raise TurnCancelled("MCP connection cancelled") from None
        except BaseException:
            self.close()
            raise
        finally:
            remove_cancel()

    def _cancel_connect(self, future):
        #Future 的回调可能在用户输入线程执行；Task.cancel 必须投递回连接线程。
        if future.cancelled() and self._loop is not None and self._task is not None:
            try:
                self._loop.call_soon_threadsafe(self._task.cancel)
            except RuntimeError:
                pass  #事件循环已经结束，无需再次清理。

    @property
    def closed(self) -> bool:
        return not self._thread.is_alive()

    async def _open(self, stack):
        #延迟导入，--help 和无 MCP 的使用方式不需要提前创建连接或进程。
        from mcp import Client as SDKClient, StdioServerParameters
        if self.config["type"] == "stdio":
            cwd = self.config.get("cwd")
            if cwd is not None:
                cwd = Path(_expand_environment(cwd)).expanduser()
                cwd = (self.config["config_dir"] / cwd).resolve()
            parameters = StdioServerParameters(
                command=_expand_environment(self.config["command"]),
                args=[_expand_environment(arg) for arg in self.config.get("args", [])],
                env={key: _expand_environment(value) for key, value in self.config.get("env", {}).items()},
                cwd=cwd,
            )
            #不传 os.environ；SDK 只继承其最小环境和这里显式配置的变量。
            transport = parameters
        else:
            import httpx2
            from mcp.client.streamable_http import streamable_http_client
            url = _expand_environment(self.config["url"])
            parsed = urlsplit(url)
            #凭据只通过请求头传递；明文 HTTP 仅供本机开发服务使用。
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                    or parsed.username or parsed.password or parsed.fragment
                    or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"})):
                raise MCPConfigError("MCP needs an HTTPS URL, or HTTP on localhost, without embedded credentials")
            http_client = await stack.enter_async_context(httpx2.AsyncClient(
                headers={key: _expand_environment(value) for key, value in self.config.get("headers", {}).items()},
                timeout=httpx2.Timeout(self.config["connect_timeout"], read=self.config["timeout"]),
                follow_redirects=False,
            ))
            transport = streamable_http_client(url, http_client=http_client)
        return await stack.enter_async_context(SDKClient(
            transport, read_timeout_seconds=self.config["timeout"]
        ))

    async def _discover_tools(self, client) -> list[dict]:
        tools = []
        cursor = None
        seen_cursors = set()
        seen_names = set()
        while True:
            page = await client.list_tools(cursor=cursor)
            for tool in page.tools:
                if self.config.get("allowed_tools") is not None and tool.name not in self.config["allowed_tools"]:
                    continue
                name = f"mcp__{normalize_mcp_name(self.name)}__{normalize_mcp_name(tool.name)}"
                if name in seen_names or len(name) > 64:
                    raise MCPConfigError("MCP tool names collide or exceed the model's 64-character limit")
                if tool.input_schema.get("type", "object") != "object":
                    raise MCPConfigError("MCP tool input schema must describe an object")
                seen_names.add(name)
                tools.append({"name": tool.name, "description": tool.description or "",
                              "inputSchema": tool.input_schema})
            cursor = page.next_cursor
            if cursor is None:
                return tools
            if cursor in seen_cursors:
                raise MCPConfigError("MCP server repeated a tools/list pagination cursor")
            seen_cursors.add(cursor)

    async def _serve(self):
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._task = asyncio.current_task()
        pending = None
        try:
            if self._ready.cancelled():
                return
            async with AsyncExitStack() as stack:
                async with asyncio.timeout(self.config["connect_timeout"]):
                    client = await self._open(stack)
                    tools = await self._discover_tools(client)
                try:
                    self._ready.set_result(tools)
                except InvalidStateError:
                    return  #初始化完成前用户已经取消，由当前协程正常退出 SDK 上下文。
                while True:
                    request = await self._queue.get()
                    if request is None:
                        break
                    tool_name, arguments, pending = request
                    if pending.cancelled():
                        continue
                    #每次请求有独立 Task。停止某次工具调用，不取消维护 SDK 上下文的 _serve。
                    call = asyncio.create_task(client.call_tool(tool_name, arguments))

                    def cancel_call(future, task=call):
                        if future.cancelled():
                            try:
                                self._loop.call_soon_threadsafe(task.cancel)
                            except RuntimeError:
                                pass

                    pending.add_done_callback(cancel_call)
                    try:
                        async with asyncio.timeout(self.config["timeout"]):
                            result = format_mcp_result(await call)
                    except asyncio.CancelledError:
                        if asyncio.current_task().cancelling():
                            raise  #关闭整个连接仍由原协程退出 SDK 上下文。
                        #仅子 Task 被取消：远程操作可能已生效；不重试，继续服务下一次请求。
                        try:
                            pending.set_result(MCPToolResult("MCP call cancelled; execution may have occurred.", is_error=True))
                        except InvalidStateError:
                            pass  #用户取消时同步 Future 已取消，不再提交迟到的结果。
                        pending = None
                        continue
                    except TimeoutError:
                        result = MCPToolResult("MCP call timed out; execution may have occurred. Do not automatically retry writes.", is_error=True)
                    except Exception as error:
                        #异常原文可能包含 Authorization 或 URL 查询凭据，只返回类型。
                        result = MCPToolResult(f"MCP call failed ({_safe_error(error)}); execution may have occurred.", is_error=True)
                    try:
                        pending.set_result(result)
                    except InvalidStateError:
                        pass  #结果到达的同时用户可能取消 Future，不能因此关闭整个连接。
                    pending = None
        finally:
            #退出或被取消时，唤醒正在同步等待的调用者，不让 Future 一直挂住。
            waiting = [pending] if pending is not None else []
            while not self._queue.empty():
                request = self._queue.get_nowait()
                if request is not None:
                    waiting.append(request[2])
            for future in waiting:
                try:
                    future.set_result(MCPToolResult("MCP connection closed; execution may have occurred.", is_error=True))
                except InvalidStateError:
                    pass

    def _run(self):
        try:
            #每个连接只运行一次 asyncio.run，不为每次工具调用重建事件循环。
            asyncio.run(self._serve())
        except BaseException as error:
            message = f"MCP connection failed ({_safe_error(error)})"
            try:
                self._ready.set_exception(MCPConfigError(message))
            except InvalidStateError:
                pass

    def call_tool(self, tool_name: str, args: dict) -> MCPToolResult:
        check_cancelled()
        #即使绕过模型工具表直接调用，也不能执行白名单之外的服务工具。
        if tool_name not in {tool["name"] for tool in self.tools}:
            return MCPToolResult("MCP tool is unavailable or excluded by allowed_tools", is_error=True)
        future = Future()
        turn = current_turn()
        remove_cancel = turn.add_cancel_callback(future.cancel) if turn else lambda: None
        try:
            check_cancelled()
            if self.closed:
                raise RuntimeError("closed")
            self._loop.call_soon_threadsafe(self._queue.put_nowait, (tool_name, args, future))
            result = future.result(timeout=self.config["timeout"] + 5)
            check_cancelled()
            return result
        except FutureCancelled:
            #中断不是普通工具错误，必须交给回合边界处理，避免模型收到错误后继续重试。
            raise TurnCancelled("MCP call cancelled; execution may have occurred. Do not automatically retry writes.") from None
        except KeyboardInterrupt:
            #原生 Ctrl+C 会先退出 result()；注销回调前必须取消请求，不能等外层再取消 Turn。
            future.cancel()
            raise
        except (FutureTimeout, RuntimeError):
            future.cancel()
            return MCPToolResult("MCP connection unavailable or timed out; execution may have occurred. No automatic retry was made.", is_error=True)
        finally:
            remove_cancel()

    def close(self):
        try:
            if self._loop is not None and self._loop.is_running():
                #先让空闲连接正常退出；执行中的调用则通过取消同一个宿主协程清理。
                self._loop.call_soon_threadsafe(self._queue.put_nowait, None)
                self._thread.join(timeout=2)
                if self._thread.is_alive() and self._loop.is_running():
                    self._loop.call_soon_threadsafe(self._task.cancel)
        except RuntimeError:
            pass  #事件循环恰好在检查与投递之间关闭，仍继续等待线程清理。
        self._thread.join(timeout=3)
        if self._thread.is_alive():
            logger.warning("MCP connection still shutting down: %s", self.name)


#连接和发现结果在进程中共享；角色权限不能写回这里，避免后组装的角色覆盖前一个角色。
mcp_clients: dict[str, MCPClient] = {}


def connect_mcp(name: str) -> str:
    #动态工具会在主循环下一次组装工具池时出现；不允许模型指定任意命令或地址。
    check_cancelled()
    global _catalog_version
    with _mcp_lock:
        check_cancelled()
        configs = get_mcp_config()
        if name not in configs:
            return MCPToolResult(f"Unknown MCP server. Configured: {', '.join(configs) or '(none)'}", is_error=True)
        if name in mcp_clients and not mcp_clients[name].closed:
            return f"MCP server '{name}' already connected"
        try:
            server = MCPClient(name, configs[name])
        except (MCPConfigError, FutureTimeout) as error:
            return MCPToolResult(str(error) or "MCP connection timed out", is_error=True)
        mcp_clients[name] = server
        #工具池按版本缓存；连接成功使旧缓存失效，主循环下次组装时才会发现新工具。
        _catalog_version += 1
        names = ", ".join(tool["name"] for tool in server.tools)
        logger.info("MCP connected: %s -> %s", name, names)
        return f"Connected to MCP server '{name}'. Discovered {len(server.tools)} tools: {names}"


def mcp_catalog_version() -> int:
    """工具池缓存的失效依据；连接或关闭服务都会推进版本。"""
    return _catalog_version


def run_connect_mcp(name: str) -> str:
    return connect_mcp(name)


def _tool_handler(client, tool_name):
    #闭包固定路由；参数名叫 client、tool 的外部工具也不能覆盖宿主连接。
    def call(**arguments):
        return client.call_tool(tool_name, arguments)
    return call


def requires_mcp_launch_confirmation(name: str) -> bool:
    #启动 stdio 等于在宿主执行外部程序，连接阶段也必须获得用户授权。
    return get_mcp_config().get(name, {}).get("type") == "stdio"


def tool_catalog() -> dict[str, dict]:
    """返回连接目录的独立元数据快照；不包含某个角色临时过滤后的授权结果。"""
    catalog = {}
    with _mcp_lock:
        for server_name, server in mcp_clients.items():
            if server.closed:
                continue
            for tool in server.tools:
                raw_name = tool["name"]
                name = f"mcp__{normalize_mcp_name(server_name)}__{normalize_mcp_name(raw_name)}"
                if name in catalog:
                    raise MCPConfigError("MCP tool names collide after normalization")
                #原始工具名由宿主分别分类：只读不等于免确认，免确认也不等于只读。
                #不读取服务 annotations/readOnlyHint，也不从名称或描述猜测是否有副作用。
                catalog[name] = {
                    "server": server_name,
                    "raw_name": raw_name,
                    "read_only": raw_name in server.config.get("read_only_tools", []),
                    "policy": "allow" if raw_name in server.config.get("auto_approve", []) else "confirm",
                    "available": True,
                }
    return catalog


def get_mcp_tool_metadata(tool_name: str) -> dict:
    """供工具执行层二次检查；未发现或已断开的工具不能取得只读资格。"""
    return tool_catalog().get(tool_name, {
        "server": "", "raw_name": "", "read_only": False,
        "policy": "confirm", "available": False,
    })


#连接工具始终可见；其它 MCP 工具只有连接成功后才加入模型的工具列表。
CONNECT_TOOL = {
    "name": "connect_mcp",
    "description": "Connect to a host-configured MCP server and discover its tools.",
    "input_schema": {
        "type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"],
    },
}


#基础工具由主循环传入；这里只添加连接工具和已发现的 MCP 工具。
def assemble_tool_pool(base_tools: list[dict], base_handlers: dict) -> tuple[list[dict], dict]:
    """Combine built-in tools with every connected server tool."""
    #复制容器，不修改主循环里原有的静态工具表。
    connection_tool = {**CONNECT_TOOL, "description": CONNECT_TOOL["description"] +
                       " Available servers: " + (", ".join(get_mcp_config()) or "(none; configure --mcp-config first)")}
    tools = [*base_tools, connection_tool]
    handlers = {**base_handlers, "connect_mcp": run_connect_mcp}
    origins = {tool["name"] for tool in tools}
    with _mcp_lock:
        servers = list(mcp_clients.items())
    for server_name, server in servers:
        if server.closed:
            continue
        safe_server = normalize_mcp_name(server_name)
        for tool_def in server.tools:
            raw_name = tool_def["name"]
            prefixed = f"mcp__{safe_server}__{normalize_mcp_name(raw_name)}"
            if prefixed in origins:
                raise MCPConfigError("MCP tool name collides with another registered tool")
            origins.add(prefixed)
            tools.append({"name": prefixed, "description": tool_def.get("description", ""),
                          "input_schema": tool_def["inputSchema"]})
            #固定当前 server 和工具名，避免循环中的函数指向最后一项。
            handlers[prefixed] = _tool_handler(server, raw_name)
    #这里只组装工具，不改变共享元数据。每个角色可以过滤自己的 tools/handlers，
    #实际调用时仍由 ToolRuntime 按当前角色和运行模式再次检查，不能靠隐藏工具代替权限。
    return tools, handlers


def close_mcp_clients():
    """退出时清理本进程启动的连接；不停止用户独立运行的远程服务。"""
    global _catalog_version
    with _mcp_lock:
        clients = list(mcp_clients.values())
        had_clients = bool(mcp_clients)
        mcp_clients.clear()
        if had_clients:
            _catalog_version += 1
    for client in clients:
        client.close()


atexit.register(close_mcp_clients)
