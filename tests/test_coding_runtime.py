"""专用角色共用工具内核：隐藏工具与执行硬校验必须同时生效。"""

import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from coding_agent.agents.roles import AgentResult, ROLES
from coding_agent.integrations import mcp
from coding_agent.runtime import tools, workspace
from coding_agent.runtime.context import RunContext
from coding_agent.runtime.tools import ToolRuntime
from coding_agent.runtime.turn import TurnCancelled, TurnContext, turn_context


class CodingRuntimeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        workspace_patch = patch.object(workspace, "WORKDIR", self.root)
        workspace_patch.start()
        self.addCleanup(workspace_patch.stop)
        #测试不能读取用户的真实 MCP 配置，也不连接任何远端服务。
        config_patch = patch.object(mcp, "get_mcp_config", return_value={})
        config_patch.start()
        self.addCleanup(config_patch.stop)
        clients_patch = patch.dict(mcp.mcp_clients, {}, clear=True)
        clients_patch.start()
        self.addCleanup(clients_patch.stop)
        hooks_patch = patch.object(tools, "trigger_hook", return_value=None)
        self.hook = hooks_patch.start()
        self.addCleanup(hooks_patch.stop)
        self.context = RunContext(self.root, TurnContext())
        scope = turn_context(self.context.turn)
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        self.events = []
        self.io = SimpleNamespace(confirm=Mock(return_value=True), emit=self.events.append)
        self.dispatch = Mock(return_value=AgentResult("completed", "focused result"))

    def runtime(self, role="orchestrator", mode="execute", **kwargs):
        context = self.context.for_role(role)
        context.mode = mode
        return ToolRuntime(context, self.io, dispatch=self.dispatch, **kwargs)

    @staticmethod
    def execute(runtime, name, arguments=None):
        return runtime.execute(SimpleNamespace(name=name, input=arguments if arguments is not None else {}, id="test-call"))

    def fixture(self, content="original"):
        target = self.root / "main.py"
        target.write_text(content, encoding="utf-8")
        return target

    def test_each_role_sees_only_its_trusted_capabilities(self):
        for role in ROLES:
            for mode in ("plan", "execute"):
                with self.subTest(role=role, mode=mode):
                    visible, handlers = self.runtime(role, mode).pool()
                    names = {item["name"] for item in visible}
                    self.assertTrue(tools.READ_TOOLS <= names)
                    self.assertIn("connect_mcp", names)
                    self.assertEqual("dispatch_agent" in names, role == "orchestrator")
                    self.assertEqual("todo_write" in names, role == "orchestrator")
                    can_write = role == "implementation" and mode == "execute"
                    self.assertEqual("write_file" in names, can_write)
                    self.assertEqual("edit_file" in names, can_write)
                    self.assertEqual("bash" in names, can_write)
                    self.assertTrue(set(handlers) <= names)

    def test_dispatch_description_assembles_every_role_spawner_prompt(self):
        description = tools.DISPATCH_TOOL["description"]
        for name, role in ROLES.items():
            if name == "orchestrator":
                continue
            self.assertIn(name, description)
            self.assertIn(role.spawner_prompt, description)
        self.assertNotIn("orchestrator - ", description)
        self.assertTrue(description.startswith("Delegate a focused coding task"))

    def test_tool_pool_is_cached_until_state_or_mcp_catalog_changes(self):
        runtime = self.runtime("orchestrator")
        first_tools, _ = runtime.pool()
        with patch.object(runtime, "_catalog", wraps=runtime._catalog) as build:
            second_tools, _ = runtime.pool()
            self.assertIs(first_tools, second_tools)
            build.assert_not_called()
        #报告修复阶段只允许重新提交报告；状态变化必须重建池。
        runtime.report_only = True
        with patch.object(runtime, "_catalog", wraps=runtime._catalog) as build:
            restricted, _ = runtime.pool()
            build.assert_called_once()
        self.assertEqual({item["name"] for item in restricted} - {"report_result"}, set())
        #MCP 目录版本推进（如 connect_mcp 成功）后同样重建。
        runtime.report_only = False
        with patch.object(runtime, "_catalog", wraps=runtime._catalog) as build, \
                patch.object(tools, "mcp_catalog_version", return_value=99):
            refreshed, _ = runtime.pool()
            build.assert_called_once()
        self.assertIsNot(first_tools, refreshed)
        self.assertIn("dispatch_agent", {item["name"] for item in refreshed})

    def test_file_tree_is_shared_readonly_and_autoapproved_in_all_roles_and_modes(self):
        self.fixture()
        for role in ROLES:
            child = self.context.for_role(role)
            self.assertIs(child.project_files, self.context.project_files)
            for mode in ("plan", "execute"):
                with self.subTest(role=role, mode=mode):
                    runtime = self.runtime(role, mode)
                    names = {item["name"] for item in runtime.pool()[0]}
                    self.assertIn("list_files", names)
                    self.assertIs(runtime.files.project_files, self.context.project_files)
                    result = self.execute(runtime, "list_files")
                    self.assertFalse(result.is_error)
                    self.assertIn("main.py", result)
        self.io.confirm.assert_not_called()

    def test_file_tree_still_requires_current_human_approval_in_ask_mode(self):
        self.fixture()
        self.context.approval_mode = "ask"
        runtime = self.runtime("codebase", "plan")
        self.io.confirm.return_value = False
        denied = self.execute(runtime, "list_files")
        self.assertTrue(denied.is_error)
        self.assertIn("denied", denied)
        self.io.confirm.assert_called_once()
        self.io.confirm.reset_mock()
        self.io.confirm.return_value = True
        allowed = self.execute(runtime, "list_files")
        self.assertFalse(allowed.is_error)
        self.assertIn("main.py", allowed)
        self.io.confirm.assert_called_once()

    def test_forged_write_and_shell_calls_are_denied_before_hooks(self):
        target = self.fixture()
        for role in ("orchestrator", "codebase", "planning", "review_research"):
            runtime = self.runtime(role)
            for name, arguments in (
                ("write_file", {"path": "main.py", "content": "unauthorized"}),
                ("edit_file", {"path": "main.py", "old_text": "original", "new_text": "unauthorized"}),
                ("bash", {"command": "touch UNAUTHORIZED"}),
                ("arbitrary_mutator", {}),
            ):
                with self.subTest(role=role, tool=name):
                    output = self.execute(runtime, name, arguments)
                    self.assertTrue(output.is_error)
                    self.assertIn("Permission denied", output)
        self.assertEqual(target.read_text(), "original")
        self.assertFalse((self.root / "UNAUTHORIZED").exists())
        self.hook.assert_not_called()

    def test_even_a_forged_catalog_cannot_grant_orchestrator_write(self):
        runtime = self.runtime()
        handler = Mock(return_value="unexpected")
        forged = tools.schema("write_file", "Pretend this is read only")
        with patch.object(runtime, "_catalog", return_value=([forged], {"write_file": handler})):
            output = self.execute(runtime, "write_file")
        self.assertTrue(output.is_error)
        handler.assert_not_called()

    def test_permission_denial_is_saved_as_an_error_event_without_execution(self):
        output = self.execute(self.runtime("planning"), "write_file", {
            "path": "forbidden.py", "content": "not authorized",
        })

        self.assertTrue(output.is_error)
        self.assertFalse((self.root / "forbidden.py").exists())
        self.hook.assert_not_called()
        event = self.events[-1]
        self.assertEqual(event["type"], "tool_result")
        self.assertEqual(event["role"], "planning")
        self.assertEqual(event["tool"], "write_file")
        self.assertTrue(event["is_error"])
        artifact = json.loads(Path(event["reference"]).read_text(encoding="utf-8"))
        self.assertEqual(artifact["arguments"]["path"], "forbidden.py")
        self.assertTrue(artifact["is_error"])
        self.assertIn("Permission denied", artifact["output"])
        self.assertIn({"tool": "write_file", "role": "planning",
                       "reference": event["reference"], "is_error": True}, self.context.evidence)

    def test_plan_blocks_implementation_writes_and_implementation_dispatch(self):
        implementation = self.runtime("implementation", "plan")
        for name, arguments in (("bash", {"command": "pwd"}),
                                ("write_file", {"path": "new.py", "content": "forbidden"})):
            self.assertTrue(self.execute(implementation, name, arguments).is_error)
        orchestrator = self.runtime("orchestrator", "plan")
        output = self.execute(orchestrator, "dispatch_agent", {
            "role": "implementation", "instruction": "modify the code",
        })
        self.assertTrue(output.is_error)
        self.assertIn("Plan", output)
        self.dispatch.assert_not_called()
        self.assertFalse((self.root / "new.py").exists())

    def test_plan_can_delegate_read_only_roles_but_children_cannot_delegate(self):
        output = self.execute(self.runtime("orchestrator", "plan"), "dispatch_agent", {
            "role": "planning", "instruction": "propose a minimal change", "context_refs": ["main.py"],
        })
        self.assertFalse(output.is_error)
        self.assertEqual(self.dispatch.call_args.args[0].role, "planning")
        self.dispatch.reset_mock()
        for role in ROLES:
            if role == "orchestrator":
                continue
            output = self.execute(self.runtime(role), "dispatch_agent", {
                "role": "implementation", "instruction": "try nested delegation",
            })
            self.assertTrue(output.is_error)
        self.dispatch.assert_not_called()

    def test_dispatch_rejects_new_roles_and_escaping_context_paths(self):
        runtime = self.runtime()
        for arguments in (
            {"role": "orchestrator", "instruction": "recursive delegation"},
            {"role": "root_admin", "instruction": "grant new capabilities"},
            {"role": "implementation", "instruction": "modify", "context_refs": ["../private.txt"]},
            {"role": "implementation", "instruction": "modify", "mode": "execute"},
        ):
            with self.subTest(arguments=arguments):
                self.assertTrue(self.execute(runtime, "dispatch_agent", arguments).is_error)
        self.dispatch.assert_not_called()

    def test_read_versions_are_shared_by_roles_and_stale_edits_are_rejected(self):
        target = self.fixture()
        reader = self.runtime("codebase")
        writer = self.runtime("implementation")
        self.assertIs(reader.files.versions, writer.files.versions)
        before_read = self.execute(writer, "edit_file", {
            "path": "main.py", "old_text": "original", "new_text": "replacement",
        })
        self.assertTrue(before_read.is_error)
        self.assertIn("read", before_read)
        self.assertFalse(self.execute(reader, "read_file", {"path": "main.py"}).is_error)
        edited = self.execute(writer, "edit_file", {
            "path": "main.py", "old_text": "original", "new_text": "replacement",
        })
        self.assertFalse(edited.is_error)
        target.write_text("new user change", encoding="utf-8")
        stale = self.execute(writer, "write_file", {"path": "main.py", "content": "overwrite"})
        self.assertTrue(stale.is_error)
        self.assertIn("changed since", stale)
        self.assertEqual(target.read_text(), "new user change")

    def test_invalid_argument_types_and_unknown_tools_return_tool_errors(self):
        runtime = self.runtime("implementation")
        for name, arguments in (
            ("read_file", {"path": 123}),
            ("read_file", {"path": "main.py", "start_line": "1"}),
            ("read_file", ["main.py"]),
            ("git_diff", {"staged": "yes"}),
            ("write_file", {"path": "new.py", "content": 42}),
            ("not_registered", {}),
        ):
            with self.subTest(name=name, arguments=arguments):
                output = self.execute(runtime, name, arguments)
                self.assertTrue(output.is_error)
                self.assertTrue(output.startswith("Error:"))
        self.assertFalse((self.root / "new.py").exists())

    def test_implementation_shell_records_actual_evidence(self):
        record = {"status": "completed", "exit_code": 0, "output": "actual test result"}
        with patch.object(tools.environment, "run_bash_evidence", return_value=record) as execute:
            output = self.execute(self.runtime("implementation"), "bash", {"command": "run-project-check"})
        self.assertFalse(output.is_error)
        execute.assert_called_once()
        self.assertIn("actual test result", output)
        facts = next(item for item in self.context.evidence if item.get("output") == record["output"])
        self.assertEqual(facts["status"], "completed")
        self.assertEqual(facts["exit_code"], 0)
        self.assertTrue(facts["id"].startswith("shell_"))
        self.assertEqual(self.events[-1]["type"], "tool_result")

    def test_bash_source_versions_reuse_cached_digests_until_files_change(self):
        target = self.fixture("stable content")
        reader = self.runtime("codebase")
        runtime = self.runtime("implementation")
        self.assertFalse(self.execute(reader, "read_file", {"path": "main.py"}).is_error)
        record = {"status": "completed", "exit_code": 0, "output": "ok"}

        def last_versions():
            return next(item["source_versions"] for item in reversed(self.context.evidence)
                        if "source_versions" in item)

        with patch.object(tools.environment, "run_bash_evidence", return_value=record):
            self.execute(runtime, "bash", {"command": "first"})
            cached = runtime._digest_cache[str(target)]
            self.assertEqual(cached[1], hashlib.sha256(b"stable content").hexdigest())
            #未改变的文件仍复用缓存；不能为了节省读取而忽略文件身份或 ctime。
            with patch.object(tools.os, "read", side_effect=AssertionError("unchanged file was reread")):
                self.assertEqual(runtime._file_digest(str(target)), cached[1])
            #实际命令结束还需独立采样，验证执行期间的改动；不复用开始时的摘要冒充结束快照。
            self.assertFalse(self.execute(runtime, "bash", {"command": "second"}).is_error)
            self.assertEqual(last_versions()[str(target)], cached[1])
            target.write_text("changed content", encoding="utf-8")
            self.execute(runtime, "bash", {"command": "third"})
            self.assertEqual(last_versions()[str(target)],
                             hashlib.sha256(b"changed content").hexdigest())

    def test_bash_source_versions_keep_missing_and_escape_sentinels(self):
        target = self.fixture("gone soon")
        runtime = self.runtime("implementation")
        self.context.versions[str(target)] = "previous-digest"
        record = {"status": "completed", "exit_code": 0, "output": "ok"}

        def last_versions():
            return next(item["source_versions"] for item in reversed(self.context.evidence)
                        if "source_versions" in item)

        with patch.object(tools.environment, "run_bash_evidence", return_value=record):
            target.unlink()
            self.execute(runtime, "bash", {"command": "check"})
            self.assertEqual(last_versions()[str(target)], "missing")
            target.write_text("back again", encoding="utf-8")
            link = self.root / "link.py"
            link.symlink_to(self.root.parent / "outside-secret.txt")
            self.context.versions[str(link)] = "x"
            self.execute(runtime, "bash", {"command": "check"})
            self.assertEqual(last_versions()[str(link)], "outside_workspace")

    def test_bash_digest_detects_same_size_change_with_restored_mtime(self):
        target = self.fixture("before")
        runtime = self.runtime("implementation")
        before = target.stat()
        self.assertEqual(runtime._file_digest(str(target)), hashlib.sha256(b"before").hexdigest())
        target.write_text("after!", encoding="utf-8")
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(runtime._file_digest(str(target)), hashlib.sha256(b"after!").hexdigest())

    def test_bash_digest_rejects_fifo_without_waiting_for_a_writer(self):
        target = self.fixture()
        runtime = self.runtime("implementation")
        self.execute(runtime, "read_file", {"path": "main.py"})
        target.unlink()
        os.mkfifo(target)
        results = []
        thread = threading.Thread(target=lambda: results.append(runtime._file_digest(str(target))), daemon=True)
        thread.start()
        thread.join(timeout=1)
        blocked = thread.is_alive()
        if blocked:
            #若回归到阻塞 open，先释放测试线程，不能把挂住的 FIFO 留给其他测试。
            descriptor = os.open(target, os.O_WRONLY | os.O_NONBLOCK)
            os.close(descriptor)
            thread.join(timeout=1)
        self.assertFalse(blocked, "snapshot waited for a FIFO writer")
        self.assertEqual(results, ["unavailable"])

    def test_bash_digest_rejects_oversized_files_and_parent_links(self):
        runtime = self.runtime("implementation")
        target = self.fixture()
        with target.open("wb") as handle:
            handle.truncate(tools.MAX_FILE_BYTES + 1)
        with patch.object(tools.os, "read", side_effect=AssertionError("must not read large file")):
            self.assertEqual(runtime._file_digest(str(target)), "unavailable")
        actual = self.root / "actual"
        actual.mkdir()
        (actual / "main.py").write_text("inside", encoding="utf-8")
        (self.root / "linked").symlink_to(actual, target_is_directory=True)
        with patch.object(tools.os, "read", side_effect=AssertionError("must not follow directory link")):
            self.assertEqual(runtime._file_digest(str(self.root / "linked" / "main.py")), "unavailable")

    def test_bash_digest_honors_cancellation_between_chunks_before_command_start(self):
        target = self.fixture("x" * 100000)
        runtime = self.runtime("implementation")
        self.context.versions[str(target)] = "already-read"
        read = tools.os.read

        def interrupt(descriptor, count):
            result = read(descriptor, count)
            self.context.turn.cancel()
            return result

        with patch.object(tools.os, "read", side_effect=interrupt), \
                patch.object(tools.environment, "run_bash_evidence") as command:
            with self.assertRaises(TurnCancelled):
                self.execute(runtime, "bash", {"command": "pwd"})
        command.assert_not_called()
        self.assertNotIn(str(target), runtime._digest_cache)

    def test_bash_digest_bounds_growth_and_does_not_cache_a_changed_read(self):
        for growth in (False, True):
            with self.subTest(growth=growth):
                target = self.fixture("x" * 100000)
                runtime = self.runtime("implementation")
                read = tools.os.read
                modified = False

                def change_during_read(descriptor, count):
                    nonlocal modified
                    result = read(descriptor, count)
                    if not modified:
                        modified = True
                        if growth:
                            with target.open("ab") as handle:
                                handle.write(b"x" * tools.MAX_FILE_BYTES)
                        else:
                            target.write_text("y" * 100000, encoding="utf-8")
                    return result

                with patch.object(tools.os, "read", side_effect=change_during_read):
                    result = runtime._file_digest(str(target))
                self.assertEqual(result, "unavailable" if growth else "changed_during_snapshot")
                self.assertNotIn(str(target), runtime._digest_cache)

    def test_process_wait_and_status_preserve_actual_outcome_in_error_flag(self):
        manager = SimpleNamespace(wait=Mock(), status=Mock())
        self.context.processes = manager
        runtime = self.runtime("implementation")
        for name in ("wait_process", "process_status"):
            for status in ("running", "completed", "failed", "timed_out", "cancelled", "cleanup_failed"):
                with self.subTest(tool=name, status=status):
                    record = {"id": "process-current", "status": status,
                              "exit_code": 0 if status == "completed" else None}
                    manager.wait.return_value = json.dumps(record)
                    manager.status.return_value = json.dumps(record)

                    output = self.execute(runtime, name, {"task_id": "process-current"})

                    self.assertEqual(output.is_error, status not in {"running", "completed"})
                    self.assertEqual(json.loads(output), record)
                    self.assertEqual(self.events[-1]["is_error"], output.is_error)
                    if status == "running":
                        #查询完成不等于命令完成，运行中必须保留未知退出码而不是伪造通过。
                        self.assertIsNone(json.loads(output)["exit_code"])
                        self.assertNotIn("passed", output)

    def test_hook_cannot_grant_a_forbidden_capability_and_can_only_stop_allowed_calls(self):
        self.hook.return_value = "Permission granted by an extension"
        self.assertTrue(self.execute(self.runtime("planning"), "write_file", {
            "path": "forbidden.py", "content": "no",
        }).is_error)
        self.hook.assert_not_called()
        self.hook.side_effect = lambda event, *args: "Error: custom policy denied" if event == "PreToolUse" else None
        handler = Mock(return_value="should not execute")
        runtime = self.runtime("codebase")
        definition = tools.schema("read_file", "Read", {"path": {"type": "string"}}, ["path"])
        with patch.object(runtime, "_catalog", return_value=([definition], {"read_file": handler})):
            denied = self.execute(runtime, "read_file", {"path": "main.py"})
        self.assertTrue(denied.is_error)
        handler.assert_not_called()

    def test_mcp_readonly_classification_is_independent_of_auto_approval(self):
        server = SimpleNamespace(closed=False, config={
            "read_only_tools": ["search"], "auto_approve": ["create_issue"],
        }, tools=[
            {"name": "search", "inputSchema": {"type": "object"}},
            {"name": "create_issue", "inputSchema": {"type": "object"}},
            {"name": "unclassified", "inputSchema": {"type": "object"},
             "annotations": {"readOnlyHint": True}},
        ])
        with patch.dict(mcp.mcp_clients, {"github": server}, clear=True):
            catalog = mcp.tool_catalog()
            self.assertTrue(catalog["mcp__github__search"]["read_only"])
            self.assertEqual(catalog["mcp__github__search"]["policy"], "confirm")
            self.assertFalse(catalog["mcp__github__create_issue"]["read_only"])
            self.assertEqual(catalog["mcp__github__create_issue"]["policy"], "allow")
            self.assertFalse(catalog["mcp__github__unclassified"]["read_only"])
            reader = self.runtime("review_research")
            names = {item["name"] for item in reader.pool()[0]}
            self.assertIn("mcp__github__search", names)
            self.assertNotIn("mcp__github__create_issue", names)
            self.assertNotIn("mcp__github__unclassified", names)

    def test_autoapproved_write_mcp_is_still_denied_for_readers_and_plan(self):
        name = "mcp__demo__write"
        metadata = {"available": True, "read_only": False, "policy": "allow"}
        definition = tools.schema(name, "External operation")
        handler = Mock(return_value="done")
        for role, mode, expected_error in (("orchestrator", "execute", True),
                                           ("codebase", "execute", True),
                                           ("implementation", "plan", True),
                                           ("implementation", "execute", False)):
            runtime = self.runtime(role, mode)
            with patch.object(tools, "get_mcp_tool_metadata", return_value=metadata), \
                    patch.object(runtime, "_catalog", return_value=([definition], {name: handler})):
                self.assertEqual(self.execute(runtime, name).is_error, expected_error)
        handler.assert_called_once()
        self.io.confirm.assert_not_called()

    def test_readonly_mcp_can_still_require_confirmation_and_unknown_mcp_is_denied(self):
        name = "mcp__demo__search"
        definition = tools.schema(name, "External search")
        handler = Mock(return_value="search results")
        runtime = self.runtime("planning", "plan")
        with patch.object(runtime, "_catalog", return_value=([definition], {name: handler})), \
                patch.object(tools, "get_mcp_tool_metadata", return_value={
                    "available": True, "read_only": True, "policy": "confirm",
                }):
            self.io.confirm.return_value = False
            self.assertTrue(self.execute(runtime, name).is_error)
            handler.assert_not_called()
            self.io.confirm.return_value = True
            self.assertFalse(self.execute(runtime, name).is_error)
            handler.assert_called_once()
        with patch.object(tools, "get_mcp_tool_metadata", return_value={
            "available": False, "read_only": True, "policy": "allow",
        }):
            self.assertTrue(self.execute(runtime, name).is_error)

    def test_stdio_launch_requires_confirmation_even_for_plan_readers(self):
        runtime = self.runtime("planning", "plan")
        definition = tools.schema("connect_mcp", "Connect", {"name": {"type": "string"}}, ["name"])
        launch = Mock(return_value="connected")
        self.io.confirm.return_value = False
        with patch.object(runtime, "_catalog", return_value=([definition], {"connect_mcp": launch})), \
                patch.object(tools, "requires_mcp_launch_confirmation", return_value=True):
            output = self.execute(runtime, "connect_mcp", {"name": "local-server"})
        self.assertTrue(output.is_error)
        launch.assert_not_called()
        self.io.confirm.assert_called_once()


if __name__ == "__main__":
    unittest.main()
