"""共用文件发现规则：不依赖 Git 或 rg，验证忽略语义、边界和有界扫描。"""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from coding_agent.runtime import project_files
from coding_agent.runtime.project_files import ProjectFiles
from coding_agent.runtime.turn import TurnCancelled, TurnContext, turn_context


class ProjectFilesTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.project = ProjectFiles(self.root)

    def fixture(self, path, content="source"):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def names(self, result):
        return {entry.path.relative_to(self.root).as_posix() for entry in result.entries}

    def test_root_patterns_anchoring_and_negation(self):
        self.fixture(".gitignore", "*.tmp\n!keep.tmp\n/root-only.py\n")
        for name in ("drop.tmp", "keep.tmp", "root-only.py", "sub/drop.tmp", "sub/root-only.py", "main.py"):
            self.fixture(name)
        result = self.project.scan()
        self.assertTrue({"keep.tmp", "sub/root-only.py", "main.py"} <= self.names(result))
        self.assertFalse({"drop.tmp", "root-only.py", "sub/drop.tmp"} & self.names(result))
        self.assertFalse(result.truncated)

    def test_nested_patterns_override_matching_parent_file_rules(self):
        self.fixture(".gitignore", "*.log\n")
        self.fixture("sub/.gitignore", "!keep.log\n/local.py\n")
        for name in ("sub/keep.log", "sub/drop.log", "sub/local.py", "sub/deep/local.py", "other/keep.log"):
            self.fixture(name)
        names = self.names(self.project.scan())
        self.assertTrue({"sub/keep.log", "sub/deep/local.py"} <= names)
        self.assertFalse({"sub/drop.log", "sub/local.py", "other/keep.log"} & names)
        #从子目录开始搜索也必须继承工作区根规则。
        narrowed = self.names(self.project.scan(self.root / "sub"))
        self.assertIn("sub/keep.log", narrowed)
        self.assertNotIn("sub/drop.log", narrowed)

    def test_excluded_parent_cannot_be_revived_by_child_negation(self):
        self.fixture(".gitignore", "generated/\n!generated/keep.py\n")
        self.fixture("generated/.gitignore", "!keep.py\n")
        target = self.fixture("generated/keep.py")
        self.assertNotIn("generated", self.names(self.project.scan()))
        self.assertTrue(self.project.is_ignored(target))
        self.assertEqual(self.project.scan(target).entries, [])

    def test_agent_rules_are_independent_and_cannot_reopen_git_or_hard_exclusions(self):
        self.fixture(".gitignore", "git-secret.py\n")
        self.fixture(".agentignore", "!git-secret.py\nprivate/\n!node_modules/\n!node_modules/tool.py\n")
        for name in ("git-secret.py", "private/secret.py", "node_modules/tool.py", ".transcripts/old.json", "main.py"):
            self.fixture(name)
        names = self.names(self.project.scan())
        self.assertIn("main.py", names)
        self.assertFalse({"git-secret.py", "private", "node_modules", ".transcripts"} & names)

    def test_nested_agent_negation_is_local_to_its_directory(self):
        self.fixture(".agentignore", "*.private\n")
        self.fixture("src/.agentignore", "!public.private\n")
        self.fixture("src/public.private")
        self.fixture("other/public.private")
        names = self.names(self.project.scan())
        self.assertIn("src/public.private", names)
        self.assertNotIn("other/public.private", names)

    def test_files_and_directories_have_different_directory_pattern_behavior(self):
        self.fixture(".gitignore", "cache/\n")
        self.fixture("cache/file.py")
        self.fixture("other/cache", "a regular file")
        names = self.names(self.project.scan())
        self.assertNotIn("cache", names)
        self.assertIn("other/cache", names)

    def test_file_target_returns_only_it_and_directory_target_excludes_itself(self):
        target = self.fixture("src/main.py")
        self.assertEqual(self.project.scan(target).files, [target])
        self.assertEqual(self.names(self.project.scan(target.parent)), {"src/main.py"})
        self.assertTrue(all(entry.path.is_absolute() for entry in self.project.scan().entries))

    def test_links_outside_paths_and_special_files_are_never_discovered(self):
        target = self.fixture("src/main.py")
        (self.root / "file-link.py").symlink_to(target)
        (self.root / "directory-link").symlink_to(target.parent, target_is_directory=True)
        (self.root / "outside-link").symlink_to(self.root.parent, target_is_directory=True)
        if hasattr(os, "mkfifo"):
            os.mkfifo(self.root / "pipe")
        self.assertEqual(self.project.scan().files, [target])
        for path in (self.root / "file-link.py", self.root / "directory-link/main.py", self.root.parent):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.project.scan(path)
        with self.assertRaises(ValueError):
            self.project.is_ignored(self.root.parent / "secret")

    def test_every_call_refreshes_rules_and_new_files(self):
        target = self.fixture("main.py")
        self.assertIn(target, self.project.scan().files)
        rule = self.fixture(".gitignore", "main.py\n")
        self.assertNotIn(target, self.project.scan().files)
        rule.write_text("", encoding="utf-8")
        added = self.fixture("added.py")
        self.assertTrue({target, added} <= set(self.project.scan().files))

    def test_depth_and_entry_limits_are_explicit(self):
        self.fixture("src/nested/main.py")
        shallow = self.project.scan(max_depth=1)
        self.assertEqual(self.names(shallow), {"src"})
        self.assertTrue(shallow.truncated)
        self.assertTrue(any("depth" in item for item in shallow.warnings))
        self.fixture("a.py")
        self.fixture("b.py")
        small = self.project.scan(max_entries=1)
        self.assertEqual(len(small.entries), 1)
        self.assertTrue(small.truncated)
        for arguments in ({"max_depth": 0}, {"max_depth": True}, {"max_entries": 0}, {"max_entries": 10001}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                self.project.scan(**arguments)

    def test_scanned_entry_budget_counts_ignored_items(self):
        self.fixture(".gitignore", "*.tmp\n")
        for index in range(10):
            self.fixture(f"ignored-{index}.tmp")
        with patch.object(project_files, "MAX_SCAN_ENTRIES", 3):
            result = self.project.scan()
        self.assertTrue(result.truncated)
        self.assertTrue(any("entry scan limit" in warning for warning in result.warnings))

    def test_directory_budget_is_bounded(self):
        self.fixture("src/main.py")
        with patch.object(project_files, "MAX_SCAN_DIRECTORIES", 1):
            result = self.project.scan()
        self.assertTrue(result.truncated)
        self.assertTrue(any("directory scan limit" in warning for warning in result.warnings))

    def test_unreadable_directory_marks_partial_results(self):
        self.fixture("open/main.py")
        self.fixture("closed/private.py")
        original = project_files._directory

        def open_directory(root, path):
            if path == self.root / "closed":
                raise PermissionError("denied")
            return original(root, path)

        with patch.object(project_files, "_directory", side_effect=open_directory):
            result = self.project.scan()
        self.assertTrue(result.truncated)
        self.assertIn("open/main.py", self.names(result))
        self.assertNotIn("closed/private.py", self.names(result))
        self.assertTrue(any("unreadable" in warning for warning in result.warnings))

    def test_unsafe_or_invalid_rule_files_fail_closed(self):
        self.fixture("main.py")
        rule = self.root / ".agentignore"
        cases = (b"x" * (project_files.MAX_IGNORE_BYTES + 1), b"\xff")
        for content in cases:
            with self.subTest(content_length=len(content)):
                rule.write_bytes(content)
                with self.assertRaisesRegex(ValueError, "cannot safely read ignore rules"):
                    self.project.scan()
        rule.unlink()
        rule.symlink_to(self.root / "main.py")
        with self.assertRaisesRegex(ValueError, "cannot safely read ignore rules"):
            self.project.scan()
        rule.unlink()
        if hasattr(os, "mkfifo"):
            os.mkfifo(rule)
            with self.assertRaisesRegex(ValueError, "cannot safely read ignore rules"):
                self.project.scan()

    def test_scan_honors_cancellation_before_and_during_traversal(self):
        self.fixture("src/main.py")
        turn = TurnContext()
        with turn_context(turn):
            turn.cancel()
            with self.assertRaises(TurnCancelled):
                self.project.scan()
        with patch.object(project_files, "check_cancelled", side_effect=[None, None, TurnCancelled("stop")]):
            with self.assertRaises(TurnCancelled):
                self.project.scan()


if __name__ == "__main__":
    unittest.main()
