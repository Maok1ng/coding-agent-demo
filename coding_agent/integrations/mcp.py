import re


#这里用进程内模拟 server 演示 tools/list 和 tools/call，不负责网络连接。
class MCPClient:
    """Small in-process stand-in for MCP tools/list and tools/call."""

    def __init__(self, name: str):
        self.name = name
        #tools 给模型展示工具定义，_handlers 保存实际执行这些工具的函数。
        self.tools: list[dict] = []
        self._handlers: dict[str, callable] = {}

    def register(self, tool_defs: list[dict], handlers: dict[str, callable]):
        #先检查名字是否有效、是否重复，再确认每个定义都有对应 handler。
        names = [tool.get("name") for tool in tool_defs]
        if any(not isinstance(name, str) or not name for name in names):
            raise ValueError("Every MCP tool needs a non-empty name")
        if len(set(names)) != len(names):
            raise ValueError(f"Duplicate MCP tool name on server {self.name!r}")
        missing = [name for name in names if name not in handlers]
        if missing:
            raise ValueError(f"Missing MCP handlers: {', '.join(missing)}")
        self.tools = list(tool_defs)
        self._handlers = dict(handlers)

    def call_tool(self, tool_name: str, args: dict) -> str:
        #**args 把模型传来的参数字典展开后交给对应函数。
        handler = self._handlers.get(tool_name)
        if not handler:
            return f"MCP error: unknown tool '{tool_name}'"
        try:
            return str(handler(**args))
        except Exception as exc:
            return f"MCP error: {type(exc).__name__}: {exc}"


#已连接服务和它们的权限策略在同一进程中共享；尚未连接时模型看不到服务工具。
mcp_clients: dict[str, MCPClient] = {}
mcp_tool_policies: dict[str, str] = {}
_DISALLOWED_CHARS = re.compile(r"[^a-zA-Z0-9_-]")

# Authorization comes from host configuration, never server descriptions.
#只允许宿主在这里决定哪些工具可直接执行；未列出的 MCP 工具默认需要确认。
MCP_HOST_POLICY = {
    ("docs", "search"): "allow",
    ("docs", "get_version"): "allow",
    ("deploy", "status"): "allow",
    ("deploy", "trigger"): "confirm",
}


def normalize_mcp_name(name: str) -> str:
    """Replace characters outside the model tool-name alphabet."""
    #正则把不适合模型工具名的字符替换成下划线。
    normalized = _DISALLOWED_CHARS.sub("_", name)
    if not normalized:
        raise ValueError("MCP names cannot normalize to an empty string")
    return normalized


#模拟 docs server：inputSchema 是服务端提供的参数格式，annotations 仅作描述。
def _mock_server_docs() -> MCPClient:
    server = MCPClient("docs")
    server.register(
        tool_defs=[
            {
                "name": "search",
                "description": "Search the documentation.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
                "annotations": {"readOnlyHint": True},
            },
            {
                "name": "get_version",
                "description": "Get the documentation API version.",
                "inputSchema": {"type": "object", "properties": {}},
                "annotations": {"readOnlyHint": True},
            },
        ],
        handlers={
            "search": lambda query: f"[docs] Found 3 results for '{query}'",
            "get_version": lambda: "[docs] API v2.1.0",
        },
    )
    return server


#模拟 deploy server：真正是否确认执行，取决于上面的 MCP_HOST_POLICY。
def _mock_server_deploy() -> MCPClient:
    server = MCPClient("deploy")
    server.register(
        tool_defs=[
            {
                "name": "trigger",
                "description": "Trigger a deployment.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"service": {"type": "string"}},
                    "required": ["service"],
                },
                "annotations": {"destructiveHint": True},
            },
            {
                "name": "status",
                "description": "Check deployment status.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"service": {"type": "string"}},
                    "required": ["service"],
                },
                "annotations": {"readOnlyHint": True},
            },
        ],
        handlers={
            "trigger": lambda service: f"[deploy] Triggered: {service}",
            "status": lambda service: f"[deploy] {service}: running (v1.4.2)",
        },
    )
    return server


