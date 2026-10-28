"""核心 API 的工作区属于宿主上下文，不依赖 CLI 或修改模块全局变量。"""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from coding_agent.application import Application
from coding_agent.runtime import environment, workspace
from coding_agent.tools.skills import SkillLoader
from tests.test_application import FakeIO, ScriptedModel, dispatch, report_response, text_response, tool_call, tool_response


class WorkspaceContextTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.projects = [self.root / name for name in ("one", "two")]
        for project in self.projects:
            project.mkdir()
            (project / "app.py").write_text("value = 1\n", encoding="utf-8")

    def test_nested_host_scopes_restore_boundary_and_task_directory(self):
        original = workspace.workspace_root()
        first, second = self.projects
        task = first / "task"
        task.mkdir()
        with workspace.use_workspace(first), workspace.use_workdir(task):
            self.assertEqual(workspace.current_workdir(), task)
            with workspace.use_workspace(second):
                self.assertEqual(workspace.workspace_root(), second)
                self.assertEqual(workspace.current_workdir(), second)
                with self.assertRaises(ValueError):
                    workspace.current_workdir(first)
            self.assertEqual(workspace.current_workdir(), task)
            with self.assertRaises(ValueError):
                workspace.safe_path("../app.py")
        self.assertEqual(workspace.workspace_root(), original)

    def test_parallel_host_scopes_do_not_share_workspace_or_process_cwd(self):
        barrier = threading.Barrier(2)
        process_cwd = Path.cwd()

        def resolve(project):
            with workspace.use_workspace(project):
                barrier.wait(timeout=5)
                return workspace.safe_path("app.py"), workspace.workspace_root()

        with ThreadPoolExecutor(max_workers=2) as pool:
            actual = list(pool.map(resolve, self.projects))
        self.assertEqual(actual, [(path / "app.py", path) for path in self.projects])
        self.assertEqual(Path.cwd(), process_cwd)

    def test_core_applications_read_write_and_run_in_their_own_workspaces(self):
        original = workspace.WORKDIR
        local = environment.LocalEnvironment(environment.ExecutionConfig(timeout=5))
        config = {"model": "fake", "max_tokens": 1000}

        def wait_background(**kwargs):
            output = kwargs["messages"][-1]["content"][0]["content"]
            task_id = json.loads(output)["task_id"]
            return tool_response(tool_call("wait_process", {"task_id": task_id, "timeout": 5}, "wait-1"))

        #不 patch WORKDIR；两个 Application 直接通过构造参数获得不同的可信边界。
        with patch("coding_agent.application.get_mcp_config", return_value={}), \
                patch("coding_agent.integrations.mcp.get_mcp_config", return_value={}), \
                patch("coding_agent.application.get_environment", return_value=local), \
                patch.object(environment, "get_environment", return_value=local):
            for index, project in enumerate(self.projects, 2):
                with self.subTest(project=project):
                    model = ScriptedModel([
                        dispatch(),
                        tool_response(tool_call("read_file", {"path": "app.py"}, "read-1")),
                        tool_response(tool_call("edit_file", {
                            "path": "app.py", "old_text": "value = 1", "new_text": f"value = {index}",
                        }, "edit-1")),
                        tool_response(tool_call("bash", {"command": "pwd"}, "foreground-1")),
                        tool_response(tool_call("bash", {
                            "command": "pwd", "run_in_background": True,
                        }, "background-1")),
                        wait_background,
                        report_response(summary="已完成修改和命令检查。"),
                        text_response("完成；附带真实执行结果。"),
                    ])
                    app = Application(config, project, FakeIO(), client=Mock(), model_call=model, evaluator=Mock())
                    self.addCleanup(app.close)
                    app.prepare()
                    result = app.submit("修改 app.py 并检查目录")
                    self.assertEqual(result.status, "completed")
                    self.assertEqual((project / "app.py").read_text(encoding="utf-8"), f"value = {index}\n")
                    facts = app.last_context.verification_records()
                    self.assertEqual(len(facts), 2)
                    for record in facts:
                        self.assertEqual(record["status"], "completed")
                        self.assertEqual(record["exit_code"], 0)
                        self.assertEqual(record["output"], str(project))
                        self.assertIn(str(project / "app.py"), record["source_versions"])
                        output_path = Path(record["full_output_path"])
                        self.assertTrue(output_path.is_relative_to(project))
                        self.assertEqual(output_path.read_text(encoding="utf-8").strip(), str(project))
        self.assertEqual(workspace.WORKDIR, original)
        self.assertEqual(workspace.workspace_root(), original)

    def test_output_and_docker_mount_follow_host_scope_not_launch_workspace(self):
        docker = environment.DockerEnvironment(environment.ExecutionConfig(backend="docker"))
        for project in self.projects:
            with self.subTest(project=project), workspace.use_workspace(project):
                args = docker.command_args("pwd", project, "fake-container")
                self.assertIn(f"type=bind,source={project},target={project}", args)
                output = project / ".task_outputs" / "scope.log"
                with environment._open_output_file(output) as handle:
                    handle.write("own scope")
                other = next(path for path in self.projects if path != project)
                with self.assertRaises(ValueError):
                    environment._open_output_file(other / "foreign.log")

    def test_skill_catalog_and_loading_follow_each_core_application(self):
        for index, project in enumerate(self.projects):
            manifest = project / "skills" / f"project-{index}" / "SKILL.md"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(f"---\nname: project-{index}\ndescription: own project\n---\nRULE_{index}\n", encoding="utf-8")
        with patch("coding_agent.integrations.mcp.get_mcp_config", return_value={}):
            for index, project in enumerate(self.projects):
                model = ScriptedModel([
                    tool_response(tool_call("load_skill", {"name": f"project-{index}"}, "skill")),
                    text_response("已读取当前项目 Skill。"),
                ])
                app = Application({"model": "fake"}, project, FakeIO(), client=Mock(),
                                  model_call=model, evaluator=Mock())
                self.addCleanup(app.close)
                app.submit("读取项目约定")
                self.assertIn(f"project-{index}", model.calls[0]["system"])
                self.assertNotIn(f"project-{1 - index}", model.calls[0]["system"])
                output = model.calls[1]["messages"][-1]["content"][0]
                self.assertFalse(output["is_error"])
                self.assertIn(f"RULE_{index}", output["content"])

    def test_skills_directory_symlink_cannot_read_another_workspace(self):
        first, second = self.projects
        manifest = second / "skills" / "external" / "SKILL.md"
        manifest.parent.mkdir(parents=True)
        manifest.write_text("SECRET_OUTSIDE_RULE", encoding="utf-8")
        (first / "skills").symlink_to(second / "skills", target_is_directory=True)
        self.assertEqual(SkillLoader(first / "skills").catalog(), "(no skills found)")


if __name__ == "__main__":
    unittest.main()
