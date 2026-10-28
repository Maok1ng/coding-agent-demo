"""图片预算与压缩回归：只构造协议数据，不请求网页或模型。"""

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from coding_agent.runtime.budget import ContextBudgetError, RequestBudget
from coding_agent.runtime.compact import ContextCompact
from coding_agent.runtime.media import IMAGE_TOKEN_RESERVE
from coding_agent.runtime.tokens import json_tokens, omit_image_data, text_tokens


def image(data="IMAGE_BASE64_SENTINEL", *, url=None):
    source = ({"type": "url", "url": url} if url is not None else
              {"type": "base64", "media_type": "image/png", "data": data})
    return {"type": "image", "source": source}


def result(identifier, content):
    return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": identifier,
                                             "content": content, "is_error": False}]}


def call(identifier):
    return {"role": "assistant", "content": [{"type": "tool_use", "id": identifier,
                                                 "name": "mcp__browser__take_screenshot", "input": {}}]}


class ImageBudgetTests(unittest.TestCase):
    def test_plain_json_estimate_is_unchanged(self):
        value = [{"role": "user", "content": "读取 app.py 然后修改接口"}, {"n": 42}]
        self.assertEqual(json_tokens(value), text_tokens(json.dumps(value, ensure_ascii=False)))

    def test_base64_size_does_not_become_text_tokens(self):
        small, large = image("abcd"), image("abcd" * 300000)
        self.assertEqual(json_tokens(small), json_tokens(large))
        self.assertGreaterEqual(json_tokens(large), IMAGE_TOKEN_RESERVE)
        self.assertLess(json_tokens(large), IMAGE_TOKEN_RESERVE + 128)
        self.assertGreaterEqual(json_tokens([small, large]), IMAGE_TOKEN_RESERVE * 2)

    def test_actual_request_budget_counts_nested_tool_result_images(self):
        messages = [call("shot"), result("shot", [{"type": "text", "text": "Screenshot"}, image()])]
        original = copy.deepcopy(messages)
        self.assertGreater(RequestBudget.history_tokens(messages), IMAGE_TOKEN_RESERVE)
        self.assertEqual(messages, original)

    def test_summary_omits_base64_and_urls_without_mutating_history(self):
        value = [result("shot", [image(), image(url="https://example.test/private.png")])]
        original = copy.deepcopy(value)
        safe = json.dumps(omit_image_data(value))
        self.assertNotIn("IMAGE_BASE64_SENTINEL", safe)
        self.assertNotIn("example.test", safe)
        self.assertIn("do not infer", safe)
        self.assertEqual(value, original)
        self.assertGreaterEqual(json_tokens(image(url="https://example.test/image.png")), IMAGE_TOKEN_RESERVE)


class ImageCompactTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.compact = ContextCompact(Mock(), "fake", self.root / ".transcripts",
                                      self.root / ".task_outputs" / "tool-results")

    def test_large_new_picture_result_is_not_replaced_by_stringified_base64(self):
        content = [{"type": "text", "text": "Screenshot saved"}, image("abcd" * 300000)]
        messages = [call("shot"), result("shot", content)]
        self.compact.tool_result_budget(messages, max_chars=1)
        self.compact.fit_tool_results(messages, target_tokens=0)
        self.assertEqual(messages[-1]["content"][0]["content"], content)
        self.assertEqual(self.compact.persist_large_output("shot", content), content)
        self.assertFalse((self.root / ".task_outputs").exists())

    def test_old_picture_preview_keeps_original_json_and_safe_text(self):
        output = [{"type": "text", "text": "Page screenshot"}, image()]
        preview = self.compact.persisted_preview("shot", output)
        path = Path(self.compact.persisted_output_path(preview))
        self.assertEqual(path.suffix, ".json")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), output)
        self.assertIn("Page screenshot", preview)
        self.assertIn("do not infer", preview)
        self.assertNotIn("IMAGE_BASE64_SENTINEL", preview)
        repeated = self.compact.persisted_preview("shot", preview)
        self.assertEqual(self.compact.persisted_output_path(repeated), str(path))
        self.assertNotIn("IMAGE_BASE64_SENTINEL", repeated)

    def test_micro_compact_old_picture_preserves_new_picture_structure(self):
        messages = []
        for index in range(4):
            messages.extend([call(str(index)), result(str(index), [image(f"OLD_IMAGE_{index}")])])
        messages.extend([{"role": "assistant", "content": "Reviewed older screenshots"},
                         call("new"), result("new", [image("NEW_IMAGE")])])
        self.compact.micro_compact(messages)
        old = messages[1]["content"][0]["content"]
        path = Path(self.compact.persisted_output_path(old))
        self.assertEqual(json.loads(path.read_text())[0]["source"]["data"], "OLD_IMAGE_0")
        self.assertEqual(messages[-1]["content"][0]["content"], [image("NEW_IMAGE")])

    def test_summary_helper_and_mechanical_summary_never_receive_image_bytes(self):
        messages = [call("shot"), result("shot", [{"type": "text", "text": "Page opened"}, image()])]
        summary = self.compact.summary_input(messages)
        body, _ = self.compact._mechanical_body(messages)
        for text in (summary, body):
            self.assertNotIn("IMAGE_BASE64_SENTINEL", text)
            self.assertIn("Page opened", text)
            self.assertIn("do not infer", text)

    def test_budgeted_manual_compaction_preserves_latest_image_and_archives_old_one(self):
        self.compact.CONTEXT_TOKEN_LIMIT = 10000
        self.compact.configure_budget("system", [], 500)
        current = {"role": "user", "content": "Fix the page"}
        newest = result("new", [image("NEW_IMAGE")])
        messages = [current, call("old"), result("old", [image("OLD_IMAGE")]),
                    *[{"role": "assistant", "content": "Observed page state"} for _ in range(6)],
                    call("new"), newest]
        with patch("coding_agent.runtime.compact.call_model") as model:
            compacted = self.compact.compact_history(messages, current["content"])
        model.assert_not_called()
        self.compact.ensure_fit(compacted)
        self.assertIn(current, compacted)
        self.assertEqual(compacted[-1], newest)
        self.assertIn("NEW_IMAGE", json.dumps(compacted))
        self.assertNotIn("OLD_IMAGE", json.dumps(compacted))
        archive, = (self.root / ".transcripts").glob("*.jsonl")
        self.assertIn("OLD_IMAGE", archive.read_text(encoding="utf-8"))

    def test_protected_images_over_budget_stop_before_model_call(self):
        self.compact.CONTEXT_TOKEN_LIMIT = 2000
        self.compact.configure_budget("system", [], 100)
        messages = [{"role": "user", "content": "Inspect this"}, call("shot"), result("shot", [image()])]
        original = copy.deepcopy(messages)
        with patch("coding_agent.runtime.compact.call_model") as model:
            with self.assertRaisesRegex(ContextBudgetError, "近期工具批次"):
                self.compact.prepare(messages, "Inspect this")
        model.assert_not_called()
        self.assertEqual(messages, original)


if __name__ == "__main__":
    unittest.main()
