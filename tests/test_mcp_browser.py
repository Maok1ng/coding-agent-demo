"""浏览器 MCP 的真实 stdio 接入和宿主权限；不访问网络或安装浏览器依赖。"""

import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from coding_agent.integrations import mcp
from coding_agent.runtime import workspace
from coding_agent.runtime.context import RunContext
from coding_agent.runtime.tools import ToolRuntime
from coding_agent.runtime.turn import TurnContext, turn_context


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "mcp_browser_server.py"
EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "mcp.browser.example.json"


class BrowserMCPTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.io = SimpleNamespace(emit=Mock(), confirm=Mock(return_value=True))
        self.config = mcp.load_mcp_config(EXAMPLE)["browser"]
        #仅在测试中替换为官方 SDK fixture；产品配置仍指向真实 Chrome DevTools MCP。
        self.config.update(command=sys.executable, args=[str(FIXTURE)],
                           connect_timeout=10, timeout=5, env={})
        patcher = patch.object(mcp, "get_mcp_config", return_value={"browser": self.config})
        patcher.start()
        self.addCleanup(patcher.stop)
        active_workspace = patch.object(workspace, "WORKDIR", self.root)
        active_workspace.start()
        self.addCleanup(active_workspace.stop)
        self.addCleanup(mcp.close_mcp_clients)

    def runtime(self, *, role="implementation", mode="execute", approval="auto"):
        context = RunContext(self.root, TurnContext(), role=role, mode=mode,
                             approval_mode=approval)
        return ToolRuntime(context, self.io)

    @staticmethod
    def execute(runtime, tool_name, **arguments):
        with turn_context(runtime.context.turn):
            return runtime.execute(SimpleNamespace(name=tool_name, input=arguments, id=tool_name))

    def test_browser_connect_uses_launch_confirmation_and_rejected_launch_never_starts(self):
        runtime = self.runtime(role="orchestrator")
        self.io.confirm.return_value = False
        with patch.object(mcp, "MCPClient", side_effect=AssertionError("must not launch")):
            output = self.execute(runtime, "connect_mcp", name="browser")
        self.assertTrue(output.is_error)
        self.assertNotIn("browser", mcp.mcp_clients)
        self.io.confirm.assert_called_once()

    def test_real_sdk_snapshot_click_and_console_results_are_kept_as_evidence(self):
        runtime = self.runtime()
        connected = self.execute(runtime, "connect_mcp", name="browser")
        self.assertFalse(connected.is_error)
        self.assertTrue(mcp.requires_mcp_launch_confirmation("browser"))
        tools, _ = runtime.pool()
        names = {item["name"] for item in tools}
        self.assertIn("mcp__browser__take_snapshot", names)
        self.assertIn("mcp__browser__take_screenshot", names)
        self.assertNotIn("mcp__browser__evaluate_script", names)
        snapshot = self.execute(runtime, "mcp__browser__take_snapshot")
        self.assertIn('uid=1 button "Save"', snapshot)
        clicked = self.execute(runtime, "mcp__browser__click", uid="1")
        self.assertEqual(json.loads(clicked)["clicked"], "1")
        console = self.execute(runtime, "mcp__browser__list_console_messages")
        self.assertIn("failed to load application data", console)
        references = [item["reference"] for item in runtime.context.evidence
                      if item.get("tool") == "mcp__browser__list_console_messages"]
        self.assertEqual(len(references), 1)
        self.assertIn("failed to load application data", Path(references[0]).read_text())
        #连接、快照、点击、日志各自审批；连接批准不能授权后续调用。
        self.assertEqual(self.io.confirm.call_count, 4)
        denied = mcp.mcp_clients["browser"].call_tool("evaluate_script", {"function": "alert(1)"})
        self.assertTrue(denied.is_error)

    def test_plan_and_read_only_roles_cannot_use_or_directly_invoke_browser_tools(self):
        #连接目录共享；即便由 Implementation 建立，也不会把权限继承给其它角色。
        self.assertIn("Connected", mcp.connect_mcp("browser"))
        for role, mode in (("orchestrator", "execute"), ("codebase", "execute"),
                           ("planning", "execute"), ("review_research", "execute"),
                           ("implementation", "plan")):
            with self.subTest(role=role, mode=mode):
                runtime = self.runtime(role=role, mode=mode, approval="full")
                names = {item["name"] for item in runtime.pool()[0]}
                self.assertNotIn("mcp__browser__take_snapshot", names)
                self.assertNotIn("mcp__browser__take_screenshot", names)
                output = self.execute(runtime, "mcp__browser__take_snapshot")
                self.assertTrue(output.is_error)
                self.assertIn("Permission denied", output)
                screenshot = self.execute(runtime, "mcp__browser__take_screenshot")
                self.assertTrue(screenshot.is_error)
                self.assertIn("Permission denied", screenshot)
        self.io.confirm.assert_not_called()

    def test_sdk_screenshot_reaches_runtime_as_image_and_saved_private_evidence(self):
        runtime = self.runtime()
        self.execute(runtime, "connect_mcp", name="browser")
        output = self.execute(runtime, "mcp__browser__take_screenshot")
        self.assertFalse(output.is_error)
        images = [block for block in output.content_blocks if block["type"] == "image"]
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["source"]["media_type"], "image/png")
        self.assertNotIn(images[0]["source"]["data"], str(output))
        evidence = next(item for item in runtime.context.evidence
                        if item.get("tool") == "mcp__browser__take_screenshot")
        path = Path(evidence["images"][0])
        self.assertTrue(path.is_relative_to(self.root / ".task_outputs"))
        self.assertEqual(path.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertIn(str(path), json.dumps(output.content_blocks))
        self.io.confirm.assert_called()
        self.assertEqual(self.io.confirm.call_count, 2)

    def test_invalid_screenshot_has_error_then_later_capture_still_works(self):
        runtime = self.runtime()
        self.execute(runtime, "connect_mcp", name="browser")
        output = self.execute(runtime, "mcp__browser__take_screenshot", format="invalid")
        self.assertTrue(output.is_error)
        self.assertIn("image rejected", output)
        self.assertNotIn("not-valid-base64", str(output))
        self.assertIsNone(output.content_blocks)
        later = self.execute(runtime, "mcp__browser__take_screenshot")
        self.assertFalse(later.is_error)
        self.assertTrue(any(block["type"] == "image" for block in later.content_blocks))

    def test_declined_browser_interaction_is_not_forwarded_to_mcp_server(self):
        self.assertIn("Connected", mcp.connect_mcp("browser"))
        runtime = self.runtime()
        self.io.confirm.return_value = False
        with patch.object(mcp.mcp_clients["browser"], "call_tool", side_effect=AssertionError("must not call")):
            output = self.execute(runtime, "mcp__browser__click", uid="1")
        self.assertTrue(output.is_error)
        self.assertIn("denied", output)


if __name__ == "__main__":
    unittest.main()
