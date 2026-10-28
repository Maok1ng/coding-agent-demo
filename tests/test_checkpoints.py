"""代码检查点回归：真实临时文件与磁盘记录，不请求模型、不修改 Git。"""

from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import uuid

from coding_agent.runtime.checkpoints import CheckpointError, CheckpointStore
from coding_agent.runtime.workspace import use_workspace
from coding_agent.tools.files import FileTools


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        active_workspace = use_workspace(self.root)
        active_workspace.__enter__()
        self.addCleanup(active_workspace.__exit__, None, None, None)
        self.store = CheckpointStore(self.root, "a" * 32)
        self.files = FileTools(self.root)

    def recorder(self, label="修改代码"):
        return self.store.begin(uuid.uuid4().hex, label)

    def write(self, recorder, path, content, *, mark=True):
        if (self.root / path).exists():
            self.files.read(path)
        change = self.files.prepare_change("write_file", path=path, content=content)
        token = recorder.prepare(change)
        self.files.apply_change(change)
        if mark:
            recorder.applied(token)
        return token

    def commit(self, path, content, label="修改代码"):
        recorder = self.recorder(label)
        self.write(recorder, path, content)
        return recorder.finish("completed")

    def head(self):
        return next(item["id"] for item in self.store.list_checkpoints() if item["is_head"])

    def test_empty_store_is_lazy_and_has_no_undo(self):
        self.assertEqual(self.head(), "C0")
        self.assertFalse(self.store.root.exists())
        self.assertIsNone(self.recorder().finish("completed"))
        with self.assertRaisesRegex(CheckpointError, "没有可撤销"):
            self.store.plan_restore()

    def test_multi_edit_turn_keeps_original_user_content_and_one_checkpoint(self):
        (self.root / "app.py").write_text("user work\n")
        recorder = self.recorder("修复接口")
        self.write(recorder, "app.py", "first edit\n")
        self.write(recorder, "app.py", "second edit\n")
        self.assertEqual(recorder.finish("completed"), "C1")
        self.assertEqual(recorder.finish("cancelled"), "C1")
        plan = self.store.plan_restore()
        self.assertEqual((plan.from_id, plan.target_id), ("C1", "C0"))
        self.store.apply_restore(plan)
        self.assertEqual((self.root / "app.py").read_text(), "user work\n")

    def test_multiple_rounds_restore_exact_target_then_branch_and_keep_old_nodes(self):
        for content in ("one\n", "two\n", "three\n"):
            self.commit("app.py", content)
        self.store.apply_restore(self.store.plan_restore("C1"))
        self.assertEqual((self.root / "app.py").read_text(), "one\n")
        self.assertEqual(self.commit("app.py", "new branch\n"), "C4")
        nodes = self.store.list_checkpoints()
        self.assertEqual([item["id"] for item in nodes], ["C0", "C1", "C2", "C3", "C4"])
        self.assertEqual(nodes[-1]["parent"], "C1")
        self.store.apply_restore(self.store.plan_restore("C3"))
        self.assertEqual((self.root / "app.py").read_text(), "three\n")
        self.store.apply_restore(self.store.plan_restore("C4"))
        self.assertEqual((self.root / "app.py").read_text(), "new branch\n")

    def test_new_file_is_removed_and_can_be_restored_from_old_branch(self):
        self.commit("nested/new.py", "new\n")
        self.store.apply_restore(self.store.plan_restore())
        self.assertFalse((self.root / "nested/new.py").exists())
        self.store.apply_restore(self.store.plan_restore("C1"))
        self.assertEqual((self.root / "nested/new.py").read_text(), "new\n")

    def test_modes_and_exact_bytes_are_restored(self):
        path = self.root / "run.py"
        path.write_bytes(b"before\r\n")
        path.chmod(0o751)
        self.commit("run.py", "after\n")
        self.store.apply_restore(self.store.plan_restore())
        self.assertEqual(path.read_bytes(), b"before\r\n")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o751)

    def test_user_edit_between_rounds_becomes_explicit_baseline_and_undo_stops_there(self):
        self.commit("app.py", "first\n")
        (self.root / "app.py").write_text("manual\n")
        self.assertEqual(self.commit("app.py", "second\n"), "C3")
        self.assertEqual(self.store.list_checkpoints()[2]["kind"], "external")
        self.store.apply_restore(self.store.plan_restore())
        self.assertEqual(self.head(), "C2")
        self.assertEqual((self.root / "app.py").read_text(), "manual\n")
        with self.assertRaisesRegex(CheckpointError, "外部修改基线"):
            self.store.plan_restore()
        self.store.apply_restore(self.store.plan_restore("C1"))
        self.assertEqual((self.root / "app.py").read_text(), "first\n")

    def test_untracked_files_and_manual_modifications_are_never_silently_overwritten(self):
        recorder = self.recorder()
        self.write(recorder, "a.py", "agent a\n")
        self.write(recorder, "b.py", "agent b\n")
        recorder.finish("completed")
        (self.root / "notes.txt").write_text("untracked user notes")
        (self.root / "b.py").write_text("manual b\n")
        with self.assertRaisesRegex(CheckpointError, "手动修改"):
            self.store.plan_restore()
        self.assertEqual((self.root / "a.py").read_text(), "agent a\n")
        self.assertEqual((self.root / "notes.txt").read_text(), "untracked user notes")

    def test_confirmation_rechecks_every_file_before_any_write(self):
        recorder = self.recorder()
        self.write(recorder, "a.py", "a")
        self.write(recorder, "b.py", "b")
        recorder.finish("completed")
        plan = self.store.plan_restore()
        (self.root / "b.py").write_text("changed while confirming")
        with self.assertRaisesRegex(CheckpointError, "确认期间"):
            self.store.apply_restore(plan)
        self.assertEqual((self.root / "a.py").read_text(), "a")
        self.assertEqual(self.head(), "C1")

    def test_plan_is_frozen_and_display_changes_cannot_change_restore_target(self):
        self.commit("app.py", "new")
        plan = self.store.plan_restore()
        plan.changes[0]["path"] = "outside.py"
        with self.assertRaises(FrozenInstanceError):
            plan.target_id = "C99"
        self.store.apply_restore(plan)
        self.assertFalse((self.root / "app.py").exists())

    def test_new_checkpoint_invalidates_prior_restore_preview(self):
        self.commit("app.py", "one")
        plan = self.store.plan_restore()
        self.commit("app.py", "two")
        with self.assertRaisesRegex(CheckpointError, "预览后"):
            self.store.apply_restore(plan)
        self.assertEqual((self.root / "app.py").read_text(), "two")

    def test_zero_net_diff_path_is_still_rechecked_before_switching_head(self):
        for content in ("one", "two", "one"):
            self.commit("app.py", content)
        plan = self.store.plan_restore("C1")
        self.assertEqual(plan.changes, [])
        (self.root / "app.py").write_text("manual during confirmation")
        with self.assertRaisesRegex(CheckpointError, "确认期间"):
            self.store.apply_restore(plan)
        self.assertEqual(self.head(), "C3")
        self.assertEqual((self.root / "app.py").read_text(), "manual during confirmation")

    def test_edit_after_undo_to_c0_still_detects_manual_change_against_original_baseline(self):
        (self.root / "app.py").write_text("one")
        self.commit("app.py", "two")
        self.store.apply_restore(self.store.plan_restore())
        (self.root / "app.py").write_text("manual")
        self.assertEqual(self.commit("app.py", "three"), "C3")
        self.assertEqual(self.store.list_checkpoints()[2]["kind"], "external")
        self.store.apply_restore(self.store.plan_restore())
        self.assertEqual((self.root / "app.py").read_text(), "manual")
        self.assertEqual(self.head(), "C2")
        self.store.apply_restore(self.store.plan_restore("C1"))
        self.assertEqual((self.root / "app.py").read_text(), "two")

    def test_ancestor_without_file_transition_uses_baseline_from_other_branch(self):
        self.commit("a.py", "first")
        (self.root / "b.py").write_text("original b")
        self.commit("b.py", "agent b")
        self.store.apply_restore(self.store.plan_restore("C1"))
        (self.root / "b.py").write_text("manual b")
        self.assertEqual(self.commit("b.py", "new branch b"), "C4")
        self.assertEqual(self.store.list_checkpoints()[-2]["kind"], "external")
        self.store.apply_restore(self.store.plan_restore("C2"))
        self.assertEqual((self.root / "b.py").read_text(), "agent b")

    def test_interrupted_write_before_mark_is_reconciled_without_replaying_file_tool(self):
        recorder = self.recorder()
        self.write(recorder, "app.py", "applied", mark=False)
        restored = CheckpointStore(self.root, "a" * 32)
        with patch.object(self.files, "apply_change", side_effect=AssertionError("must not replay")):
            nodes = restored.list_checkpoints()
        self.assertEqual(nodes[-1]["status"], "interrupted")
        self.assertEqual((self.root / "app.py").read_text(), "applied")
        restored.apply_restore(restored.plan_restore())
        self.assertFalse((self.root / "app.py").exists())

    def test_pending_write_that_never_happened_does_not_create_checkpoint(self):
        change = self.files.prepare_change("write_file", path="app.py", content="not applied")
        recorder = self.recorder()
        recorder.prepare(change)
        self.assertIsNone(recorder.finish("cancelled"))
        self.assertEqual(self.head(), "C0")
        self.assertFalse((self.root / "app.py").exists())

    def test_failed_unapplied_write_can_be_followed_by_another_edit(self):
        change = self.files.prepare_change("write_file", path="app.py", content="not applied")
        recorder = self.recorder()
        token = recorder.prepare(change)
        recorder.failed(token)
        self.write(recorder, "app.py", "successful retry")
        self.assertEqual(recorder.finish("completed"), "C1")
        self.store.apply_restore(self.store.plan_restore())
        self.assertFalse((self.root / "app.py").exists())

    def test_unknown_change_after_write_is_recorded_and_cannot_be_auto_restored(self):
        recorder = self.recorder()
        self.write(recorder, "app.py", "agent")
        (self.root / "app.py").write_text("outside writer")
        recorder.finish("cancelled")
        self.assertEqual(self.store.list_checkpoints()[-1]["status"], "uncertain")
        with self.assertRaisesRegex(CheckpointError, "结果未知"):
            self.store.plan_restore()

    def test_external_write_during_same_turn_refuses_next_tracked_write(self):
        recorder = self.recorder()
        self.write(recorder, "app.py", "agent")
        (self.root / "app.py").write_text("outside writer")
        self.files.read("app.py")
        change = self.files.prepare_change("write_file", path="app.py", content="later agent")
        with self.assertRaisesRegex(CheckpointError, "本轮被外部"):
            recorder.prepare(change)
        self.assertEqual((self.root / "app.py").read_text(), "outside writer")

    def test_restore_failure_rolls_back_already_written_files_and_keeps_head(self):
        recorder = self.recorder()
        self.write(recorder, "a.py", "a")
        self.write(recorder, "b.py", "b")
        recorder.finish("completed")
        plan = self.store.plan_restore()
        original = self.store._write_snapshot
        calls = []

        def fail_second(path, target, expected):
            calls.append(path)
            if len(calls) == 2:
                raise OSError("disk failure")
            return original(path, target, expected)

        with patch.object(self.store, "_write_snapshot", side_effect=fail_second):
            with self.assertRaises(CheckpointError):
                self.store.apply_restore(plan)
        self.assertEqual((self.root / "a.py").read_text(), "a")
        self.assertEqual((self.root / "b.py").read_text(), "b")
        self.assertEqual(self.head(), "C1")

    def test_unrecoverable_partial_restore_locks_following_restore(self):
        recorder = self.recorder()
        self.write(recorder, "a.py", "a")
        self.write(recorder, "b.py", "b")
        recorder.finish("completed")
        plan = self.store.plan_restore()
        original = self.store._write_snapshot
        calls = []

        def fail_after_first(path, target, expected):
            calls.append(path)
            if len(calls) > 1:
                raise OSError("persistent disk failure")
            return original(path, target, expected)

        with patch.object(self.store, "_write_snapshot", side_effect=fail_after_first):
            with self.assertRaisesRegex(CheckpointError, "锁定自动恢复"):
                self.store.apply_restore(plan)
        with self.assertRaisesRegex(CheckpointError, "部分文件状态未知"):
            self.store.plan_restore()

    def test_final_index_replace_then_sync_error_does_not_roll_files_back_under_target_head(self):
        (self.root / "app.py").write_text("original")
        self.commit("app.py", "agent")
        plan = self.store.plan_restore()
        original_write = self.store._write
        committed = False

        def uncertain_commit(parent, name, data):
            nonlocal committed
            if name == "index.json" and data["head"] == "C0" and not data["restore_pending"]:
                original_write(parent, name, data)
                committed = True
                raise OSError("replace succeeded; fsync failed")
            if committed and name == "index.json":
                raise OSError("a rollback index write would fail before replacement")
            return original_write(parent, name, data)

        with patch.object(self.store, "_write", side_effect=uncertain_commit):
            with self.assertRaisesRegex(CheckpointError, "恢复文件已写入.*同步未确认"):
                self.store.apply_restore(plan)
        self.assertTrue(committed)
        self.assertEqual((self.root / "app.py").read_text(), "original")
        self.assertEqual(self.head(), "C0")

    def test_final_index_failure_before_replace_keeps_target_files_for_read_only_reconciliation(self):
        (self.root / "app.py").write_text("original")
        self.commit("app.py", "agent")
        plan = self.store.plan_restore()
        original_write = self.store._write

        def fail_commit(parent, name, data):
            if name == "index.json" and data["head"] == "C0" and not data["restore_pending"]:
                raise OSError("final index replacement failed")
            return original_write(parent, name, data)

        with patch.object(self.store, "_write", side_effect=fail_commit):
            with self.assertRaisesRegex(CheckpointError, "恢复文件已写入"):
                self.store.apply_restore(plan)
        self.assertEqual((self.root / "app.py").read_text(), "original")
        index = json.loads((self.store.root / "index.json").read_text())
        self.assertTrue(index["restore_pending"])
        self.assertEqual(index["head"], "C1")
        with patch.object(self.store, "_write_snapshot", side_effect=AssertionError("must not rewrite files")):
            self.assertEqual(self.head(), "C0")

    def test_corrupt_next_id_cannot_overwrite_existing_checkpoint(self):
        self.commit("app.py", "one")
        self.commit("app.py", "two")
        checkpoint = (self.store.root / "C1.json").read_bytes()
        index_path = self.store.root / "index.json"
        index = json.loads(index_path.read_text())
        index["next_id"] = 1
        index_path.write_text(json.dumps(index))
        with self.assertRaisesRegex(CheckpointError, "编号计数损坏"):
            self.store.list_checkpoints()
        with self.assertRaisesRegex(CheckpointError, "编号计数损坏"):
            self.commit("app.py", "three")
        self.assertEqual((self.store.root / "C1.json").read_bytes(), checkpoint)
        self.assertEqual((self.root / "app.py").read_text(), "two")

    def test_same_content_round_creates_no_empty_checkpoint(self):
        (self.root / "app.py").write_text("same")
        self.assertIsNone(self.commit("app.py", "same"))
        self.assertEqual(self.head(), "C0")

    def test_state_and_snapshot_files_are_private(self):
        self.commit("app.py", "secret")
        self.assertEqual(stat.S_IMODE(self.store.root.stat().st_mode), 0o700)
        for path in self.store.root.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_malformed_identifiers_and_host_paths_are_rejected(self):
        for identifier in ("../outside", "", "C01", "C-1", 1):
            with self.subTest(identifier=identifier), self.assertRaises(CheckpointError):
                self.store.plan_restore(identifier)
        recorder = self.recorder()
        for path in (".sessions/new.json", ".task_outputs/checkpoints/fake", ".git/config"):
            change = self.files.prepare_change("write_file", path=path, content="bad")
            with self.subTest(path=path), self.assertRaises(CheckpointError):
                recorder.prepare(change)

    def test_storage_symlink_and_target_symlink_are_rejected(self):
        with tempfile.TemporaryDirectory() as outside:
            (self.root / ".task_outputs").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(CheckpointError):
                self.commit("app.py", "secret")
            (self.root / ".task_outputs").unlink()
        self.commit("app.py", "tracked")
        (self.root / "app.py").unlink()
        (self.root / "other.py").write_text("user")
        (self.root / "app.py").symlink_to(self.root / "other.py")
        with self.assertRaises(CheckpointError):
            self.store.plan_restore()
        self.assertEqual((self.root / "other.py").read_text(), "user")

    def test_corrupt_snapshot_hash_and_cycle_are_rejected(self):
        self.commit("app.py", "one")
        path = self.store.root / "C1.json"
        node = json.loads(path.read_text())
        node["files"]["app.py"]["after"]["sha256"] = "0" * 64
        path.write_text(json.dumps(node))
        with self.assertRaisesRegex(CheckpointError, "校验失败"):
            self.store.plan_restore()
        index_path = self.store.root / "index.json"
        index = json.loads(index_path.read_text())
        index["nodes"]["C1"]["parent"] = "C1"
        index_path.write_text(json.dumps(index))
        with self.assertRaisesRegex(CheckpointError, "版本链损坏"):
            self.store.list_checkpoints()


if __name__ == "__main__":
    unittest.main()
