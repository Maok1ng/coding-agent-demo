"""用官方 SDK 启动真实 MCP 测试服务；不是产品中的模拟客户端。"""

import asyncio
import os
from pathlib import Path

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError


def create_server():
    server = MCPServer("integration-test")

    @server.tool()
    def echo(text: str) -> dict:
        """返回参数、进程和目录，用于确认发生了真实的跨进程调用。"""
        return {"text": text, "pid": os.getpid(), "cwd": str(Path.cwd()),
                "explicit_env": os.getenv("MCP_TEST_EXPLICIT"),
                "host_secret_leaked": "MCP_TEST_HOST_SECRET" in os.environ}

    @server.tool()
    def fail() -> str:
        """检查 is_error 能从服务端一路返回主循环。"""
        raise ToolError("fixture failure")

    @server.tool()
    async def slow(seconds: float) -> str:
        """检查客户端超时与关闭，不需要访问任何公网地址。"""
        await asyncio.sleep(seconds)
        return "finished"

    @server.tool()
    def named_args(client: str, tool: str) -> str:
        """这些参数名不能覆盖客户端工具路由中的变量。"""
        return f"{client}/{tool}"

    return server


if __name__ == "__main__":
    create_server().run()
