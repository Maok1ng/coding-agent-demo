"""压缩归档安全与行为回归：全部使用临时工作区，不访问模型或网络。"""

import copy
import json
from pathlib import Path
import stat
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from coding_agent.runtime.compact import ContextCompact


class CompactTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.addCleanup(outside.cleanup)
        self.root = Path(directory.name).resolve()
        self.outside = Path(outside.name).resolve()
        self.transcripts = self.root / ".transcripts"
        self.outputs = self.root / ".task_outputs" / "tool-results"
        self.compact = ContextCompact(Mock(), "fake-model", self.transcripts, self.outputs)

    @staticmethod
    def result(tool_id, content):
        return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id,
                                                "content": content, "is_error": False}]}

    @staticmethod
    def call(tool_id):
        return {"role": "assistant", "content": [{"type": "tool_use", "id": tool_id,
                                                     "name": "read_file", "input": {"path": "app.py"}}]}

    @staticmethod
    def summary_response(text):
        return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=text)])

    def test_rejects_output_directory_in_different_workspace(self):
        with self.assertRaisesRegex(ValueError, "同一个工作区"):
            ContextCompact(Mock(), "fake", self.transcripts, self.outside / "tool-results")
        self.assertEqual(list(self.outside.iterdir()), [])

    def test_transcript_symlink_before_construction_cannot_write_outside(self):
        self.transcripts.symlink_to(self.outside, target_is_directory=True)
        compact = ContextCompact(Mock(), "fake", self.transcripts, self.outputs)

        with self.assertRaises((OSError, ValueError)):
            compact.write_transcript([{"role": "user", "content": "private conversation"}])

        self.assertEqual(compact.workspace, self.root)
        self.assertEqual(list(self.outside.iterdir()), [])
        self.assertTrue(self.transcripts.is_symlink())

    def test_intermediate_task_outputs_symlink_cannot_write_outside(self):
        (self.root / ".task_outputs").symlink_to(self.outside, target_is_directory=True)

        with self.assertRaises((OSError, ValueError)):
            self.compact.save_output("tool", "private result")

        self.assertEqual(list(self.outside.iterdir()), [])

    def test_tool_results_symlink_cannot_write_outside(self):
        self.outputs.parent.mkdir()
        self.outputs.symlink_to(self.outside, target_is_directory=True)

        with self.assertRaises((OSError, ValueError)):
            self.compact.persisted_preview("tool", "private result")

        self.assertEqual(list(self.outside.iterdir()), [])

    def test_internal_directory_symlink_is_rejected_too(self):
        target = self.root / "other-folder"
        target.mkdir()
        self.transcripts.symlink_to(target, target_is_directory=True)

        with self.assertRaises((OSError, ValueError)):
            self.compact.write_transcript([])

        self.assertEqual(list(target.iterdir()), [])

    def test_directory_swapped_for_symlink_after_construction_is_rejected(self):
        self.compact.save_output("before", "first")
        old = self.root / "previous-results"
        self.outputs.rename(old)
        self.outputs.symlink_to(self.outside, target_is_directory=True)

        with self.assertRaises((OSError, ValueError)):
            self.compact.save_output("after", "must not escape")

        self.assertEqual(list(self.outside.iterdir()), [])
        self.assertEqual(len(list(old.iterdir())), 1)

    def test_preexisting_fixed_tool_file_and_symlink_are_never_overwritten(self):
        self.outputs.mkdir(parents=True)
        existing = self.outputs / "repeat.txt"
        existing.write_text("user data", encoding="utf-8")
        target = self.outside / "private.txt"
        target.write_text("outside data", encoding="utf-8")
        link = self.outputs / "linked.txt"
        link.symlink_to(target)

        first = self.compact.save_output("repeat", "first result")
        second = self.compact.save_output("repeat", "second result")
        linked = self.compact.save_output("linked", "safe new result")

        self.assertNotEqual(first, second)
        self.assertEqual(first.read_text(encoding="utf-8"), "first result")
        self.assertEqual(second.read_text(encoding="utf-8"), "second result")
        self.assertEqual(linked.read_text(encoding="utf-8"), "safe new result")
        self.assertEqual(existing.read_text(encoding="utf-8"), "user data")
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), "outside data")

    def test_uuid_collision_skips_preexisting_file_and_link(self):
        self.outputs.mkdir(parents=True)
        target = self.outside / "target.txt"
        target.write_text("outside", encoding="utf-8")
        original = self.outputs / f"tool_{'a' * 32}.txt"
        original.write_text("existing", encoding="utf-8")
        link = self.outputs / f"tool_{'b' * 32}.txt"
        link.symlink_to(target)
        values = [types.SimpleNamespace(hex=character * 32) for character in "abc"]

        with patch("coding_agent.runtime.compact.uuid.uuid4", side_effect=values):
            saved = self.compact.save_output("tool", "new archive")

        self.assertEqual(saved.name, f"tool_{'c' * 32}.txt")
        self.assertEqual(original.read_text(encoding="utf-8"), "existing")
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), "outside")

    def test_transcript_collision_never_truncates_existing_history(self):
        self.transcripts.mkdir()
        existing = self.transcripts / f"transcript_{'a' * 32}.jsonl"
        existing.write_text("previous history", encoding="utf-8")
        values = [types.SimpleNamespace(hex=character * 32) for character in "ab"]

        with patch("coding_agent.runtime.compact.uuid.uuid4", side_effect=values):
            saved = self.compact.write_transcript([{"role": "user", "content": "new"}])

        self.assertNotEqual(saved, existing)
        self.assertEqual(existing.read_text(encoding="utf-8"), "previous history")
        self.assertEqual(json.loads(saved.read_text(encoding="utf-8"))["content"], "new")

    def test_created_archive_directories_and_files_are_private(self):
        self.outputs.mkdir(parents=True, mode=0o755)
        self.outputs.parent.chmod(0o755)
        self.outputs.chmod(0o755)

        output = self.compact.save_output("private", "sensitive output")
        transcript = self.compact.write_transcript([{"role": "user", "content": "sensitive message"}])

        for directory in (self.transcripts, self.outputs.parent, self.outputs):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        for path in (output, transcript):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_tool_id_is_only_filename_label_not_path(self):
        saved = self.compact.save_output("../../outside/name", "safe content")

        self.assertEqual(saved.parent, self.outputs)
        self.assertEqual(saved.read_text(encoding="utf-8"), "safe content")
        self.assertFalse((self.root / "outside").exists())

    def test_persisted_preview_reuses_valid_existing_archive(self):
        original = self.compact.persisted_preview("tool", "x" * 40000, preview_chars=20)
        path = self.compact.persisted_output_path(original)
        with patch.object(self.compact, "save_output", side_effect=AssertionError("must reuse archive")):
            preview = self.compact.persisted_preview("tool", original, preview_chars=10)

        self.assertEqual(self.compact.persisted_output_path(preview), path)
        self.assertIn("Preview:\n" + "x" * 10 + "\n", preview)
        self.assertEqual(Path(path).read_text(encoding="utf-8"), "x" * 40000)

    def test_existing_output_reference_cannot_follow_file_or_directory_symlink(self):
        target = self.outside / "secret.txt"
        target.write_text("outside secret", encoding="utf-8")
        self.outputs.mkdir(parents=True)
        linked_file = self.outputs / "secret.txt"
        linked_file.symlink_to(target)
        linked_directory = self.outputs / "nested"
        linked_directory.symlink_to(self.outside, target_is_directory=True)
        for path in (linked_file, linked_directory / "secret.txt", target):
            with self.subTest(path=path):
                preview = f"<persisted-output>\nFull output: {path}\nPreview:\nnone\n</persisted-output>"
                self.assertIsNone(self.compact.persisted_output_path(preview))

    def test_archive_marker_cannot_follow_symlink(self):
        self.transcripts.mkdir()
        target = self.outside / "history.jsonl"
        target.write_text("outside history", encoding="utf-8")
        link = self.transcripts / "history.jsonl"
        link.symlink_to(target)

        self.assertFalse(self.compact.is_archive_marker({"role": "user", "content": f"[2 messages archived at {link}]"}))
        saved = self.compact.write_transcript([{"role": "user", "content": "history"}])
        self.assertTrue(self.compact.is_archive_marker({"role": "user", "content": f"[2 messages archived at {saved}]"}))

    def test_prepare_force_shrink_compacts_even_under_budget(self):
        #缓存过期窗口：未超预算也机械收缩旧结果（窗口模式下 fit 目标为 0）；
        #LLM 摘要仍只在真正超预算时触发。
        messages = [{"role": "user", "content": "task"},
                    self.call("t1"), self.result("t1", "result " * 800),
                    {"role": "assistant", "content": "检查完成。"},
                    {"role": "user", "content": "继续"}]
        with patch("coding_agent.runtime.compact.call_model", side_effect=AssertionError("not needed")):
            normal = self.compact.prepare(copy.deepcopy(messages), "task")
            self.assertEqual(normal[2]["content"][0]["content"], messages[2]["content"][0]["content"])
            shrunk = self.compact.prepare(copy.deepcopy(messages), "task", force_shrink=True)
        preview = shrunk[2]["content"][0]["content"]
        self.assertIn("<persisted-output>", preview)
        self.assertLess(len(preview), 2000)
        self.assertIsNotNone(self.compact.persisted_output_path(preview))

    def test_prepare_archives_large_new_output_without_losing_original(self):
        output = "result " * 31000
        messages = [self.call("large"), self.result("large", output)]
        with patch("coding_agent.runtime.compact.call_model", side_effect=AssertionError("not needed")):
            prepared = self.compact.prepare(messages, "inspect result")

        preview = prepared[-1]["content"][0]["content"]
        self.assertLess(len(preview), 3000)
        path = self.compact.persisted_output_path(preview)
        self.assertIsNotNone(path)
        self.assertEqual(Path(path).read_text(encoding="utf-8"), output)

    def test_prepare_snips_messages_but_archives_full_history(self):
        messages = [{"role": "user" if index % 2 == 0 else "assistant", "content": f"message {index} " + "detail " * 100}
                    for index in range(60)]
        original = copy.deepcopy(messages)

        prepared = self.compact.prepare(messages, "current task")

        self.assertLess(len(prepared), len(original))
        self.assertEqual(prepared[:3], original[:3])
        self.assertEqual(prepared[-1], original[-1])
        self.assertTrue(self.compact.is_archive_marker(prepared[3]))
        archive = next(self.transcripts.glob("*.jsonl"))
        self.assertEqual([json.loads(line) for line in archive.read_text(encoding="utf-8").splitlines()], original)

    def test_micro_compact_preserves_recent_and_unseen_results(self):
        messages = []
        for index in range(4):
            messages.extend([self.call(f"past-{index}"), self.result(f"past-{index}", f"old {index} " * 100)])
        messages.extend([self.call("fresh"), self.result("fresh", "new output " * 100)])
        original = copy.deepcopy(messages)

        compacted = self.compact.micro_compact(messages)

        old = compacted[1]["content"][0]["content"]
        self.assertTrue(old.startswith("[Earlier tool result saved at "))
        path = self.compact.persisted_output_path(old)
        self.assertEqual(Path(path).read_text(encoding="utf-8"), original[1]["content"][0]["content"])
        self.assertEqual(compacted[3:], original[3:])

    def test_manual_compaction_archives_before_fake_summary(self):
        #历史请求中部有约束，不能截掉它后仍把机械摘要当作完整结果。
        constraint = "Do not modify database migrations."
        messages = [{"role": "user", "content": "first " + "a" * 2500 + constraint + "b" * 1200},
                    {"role": "assistant", "content": "answer"}]

        def summarize(*args, **kwargs):
            archive = next(self.transcripts.glob("*.jsonl"))
            self.assertEqual(len(archive.read_text(encoding="utf-8").splitlines()), 2)
            self.assertNotIn("tools", kwargs)
            self.assertIn(constraint, kwargs["messages"][0]["content"])
            return self.summary_response("已完成阅读，待执行检查")

        with patch("coding_agent.runtime.compact.call_model", side_effect=summarize) as model:
            result = self.compact.compact_history(messages, "current task")

        model.assert_called_once()
        self.assertEqual(len(result), 1)
        self.assertIn("[Compacted]", result[0]["content"])
        self.assertIn("current task", result[0]["content"])
        self.assertIn("已完成阅读", result[0]["content"])

    def test_reactive_compaction_keeps_complete_recent_tool_pair(self):
        messages = [
            {"role": "user", "content": "task"}, {"role": "assistant", "content": "analysis"},
            {"role": "user", "content": "detail"}, {"role": "assistant", "content": "more"},
            self.call("pair"), self.result("pair", "actual output"),
            {"role": "assistant", "content": "read output"}, {"role": "user", "content": "next"},
            {"role": "assistant", "content": "latest"}, {"role": "user", "content": "current"},
        ]
        with patch("coding_agent.runtime.compact.call_model", side_effect=AssertionError("mechanical summary is sufficient")):
            result = self.compact.reactive_compact(messages, "current request")

        self.assertIn("[Reactive compact (mechanical)]", result[0]["content"])
        self.assertIn("[USER] detail", result[0]["content"])
        self.assertIn("[ASSISTANT] more", result[0]["content"])
        self.assertEqual(result[1:], messages[4:])
        archive = next(self.transcripts.glob("*.jsonl"))
        self.assertEqual([json.loads(line) for line in archive.read_text(encoding="utf-8").splitlines()], messages)


if __name__ == "__main__":
    unittest.main()
