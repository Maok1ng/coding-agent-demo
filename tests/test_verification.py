"""最终检查报告只整理真实命令与当前文件摘要，不使用模型的成功声明。"""

import hashlib
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from coding_agent.runtime import verification
from coding_agent.runtime.turn import TurnCancelled, TurnContext, turn_context
from coding_agent.runtime.verification import (
    build_verification_summary, capture_source_versions, format_verification_summary,
)


class VerificationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.records = []
        self.changes = {}
        self.context = SimpleNamespace(workspace=self.root, changes=self.changes,
                                       verification_records=lambda: self.records)

    def fixture(self, path="main.py", content="value = 1\n"):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target, hashlib.sha256(target.read_bytes()).hexdigest()

    def command(self, *, identifier="command-1", status="completed", code=0, versions=None, **extra):
        record = {"id": identifier, "command": "python -m unittest", "status": status,
                  "exit_code": code, "cleanup_confirmed": True, "source_versions": versions or {},
                  **extra}
        self.records.append(record)
        return record

    def changed(self, target, digest):
        self.changes[str(target)] = {"path": target.relative_to(self.root).as_posix(),
                                    "before_sha256": None, "after_sha256": digest,
                                    "reference": "change.json"}

    def test_empty_answer_does_not_add_report(self):
        summary = build_verification_summary(self.context)
        self.assertEqual(summary["final_status"], "not_run")
        self.assertEqual(format_verification_summary(summary), "")

    def test_file_modification_without_commands_is_explicitly_unverified(self):
        target, digest = self.fixture()
        self.changed(target, digest)
        summary = build_verification_summary(self.context)
        self.assertEqual(summary["final_status"], "not_run")
        self.assertEqual(summary["changes"][0]["current_sha256"], digest)
        self.assertIn("未运行检查命令", format_verification_summary(summary))

    def test_real_success_with_matching_file_version_is_recorded_without_test_claim(self):
        target, digest = self.fixture()
        self.changed(target, digest)
        self.command(versions={str(target): digest}, full_output_path="result.log")
        summary = build_verification_summary(self.context)
        self.assertEqual(summary["final_status"], "recorded")
        self.assertEqual(summary["commands"][0]["outcome"], "successful")
        self.assertEqual(summary["commands"][0]["freshness"], "current")
        rendered = format_verification_summary(summary)
        self.assertIn("执行成功", rendered)
        self.assertNotIn("测试全部通过", rendered)
        self.assertIn("不证明运行了测试", rendered)

    def test_exit_zero_cannot_override_failure_timeout_cancel_or_cleanup_status(self):
        for status, flags, expected in (
            ("failed", {}, "failed"),
            ("timed_out", {}, "timed_out"),
            ("completed", {"timed_out": True}, "timed_out"),
            ("cancelled", {}, "cancelled"),
            ("completed", {"cancelled": True}, "cancelled"),
            ("running", {}, "running"),
            ("cleanup_failed", {}, "incomplete"),
            ("completed", {"cleanup_confirmed": False}, "incomplete"),
            ("completed", {"exit_code": None}, "failed"),
            ("completed", {"exit_code": False}, "failed"),
        ):
            with self.subTest(status=status, flags=flags):
                self.records.clear()
                self.command(status=status, **flags)
                command = build_verification_summary(self.context)["commands"][0]
                self.assertEqual(command["outcome"], expected)

    def test_changed_or_deleted_code_makes_prior_success_stale(self):
        for operation in ("edit", "delete"):
            with self.subTest(operation=operation):
                self.records.clear()
                target, digest = self.fixture()
                self.changed(target, digest)
                self.command(versions={str(target): digest})
                if operation == "edit":
                    target.write_text("value = 2\n", encoding="utf-8")
                else:
                    target.unlink()
                summary = build_verification_summary(self.context)
                self.assertEqual(summary["final_status"], "stale")
                self.assertEqual(summary["commands"][0]["freshness"], "stale")
                self.assertEqual(summary["commands"][0]["stale_paths"], ["main.py"])

    def test_file_added_after_command_cannot_borrow_older_check(self):
        first, first_hash = self.fixture()
        self.command(versions={str(first): first_hash})
        new, new_hash = self.fixture("new.py")
        self.changed(new, new_hash)
        summary = build_verification_summary(self.context)
        self.assertEqual(summary["final_status"], "stale")
        self.assertIn("new.py", summary["commands"][0]["stale_paths"])

    def test_newer_check_can_cover_current_code_but_old_facts_remain(self):
        target, before = self.fixture()
        self.command(identifier="older", versions={str(target): before})
        target, after = self.fixture(content="value = 2\n")
        self.changed(target, after)
        self.command(identifier="newer", versions={str(target): after})
        summary = build_verification_summary(self.context)
        self.assertEqual(summary["final_status"], "recorded")
        self.assertEqual([item["freshness"] for item in summary["commands"]], ["stale", "current"])
        #失败后的修复重跑仍保留失败记录，不能把历史改成全成功。
        self.records[0].update(status="failed", exit_code=7)
        summary = build_verification_summary(self.context)
        self.assertEqual(summary["final_status"], "recorded")
        self.assertEqual(summary["commands"][0]["outcome"], "failed")
        self.assertTrue(summary["commands"][0]["recovered"])
        self.assertEqual(summary["commands"][0]["superseded_by"], "newer")
        self.assertEqual(summary["historical_failure_count"], 1)
        self.assertEqual(summary["recovered_failure_count"], 1)
        self.assertIn("先前失败，后续同命令已重跑", format_verification_summary(summary))

    def test_latest_stale_or_unverified_success_does_not_recover_old_failure(self):
        for freshness in ("stale", "unverified"):
            with self.subTest(freshness=freshness):
                self.records.clear()
                target, digest = self.fixture()
                self.command(identifier="failed", status="failed", code=1, versions={str(target): digest})
                versions = {str(target): "f" * 64} if freshness == "stale" else {}
                self.command(identifier="rerun", versions=versions)
                summary = build_verification_summary(self.context)
                self.assertEqual(summary["final_status"], "failed")
                self.assertFalse(summary["commands"][0]["recovered"])

    def test_latest_failed_attempt_is_not_hidden_by_earlier_success(self):
        target, digest = self.fixture()
        self.command(identifier="passed", versions={str(target): digest})
        self.command(identifier="failed", status="timed_out", code=0, versions={str(target): digest})
        summary = build_verification_summary(self.context)
        self.assertEqual(summary["final_status"], "failed")
        self.assertEqual(summary["recovered_failure_count"], 0)

    def test_different_commands_do_not_recover_failures_even_when_display_prefix_matches(self):
        target, digest = self.fixture()
        self.command(identifier="failed", status="failed", code=1, versions={str(target): digest},
                     command="x" * 2000 + " first")
        self.command(identifier="different", versions={str(target): digest}, command="x" * 2000 + " second")
        summary = build_verification_summary(self.context)
        self.assertEqual(summary["commands"][0]["command"], summary["commands"][1]["command"])
        self.assertEqual(summary["final_status"], "failed")
        self.assertEqual(summary["latest_command_count"], 2)
        self.assertFalse(summary["commands"][0]["recovered"])

    def test_cleanup_cancel_and_running_facts_are_not_recovered_by_reruns(self):
        target, digest = self.fixture()
        for status in ("cleanup_failed", "cancelled", "running"):
            with self.subTest(status=status):
                self.records.clear()
                self.command(identifier="unsafe", status=status, code=None, versions={str(target): digest})
                self.command(identifier="rerun", versions={str(target): digest})
                summary = build_verification_summary(self.context)
                self.assertEqual(summary["final_status"], "incomplete")
                self.assertFalse(summary["commands"][0]["recovered"])

    def test_recovered_omitted_failure_is_still_counted_and_explained(self):
        target, digest = self.fixture()
        self.command(identifier="older", status="failed", code=1, versions={str(target): digest})
        for index in range(verification.MAX_COMMANDS + 2):
            self.command(identifier=str(index), versions={str(target): digest})
        summary = build_verification_summary(self.context)
        self.assertEqual(summary["final_status"], "recorded")
        self.assertEqual(summary["historical_failure_count"], 1)
        self.assertEqual(summary["recovered_failure_count"], 1)
        self.assertFalse(any(item["outcome"] == "failed" for item in summary["commands"]))
        self.assertIn("记录中有 1 次失败或超时", format_verification_summary(summary))

    def test_changed_during_command_is_stale_even_when_restored_before_final_report(self):
        target, before = self.fixture()
        self.command(versions={str(target): before},
                     source_versions_after={str(target): "f" * 64})
        summary = build_verification_summary(self.context)
        self.assertEqual(summary["commands"][0]["freshness"], "stale")

    def test_missing_versions_or_unavailable_snapshot_is_unverified(self):
        self.command()
        summary = build_verification_summary(self.context)
        self.assertEqual(summary["commands"][0]["freshness"], "unverified")
        self.assertEqual(summary["final_status"], "recorded")
        target, digest = self.fixture()
        self.records.clear()
        self.command(versions={str(target): digest})
        with patch.object(verification, "_snapshot", return_value="changed_during_snapshot"):
            summary = build_verification_summary(self.context)
        self.assertEqual(summary["commands"][0]["freshness"], "unverified")

    def test_duplicate_background_records_keep_final_state_and_initial_versions(self):
        target, digest = self.fixture()
        self.command(identifier="background", status="running", code=None, versions={str(target): digest})
        self.records.append({"id": "background", "command": "python -m unittest", "status": "completed",
                             "exit_code": 0, "full_output_path": "background.log"})
        summary = build_verification_summary(self.context)
        self.assertEqual(summary["command_count"], 1)
        self.assertEqual(summary["commands"][0]["outcome"], "successful")
        self.assertEqual(summary["commands"][0]["freshness"], "current")
        self.assertEqual(summary["commands"][0]["full_output_path"], "background.log")

    def test_command_recovery_uses_host_start_order_instead_of_background_completion_order(self):
        target, digest = self.fixture()
        #先启动的后台失败最后才领到结果，但后启动的当前版本重跑已成功。
        self.command(identifier="foreground-rerun", versions={str(target): digest}, execution_order=5)
        self.command(identifier="background-older", status="failed", code=1,
                     versions={str(target): digest}, execution_order=2)
        summary = build_verification_summary(self.context)
        self.assertEqual(summary["final_status"], "recorded")
        self.assertEqual([item["execution_order"] for item in summary["commands"]], [2, 5])
        self.assertTrue(summary["commands"][0]["recovered"])
        self.assertEqual(summary["commands"][0]["superseded_by"], "foreground-rerun")

    def test_capture_versions_is_public_bounded_and_never_follows_invalid_paths(self):
        target, digest = self.fixture()
        result = capture_source_versions(self.root, [target, "main.py", "missing.py", "../outside", None])
        self.assertEqual(result, {str(target): digest, str(self.root / "missing.py"): "missing"})
        (self.root / "linked.py").symlink_to(target)
        self.assertEqual(capture_source_versions(self.root, ["linked.py"]),
                         {str(self.root / "linked.py"): "unavailable"})
        with patch.object(verification, "MAX_TRACKED_FILES", 1):
            result = capture_source_versions(self.root, [target, "second.py"])
        self.assertEqual(result[str(self.root / "second.py")], "budget_limited")

    def test_unidentified_foreground_commands_are_not_accidentally_deduplicated(self):
        for _ in range(2):
            self.command(identifier=None)
        self.assertEqual(build_verification_summary(self.context)["command_count"], 2)

    def test_report_bounds_commands_but_aggregate_still_includes_omitted_failure(self):
        self.command(identifier="old-failure", status="failed", code=1)
        for index in range(verification.MAX_COMMANDS + 5):
            self.command(identifier=str(index))
        summary = build_verification_summary(self.context)
        self.assertEqual(len(summary["commands"]), verification.MAX_COMMANDS)
        self.assertEqual(summary["command_count"], verification.MAX_COMMANDS + 6)
        self.assertEqual(summary["omitted_commands"], 6)
        self.assertEqual(summary["final_status"], "failed")
        self.assertIn("另有 6 条", format_verification_summary(summary))
        self.assertIn("存在未被有效重跑解除的执行失败或超时", format_verification_summary(summary))

    def test_snapshot_limits_files_and_bytes_without_claiming_freshness(self):
        first, first_hash = self.fixture()
        second, second_hash = self.fixture("other.py")
        self.command(versions={str(first): first_hash, str(second): second_hash})
        with patch.object(verification, "MAX_TRACKED_FILES", 1):
            summary = build_verification_summary(self.context)
        self.assertEqual(summary["commands"][0]["freshness"], "unverified")
        self.assertIn("other.py", summary["commands"][0]["unverified_paths"])
        with patch.object(verification, "MAX_SNAPSHOT_BYTES", 1):
            summary = build_verification_summary(self.context)
        self.assertEqual(summary["commands"][0]["freshness"], "unverified")
        self.assertTrue(any("预算" in notice for notice in summary["notices"]))

    def test_safe_snapshots_reject_symlinks_fifo_and_outside_paths(self):
        target, digest = self.fixture()
        (self.root / "linked.py").symlink_to(target)
        os.mkfifo(self.root / "pipe.py")
        self.command(versions={str(self.root / "linked.py"): digest,
                               str(self.root / "pipe.py"): digest,
                               "../outside": digest, str(self.root.parent / "outside"): digest})
        summary = build_verification_summary(self.context)
        self.assertEqual(summary["commands"][0]["freshness"], "unverified")
        self.assertEqual(summary["commands"][0]["unverified_paths"], ["linked.py", "pipe.py"])

    def test_cancelled_report_does_not_read_files(self):
        turn = TurnContext()
        with turn_context(turn):
            turn.cancel()
            with patch.object(verification, "_snapshot") as snapshot, self.assertRaises(TurnCancelled):
                build_verification_summary(self.context)
            snapshot.assert_not_called()

    def test_terminal_report_sanitizes_commands_paths_and_limits_long_commands(self):
        target, digest = self.fixture()
        self.changed(target, digest)
        self.changes[str(target)]["path"] = "unsafe\033[2J\npath"
        self.command(command="pytest\033[2J\n" + "x" * 3000)
        summary = build_verification_summary(self.context)
        self.assertEqual(len(summary["commands"][0]["command"]), 2000)
        self.assertTrue(summary["commands"][0]["command_truncated"])
        rendered = format_verification_summary(summary)
        self.assertNotIn("\033", rendered)
        self.assertLess(len(rendered), 1000)


if __name__ == "__main__":
    unittest.main()