MOCK_SERVERS = {
    "docs": _mock_server_docs,
    "deploy": _mock_server_deploy,
}


def connect_mcp(name: str) -> str:
    #连接只登记模拟 server；动态工具会在主循环下一次组装工具池时出现。
    if name in mcp_clients:
        return f"MCP server '{name}' already connected"
    factory = MOCK_SERVERS.get(name)
    if not factory:
        return f"Unknown server '{name}'. Available: {', '.join(MOCK_SERVERS)}"
    server = factory()
    mcp_clients[name] = server
    names = ", ".join(tool["name"] for tool in server.tools)
    print(f"  [mcp] connected: {name} -> {names}")
    return (
        f"Connected to MCP server '{name}'. "
        f"Discovered {len(server.tools)} tools: {names}"
    )


def run_connect_mcp(name: str) -> str:
    return connect_mcp(name)


#连接工具始终可见；其它 MCP 工具只有连接成功后才加入模型的工具列表。
CONNECT_TOOL = {
    "name": "connect_mcp",
    "description": "Connect to an MCP server and discover its tools.",
    "input_schema": {
        "type": "object",
        "properties": {"name": {"type": "string", "enum": ["docs", "deploy"]}},
        "required": ["name"],
    },
}

#基础工具由 main.py 传入；这里只添加连接工具和已发现的 MCP 工具。
def assemble_tool_pool(base_tools: list[dict], base_handlers: dict) -> tuple[list[dict], dict]:
    """Combine built-in tools with every connected server tool."""
    global mcp_tool_policies
    #复制容器，不修改主循环里原有的静态工具表。
    tools = [*base_tools, CONNECT_TOOL]
    handlers = {**base_handlers, "connect_mcp": run_connect_mcp}
    policies: dict[str, str] = {}
    origins = {
        tool["name"]: f"built-in tool {tool['name']!r}"
        for tool in tools
    }

    #每个连接的 server 都把原始工具名转换成 mcp__服务名__工具名。
    for server_name, server in mcp_clients.items():
        safe_server = normalize_mcp_name(server_name)
        for tool_def in server.tools:
            raw_name = tool_def["name"]
            safe_tool = normalize_mcp_name(raw_name)
            prefixed = f"mcp__{safe_server}__{safe_tool}"
            if len(prefixed) > 64:
                raise ValueError(f"MCP tool name is longer than 64 characters: {prefixed}")
            origin = f"MCP tool {server_name!r}/{raw_name!r}"
            if prefixed in origins:
                #规范化可能让不同原名变成同一个工具名，冲突时直接报错。
                raise ValueError(
                    "MCP tool name collision after normalization: "
                    f"{prefixed!r} maps both {origins[prefixed]} and {origin}"
                )
            schema = tool_def.get("inputSchema", {})
            #Anthropic 工具输入必须是 object；保留服务端原来的字段定义。
            if not isinstance(schema, dict) or schema.get("type", "object") != "object":
                raise ValueError(f"Invalid input schema for {origin}")
            origins[prefixed] = origin
            tools.append({
                "name": prefixed,
                "description": tool_def.get("description", ""),
                "input_schema": schema,
            })
            #默认参数固定当前 server 和工具名，避免循环中的 lambda 指向最后一项。
            handlers[prefixed] = (
                lambda *, client=server, tool=raw_name, **kwargs:
                client.call_tool(tool, kwargs)
            )
            #权限以宿主配置为准；server 提供的 annotations 不直接授权。
            policies[prefixed] = MCP_HOST_POLICY.get(
                (server_name, raw_name), "confirm"
            )

    mcp_tool_policies = policies
    return tools, handlers


#权限 hook 每次通过函数读取最新策略，避免持有旧的字典引用。
def get_mcp_policy(tool_name: str) -> str:
    return mcp_tool_policies.get(tool_name, "confirm")


#把已连接服务写进主 Agent 的提示词；不重复创建另一份基础提示词。
def assemble_system_prompt(base_system: str) -> str:
    if not mcp_clients:
        return base_system
    return base_system + "\n\nConnected MCP servers: " + ", ".join(mcp_clients)
