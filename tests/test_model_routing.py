"""模型路由只使用假客户端、临时记忆目录和假环境凭据，不请求模型服务。"""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from coding_agent.runtime.budget import ContextBudgetError
from coding_agent.runtime.context import RunContext
from coding_agent.runtime.goal import PromptGoalEvaluator
from coding_agent.runtime.memory import MemoryManager
from coding_agent.runtime.model_router import ModelRouter, ModelSelection, bounded_auxiliary_request
from coding_agent.runtime.model_settings import ModelSettings
from coding_agent.runtime.turn import TurnContext, turn_context
from tests.test_application import text_response


class ModelRouterTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict("os.environ", {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.config = {"model": "legacy", "api_key": "legacy-private-key",
                       "base_url": "https://original.invalid", "max_tokens": 8000,
                       "context_token_limit": 16000}
        self.host = Mock()

    def settings(self, profile, task="implementation"):
        settings = ModelSettings(self.config)
        settings.data = {"version": 1, "default": None,
                         "profiles": {"chosen": profile}, "bindings": {task: "chosen"}}
        return settings

    def test_legacy_and_same_endpoint_model_override_reuse_injected_host(self):
        models = ModelRouter(self.host, self.config, self.settings({"model": "writer"}))
        with patch("coding_agent.runtime.model_router.Anthropic") as factory:
            selected = models.select("implementation")
            self.assertIs(selected.client, self.host)
            self.assertEqual(selected.config["model"], "writer")
            self.assertEqual(selected.profile, "chosen")
            self.assertEqual(models.select("planning").config["model"], "legacy")
            models.close()
        factory.assert_not_called()
        self.host.close.assert_not_called()

    def test_independent_endpoint_uses_only_explicit_env_and_reuses_owned_host(self):
        settings = self.settings({"model": "remote", "base_url": "https://second.invalid",
                                  "api_key_env": "CODING_TEST_SECOND_KEY"})
        owned = Mock()
        models = ModelRouter(self.host, self.config, settings)
        with patch.dict("os.environ", {"CODING_TEST_SECOND_KEY": "new-private-key"}), \
                patch("coding_agent.runtime.model_router.Anthropic", return_value=owned) as factory:
            first = models.select("implementation")
            second = models.select("implementation")
        factory.assert_called_once_with(auth_token="new-private-key", base_url="https://second.invalid")
        self.assertIs(first.client, second.client)
        self.assertNotIn("api_key", first.config)
        self.assertNotIn("new-private-key", repr(first))
        self.assertNotIn("legacy-private-key", repr(first))
        models.close()
        owned.close.assert_called_once()
        self.host.close.assert_not_called()
        models.close()
        owned.close.assert_called_once()

    def test_missing_profile_env_never_falls_back_to_legacy_credentials(self):
        models = ModelRouter(self.host, self.config, self.settings({
            "model": "remote", "api_key_env": "CODING_TEST_MISSING_KEY"}))
        with patch.dict("os.environ", {}, clear=True), \
                patch("coding_agent.runtime.model_router.Anthropic") as factory:
            with self.assertRaisesRegex(ValueError, "环境变量未设置"):
                models.select("implementation")
        factory.assert_not_called()
        self.host.messages.create.assert_not_called()

    def test_endpoint_changed_without_explicit_env_is_rejected_even_for_fallback(self):
        models = ModelRouter(self.host, self.config)
        with patch("coding_agent.runtime.model_router.Anthropic") as factory:
            with self.assertRaisesRegex(ValueError, "不能继承旧端点密钥"):
                models.select("compaction", fallback={**self.config, "base_url": "https://new.invalid"})
        factory.assert_not_called()

    def test_lazy_profile_only_setup_does_not_require_old_model_or_key(self):
        settings = ModelSettings({})
        settings.data = {"version": 1, "default": "only", "profiles": {"only": {
            "model": "new-model", "api_key_env": "CODING_TEST_ONLY_KEY"}}, "bindings": {}}
        with patch("coding_agent.runtime.model_router.Anthropic") as factory:
            models = ModelRouter(None, {}, settings)
            factory.assert_not_called()
            with patch.dict("os.environ", {"CODING_TEST_ONLY_KEY": "profile-only-key"}):
                selected = models.select("orchestrator")
            self.assertEqual(selected.config["model"], "new-model")
            factory.assert_called_once_with(auth_token="profile-only-key", base_url="https://api.anthropic.com")
            models.close()

    def test_lazy_legacy_client_is_created_once_and_closed_by_router(self):
        with patch("coding_agent.runtime.model_router.Anthropic") as factory:
            models = ModelRouter(None, self.config)
            factory.assert_not_called()
            first = models.select("orchestrator")
            second = models.select("implementation")
            self.assertIs(first.client, second.client)
            factory.assert_called_once_with(auth_token="legacy-private-key", base_url="https://original.invalid")
            models.close()
            factory.return_value.close.assert_called_once()

    def test_credential_change_creates_a_separate_owned_connection(self):
        models = ModelRouter(self.host, self.config, self.settings({
            "model": "same-model", "api_key_env": "CODING_TEST_ROTATING_KEY"}))
        clients = [Mock(), Mock()]
        with patch("coding_agent.runtime.model_router.Anthropic", side_effect=clients) as factory:
            with patch.dict("os.environ", {"CODING_TEST_ROTATING_KEY": "first-key"}):
                first = models.select("implementation")
            with patch.dict("os.environ", {"CODING_TEST_ROTATING_KEY": "second-key"}):
                second = models.select("implementation")
        self.assertIsNot(first.client, second.client)
        self.assertEqual(factory.call_count, 2)
        models.close()
        for client in clients:
            client.close.assert_called_once()

    def test_legacy_auxiliary_names_and_compaction_role_fallback_are_preserved(self):
        models = ModelRouter(self.host, {**self.config, "memory_model": "memory-old",
                                       "goal_evaluator_model": "judge-old"})
        self.assertEqual(models.select("memory").config["model"], "memory-old")
        self.assertEqual(models.select("goal_evaluator").config["model"], "judge-old")
        writer = {**self.config, "model": "writer", "max_tokens": 4000, "profile": "writer"}
        selected = models.select("compaction", fallback=writer)
        self.assertEqual(selected.config["model"], "writer")
        self.assertEqual(selected.config["max_tokens"], 4000)
        self.assertEqual(selected.profile, "writer")

    def test_invalid_budget_cannot_be_silently_converted(self):
        for key, value in (("max_tokens", True), ("max_tokens", "8000"),
                           ("context_token_limit", 900), ("context_token_limit", "16000")):
            with self.subTest(key=key, value=value):
                models = ModelRouter(self.host, {**self.config, key: value})
                with self.assertRaisesRegex(ValueError, key):
                    models.select("implementation")

    def test_owned_close_attempts_every_connection_even_when_one_fails(self):
        models = ModelRouter(None, self.config)
        bad, good = Mock(), Mock()
        bad.close.side_effect = RuntimeError("private details must not be shown")
        models._clients = {"first": bad, "second": good}
        with self.assertRaisesRegex(RuntimeError, "RuntimeError") as caught:
            models.close()
        self.assertNotIn("private details", str(caught.exception))
        good.close.assert_called_once()


class AuxiliaryRoutingTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict("os.environ", {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.client = Mock()
        self.config = {"model": "legacy", "max_tokens": 1000,
                       "context_token_limit": 16000, "memory_home": str(self.root / "host")}
        self.settings = ModelSettings(self.config)
        self.settings.data = {"version": 1, "default": None, "profiles": {
            "aux": {"model": "aux-model", "max_tokens": 100, "context_token_limit": 8000}},
            "bindings": {"memory": "aux", "goal_evaluator": "aux"}}
        self.models = ModelRouter(self.client, self.config, self.settings)

    def test_memory_uses_bound_model_output_cap_and_current_router_snapshot(self):
        model = Mock(return_value=text_response("[]"))
        manager = MemoryManager(self.root, self.client, self.config, model_call=model, models=self.models)
        context = RunContext(self.root, TurnContext())
        with turn_context(context.turn):
            self.assertEqual(manager._request("selection", {"catalog": []}, context, 200), [])
            self.settings.data["profiles"]["aux"]["model"] = "next-aux-model"
            manager._request("selection", {"catalog": []}, context, 200)
        self.assertEqual([call.kwargs["model"] for call in model.call_args_list], ["aux-model", "next-aux-model"])
        self.assertTrue(all(call.kwargs["max_tokens"] == 100 for call in model.call_args_list))
        self.assertEqual(context.calls, [2])

    def test_memory_budget_failure_happens_before_request_and_does_not_consume_call(self):
        model = Mock()
        manager = MemoryManager(self.root, self.client, self.config, model_call=model, models=self.models)
        context = RunContext(self.root, TurnContext())
        with turn_context(context.turn), self.assertRaises(ContextBudgetError):
            manager._request("selection", {"catalog": "x" * 100000}, context, 200)
        model.assert_not_called()
        self.assertEqual(context.calls, [0])

    def test_goal_evaluator_routes_model_with_tool_free_independent_budget(self):
        evaluator = PromptGoalEvaluator(self.client, "legacy-judge", models=self.models)
        response = text_response('{"decision":"complete","reason":"real checks passed"}')
        with patch("coding_agent.runtime.goal.call_model", return_value=response) as model:
            result = evaluator.evaluate("tests pass", [{"role": "user", "content": "exit=0"}])
        self.assertEqual(result.decision, "complete")
        self.assertEqual(model.call_args.kwargs["model"], "aux-model")
        self.assertEqual(model.call_args.kwargs["max_tokens"], 100)
        self.assertNotIn("tools", model.call_args.kwargs)

    def test_goal_evaluator_rejects_oversized_goal_without_any_provider_request(self):
        evaluator = PromptGoalEvaluator(self.client, "legacy-judge", models=self.models)
        with patch("coding_agent.runtime.goal.call_model") as model, self.assertRaises(ContextBudgetError):
            evaluator.evaluate("x" * 100000, [])
        model.assert_not_called()

    def test_auxiliary_task_never_raises_its_original_output_limit(self):
        selected = ModelSelection(self.client, {"max_tokens": 8000, "context_token_limit": 16000})
        self.assertEqual(bounded_auxiliary_request(selected, "system", [{"role": "user", "content": "facts"}], 200), 200)


if __name__ == "__main__":
    unittest.main()
