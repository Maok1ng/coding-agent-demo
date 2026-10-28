"""MCP 配置、权限、真实 stdio/HTTP、错误状态和退出清理回归测试。"""

import asyncio
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import AsyncMock, patch

import uvicorn
from mcp.types import CallToolResult, ImageContent, ListToolsResult, TextContent, Tool

from coding_agent.integrations import mcp
from coding_agent.runtime.turn import TurnCancelled, TurnContext, turn_context
from tests.fixtures.mcp_server import create_server


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "mcp_server.py"


class MCPTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.path = self.root / "mcp.json"
        self.addCleanup(mcp.close_mcp_clients)

    def configs(self, **servers):
        self.path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
        return mcp.load_mcp_config(self.path)

    def stdio_config(self, **overrides):
        return {"type": "stdio", "command": sys.executable, "args": [str(FIXTURE)],
                "cwd": ".", "connect_timeout": 10, "timeout": 5, **overrides}

    def test_missing_config_is_optional_and_invalid_config_is_rejected(self):
        self.assertEqual(mcp.load_mcp_config(self.path), {})
        for value in ([], {}, {"mcpServers": []},
                      {"mcpServers": {"test": {"type": "sse", "url": "https://example.invalid"}}},
                      {"mcpServers": {"test": self.stdio_config(timeout=True)}},
                      {"mcpServers": {"test": self.stdio_config(timeout=float("nan"))}},
                      {"mcpServers": {"test": self.stdio_config(auto_approve="*")}},
                      {"mcpServers": {"test": self.stdio_config(auto_approve=None)}},
                      {"mcpServers": {"test": self.stdio_config(read_only_tools="echo")}},
                      {"mcpServers": {"test": self.stdio_config(read_only_tools=None)}},
                      {"mcpServers": {"test": self.stdio_config(read_only_tools=[""])}},
                      {"mcpServers": {"test": self.stdio_config(args=None)}},
                      {"mcpServers": {"test": self.stdio_config(headers={"Authorization": 123})}},
                      {"mcpServers": {"a.b": self.stdio_config(), "a_b": self.stdio_config()}}):
            self.path.write_text(json.dumps(value), encoding="utf-8")
            with self.subTest(value=value), self.assertRaises(mcp.MCPConfigError):
                mcp.load_mcp_config(self.path)

    def test_real_stdio_discovery_calls_reuse_and_process_cleanup(self):
        configs = self.configs(local=self.stdio_config(env={"MCP_TEST_EXPLICIT": "${MCP_TEST_VALUE}"}))
        with patch.dict(os.environ, {"MCP_TEST_VALUE": "passed", "MCP_TEST_HOST_SECRET": "not-inherited"}):
            client = mcp.MCPClient("local", configs["local"])
        self.addCleanup(client.close)
        self.assertEqual({tool["name"] for tool in client.tools}, {"echo", "fail", "slow", "named_args"})
        first = json.loads(client.call_tool("echo", {"text": "你好"}))
        second = json.loads(client.call_tool("echo", {"text": "again"}))
        self.assertNotEqual(first["pid"], os.getpid())
        self.assertEqual(first["pid"], second["pid"])
        self.assertEqual(first["cwd"], str(self.root))
        self.assertEqual(first["explicit_env"], "passed")
        self.assertFalse(first["host_secret_leaked"])
        self.assertTrue(client.call_tool("fail", {}).is_error)
        self.assertFalse(client.call_tool("echo", {"text": "still connected"}).is_error)
        client.close()
        self.assertTrue(client.closed)
        with self.assertRaises(ProcessLookupError):
            os.kill(first["pid"], 0)
        self.assertTrue(client.call_tool("echo", {"text": "closed"}).is_error)

    def test_allowed_tools_routing_and_host_owned_permissions(self):
        configs = self.configs(local=self.stdio_config(
            allowed_tools=["echo", "named_args"], auto_approve=["echo"]
        ))
        with patch.object(mcp, "get_mcp_config", return_value=configs):
            before, _ = mcp.assemble_tool_pool([], {})
            self.assertEqual([tool["name"] for tool in before], ["connect_mcp"])
            self.assertIn("local", before[0]["description"])
            self.assertIn("Connected", mcp.connect_mcp("local"))
            self.assertIn("already connected", mcp.connect_mcp("local"))
            self.assertTrue(mcp.connect_mcp("arbitrary-command").is_error)
            tools, handlers = mcp.assemble_tool_pool([], {})
            self.assertEqual({tool["name"] for tool in tools}, {
                "connect_mcp", "mcp__local__echo", "mcp__local__named_args"
            })
            self.assertIn("input/value", handlers["mcp__local__named_args"](client="input", tool="value"))
            self.assertEqual(mcp.get_mcp_tool_metadata("mcp__local__echo")["policy"], "allow")
            self.assertEqual(mcp.get_mcp_tool_metadata("mcp__local__named_args")["policy"], "confirm")
            self.assertTrue(mcp.mcp_clients["local"].call_tool("fail", {}).is_error)
            #连接程序的许可与发现后的工具许可分开，read_only_tools/auto_approve 都不能免除前者。
            self.assertTrue(mcp.requires_mcp_launch_confirmation("local"))

    def test_read_only_classification_and_auto_approval_are_independent(self):
        #只用发现目录做分类回归，不需要启动外部进程或依赖服务端的只读声明。
        client = types.SimpleNamespace(
            closed=False,
            config={"read_only_tools": ["search"], "auto_approve": ["write"]},
            tools=[
                {"name": "search", "description": "", "inputSchema": {"type": "object"}},
                {"name": "write", "description": "", "inputSchema": {"type": "object"},
                 "annotations": {"readOnlyHint": True}},
            ],
        )
        with patch.dict(mcp.mcp_clients, {"local": client}, clear=True):
            self.assertEqual(mcp.get_mcp_tool_metadata("mcp__local__search"), {
                "server": "local", "raw_name": "search", "read_only": True,
                "policy": "confirm", "available": True,
            })
            self.assertFalse(mcp.get_mcp_tool_metadata("mcp__local__write")["read_only"])
            self.assertEqual(mcp.get_mcp_tool_metadata("mcp__local__write")["policy"], "allow")
            unknown = mcp.get_mcp_tool_metadata("mcp__local__unknown")
            self.assertFalse(unknown["read_only"])
            self.assertFalse(unknown["available"])
            self.assertEqual(unknown["policy"], "confirm")
            client.closed = True
            self.assertFalse(mcp.get_mcp_tool_metadata("mcp__local__search")["available"])

    def test_role_pool_assembly_does_not_replace_shared_metadata(self):
        client = types.SimpleNamespace(
            closed=False, config={"read_only_tools": ["search"]},
            tools=[{"name": "search", "description": "", "inputSchema": {"type": "object"}}],
        )
        with patch.dict(mcp.mcp_clients, {"local": client}, clear=True), \
                patch.object(mcp, "get_mcp_config", return_value={}):
            before = mcp.tool_catalog()
            self.assertTrue(before["mcp__local__search"]["read_only"])
            first_tools, first_handlers = mcp.assemble_tool_pool([], {})
            #模拟某个角色只保留本地工具，再为另一个角色组装；两者不能改写共享分类。
            first_tools.clear()
            first_handlers.clear()
            mcp.assemble_tool_pool([{"name": "read_file"}], {"read_file": lambda: ""})
            self.assertEqual(mcp.tool_catalog(), before)
            #调用方拿到的是独立字典，不能通过修改快照提升下一次调用的权限。
            before["mcp__local__search"]["read_only"] = False
            self.assertTrue(mcp.get_mcp_tool_metadata("mcp__local__search")["read_only"])

    def test_error_flag_survives_shared_tool_runtime(self):
        from coding_agent.runtime.context import RunContext
        from coding_agent.runtime.tools import ToolRuntime
        from coding_agent.runtime import workspace

        result = mcp.MCPToolResult("failed", is_error=True)
        block = types.SimpleNamespace(name="mcp__local__fail", input={}, id="test")
        client = types.SimpleNamespace(
            closed=False, config={"auto_approve": ["fail"]}, call_tool=lambda *args: result,
            tools=[{"name": "fail", "description": "", "inputSchema": {"type": "object"}}],
        )
        context = RunContext(workspace=self.root, turn=TurnContext(), role="implementation")
        io = types.SimpleNamespace(confirm=lambda *args: True, emit=lambda event: None)
        with patch.dict(mcp.mcp_clients, {"local": client}, clear=True), \
                patch.object(mcp, "get_mcp_config", return_value={}), \
                patch.object(workspace, "WORKDIR", self.root):
            output = ToolRuntime(context, io).execute(block)
        self.assertIn("failed", output)
        self.assertTrue(output.is_error)

    def test_missing_token_and_failed_connection_do_not_leak_secrets(self):
        configs = self.configs(github={"url": "https://example.invalid/mcp", "headers": {
            "Authorization": "Bearer ${MCP_TEST_MISSING_TOKEN}"
        }})
        with patch.dict(os.environ, {}, clear=True), patch.object(mcp, "get_mcp_config", return_value=configs):
            output = mcp.connect_mcp("github")
        self.assertTrue(output.is_error)
        self.assertIn("MCP_TEST_MISSING_TOKEN", output)
        self.assertNotIn("github", mcp.mcp_clients)
        with patch.object(mcp.MCPClient, "_open", new=AsyncMock(side_effect=RuntimeError("Bearer SECRET"))):
            with self.assertRaises(mcp.MCPConfigError) as caught:
                mcp.MCPClient("github", configs["github"])
        self.assertNotIn("SECRET", str(caught.exception))

    def test_timeout_does_not_retry_and_connection_can_close(self):
        configs = self.configs(local=self.stdio_config(timeout=0.1))
        client = mcp.MCPClient("local", configs["local"])
        self.addCleanup(client.close)
        result = client.call_tool("slow", {"seconds": 3})
        self.assertTrue(result.is_error)
        self.assertIn("may have occurred", result)
        client.close()
        self.assertTrue(client.closed)

    def test_close_cancels_in_flight_call_and_cleans_up_process(self):
        configs = self.configs(local=self.stdio_config(timeout=30))
        client = mcp.MCPClient("local", configs["local"])
        self.addCleanup(client.close)
        pid = json.loads(client.call_tool("echo", {"text": "pid"}))["pid"]
        results = []
        caller = threading.Thread(target=lambda: results.append(client.call_tool("slow", {"seconds": 30})))
        caller.start()
        client.close()
        caller.join(timeout=5)
        self.assertFalse(caller.is_alive())
        self.assertTrue(client.closed)
        self.assertTrue(results[0].is_error)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_cancel_turn_stops_only_its_call_and_reuses_stdio_connection(self):
        configs = self.configs(local=self.stdio_config(timeout=30))
        client = mcp.MCPClient("local", configs["local"])
        self.addCleanup(client.close)
        pid = json.loads(client.call_tool("echo", {"text": "pid"}))["pid"]
        turn = TurnContext()
        started = threading.Event()
        results = []

        def call():
            with turn_context(turn):
                started.set()
                try:
                    client.call_tool("slow", {"seconds": 30})
                except TurnCancelled as error:
                    results.append(error)

        caller = threading.Thread(target=call)
        caller.start()
        self.assertTrue(started.wait(timeout=2))
        #让真实 stdio 请求进入服务端；随后取消的是这个请求，而非 SDK 连接协程。
        time.sleep(0.2)
        turn.cancel()
        caller.join(timeout=3)
        self.assertFalse(caller.is_alive())
        self.assertEqual(len(results), 1)
        self.assertIsInstance(results[0], TurnCancelled)
        self.assertIn("may have occurred", str(results[0]))
        self.assertFalse(client.closed)
        result = json.loads(client.call_tool("echo", {"text": "new turn"}))
        self.assertEqual(result["pid"], pid)
        self.assertEqual(result["text"], "new turn")

    def test_cancel_connect_cleans_only_initializing_connection(self):
        configs = self.configs(local=self.stdio_config(timeout=30))
        existing = mcp.MCPClient("existing", configs["local"])
        self.addCleanup(existing.close)
        turn = TurnContext()
        started = threading.Event()
        cleaned = threading.Event()
        clients = []
        results = []

        async def slow_open(client, stack):
            clients.append(client)
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        def connect():
            with turn_context(turn):
                try:
                    mcp.MCPClient("initializing", configs["local"])
                except TurnCancelled as error:
                    results.append(error)

        with patch.object(mcp.MCPClient, "_open", new=slow_open):
            caller = threading.Thread(target=connect)
            caller.start()
            self.assertTrue(started.wait(timeout=2))
            turn.cancel()
            caller.join(timeout=3)
        self.assertFalse(caller.is_alive())
        self.assertTrue(cleaned.is_set())
        self.assertTrue(clients[0].closed)
        self.assertIsInstance(results[0], TurnCancelled)
        self.assertIn("still connected", existing.call_tool("echo", {"text": "still connected"}))

    def test_cancelled_turn_cannot_start_another_mcp_call(self):
        configs = self.configs(local=self.stdio_config())
        client = mcp.MCPClient("local", configs["local"])
        self.addCleanup(client.close)
        turn = TurnContext()
        with turn_context(turn):
            turn.cancel()
            with patch.object(client._loop, "call_soon_threadsafe", side_effect=AssertionError("must not enqueue")):
                with self.assertRaises(TurnCancelled):
                    client.call_tool("echo", {"text": "must not run"})

    def test_native_keyboard_interrupt_cancels_request_before_callback_removal(self):
        started = threading.Event()
        stopped = threading.Event()
        futures = []
        calls = []

        async def call_tool(name, arguments):
            calls.append(name)
            if name == "slow":
                started.set()
                try:
                    await asyncio.sleep(30)
                finally:
                    stopped.set()
            return CallToolResult(content=[TextContent(type="text", text="still connected")])

        #只模拟 Future.result() 收到原生 Ctrl+C；_serve 队列和独立 Task 仍真实运行。
        class InterruptedFuture(mcp.Future):
            def __init__(self):
                super().__init__()
                futures.append(self)

            def result(self, timeout=None):
                if not started.wait(timeout=2):
                    raise AssertionError("tool request did not start")
                raise KeyboardInterrupt

        sdk = types.SimpleNamespace(
            call_tool=call_tool,
            list_tools=AsyncMock(return_value=ListToolsResult(tools=[
                Tool(name="slow", input_schema={"type": "object"}),
                Tool(name="echo", input_schema={"type": "object"}),
            ])),
        )
        configs = self.configs(local=self.stdio_config(timeout=30))
        turn = TurnContext()
        with patch.object(mcp.MCPClient, "_open", new=AsyncMock(return_value=sdk)) as open_client:
            client = mcp.MCPClient("local", configs["local"])
            self.addCleanup(client.close)
            with turn_context(turn), patch.object(mcp, "Future", InterruptedFuture):
                with self.assertRaises(KeyboardInterrupt) as caught:
                    client.call_tool("slow", {})
            self.assertIs(type(caught.exception), KeyboardInterrupt)
            self.assertFalse(turn.cancelled.is_set())
            self.assertEqual(turn._callbacks, [])
            self.assertTrue(futures[0].cancelled())
            self.assertTrue(stopped.wait(timeout=2))
            self.assertFalse(client.closed)
            self.assertIn("still connected", client.call_tool("echo", {}))
            open_client.assert_awaited_once()
        self.assertEqual(calls, ["slow", "echo"])

    def test_bad_http_address_is_rejected_before_network_access(self):
        for url in ("http://example.invalid/mcp", "https://user:secret@example.invalid/mcp"):
            configs = self.configs(remote={"type": "http", "url": url})
            with patch("httpx2.AsyncClient", side_effect=AssertionError("must not connect")):
                with self.assertRaises(mcp.MCPConfigError) as caught:
                    mcp.MCPClient("remote", configs["remote"])
            self.assertNotIn("secret", str(caught.exception))

    def test_explicit_config_path_must_exist(self):
        mcp.get_mcp_config.cache_clear()
        self.addCleanup(mcp.get_mcp_config.cache_clear)
        with patch.dict(os.environ, {"AGENT_MCP_CONFIG": str(self.path)}):
            with self.assertRaises(mcp.MCPConfigError):
                mcp.get_mcp_config()

    def test_text_structured_and_invalid_image_results(self):
        result = mcp.format_mcp_result(CallToolResult(
            content=[TextContent(type="text", text="found"),
                     ImageContent(type="image", data="eA==", mime_type="image/png")],
            structured_content={"count": 1}, is_error=True,
        ))
        self.assertTrue(result.is_error)
        self.assertIn("found", result)
        self.assertIn('"count": 1', result)
        self.assertIn("image rejected", result)
        self.assertNotIn("eA==", result)
        self.assertIsNone(result.content_blocks)

    def test_valid_image_keeps_mixed_content_order_without_base64_in_text_preview(self):
        from tests.fixtures.mcp_browser_server import PNG
        result = mcp.format_mcp_result(CallToolResult(content=[
            TextContent(type="text", text="before"),
            ImageContent(type="image", data=PNG, mime_type="image/png"),
            TextContent(type="text", text="after"),
        ], structured_content={"page": "fixture"}))
        self.assertFalse(result.is_error)
        self.assertEqual([block["type"] for block in result.content_blocks],
                         ["text", "text", "image", "text", "text"])
        self.assertEqual(result.content_blocks[0]["text"], "before")
        self.assertEqual(result.content_blocks[3]["text"], "after")
        self.assertEqual(result.content_blocks[2]["source"]["type"], "base64")
        self.assertNotIn(PNG, str(result))

    def test_image_limit_does_not_decode_additional_images(self):
        image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "opaque"}}
        with patch("coding_agent.runtime.media.normalize_image", return_value=image) as normalize:
            result = mcp.format_mcp_result(CallToolResult(content=[
                ImageContent(type="image", data="opaque", mime_type="image/png") for _ in range(7)
            ]))
        self.assertEqual(normalize.call_count, 4)
        self.assertEqual(sum(block["type"] == "image" for block in result.content_blocks), 4)
        self.assertTrue(result.is_error)
        self.assertIn("at most 4", result)

    def test_plain_text_result_keeps_existing_string_interface(self):
        result = mcp.format_mcp_result(CallToolResult(content=[TextContent(type="text", text="plain")]))
        self.assertEqual(result, "plain")
        self.assertIsNone(result.content_blocks)

    def test_discovery_handles_pagination_and_rejects_name_collisions(self):
        client = object.__new__(mcp.MCPClient)
        client.name, client.config = "local", {}
        tool = lambda name: Tool(name=name, input_schema={"type": "object"})
        sdk = types.SimpleNamespace(list_tools=AsyncMock(side_effect=[
            ListToolsResult(tools=[tool("first")], next_cursor="page2"),
            ListToolsResult(tools=[tool("second")]),
        ]))
        self.assertEqual([item["name"] for item in asyncio.run(client._discover_tools(sdk))], ["first", "second"])
        sdk.list_tools.assert_any_await(cursor="page2")
        sdk.list_tools = AsyncMock(return_value=ListToolsResult(tools=[tool("a.b"), tool("a_b")]))
        with self.assertRaises(mcp.MCPConfigError):
            asyncio.run(client._discover_tools(sdk))

    def test_real_http_transport_with_authentication_header(self):
        #绑定端口 0 让系统分配空闲端口；传入同一 socket，避免先查端口再启动的竞态。
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        self.addCleanup(listener.close)
        port = listener.getsockname()[1]
        app = create_server().streamable_http_app()
        seen_headers = []

        async def authenticated(scope, receive, send):
            if scope["type"] == "http":
                headers = dict(scope["headers"])
                seen_headers.append(headers.get(b"authorization"))
                if headers.get(b"authorization") != b"Bearer local-test-token":
                    await send({"type": "http.response.start", "status": 401, "headers": []})
                    await send({"type": "http.response.body", "body": b"unauthorized"})
                    return
            await app(scope, receive, send)

        server = uvicorn.Server(uvicorn.Config(authenticated, log_level="error", lifespan="on"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()

        def stop_server():
            server.should_exit = True
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.addCleanup(stop_server)
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(server.started)
        configs = self.configs(remote={"type": "http", "url": f"http://127.0.0.1:{port}/mcp",
                                       "headers": {"Authorization": "Bearer ${MCP_TEST_TOKEN}"},
                                       "connect_timeout": 5, "timeout": 5})
        with patch.dict(os.environ, {"MCP_TEST_TOKEN": "wrong-test-token"}):
            with self.assertRaises(mcp.MCPConfigError) as caught:
                mcp.MCPClient("remote", configs["remote"])
        #SDK 有时将 HTTP 失败转换成无 response 属性的 MCPError，只保证安全报错。
        self.assertIn("MCP connection failed", str(caught.exception))
        self.assertNotIn("wrong-test-token", str(caught.exception))
        seen_headers.clear()
        with patch.dict(os.environ, {"MCP_TEST_TOKEN": "local-test-token"}):
            client = mcp.MCPClient("remote", configs["remote"])
        self.addCleanup(client.close)
        self.assertIn("http works", client.call_tool("echo", {"text": "http works"}))
        self.assertTrue(seen_headers)
        self.assertEqual(set(seen_headers), {b"Bearer local-test-token"})
        client.close()
        self.assertTrue(client.closed)


if __name__ == "__main__":
    unittest.main()
