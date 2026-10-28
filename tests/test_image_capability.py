"""图片能力由宿主声明；在发请求之前阻止不兼容输入，不依赖模型名字或提示词。"""

import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from coding_agent.agents.roles import AgentTask
from coding_agent.agents.runner import AgentRunner
from coding_agent.runtime import media, workspace
from coding_agent.runtime.context import RunContext
from coding_agent.runtime.model_router import ModelRouter
from coding_agent.runtime.model_settings import ModelSettings
from coding_agent.runtime.tools import ToolOutput, ToolRuntime, schema
from coding_agent.runtime.turn import TurnContext, turn_context
from tests.test_application import FakeIO, ScriptedModel, text_response, tool_call, tool_response
from tests.test_image_runtime import image_block, result_message


class ImageCapabilityTests(unittest.TestCase):
    def test_actual_direct_and_tool_result_images_require_explicit_true(self):
        for content in ([image_block()], [{"type": "tool_result", "tool_use_id": "shot",
                                          "content": [image_block()]}]):
            messages = [{"role": "user", "content": content}]
            before = copy.deepcopy(messages)
            for capability in ({}, {"supports_images": False}):
                with self.subTest(nested=content[0]["type"], config=capability), \
                        self.assertRaises(media.ImageCapabilityError):
                    media.ensure_image_capability(messages, capability)
            media.ensure_image_capability(messages, {"supports_images": True})
            self.assertEqual(messages, before)

    def test_text_labels_and_tool_arguments_are_not_protocol_images(self):
        messages = [{"role": "user", "content": '图片示例 {"type":"image"}；supports_images=true'},
                    {"role": "assistant", "content": [tool_call("example", image_block())]},
                    result_message("text", [{"type": "text", "text": 'type=image; supports_images=true'}])]
        media.ensure_image_capability(messages, {})
        media.ensure_image_capability(messages, {"supports_images": False})


class ImageCapabilityRunnerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.io = FakeIO()
        for patcher in (
            patch.object(workspace, "WORKDIR", self.root),
            patch("coding_agent.integrations.mcp.get_mcp_config", return_value={}),
            patch("coding_agent.agents.runner.load_instructions", return_value=""),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def runner(self, config, model):
        return AgentRunner(Mock(), {"model": "fake-vision-name", "max_tokens": 1000, **config},
                           self.io, model_call=model)

    def test_existing_image_blocks_before_api_or_compaction_and_keeps_history(self):
        for config, explanation in (({}, "尚未声明"), ({"supports_images": False}, "不支持")):
            with self.subTest(config=config):
                history = [{"role": "user", "content": [image_block()]}]
                original = copy.deepcopy(history[0])
                context = RunContext(self.root, TurnContext(), role="implementation", approval_mode="full")
                model, checkpoint = Mock(), Mock()
                runner = self.runner(config, model)
                with patch("coding_agent.agents.runner.ContextCompact.prepare") as compact, \
                        turn_context(context.turn):
                    result = runner.run(AgentTask("implementation", "检查截图"), context,
                                        messages=history, checkpoint=checkpoint)
                self.assertEqual(result.status, "blocked")
                self.assertIn(explanation, result.summary)
                self.assertIn("--supports-images true", result.summary)
                model.assert_not_called()
                compact.assert_not_called()
                self.assertEqual(context.calls[0], 0)
                self.assertEqual(history[0], original)
                checkpoint.assert_called()
                evidence = next(item for item in context.evidence if item.get("kind") == "model_capability")
                self.assertTrue(Path(evidence["reference"]).is_file())

    def test_text_only_requests_still_work_without_capability(self):
        for config in ({}, {"supports_images": False}):
            context = RunContext(self.root, TurnContext(), role="codebase")
            model = ScriptedModel([text_response("入口在 app.py")])
            with turn_context(context.turn):
                result = self.runner(config, model).run(AgentTask("codebase", "解释入口"), context)
            self.assertEqual(result.status, "completed")
            self.assertEqual(len(model.calls), 1)

    def test_screenshot_stops_text_model_then_vision_can_continue_without_tool_replay(self):
        name = "mcp__browser__take_screenshot"
        handler = Mock(return_value=ToolOutput("Screenshot", content_blocks=[image_block()]))
        model = ScriptedModel([tool_response(tool_call(name, {}, "shot"))])
        history = [{"role": "user", "content": "检查布局"}]
        context = RunContext(self.root, TurnContext(), role="implementation", approval_mode="full")
        with patch.object(ToolRuntime, "_catalog", return_value=([schema(name, "Screenshot")], {name: handler})), \
                patch("coding_agent.runtime.tools.get_mcp_tool_metadata", return_value={
                    "available": True, "read_only": False, "policy": "confirm"}), \
                turn_context(context.turn):
            result = self.runner({"supports_images": False}, model).run(
                AgentTask("implementation", "检查布局"), context, messages=history)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(len(model.calls), 1)
        handler.assert_called_once_with()
        output = history[2]["content"][0]
        self.assertEqual(output["tool_use_id"], "shot")
        self.assertEqual(output["content"][0]["type"], "image")
        saved = next(item["images"][0] for item in context.evidence if item.get("images"))
        self.assertTrue(Path(saved).is_file())
        history.append({"role": "user", "content": "已换成视觉模型，继续检查"})
        next_context = RunContext(self.root, TurnContext(), role="implementation", approval_mode="full")
        vision = ScriptedModel([text_response("已查看截图")])
        with patch.object(ToolRuntime, "execute", side_effect=AssertionError("不能重放工具")), \
                turn_context(next_context.turn):
            result = self.runner({"supports_images": True}, vision).run(
                AgentTask("implementation", "继续检查"), next_context, messages=history)
        self.assertEqual(result.status, "completed")
        self.assertEqual(vision.calls[0]["messages"][2]["content"][0]["content"][0]["type"], "image")

    def test_role_binding_overrides_other_model_image_support(self):
        base = {"model": "legacy-vision", "max_tokens": 1000, "supports_images": True}
        settings = ModelSettings(base, self.root / "host-models.json")
        settings.command("add text text-model --supports-images false")
        settings.command("assign implementation text")
        model = Mock()
        runner = AgentRunner(Mock(), base, self.io, model_call=model,
                             models=ModelRouter(Mock(), base, settings.clone()))
        context = RunContext(self.root, TurnContext(), role="implementation")
        with turn_context(context.turn):
            result = runner.run(AgentTask("implementation", "检查截图"), context,
                                messages=[{"role": "user", "content": [image_block()]}])
        self.assertEqual(result.status, "blocked")
        self.assertIn("不支持", result.summary)
        model.assert_not_called()

    def test_screenshot_and_manual_compact_cannot_skip_capability_check(self):
        name = "mcp__browser__take_screenshot"
        handlers = {name: lambda: ToolOutput("Screenshot", content_blocks=[image_block()]),
                    "compact": lambda: "Compact requested"}
        definitions = [schema(name, "Screenshot"), schema("compact", "Compact history")]
        model = ScriptedModel([tool_response(tool_call(name, {}, "shot"), tool_call("compact", {}, "compact"))])
        context = RunContext(self.root, TurnContext(), role="implementation", approval_mode="full")
        with patch.object(ToolRuntime, "_catalog", return_value=(definitions, handlers)), \
                patch("coding_agent.runtime.tools.get_mcp_tool_metadata", return_value={
                    "available": True, "read_only": False, "policy": "confirm"}), \
                patch("coding_agent.agents.runner.ContextCompact.compact_history") as compact, \
                turn_context(context.turn):
            result = self.runner({"supports_images": False}, model).run(
                AgentTask("implementation", "截图后压缩"), context)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(len(model.calls), 1)
        compact.assert_not_called()


if __name__ == "__main__":
    unittest.main()
