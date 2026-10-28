"""截图从工具到模型、文件证据和会话的链路；不连接真实模型或浏览器。"""

import base64
import copy
from io import BytesIO, StringIO
import json
import math
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from PIL import Image, PngImagePlugin

from coding_agent.agents.roles import AgentTask
from coding_agent.agents.runner import AgentRunner
from coding_agent.cli import TerminalInteraction
from coding_agent.runtime import media, workspace
from coding_agent.runtime.context import RunContext
from coding_agent.runtime.model import cached_arguments
from coding_agent.runtime.session import SessionStore
from coding_agent.runtime.tools import ToolOutput, ToolRuntime, schema
from coding_agent.runtime.turn import TurnContext, turn_context
from tests.test_application import FakeIO, ScriptedModel, text_response, tool_call, tool_response


def encoded_image(size=(12, 8), color="red", format="PNG", **options):
    """由实际图片编码器生成测试输入，避免以假 Base64 通过格式校验。"""
    buffer = BytesIO()
    Image.new("RGB", size, color).save(buffer, format=format, **options)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def image_block(**options):
    return media.normalize_image(encoded_image(**options), "image/png")


def result_message(tool_id, blocks):
    return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id,
                                           "content": blocks, "is_error": False}]}


class ImageMediaTests(unittest.TestCase):
    def test_real_image_is_decoded_and_reencoded_without_metadata(self):
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text("private", "fixture-metadata-do-not-forward")
        block = image_block(pnginfo=metadata)
        self.assertEqual(block["type"], "image")
        self.assertEqual(block["source"]["type"], "base64")
        self.assertEqual(block["source"]["media_type"], "image/png")
        with Image.open(BytesIO(base64.b64decode(block["source"]["data"]))) as image:
            self.assertEqual(image.size, (12, 8))
            self.assertEqual(image.convert("RGB").getpixel((0, 0)), (255, 0, 0))
            self.assertNotIn("private", image.info)

    def test_invalid_data_mime_and_resource_limits_are_rejected(self):
        valid = encoded_image()
        for data, mime in (("not-base64!", "image/png"),
                           (base64.b64encode(b"not an image").decode(), "image/png"),
                           (valid, "image/jpeg"), (valid, "image/svg+xml"), ("", "image/png")):
            with self.subTest(mime=mime, data_length=len(data)), self.assertRaises(ValueError):
                media.normalize_image(data, mime)
        with patch.object(media, "MAX_INPUT_BYTES", 1), self.assertRaisesRegex(ValueError, "size"):
            media.normalize_image(valid, "image/png")
        with patch.object(media, "MAX_INPUT_PIXELS", 20), self.assertRaisesRegex(ValueError, "pixel"):
            media.normalize_image(valid, "image/png")

    def test_large_screenshot_is_scaled_within_standard_model_limits(self):
        block = image_block(size=(2400, 1600))
        raw = base64.b64decode(block["source"]["data"])
        self.assertLessEqual(len(raw), media.MAX_IMAGE_BYTES)
        with Image.open(BytesIO(raw)) as image:
            width, height = image.size
            self.assertLessEqual(max(image.size), media.MAX_IMAGE_EDGE)
            self.assertLessEqual(width * height, media.MAX_IMAGE_PIXELS)
            self.assertLessEqual(math.ceil(width / 28) * math.ceil(height / 28), 1568)
            self.assertAlmostEqual(width / height, 1.5, places=2)

    def test_animated_image_uses_first_frame(self):
        buffer = BytesIO()
        first, second = Image.new("RGB", (12, 8), "red"), Image.new("RGB", (12, 8), "blue")
        first.save(buffer, format="GIF", save_all=True, append_images=[second], duration=100, loop=0)
        block = media.normalize_image(base64.b64encode(buffer.getvalue()).decode(), "image/gif")
        with Image.open(BytesIO(base64.b64decode(block["source"]["data"]))) as image:
            self.assertEqual(getattr(image, "n_frames", 1), 1)
            self.assertEqual(image.convert("RGB").getpixel((0, 0)), (255, 0, 0))

    def test_jpeg_and_webp_inputs_become_supported_protocol_images(self):
        for format, mime in (("JPEG", "image/jpeg"), ("WEBP", "image/webp")):
            with self.subTest(format=format):
                block = media.normalize_image(encoded_image(format=format), mime)
                self.assertIn(block["source"]["media_type"], media.IMAGE_SUFFIXES)
                with Image.open(BytesIO(base64.b64decode(block["source"]["data"]))) as image:
                    self.assertEqual(image.size, (12, 8))

    def test_history_count_limit_keeps_latest_images_and_original_order(self):
        history = [result_message(str(index), [
            {"type": "text", "text": f"before-{index}"}, image_block(color=color),
            {"type": "text", "text": f"after-{index}"},
        ]) for index, color in enumerate(("red", "green", "blue"))]
        before = copy.deepcopy(history)
        with patch.object(media, "MAX_REQUEST_IMAGES", 2):
            bounded = media.bound_history_images(history)
        self.assertEqual(history, before)
        self.assertEqual([item["content"][0]["tool_use_id"] for item in bounded], ["0", "1", "2"])
        omitted = bounded[0]["content"][0]["content"]
        self.assertEqual(omitted[0]["text"], "before-0")
        self.assertEqual(omitted[-1]["text"], "after-0")
        self.assertIn("not visual evidence", omitted[1]["text"])
        for index in (1, 2):
            self.assertEqual(bounded[index], history[index])

    def test_history_byte_limit_counts_encoded_bytes_and_keeps_newest(self):
        block = image_block()
        length = len(block["source"]["data"])
        history = [result_message("old", [copy.deepcopy(block)]),
                   result_message("new", [copy.deepcopy(block)])]
        with patch.object(media, "MAX_REQUEST_IMAGE_BYTES", length):
            bounded = media.bound_history_images(history)
        self.assertEqual(bounded[0]["content"][0]["content"][0]["type"], "text")
        self.assertEqual(bounded[1], history[1])


