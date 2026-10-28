"""系统设置进入 CLI、角色运行和摘要请求；不调用真实模型或读取用户设置。"""

from contextlib import nullcontext, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from coding_agent.application import Application
from coding_agent.cli import main, run_cli
from coding_agent.runtime import environment, workspace
from coding_agent.runtime.budget import RequestBudget
from coding_agent.runtime.compact import ContextCompact
from coding_agent.runtime.model_router import ModelRouter
from coding_agent.runtime.model_settings import ModelSettings
from coding_agent.runtime.session import SessionStore
from tests.test_application import FakeIO, ScriptedModel, dispatch, report_response, text_response


class ModelsIntegrationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.config = {"model": "legacy", "max_tokens": 1000, "context_token_limit": 16000,
                       "base_url": "https://legacy.example.invalid", "memory_enabled": False,
                       "memory_home": str(self.root / "memory-home")}
        for patcher in (
            patch.dict(os.environ),
            patch.object(workspace, "WORKDIR", self.root),
            patch.object(workspace, "CONFIG_PATH", self.root / "missing-default.json"),
            patch("coding_agent.integrations.mcp.get_mcp_config", return_value={}),
            patch.object(environment, "get_environment", return_value=environment.LocalEnvironment(
                environment.ExecutionConfig(timeout=5))),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ.pop("AGENT_MODELS_CONFIG", None)
        self.settings = ModelSettings(self.config, self.root / "host" / "models.json")
        self.host, self.io = Mock(), FakeIO()

    def app(self, responses, *, session=None):
        model = ScriptedModel(responses)
        app = Application(self.config, self.root, self.io, session=session, client=self.host,
                          model_call=model, evaluator=Mock(), model_settings=self.settings)
        self.addCleanup(app.close)
        return app, model

    def test_user_commands_persist_without_model_call_or_permission_goal_changes(self):
        app, model = self.app([])
        app.goal.set_goal("旧目标")
        app.goal.pause()
        app.submit("/plan")
        app.submit("/models add fast quick-model --max-tokens 500 --context-limit 24000")
        app.submit("/models assign codebase fast")
        app.submit("/models")
        self.assertEqual(model.calls, [])
        self.assertEqual(app.goal.active.status, "paused")
        self.assertEqual(app.plan.mode, "plan")
        self.assertEqual(app.approval_mode, "auto")
        self.assertEqual(self.settings.data["bindings"]["codebase"], "fast")
        self.assertTrue(self.settings.path.is_file())
        self.assertEqual(app.messages, [])

    def test_parent_child_models_and_budgets_use_one_turn_snapshot(self):
        self.settings.command("add first first-coder --max-tokens 2000 --context-limit 24000")
        self.settings.command("add second second-coder --max-tokens 3000 --context-limit 32000")
        self.settings.command("assign implementation first")

        def change_next_turn(**arguments):
            self.settings.command("assign implementation second")
            return dispatch()

        app, model = self.app([change_next_turn, report_response(), text_response("第一轮完成"),
                               dispatch(), report_response(), text_response("第二轮完成")])
        self.assertEqual(app.submit("检查实现").status, "completed")
        self.assertEqual(app.submit("再检查一次").status, "completed")
        self.assertEqual([call["model"] for call in model.calls],
                         ["legacy", "first-coder", "legacy", "legacy", "second-coder", "legacy"])
        self.assertEqual([call["max_tokens"] for call in model.calls], [1000, 2000, 1000, 1000, 3000, 1000])
        limits = [16000, 24000, 16000, 16000, 32000, 16000]
        for call, limit in zip(model.calls, limits):
            budget = RequestBudget.from_request(limit, call["system"], call["tools"], call["max_tokens"])
            self.assertLessEqual(budget.history_tokens(call["messages"]), budget.history_limit)

    def test_changing_default_in_cli_applies_to_next_real_turn(self):
        app, model = self.app([text_response("使用新模型")])
        app.submit("/models add general general-model --max-tokens 600 --context-limit 20000")
        app.submit("/models use general")
        self.assertEqual(model.calls, [])
        result = app.submit("解释项目入口")
        self.assertEqual(result.status, "completed")
        self.assertEqual(model.calls[0]["model"], "general-model")
        self.assertEqual(model.calls[0]["max_tokens"], 600)

    def test_compaction_uses_its_selected_model_client_and_independent_budget(self):
        self.settings.command("add summary summarizer --max-tokens 500 --context-limit 4000")
        self.settings.command("assign compaction summary")
        router = ModelRouter(self.host, self.config, self.settings.clone())
        compactor = ContextCompact(self.host, "legacy", self.root / ".transcripts",
                                   self.root / ".task_outputs" / "tool-results", token_limit=10000,
                                   summary_route=lambda: router.select("compaction", fallback=self.config))
        compactor.configure_budget("root prompt", [], 1000)
        current = {"role": "user", "content": "保留当前要求"}
        history = [current, *[{"role": "assistant", "content": "old context " * 1000} for _ in range(12)],
                   *[{"role": "assistant", "content": "recent"} for _ in range(5)]]
        response = SimpleNamespace(content=[SimpleNamespace(type="text", text="已归档旧上下文")])
        with patch("coding_agent.runtime.compact.call_model", return_value=response) as model:
            actual = compactor.prepare(history, current["content"])
        model.assert_called_once()
        self.assertIs(model.call_args.args[0], self.host)
        call = model.call_args.kwargs
        self.assertEqual((call["model"], call["max_tokens"]), ("summarizer", 500))
        budget = RequestBudget.from_request(4000, call["system"], [], 500)
        self.assertLessEqual(budget.history_tokens(call["messages"]), budget.history_limit)
        self.assertIn(current, actual)
        compactor.ensure_fit(actual)

    def test_resume_keeps_current_system_bindings_and_does_not_restore_old_model(self):
        store = SessionStore(self.root)
        session = store.open()
        session.save([{"role": "user", "content": "旧任务"},
                      {"role": "assistant", "content": "旧回答"}], {"model": "historic-model"})
        identity = session.id
        session.close()
        resumed = store.open(identity)
        self.addCleanup(resumed.close)
        self.settings.command("add current current-model")
        self.settings.command("use current")
        app, model = self.app([text_response("等待新任务后使用当前模型")], session=resumed)
        self.assertEqual(model.calls, [])
        app.submit("现在解释入口")
        self.assertEqual(model.calls[0]["model"], "current-model")
        self.assertNotIn("historic-model", self.settings.describe())
        app.close()

    def test_invalid_models_command_is_user_feedback_without_new_turn(self):
        app, model = self.app([])
        app.submit("/models assign unknown missing")
        self.assertEqual(model.calls, [])
        self.assertEqual(app.messages, [])
        self.assertTrue(any(event["type"] == "warning" for event in self.io.events))

    def test_cli_explains_missing_profile_credentials_without_provider_call(self):
        self.settings.command("add remote remote-model --api-key-env CODING_TEST_MISSING_KEY")
        self.settings.command("use remote")
        os.environ.pop("CODING_TEST_MISSING_KEY", None)
        app, model = self.app([])
        with (patch("coding_agent.runtime.terminal.read_user_input", side_effect=["解释入口", "exit"]),
              patch("coding_agent.runtime.terminal.monitor_interrupts", return_value=nullcontext(None))):
            run_cli(app)
        self.assertEqual(model.calls, [])
        warnings = [event["text"] for event in self.io.events if event["type"] == "warning"]
        self.assertTrue(any("[models]" in text and "环境变量未设置" in text for text in warnings))

    def test_standalone_cli_add_list_assign_work_without_model_config_or_credentials(self):
        settings_path = self.root / "standalone" / "models.json"
        arguments = [sys.executable, "-m", "coding_agent", "--workspace", str(self.root),
                     "--config", str(self.root / "missing.json"), "--models-config", str(settings_path), "--models"]
        env = {key: value for key, value in os.environ.items() if not key.startswith("AGENT_")}
        env["PYTHONPATH"] = str(workspace.APP_DIR)
        for command in (["add", "fast", "quick-model", "--max-tokens", "500", "--context-limit", "20000",
                         "--supports-images", "false"],
                        ["assign", "codebase", "fast"], ["list"]):
            result = subprocess.run([*arguments, *command], env=env, cwd=self.root,
                                    text=True, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("fast", result.stdout)
        self.assertIn("图片 不支持", result.stdout)
        self.assertEqual(json.loads(settings_path.read_text())["bindings"], {"codebase": "fast"})
        self.assertIs(json.loads(settings_path.read_text())["profiles"]["fast"]["supports_images"], False)
        self.assertFalse((self.root / ".sessions").exists())
        self.assertFalse((self.root / "missing.json").exists())

    def test_cli_explicit_settings_path_overrides_config_path_before_any_runtime_start(self):
        config_path = self.root / "legacy.json"
        config_path.write_text(json.dumps({**self.config, "models_config": str(self.root / "wrong.json")}),
                               encoding="utf-8")
        args = ["coding-agent", "--workspace", str(self.root), "--config", str(config_path),
                "--models-config", str(self.settings.path), "--models", "add", "fast", "quick-model"]
        with (patch.object(sys, "argv", args), patch("coding_agent.cli.run_cli") as running,
              patch("coding_agent.runtime.session.SessionStore") as session, redirect_stdout(io.StringIO())):
            main()
        running.assert_not_called()
        session.assert_not_called()
        self.assertTrue(self.settings.path.is_file())
        self.assertFalse((self.root / "wrong.json").exists())


if __name__ == "__main__":
    unittest.main()
