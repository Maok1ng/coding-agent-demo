"""真实 MCP stdio 测试服务；只检查浏览器形状工具的接入，不启动或模拟产品浏览器。"""

from mcp.server import MCPServer
from mcp.types import CallToolResult, ImageContent, TextContent


#固定的 1×1 PNG；仅用作真实 SDK 内容传输样本，不代表真实页面截图。
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"


def create_server():
    server = MCPServer("browser-integration-test")

    @server.tool()
    def take_snapshot() -> str:
        """返回固定验收样本，检查真实 SDK 传输和后续工具参数。"""
        return 'uid=1 button "Save"\nuid=2 textbox "Name"'

    @server.tool()
    def take_screenshot(format: str = "png") -> CallToolResult:
        """检查图片协议、失败传播和后续恢复；不打开真实浏览器。"""
        data = "not-valid-base64" if format == "invalid" else PNG
        return CallToolResult(content=[
            TextContent(type="text", text="Fixture screenshot; not a real browser capture."),
            ImageContent(type="image", data=data, mime_type="image/png"),
        ])

    @server.tool()
    def click(uid: str) -> dict:
        """让测试核实模型参数确实传入了服务进程。"""
        return {"clicked": uid, "status": "completed"}

    @server.tool()
    def list_console_messages() -> list[dict]:
        """保留实际错误文字；错误日志不能当成浏览器测试通过。"""
        return [{"type": "error", "text": "Fixture: failed to load application data"}]

    @server.tool()
    def evaluate_script(function: str) -> str:
        """服务可能有的危险工具不应穿过宿主白名单。"""
        return "should never be exposed: " + function

    return server


if __name__ == "__main__":
    create_server().run()