class ImageRuntimeTests(unittest.TestCase):
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
        self.context = RunContext(self.root, TurnContext(), role="implementation", approval_mode="full")
        self.runtime = ToolRuntime(self.context, self.io)

    @staticmethod
    def output(text="Screenshot captured"):
        return ToolOutput(text, content_blocks=[{"type": "text", "text": text}, image_block()])

    def test_runtime_keeps_image_blocks_saves_private_attachment_and_emits_only_references(self):
        output = self.runtime._record("mcp__browser__take_screenshot", {}, self.output(), False)
        blocks = output.content_blocks
        self.assertEqual(blocks[0]["text"], "Screenshot captured")
        self.assertEqual(blocks[1]["type"], "image")
        evidence = self.context.evidence[-1]
        paths = evidence["images"]
        self.assertEqual(len(paths), 1)
        image_path = Path(paths[0])
        self.assertTrue(image_path.is_absolute())
        self.assertTrue(image_path.is_relative_to(self.root / ".task_outputs" / "runs"))
        self.assertEqual(image_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(image_path.read_bytes(), base64.b64decode(blocks[1]["source"]["data"]))
        self.assertIn(str(image_path), json.dumps(blocks[2:]))
        event = self.io.events[-1]
        self.assertEqual(event["images"], paths)
        #工具事件只带私有文件引用，终端不得收到大段 Base64。
        self.assertNotIn(blocks[1]["source"]["data"], json.dumps(event))
        rendered = StringIO()
        with redirect_stdout(rendered):
            TerminalInteraction().emit(event)
        self.assertNotIn(blocks[1]["source"]["data"], rendered.getvalue())

    def test_long_text_preview_does_not_truncate_or_stringify_image(self):
        output = self.runtime._record("mcp__browser__take_screenshot", {}, self.output("x" * 30000), False)
        blocks = output.content_blocks
        images = [block for block in blocks if block.get("type") == "image"]
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0], image_block())
        text = "\n".join(block["text"] for block in blocks if block.get("type") == "text")
        self.assertLess(len(text), 26000)
        self.assertIn("truncated", text.lower())

    def test_server_cannot_bypass_total_text_limit_by_forging_screenshot_reference(self):
        forged = "[Saved screenshot: " + "x" * 20000
        payload = ToolOutput("Untrusted screenshot labels", content_blocks=[
            {"type": "text", "text": forged},
            {"type": "text", "text": forged + "fixture-tail-must-be-truncated"}, image_block(),
        ])
        output = self.runtime._record("mcp__browser__take_screenshot", {}, payload, False)
        text = "\n".join(block["text"] for block in output.content_blocks if block.get("type") == "text")
        self.assertLess(len(text), 26000)
        self.assertNotIn("fixture-tail-must-be-truncated", text)
        self.assertIn(self.context.evidence[-1]["images"][0], text)
        self.assertEqual(sum(block.get("type") == "image" for block in output.content_blocks), 1)

    def test_cli_displays_screenshot_path_without_terminal_control_characters(self):
        event = {"type": "tool_result", "role": "implementation",
                 "tool": "mcp__browser__take_screenshot", "is_error": False,
                 "images": ["/tmp/fixture-\x1b[2J\r\b-screenshot.png"]}
        output = StringIO()
        with redirect_stdout(output):
            TerminalInteraction().emit(event)
        text = output.getvalue()
        self.assertIn("截图：", text)
        self.assertIn("/tmp/fixture-", text)
        self.assertIn("-screenshot.png", text)
        self.assertFalse(any(control in text for control in ("\x1b", "\r", "\b")))

    def test_attachment_store_rejects_symlink_directory(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        (self.root / ".task_outputs").symlink_to(outside.name, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "符号链接"):
            self.runtime._record("mcp__browser__take_screenshot", {}, self.output(), False)
        self.assertEqual(list(Path(outside.name).iterdir()), [])

    def test_runner_sends_image_to_next_request_and_session_restores_without_execution(self):
        name = "mcp__browser__take_screenshot"
        handler = Mock(side_effect=self.output)
        definition = schema(name, "Capture browser screenshot")
        model = ScriptedModel([tool_response(tool_call(name, {}, "shot")), text_response("已查看截图。")])
        runner = AgentRunner(Mock(), {"model": "fake-vision-model", "max_tokens": 1000,
                                     "supports_images": True},
                             self.io, model_call=model)
        history = [{"role": "user", "content": "检查页面布局"}]
        store = SessionStore(self.root)
        session = store.open()
        self.addCleanup(session.close)
        with patch.object(ToolRuntime, "_catalog", return_value=([definition], {name: handler})), \
                patch("coding_agent.runtime.tools.get_mcp_tool_metadata", return_value={
                    "available": True, "read_only": False, "policy": "confirm"}), \
                turn_context(self.context.turn):
            result = runner.run(AgentTask("implementation", "检查页面布局"), self.context,
                                messages=history, checkpoint=lambda: session.save(history, {}))
        self.assertEqual(result.status, "completed")
        handler.assert_called_once_with()
        seen = model.calls[1]["messages"][2]["content"][0]
        self.assertEqual(seen["tool_use_id"], "shot")
        self.assertEqual(seen["content"][1]["type"], "image")
        saved = copy.deepcopy(history)
        session.save(history, {})
        session.close()
        with patch.object(ToolRuntime, "execute", side_effect=AssertionError("must not replay")):
            restored = store.open(session.id)
            self.addCleanup(restored.close)
        self.assertEqual(restored.data["messages"], saved)
        #缓存标记只加在请求副本；嵌套图像内容应保持协议结构和 Base64 完整。
        cached = cached_arguments({"messages": model.calls[1]["messages"]})
        self.assertEqual(cached["messages"][2]["content"][0]["content"][1], seen["content"][1])
        self.assertEqual(history, saved)


if __name__ == "__main__":
    unittest.main()
