"""不调用模型：文件定位、读写前置条件和只读 Git 查询的行为回归。"""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from coding_agent.runtime import environment, workspace
from coding_agent.runtime.project_files import FileEntry, ScanResult
from coding_agent.runtime.turn import TurnCancelled, TurnContext, turn_context
from coding_agent.tools import files
from coding_agent.tools.files import FileTools, MAX_OUTPUT_CHARS


class FileToolsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        root_patch = patch.object(workspace, "WORKDIR", self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        self.tools = FileTools(self.root)

    def fixture(self, path, content):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def test_numbered_ranges_and_shared_read_versions(self):
        target = self.fixture("main.py", "one\ntwo\nthree\nfour\n")
        versions = {}
        first = FileTools(self.root, versions)
        self.assertEqual(first.read("main.py", start_line=2, end_line=3), "2: two\n3: three")
        self.assertIn(str(target), versions)
        second = FileTools(self.root, versions)
        diff = second.edit("main.py", "two", "updated")
        self.assertTrue(diff.startswith("Edited main.py"))
        self.assertIn("-two\n+updated", diff)
        self.assertIn("updated", target.read_text())
        self.assertIn("truncated", first.read("main.py", limit=1))

    def test_empty_file_and_invalid_line_ranges(self):
        self.fixture("empty.txt", "")
        self.assertEqual(self.tools.read("empty.txt"), "(empty file)")
        self.fixture("file.txt", "one\ntwo\n")
        for options in ({"start_line": 0}, {"start_line": True}, {"start_line": 4},
                        {"start_line": 2, "end_line": 1}, {"limit": 0}):
            with self.subTest(options=options):
                self.assertTrue(self.tools.read("file.txt", **options).startswith("Error:"))

    def test_existing_file_requires_read_and_detects_user_edits(self):
        target = self.fixture("main.py", "original")
        self.assertIn("read the existing file", self.tools.write("main.py", "overwrite"))
        self.tools.read("main.py")
        target.write_text("user edited", encoding="utf-8")
        self.assertIn("changed since", self.tools.edit("main.py", "original", "model edited"))
        self.assertEqual(target.read_text(), "user edited")
        self.tools.read("main.py")
        self.assertTrue(self.tools.edit("main.py", "user edited", "accepted").startswith("Edited main.py"))
        self.assertTrue(self.tools.edit("main.py", "accepted", "second edit").startswith("Edited main.py"))

    def test_atomic_write_rechecks_version_and_cleans_temporary_file(self):
        target = self.fixture("main.py", "original")
        self.tools.read("main.py")
        original = self.tools._contents
        calls = 0

        def change_during_save(path):
            nonlocal calls
            calls += 1
            if calls == 2:
                target.write_text("concurrent change", encoding="utf-8")
            return original(path)

        with patch.object(self.tools, "_contents", side_effect=change_during_save):
            self.assertIn("changed before writing", self.tools.write("main.py", "replacement"))
        self.assertEqual(target.read_text(), "concurrent change")
        self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def test_unique_edit_and_executable_permissions_are_preserved(self):
        target = self.fixture("script.sh", "same same")
        target.chmod(0o755)
        self.tools.read("script.sh")
        self.assertIn("Expected 1", self.tools.edit("script.sh", "same", "new"))
        self.assertIn("Expected 1", self.tools.edit("script.sh", "", "new"))
        self.assertTrue(self.tools.edit("script.sh", "same same", "new").startswith("Edited script.sh"))
        self.assertEqual(target.stat().st_mode & 0o777, 0o755)

    def test_new_files_and_deleted_read_file(self):
        self.assertTrue(self.tools.write("new/file.py", "你好").startswith("Wrote 6 bytes"))
        self.assertTrue(self.tools.edit("new/file.py", "你好", "done").startswith("Edited new/file.py"))
        self.tools.read("new/file.py")
        (self.root / "new/file.py").unlink()
        self.assertIn("disappeared", self.tools.write("new/file.py", "replacement"))

    def test_write_diff_covers_untracked_files_and_final_newline_changes(self):
        created = self.tools.write("new.py", "value = 1\n")
        self.assertIn("--- /dev/null\n+++ b/new.py", created)
        self.assertIn("+value = 1", created)
        changed = self.tools.write("new.py", "value = 2")
        self.assertIn("-value = 1\n+value = 2", changed)
        self.assertIn("No newline at end of file", changed)
        newline = self.tools.write("new.py", "value = 2\n")
        self.assertIn("-value = 2", newline)
        self.assertIn("+value = 2", newline)

    def test_prepared_changes_leave_files_directories_and_read_versions_untouched(self):
        target = self.fixture("main.py", "value = 1\n")
        self.tools.read("main.py")
        versions = dict(self.tools.versions)
        edited = self.tools.prepare_change("edit_file", path="main.py",
                                           old_text="value = 1", new_text="value = 2")
        self.assertEqual(target.read_text(), "value = 1\n")
        self.assertEqual(self.tools.versions, versions)
        self.assertIn("-value = 1\n+value = 2", edited.diff)
        self.assertEqual(edited.file_path, target)
        self.assertEqual(edited.expected, versions[str(target)])
        with self.assertRaises(AttributeError):
            edited.content = "unreviewed content"
        created = self.tools.prepare_change("write_file", path="new/file.py", content="你好\n")
        self.assertFalse((self.root / "new").exists())
        self.assertIsNone(created.expected)
        self.assertEqual(self.tools.versions, versions)
        self.assertEqual(self.tools.apply_change(edited), f"Edited main.py\n{edited.diff}")
        self.assertEqual(target.read_text(), edited.content)
        self.assertEqual(self.tools.apply_change(created), f"Wrote 7 bytes to new/file.py\n{created.diff}")
        self.assertEqual((self.root / "new/file.py").read_text(), created.content)

    def test_prepared_change_keeps_its_original_version_even_after_another_read(self):
        target = self.fixture("main.py", "original\n")
        self.tools.read("main.py")
        change = self.tools.prepare_change("write_file", path="main.py", content="agent change\n")
        target.write_text("user change\n", encoding="utf-8")
        self.tools.read("main.py")
        self.assertNotEqual(self.tools.versions[str(target)], change.expected)
        with self.assertRaisesRegex(ValueError, "changed before writing"):
            self.tools.apply_change(change)
        self.assertEqual(target.read_text(), "user change\n")
        self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def test_prepared_new_file_refuses_to_replace_a_concurrently_created_file(self):
        change = self.tools.prepare_change("write_file", path="new.py", content="agent\n")
        target = self.fixture("new.py", "user\n")
        self.tools.read("new.py")
        with self.assertRaisesRegex(ValueError, "created concurrently"):
            self.tools.apply_change(change)
        self.assertEqual(target.read_text(), "user\n")
        self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def test_prepared_change_rejects_a_symlink_retarget_even_with_identical_contents(self):
        first = self.fixture("first/main.py", "original\n")
        second = self.fixture("second/main.py", "original\n")
        for directory in (False, True):
            with self.subTest(directory=directory):
                link = self.root / "alias"
                link.symlink_to(first.parent if directory else first, target_is_directory=directory)
                path = "alias/main.py" if directory else "alias"
                self.tools.read(path)
                change = self.tools.prepare_change("write_file", path=path, content="agent\n")
                link.unlink()
                link.symlink_to(second.parent if directory else second, target_is_directory=directory)
                with self.assertRaisesRegex(ValueError, "target changed after preview"):
                    self.tools.apply_change(change)
                self.assertEqual(first.read_text(), "original\n")
                self.assertEqual(second.read_text(), "original\n")
                link.unlink()

    def test_preparation_rejects_invalid_operations_and_oversized_candidates_without_writing(self):
        with patch.object(files, "MAX_FILE_BYTES", 3):
            with self.assertRaisesRegex(ValueError, "content exceeds"):
                self.tools.prepare_change("write_file", path="new/large.py", content="你好")
        self.assertFalse((self.root / "new").exists())
        for name, arguments in (("bash", {"path": "x.py", "content": "x"}),
                                ("write_file", {"path": "x.py", "content": "x", "approved": True})):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "invalid file change"):
                self.tools.prepare_change(name, **arguments)
        self.assertEqual(self.tools.versions, {})

    def test_cancelled_preparation_or_application_cannot_create_candidate_directories(self):
        change = self.tools.prepare_change("write_file", path="new/file.py", content="agent\n")
        turn = TurnContext()
        with turn_context(turn):
            turn.cancel()
            with self.assertRaises(TurnCancelled):
                self.tools.prepare_change("write_file", path="other/file.py", content="agent\n")
            with self.assertRaises(TurnCancelled):
                self.tools.apply_change(change)
        self.assertFalse((self.root / "new").exists())
        self.assertFalse((self.root / "other").exists())
        self.assertEqual(self.tools.versions, {})

    def test_cancellation_during_final_version_check_prevents_replace_and_cleans_temporary_file(self):
        target = self.fixture("main.py", "original\n")
        self.tools.read("main.py")
        change = self.tools.prepare_change("write_file", path="main.py", content="agent\n")
        original = self.tools._contents
        turn = TurnContext()

        def cancel_after_check(path):
            content = original(path)
            turn.cancel()
            return content

        with turn_context(turn), patch.object(self.tools, "_contents", side_effect=cancel_after_check):
            with self.assertRaises(TurnCancelled):
                self.tools.apply_change(change)
        self.assertEqual(target.read_text(), "original\n")
        self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def test_path_escape_is_rejected_across_tools(self):
        (self.root / "outside").symlink_to(self.root.parent, target_is_directory=True)
        for path in ("../not-in-workspace", "outside/not-in-workspace"):
            with self.subTest(path=path):
                self.assertTrue(self.tools.read(path).startswith("Error:"))
                self.assertTrue(self.tools.write(path, "no").startswith("Error:"))
                self.assertTrue(self.tools.edit(path, "old", "new").startswith("Error:"))
                self.assertTrue(self.tools.list_files(path=path).startswith("Error:"))
                self.assertTrue(self.tools.search_code("secret", path=path).startswith("Error:"))
                self.assertTrue(self.tools.git_diff(path=path).startswith("Error:"))
        self.assertTrue(self.tools.glob("../*").startswith("Error:"))

    def test_literal_search_has_paths_line_numbers_context_and_exclusions(self):
        self.fixture("src/main.py", "before\nneedle.*$(echo no)\nafter\n")
        for folder in (".git", ".sessions", ".tasks", "node_modules", ".venv"):
            self.fixture(f"{folder}/hidden.txt", "needle.*$(echo no)")
        result = self.tools.search_code("needle.*$(echo no)", context_lines=1)
        self.assertIn("src/main.py:1: before", result)
        self.assertIn("src/main.py:2: needle.*$(echo no)", result)
        self.assertIn("src/main.py:3: after", result)
        self.assertNotIn("hidden.txt", result)

    def test_search_fallback_and_result_caps(self):
        self.fixture("file.py", "needle\nneedle\nneedle\n")
        self.fixture("node_modules/file.py", "needle")
        with patch("coding_agent.tools.files.shutil.which", return_value=None):
            result = self.tools.search_code("needle", context_lines=0, max_results=1)
        self.assertIn("file.py:1: needle", result)
        self.assertIn("truncated", result)
        self.assertNotIn("node_modules", result)
        if shutil.which("rg"):
            self.assertIn("truncated", self.tools.search_code("needle", context_lines=0, max_results=1))
        self.assertTrue(self.tools.search_code("needle", path="node_modules").startswith("Error:"))
        self.assertTrue(self.tools.search_code("", max_results=1).startswith("Error:"))

    def test_search_and_read_character_caps(self):
        self.fixture("long.py", "needle" + "x" * (MAX_OUTPUT_CHARS * 2))
        self.assertIn("truncated", self.tools.read("long.py"))
        self.assertIn("truncated", self.tools.search_code("needle"))

    def test_discovery_tools_and_search_backends_share_nested_ignore_rules(self):
        self.fixture(".gitignore", "git_hidden.py\n*.generated.py\n!keep.generated.py\n")
        self.fixture(".agentignore", "agent_hidden.py\n!git_hidden.py\n")
        self.fixture("src/.gitignore", "*.tmp.py\n!keep.tmp.py\n!restored.generated.py\n")
        self.fixture("src/.agentignore", "nested_private.py\n")
        visible = {"main.py", "keep.generated.py", "src/worker.py", "src/keep.tmp.py",
                   "src/restored.generated.py"}
        hidden = {"git_hidden.py", "agent_hidden.py", "other.generated.py", "src/drop.tmp.py",
                  "src/nested_private.py", "node_modules/dependency.py", ".transcripts/history.py"}
        for path in visible | hidden:
            self.fixture(path, "discovery_needle\n")

        self.assertEqual(set(self.tools.glob("**/*.py").splitlines()), visible)
        tree = self.tools.list_files(max_depth=4)
        for path in visible:
            self.assertIn(Path(path).name, tree)
        for path in hidden:
            self.assertNotIn(Path(path).name, tree)

        executable = shutil.which("rg")
        backends = [("python", None)] + ([("rg", executable)] if executable else [])
        for backend, selected in backends:
            with self.subTest(backend=backend), \
                    patch("coding_agent.tools.files.shutil.which", return_value=selected):
                result = self.tools.search_code("discovery_needle", context_lines=0)
                paths = {line.split(":", 1)[0] for line in result.splitlines()}
                self.assertEqual(paths, visible)

    def test_explicit_ignored_search_is_rejected_but_read_remains_available(self):
        self.fixture(".gitignore", "git_hidden.py\n")
        self.fixture(".agentignore", "agent_hidden.py\n")
        for path in ("git_hidden.py", "agent_hidden.py"):
            self.fixture(path, "known content\n")
            with self.subTest(path=path):
                self.assertTrue(self.tools.search_code("known", path=path).startswith("Error:"))
                self.assertEqual(self.tools.read(path), "1: known content")

    def test_glob_recursive_root_matching_and_fixed_prefix_scope(self):
        self.fixture("main.py", "root")
        self.fixture("src/worker.py", "worker")
        self.fixture("src/nested/helper.py", "helper")
        self.assertEqual(self.tools.glob("*.py"), "main.py")
        self.assertEqual(set(self.tools.glob("**/*.py").splitlines()),
                         {"main.py", "src/worker.py", "src/nested/helper.py"})
        #无关目录中的坏规则不应让带固定 src/ 前缀的查询失败。
        self.fixture("unrelated/.agentignore", "").write_bytes(b"\xff")
        self.fixture("unrelated/unused.py", "unused")
        self.assertEqual(self.tools.glob("*.py"), "main.py")
        self.assertEqual(self.tools.glob("main.py"), "main.py")
        self.assertEqual(set(self.tools.glob("src/**/*.py").splitlines()),
                         {"src/worker.py", "src/nested/helper.py"})

    def test_process_group_denial_is_confirmed_only_after_verified_exit(self):
        #EPERM 时以 poll 结果确认：已退出才算清理成功；仍在运行必须保留未确认状态。
        process = Mock(pid=123)
        process.poll.return_value = 0
        with patch.object(environment.os, "killpg", side_effect=PermissionError("exited group")):
            self.assertTrue(environment.stop_process_group(process))
        process.poll.return_value = None
        with patch.object(environment.os, "killpg", side_effect=PermissionError("still running")):
            self.assertFalse(environment.stop_process_group(process))

    def test_edit_retries_with_uniform_indentation_and_still_requires_uniqueness(self):
        self.fixture("block.py", "def run():\n    if ready:\n        go()\n")
        self.tools.read("block.py")
        #模型复制代码时丢掉统一缩进；命中后替换文本也加同样缩进。
        edited = self.tools.edit("block.py", "if ready:\n    go()", "if ready:\n    stop()")
        self.assertTrue(edited.startswith("Edited"))
        self.assertIn("    if ready:\n        stop()", (self.root / "block.py").read_text())
        #tab 缩进同样覆盖。
        self.fixture("tabs.py", "def run():\n\tif ready:\n\t\tgo()\n")
        self.tools.read("tabs.py")
        self.assertTrue(self.tools.edit("tabs.py", "if ready:\n\tgo()", "if ready:\n\tstop()").startswith("Edited"))
        #歧义与真实缺失仍然失败，不做缩进猜测。
        self.fixture("dup.py", "    a = 1\n    a = 1\n")
        self.tools.read("dup.py")
        self.assertTrue(self.tools.edit("dup.py", "a = 1", "b = 1").startswith("Error"))
        self.assertTrue(self.tools.edit("block.py", "no such line", "x").startswith("Error"))
        self.assertTrue(self.tools.edit("block.py", "", "x").startswith("Error"))

    def test_tree_depth_entry_limits_and_refresh_after_changes(self):
        self.fixture("a.py", "needle")
        self.fixture("b.py", "needle")
        self.fixture("src/deep.py", "needle")
        shallow = self.tools.list_files(max_depth=1)
        self.assertIn("src/", shallow)
        self.assertNotIn("deep.py", shallow)
        self.assertIn("truncated", shallow)
        self.assertIn("deep.py", self.tools.list_files(max_depth=2))
        limited = self.tools.list_files(max_entries=1)
        self.assertIn("a.py", limited)
        self.assertNotIn("b.py", limited)
        self.assertIn("truncated", limited)

        self.fixture("new.py", "needle")
        self.fixture(".agentignore", "/a.py\n")
        refreshed = self.tools.list_files()
        self.assertIn("new.py", refreshed)
        self.assertNotIn("a.py", refreshed)
        self.assertEqual(set(self.tools.glob("*.py").splitlines()), {"b.py", "new.py"})

    def test_tree_rejects_invalid_limits(self):
        for arguments in ({"max_depth": 0}, {"max_depth": True}, {"max_depth": 11},
                          {"max_entries": 0}, {"max_entries": True}, {"max_entries": 1001}):
            with self.subTest(arguments=arguments):
                self.assertTrue(self.tools.list_files(**arguments).startswith("Error:"))

    def test_incomplete_discovery_cannot_report_an_unqualified_absence(self):
        target = self.fixture("known.py", "ordinary content")
        #公共扫描结果明确不完整，消费它的工具不能把局部无结果当作全仓库无结果。
        listing = ScanResult([FileEntry(target, False)], truncated=True,
                             warnings=["file listing limit reached; narrow the path"])
        with patch.object(self.tools.project_files, "scan", return_value=listing):
            glob_result = self.tools.glob("*.missing")
            self.assertNotEqual(glob_result, "No files found")
            self.assertIn("truncated", glob_result)
            executable = shutil.which("rg")
            backends = [("python", None)] + ([("rg", executable)] if executable else [])
            for backend, selected in backends:
                with self.subTest(backend=backend), \
                        patch("coding_agent.tools.files.shutil.which", return_value=selected):
                    result = self.tools.search_code("unknown_symbol", context_lines=0)
                    self.assertNotEqual(result, "No matches found")
                    self.assertIn("truncated", result)
                    self.assertIn("narrow", result)

    def test_handlers_expose_only_fixed_read_git_interfaces(self):
        self.assertEqual(set(self.tools.handlers()), {"read_file", "read_files", "write_file", "edit_file", "glob", "search_many",
                                                   "list_files", "search_code", "git_status", "git_diff", "load_skill"})
        with self.assertRaises(TypeError):
            self.tools.git_status(command="reset --hard")

    @unittest.skipUnless(shutil.which("git"), "Git is required")
    def test_git_status_and_diff_are_workspace_scoped_and_no_textconv(self):
        def git(*args):
            return subprocess.run(["git", *args], cwd=self.root, check=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        git("init", "-q")
        git("config", "user.email", "test@example.invalid")
        git("config", "user.name", "Test")
        self.fixture("project/main.py", "old\n")
        self.fixture("outside.py", "outside\n")
        self.fixture("project/.gitattributes", "*.py diff=unsafe\n")
        git("add", ".")
        git("commit", "-qm", "initial")
        git("config", "diff.unsafe.textconv", "touch MUST_NOT_EXIST")
        git("config", "diff.external", "touch MUST_NOT_EXIST")
        self.fixture("project/main.py", "new\n")
        self.fixture("outside.py", "outside modified\n")
        nested = FileTools(self.root / "project")
        self.assertIn("main.py", nested.git_status())
        self.assertNotIn("outside.py", nested.git_status())
        result = nested.git_diff()
        self.assertIn("-old", result)
        self.assertIn("+new", result)
        self.assertNotIn("outside.py", result)
        self.assertFalse((self.root / "MUST_NOT_EXIST").exists())
        self.assertFalse((self.root / "project/MUST_NOT_EXIST").exists())
        git("add", "project/main.py")
        self.assertIn("+new", nested.git_diff("main.py", staged=True))
        self.assertTrue(nested.git_diff(staged="yes").startswith("Error:"))

    def test_git_without_repository_reports_error(self):
        with patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(self.root.parent)}):
            self.assertTrue(self.tools.git_status().startswith("Error:"))

    @unittest.skipUnless(shutil.which("git"), "Git is required")
    def test_git_disables_all_content_filters_without_losing_diff(self):
        def git(*args):
            return subprocess.run(["git", *args], cwd=self.root, check=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        git("init", "-q")
        git("config", "user.email", "test@example.invalid")
        git("config", "user.name", "Test")
        self.fixture("main.py", "old\n")
        self.fixture(".gitattributes", "*.py filter=unsafe\n")
        git("add", ".")
        git("commit", "-qm", "initial")
        self.fixture("main.py", "new\n")
        #这些故意带副作用的配置只存在于临时仓库；测试实现必须在 Git 有机会执行前禁用它们。
        git("config", "filter.unsafe.clean", "touch CLEAN_RAN; cat")
        git("config", "filter.unsafe.smudge", "touch SMUDGE_RAN; cat")
        git("config", "filter.unsafe.required", "true")
        for process_filter in (False, True):
            with self.subTest(process_filter=process_filter):
                if process_filter:
                    git("config", "filter.unsafe.process", "touch PROCESS_RAN; exit 1")
                diff = self.tools.git_diff()
                self.assertIn("-old", diff)
                self.assertIn("+new", diff)
                self.assertIn("main.py", self.tools.git_status())
                for marker in ("CLEAN_RAN", "SMUDGE_RAN", "PROCESS_RAN"):
                    self.assertFalse((self.root / marker).exists(), marker)

    def test_git_filter_config_queries_fail_closed_if_keys_are_unsafe_or_truncated(self):
        for raw, code, truncated in ((b"filter.bad=name.clean\x00", 0, False),
                                      (b"filter.unsafe.clean\x00", 0, True),
                                      (b"config failed", 128, False)):
            with self.subTest(raw=raw, code=code, truncated=truncated), \
                    patch.object(self.tools, "_git_process", return_value=(raw, code, truncated)) as run:
                result = self.tools.git_diff()
                self.assertTrue(result.startswith("Error:"))
                #异常查询之后，不能继续运行可能启动过滤器的实际 diff/status。
                run.assert_called_once()

    @unittest.skipUnless(shutil.which("git"), "Git is required")
    def test_git_filter_query_is_rechecked_for_every_read_only_query(self):
        def git(*args):
            return subprocess.run(["git", *args], cwd=self.root, check=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        def config_queries(run):
            return sum(1 for call in run.call_args_list if "config" in call.args[0])

        git("init", "-q")
        git("config", "user.email", "test@example.invalid")
        git("config", "user.name", "Test")
        self.fixture("main.py", "one\n")
        git("add", ".")
        git("commit", "-qm", "initial")
        self.fixture("main.py", "two\n")
        with patch.object(self.tools, "_git_process", wraps=self.tools._git_process) as run:
            self.tools.git_status()
            first = config_queries(run)
            self.assertGreaterEqual(first, 1)
            self.tools.git_status()
            self.assertEqual(config_queries(run), first + 1)
        #每次都重新检查实际生效配置，不能只靠 cwd 下某几个 config 文件的时间戳。
        git("config", "filter.late.clean", "cat")
        with patch.object(self.tools, "_git_process", wraps=self.tools._git_process) as run:
            self.tools.git_status()
            self.assertGreaterEqual(config_queries(run), 1)
        #查询失败不缓存：新实例连续失败时每次都重试，而不是沿用失效结果。
        fresh = FileTools(self.root)
        with patch.object(fresh, "_git_process", return_value=(b"boom", 128, False)) as run:
            self.assertTrue(fresh.git_status().startswith("Error:"))
            self.assertGreaterEqual(config_queries(run), 1)
            run.reset_mock()
            self.assertTrue(fresh.git_status().startswith("Error:"))
            self.assertGreaterEqual(config_queries(run), 1)

    @unittest.skipUnless(shutil.which("git"), "Git is required")
    def test_new_parent_repo_and_worktree_filters_cannot_run_from_read_only_queries(self):
        def git(*args):
            return subprocess.run(["git", *args], cwd=self.root, check=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        git("init", "-q")
        git("config", "user.email", "test@example.invalid")
        git("config", "user.name", "Test")
        self.fixture("project/main.py", "old\n")
        git("add", ".")
        git("commit", "-qm", "initial")
        worktree = self.root / "linked-worktree"
        git("worktree", "add", "-qb", "fixture-worktree", str(worktree))
        #父仓库的子目录与 worktree 的 .git 都不是 cwd/.git/config；两者必须读取真实配置。
        for directory in (self.root / "project", worktree / "project"):
            with self.subTest(directory=directory):
                nested = FileTools(directory)
                self.assertEqual(nested.git_diff(), "No changes")
                driver = "late_parent" if directory == self.root / "project" else "late_worktree"
                marker = self.root / f"{driver}_RAN"
                git("config", f"filter.{driver}.clean", f'touch "{marker}"; cat')
                (directory / ".gitattributes").write_text(f"main.py filter={driver}\n", encoding="utf-8")
                (directory / "main.py").write_text("new\n", encoding="utf-8")
                diff = nested.git_diff()
                self.assertIn("-old", diff)
                self.assertIn("+new", diff)
                self.assertIn("main.py", nested.git_status())
                self.assertFalse(marker.exists(), "read-only Git query executed the new content filter")

    def test_read_only_subprocess_is_cancelled_with_turn(self):
        turn = TurnContext()
        real_popen = subprocess.Popen
        children = []

        def slow_process(_args, **kwargs):
            child = real_popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
            children.append(child)
            return child

        timer = threading.Timer(0.05, turn.cancel)
        try:
            with turn_context(turn), patch("coding_agent.tools.files.subprocess.Popen", side_effect=slow_process):
                timer.start()
                with self.assertRaises(TurnCancelled):
                    self.tools.git_status()
        finally:
            timer.cancel()
            timer.join()
        self.assertIsNotNone(children[0].poll())

    def test_read_only_subprocess_cancellation_kills_descendants(self):
        turn = TurnContext()
        real_popen = subprocess.Popen
        started = threading.Event()
        children = []
        script = (
            "import pathlib, subprocess, sys, time; "
            "child = subprocess.Popen([sys.executable, '-c', "
            "\"import pathlib,time; time.sleep(0.4); pathlib.Path('DESCENDANT_RAN').touch()\"]); "
            "pathlib.Path('child.pid').write_text(str(child.pid)); time.sleep(30)"
        )

        def slow_process(_args, **kwargs):
            child = real_popen([sys.executable, "-c", script], **kwargs)
            children.append(child)
            started.set()
            return child

        def cancel_after_spawn():
            started.wait(timeout=2)
            #给父进程一点时间启动其子进程；之后以完成标记确认子进程没有继续执行。
            threading.Event().wait(0.1)
            turn.cancel()

        canceller = threading.Thread(target=cancel_after_spawn)
        canceller.start()
        try:
            with turn_context(turn), patch("coding_agent.tools.files.subprocess.Popen", side_effect=slow_process):
                with self.assertRaises(TurnCancelled):
                    self.tools.git_status()
        finally:
            canceller.join(timeout=3)
        threading.Event().wait(0.5)
        self.assertIsNotNone(children[0].poll())
        self.assertTrue((self.root / "child.pid").exists())
        self.assertFalse((self.root / "DESCENDANT_RAN").exists())


if __name__ == "__main__":
    unittest.main()
