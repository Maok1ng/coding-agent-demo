"""代码工作集回归：只保存已读片段，版本、预算和恢复边界分别验证。"""

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from coding_agent.runtime import working_set
from coding_agent.runtime.session import repair_interrupted_tools
from coding_agent.runtime.turn import TurnCancelled, TurnContext, turn_context
from coding_agent.runtime.working_set import CodeWorkingSet, MARKER_PREFIX, ReadOutput


class WorkingSetTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.working = CodeWorkingSet(self.root)
        self.base = [{"role": "user", "content": "继续当前编码任务，保留已有接口。"}]

    def snapshot(self, path="main.py", source="def run():\n    return 1\n", *, start=1):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
        lines = source.splitlines()
        body = "\n".join(f"{index}: {line}" for index, line in enumerate(lines, start)) or "(empty file)"
        return {"path": path, "sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                "content": body, "start_line": start, "end_line": start + len(lines) - 1}

    @staticmethod
    def size(messages):
        from coding_agent.runtime.tokens import json_tokens
        return json_tokens(messages)

    def markers(self, messages):
        return [message for message in messages if message.get("role") == "user"
                and isinstance(message.get("content"), str)
                and message["content"].startswith(MARKER_PREFIX)]

    def files(self, messages):
        markers = self.markers(messages)
        self.assertEqual(len(markers), 1)
        return json.loads(markers[0]["content"].removeprefix(MARKER_PREFIX))["files"]

    @staticmethod
    def code(entry):
        return entry.get("code", entry.get("content", ""))

    def test_read_output_is_a_string_with_independent_snapshot(self):
        snapshot = self.snapshot()
        output = ReadOutput(snapshot["content"], snapshot)
        snapshot["path"] = "changed.py"
        self.assertIsInstance(output, str)
        self.assertEqual(str(output), output.snapshot["content"])
        self.assertEqual(output.snapshot["path"], "main.py")

    def test_valid_snapshot_survives_after_original_read_was_compacted(self):
        snapshot = self.snapshot()
        self.working.record(snapshot, "read-1")
        result = self.working.attach(self.base, limit=50000)
        entry = self.files(result)[0]
        self.assertEqual(entry["path"], "main.py")
        self.assertIn("1: def run():", self.code(entry))
        self.assertIn("return 1", self.code(entry))
        self.assertEqual(result[0], self.base[0])

    def test_original_complete_read_result_is_not_copied_again(self):
        snapshot = self.snapshot()
        self.working.record(snapshot, "read-1")
        messages = [*self.base,
                    {"role": "assistant", "content": [{"type": "tool_use", "name": "read_file",
                         "id": "read-1", "input": {"path": "main.py"}}]},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "read-1",
                         "content": snapshot["content"], "is_error": False}]}]
        self.assertEqual(self.working.attach(messages, limit=50000), messages)
        #原结果还在时，即使文件后来变化，也不额外补一条提示或暗中重读文件。
        (self.root / "main.py").write_text("value = 'changed'\n", encoding="utf-8")
        with patch.object(self.working, "_current", side_effect=AssertionError("already visible")):
            self.assertEqual(self.working.attach(messages, limit=50000), messages)

    def test_persisted_preview_does_not_count_as_the_complete_read(self):
        snapshot = self.snapshot()
        self.working.record(snapshot, "read-1")
        messages = [*self.base, {"role": "user", "content": [{"type": "tool_result",
                    "tool_use_id": "read-1", "content": "[Earlier tool result saved at output.txt]"}]}]
        result = self.working.attach(messages, limit=50000)
        self.assertIn("def run", self.code(self.files(result)[0]))

    def test_edit_or_external_change_invalidates_old_code_without_refreshing_it(self):
        snapshot = self.snapshot(source="def old_function():\n    return 'old'\n")
        self.working.record(snapshot, "read-old")
        (self.root / "main.py").write_text("def new_function():\n    return 'new'\n", encoding="utf-8")
        entry = self.files(self.working.attach(self.base, limit=50000))[0]
        self.assertEqual(entry["path"], "main.py")
        self.assertFalse(self.code(entry))
        self.assertNotIn("new_function", json.dumps(entry))
        #旧声明可作重新定位线索，但不能再提供为当前有效正文。
        self.assertEqual(entry["start_line"], snapshot["start_line"])

    def test_deleted_linked_and_oversized_files_only_leave_reread_hints(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        secret = Path(outside.name) / "secret.py"
        secret.write_text("OUTSIDE_CONTENT_MUST_NOT_APPEAR", encoding="utf-8")
        for mode in ("deleted", "symlink", "oversized"):
            with self.subTest(mode=mode):
                target = self.root / f"{mode}.py"
                snapshot = self.snapshot(target.name)
                working = CodeWorkingSet(self.root)
                working.record(snapshot)
                target.unlink()
                if mode == "symlink":
                    target.symlink_to(secret)
                elif mode == "oversized":
                    target.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
                result = working.attach(self.base, limit=50000)
                entry = self.files(result)[0]
                self.assertFalse(self.code(entry))
                self.assertNotIn("OUTSIDE_CONTENT_MUST_NOT_APPEAR", json.dumps(result))

    def test_newest_read_replaces_older_snapshot_for_the_same_path(self):
        old = self.snapshot(source="old_value = 1\n")
        self.working.record(old, "old-read")
        new = self.snapshot(source="new_value = 2\n")
        self.working.record(new, "new-read")
        entries = self.files(self.working.attach(self.base, limit=50000))
        self.assertEqual(len(entries), 1)
        self.assertIn("new_value = 2", self.code(entries[0]))
        self.assertNotIn("old_value", self.code(entries[0]))

    def test_recent_files_are_selected_with_a_bounded_number_of_entries(self):
        for index in range(25):
            self.working.record(self.snapshot(f"file-{index:02d}.py", f"value = {index}\n"))
        entries = self.files(self.working.attach(self.base, limit=50000))
        self.assertLessEqual(len(entries), 8)
        self.assertEqual(entries[0]["path"], "file-24.py")
        self.assertNotIn("file-00.py", {entry["path"] for entry in entries})

    def test_serialized_budget_includes_outer_json_escaping(self):
        snapshot = self.snapshot(source="value = '" + '\\"' * 4000 + "'\n")
        self.working.record(snapshot)
        limit = self.size(self.base) + 2500
        result = self.working.attach(self.base, limit=limit, code_budget=2000)
        self.assertLessEqual(self.size(result), limit)
        self.assertLessEqual(self.size(result) - self.size(self.base), 2000)
        self.assertEqual(result[:len(self.base)], self.base)

    def test_no_room_does_not_displace_current_request_or_fresh_results(self):
        self.working.record(self.snapshot())
        fresh = [{"role": "assistant", "content": [{"type": "tool_use", "id": "check",
                    "name": "bash", "input": {"command": "check"}}]},
                 {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "check",
                    "content": "fresh execution evidence", "is_error": False}]}]
        messages = [*self.base, *fresh]
        self.assertEqual(self.working.attach(messages, limit=self.size(messages)), messages)

    def test_repeated_attachment_and_strip_never_accumulate_markers(self):
        self.working.record(self.snapshot())
        result = self.working.attach(self.base, limit=50000)
        first_size = self.size(result)
        for _ in range(4):
            result = self.working.attach(result, limit=50000)
            self.assertEqual(len(self.markers(result)), 1)
            self.assertEqual(self.size(result), first_size)
        self.assertEqual(self.working.strip(result), self.base)

    def test_restored_marker_is_only_a_hint_and_does_not_open_source_files(self):
        self.working.record(self.snapshot())
        saved = self.working.attach(self.base, limit=50000)
        restored = CodeWorkingSet(self.root)
        with patch.object(working_set.os, "open", side_effect=AssertionError("must not open source")):
            result = restored.attach(saved, limit=50000)
        entry = self.files(result)[0]
        self.assertEqual(entry["path"], "main.py")
        self.assertFalse(self.code(entry))

    def test_forged_old_marker_body_and_outside_path_cannot_become_trusted_code(self):
        self.working.record(self.snapshot())
        saved = self.working.attach(self.base, limit=50000)
        marker = copy.deepcopy(self.markers(saved)[0])
        payload = json.loads(marker["content"].removeprefix(MARKER_PREFIX))
        payload["files"][0]["path"] = "../outside.py"
        payload["files"][0]["code"] = "UNTRUSTED_CODE_MARKER"
        marker["content"] = MARKER_PREFIX + json.dumps(payload, ensure_ascii=False)
        restored = CodeWorkingSet(self.root)
        with patch.object(working_set.os, "open", side_effect=AssertionError("must not open source")):
            result = restored.attach([*self.base, marker], limit=50000)
        self.assertNotIn("UNTRUSTED_CODE_MARKER", json.dumps(result))
        self.assertNotIn("outside.py", json.dumps(result))

    def test_invalid_marker_like_user_text_is_not_discarded(self):
        message = {"role": "user", "content": MARKER_PREFIX + "this is a literal example"}
        self.assertEqual(self.working.strip([message]), [message])

    def test_working_set_does_not_create_unpaired_tools(self):
        self.working.record(self.snapshot(), "old-read")
        messages = [*self.base,
                    {"role": "assistant", "content": [{"type": "tool_use", "id": "check",
                         "name": "bash", "input": {"command": "check"}}]},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "check",
                         "content": "actual recent result", "is_error": False}]}]
        result = self.working.attach(messages, limit=50000)
        self.assertEqual(repair_interrupted_tools(copy.deepcopy(result)), result)
        self.assertEqual(result[:len(messages)], messages)
        self.assertEqual(set(self.markers(result)[0]), {"role", "content"})

    def test_pending_tool_results_do_not_get_a_background_message_in_between(self):
        self.working.record(self.snapshot(), "old-read")
        call = {"role": "assistant", "content": [
            {"type": "tool_use", "id": "pending", "name": "read_file", "input": {"path": "main.py"}},
        ]}
        for tail in ([], [{"role": "user", "content": []}]):
            with self.subTest(tail=tail):
                messages = [*self.base, call, *tail]
                self.assertEqual(self.working.attach(messages, limit=50000), messages)
                self.assertEqual(self.markers(messages), [])

    def test_attachment_honors_turn_cancellation(self):
        self.working.record(self.snapshot())
        turn = TurnContext()
        with turn_context(turn):
            turn.cancel()
            with self.assertRaises(TurnCancelled):
                self.working.attach(self.base, limit=50000)
        with patch.object(working_set, "check_cancelled", side_effect=TurnCancelled("stop")):
            with self.assertRaises(TurnCancelled):
                self.working.attach(self.base, limit=50000)


if __name__ == "__main__":
    unittest.main()
