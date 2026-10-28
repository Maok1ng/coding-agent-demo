"""系统模型设置的路径、命令、任务选择与持久化边界；不读真实配置或调用模型。"""

from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from coding_agent.runtime.model_settings import MODEL_TASKS, ModelSettings, ModelSettingsError
from coding_agent.runtime.model_router import ModelRouter


class ModelSettingsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.path = self.root / "host" / "models.json"
        self.config = {"model": "legacy-model", "max_tokens": 1000, "context_token_limit": 16000,
                       "api_key": "FAKE-LEGACY-SECRET", "base_url": "https://old.example.invalid"}
        patcher = patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("AGENT_MODELS_CONFIG", None)

    def settings(self, config=None, path=None):
        return ModelSettings(self.config if config is None else config, self.path if path is None else path)

    def save(self, value):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(value), encoding="utf-8")

    def empty(self):
        return {"version": 1, "default": None, "profiles": {}, "bindings": {}}

    def test_default_constructor_does_not_read_home(self):
        with patch.object(ModelSettings, "reload") as reload:
            settings = ModelSettings(self.config)
        reload.assert_not_called()
        self.assertEqual(settings.resolve("implementation")["model"], "legacy-model")
        self.assertFalse(self.path.exists())

    def test_cli_load_can_explicitly_choose_default_user_path(self):
        self.save({**self.empty(), "profiles": {"fast": {"model": "fast-model"}}, "default": "fast"})
        with patch("coding_agent.runtime.model_settings.default_path", return_value=self.path):
            settings = ModelSettings(self.config, load=True)
        self.assertEqual(settings.resolve("codebase")["model"], "fast-model")

    def test_path_priority_is_argument_config_environment_then_user_default(self):
        os.environ["AGENT_MODELS_CONFIG"] = str(self.path)
        config_path = self.root / "custom" / "models.json"
        explicit = self.root / "explicit" / "models.json"
        self.assertEqual(ModelSettings(self.config).path, self.path)
        self.assertEqual(ModelSettings({**self.config, "models_config": str(config_path)}).path, config_path)
        self.assertEqual(ModelSettings({**self.config, "models_config": str(config_path)}, explicit).path, explicit)

    def test_commands_manage_default_and_task_binding_with_private_atomic_file(self):
        settings = self.settings()
        self.assertIn("已添加", settings.command("add fast fast-model --max-tokens 500 --context-limit 12000"))
        settings.command("add strong strong-model")
        settings.command("use fast")
        settings.command("assign implementation strong")
        self.assertEqual(settings.resolve("codebase")["model"], "fast-model")
        self.assertEqual(settings.resolve("implementation")["model"], "strong-model")
        loaded = self.settings()
        self.assertEqual(loaded.data, settings.data)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])
        self.assertNotIn("FAKE-LEGACY-SECRET", self.path.read_text())
        self.assertNotIn("api_key", json.loads(self.path.read_text())["profiles"]["fast"])
        self.assertEqual(self.config["model"], "legacy-model")

    def test_image_capability_commands_persist_and_display_without_model_probe(self):
        settings = self.settings()
        settings.command("add vision vision-model --supports-images true")
        settings.command("add text text-model --supports-images false")
        settings.command("add unknown unknown-model")
        settings.command("assign implementation vision")
        self.assertIs(settings.resolve("implementation")["supports_images"], True)
        self.assertIs(self.settings().data["profiles"]["text"]["supports_images"], False)
        self.assertIsNone(settings.resolve("codebase")["supports_images"])
        display = settings.describe()
        self.assertIn("图片 支持", display)
        self.assertIn("图片 不支持", display)
        self.assertIn("图片 未声明", display)
        self.assertIn("--supports-images true|false", settings.command("help"))

    def test_image_capability_belongs_to_selected_model_and_fallback_inherits_only_same_profile(self):
        settings = self.settings({**self.config, "supports_images": True,
                                  "memory_model": "different-memory"})
        self.assertIs(settings.resolve("implementation")["supports_images"], True)
        self.assertIsNone(settings.resolve("memory")["supports_images"])
        settings.command("add unspecified another-model")
        settings.command("add text text-model --supports-images false")
        settings.command("assign implementation unspecified")
        self.assertIsNone(settings.resolve("implementation")["supports_images"])
        settings.command("add vision vision-model --supports-images true")
        settings.command("assign implementation vision")
        parent = settings.resolve("implementation")
        self.assertIs(settings.resolve("compaction", fallback=parent)["supports_images"], True)
        settings.command("assign compaction unspecified")
        self.assertIsNone(settings.resolve("compaction", fallback=parent)["supports_images"])
        settings.command("assign implementation text")
        self.assertIs(settings.resolve("implementation")["supports_images"], False)

    def test_invalid_image_capabilities_are_rejected_without_saving(self):
        settings = self.settings()
        for value in ("yes", "1", "True", "null", "false --supports-images true"):
            with self.subTest(value=value), self.assertRaises(ModelSettingsError):
                settings.command(f"add invalid model --supports-images {value}")
            self.assertFalse(self.path.exists())
        for value in (1, 0, "true", "false", None, [], {}):
            with self.subTest(value=value):
                self.save({**self.empty(), "profiles": {"invalid": {
                    "model": "m", "supports_images": value}}})
                with self.assertRaises(ModelSettingsError):
                    self.settings()
                with self.assertRaises(ModelSettingsError):
                    ModelSettings({**self.config, "supports_images": value}).resolve("implementation")

    def test_direct_router_preserves_unknown_and_does_not_infer_different_legacy_model(self):
        router = ModelRouter(object(), {**self.config, "supports_images": True,
                                         "goal_evaluator_model": "different-evaluator"})
        selected = router.select("implementation")
        self.assertIs(selected.config["supports_images"], True)
        self.assertIsNone(router.select("goal_evaluator").config["supports_images"])
        self.assertIs(router.select("compaction", fallback=selected.config).config["supports_images"], True)
        self.assertIsNone(ModelRouter(object(), self.config).select("codebase").config["supports_images"])
        for value in (1, "true", None):
            with self.subTest(value=value), self.assertRaises(ModelSettingsError):
                ModelRouter(object(), {**self.config, "supports_images": value}).select("implementation")

    def test_legacy_memory_and_goal_models_remain_until_explicitly_bound(self):
        settings = self.settings({**self.config, "memory_model": "old-memory", "goal_evaluator_model": "old-goal"})
        settings.command("add fast fast-model")
        settings.command("use fast")
        self.assertEqual(settings.resolve("memory")["model"], "old-memory")
        self.assertEqual(settings.resolve("goal_evaluator")["model"], "old-goal")
        settings.command("assign memory fast")
        self.assertEqual(settings.resolve("memory")["model"], "fast-model")

    def test_compaction_inherits_owner_unless_explicitly_bound(self):
        settings = self.settings()
        settings.command("add fast fast-model")
        settings.command("add strong strong-model --base-url https://new.example.invalid --api-key-env OTHER_TOKEN")
        settings.command("use fast")
        settings.command("assign implementation strong")
        parent = settings.resolve("implementation")
        summary = settings.resolve("compaction", fallback=parent)
        self.assertEqual(summary["model"], "strong-model")
        self.assertEqual(summary["api_key_env"], "OTHER_TOKEN")
        self.assertEqual(summary["profile"], "strong")
        settings.command("assign compaction fast")
        summary = settings.resolve("compaction", fallback=parent)
        self.assertEqual(summary["model"], "fast-model")
        self.assertEqual(summary["base_url"], self.config["base_url"])
        self.assertNotIn("api_key_env", summary)
        self.assertEqual(summary["api_key"], "FAKE-LEGACY-SECRET")

    def test_cross_endpoint_requires_explicit_environment_reference(self):
        settings = self.settings()
        with self.assertRaisesRegex(ModelSettingsError, "api_key_env"):
            settings.command("add other another-model --base-url https://other.example.invalid")
        self.assertEqual(settings.data, self.empty())
        self.assertFalse(self.path.exists())
        settings.command("add other another-model --base-url https://other.example.invalid --api-key-env OTHER_TOKEN")
        settings.command("use other")
        os.environ["OTHER_TOKEN"] = "FAKE-PROFILE-SECRET"
        with patch("coding_agent.runtime.model_settings.os.getenv", side_effect=AssertionError("do not read values")):
            chosen = settings.resolve("implementation")
        self.assertEqual(chosen["api_key_env"], "OTHER_TOKEN")
        self.assertNotIn("api_key", chosen)
        self.assertNotIn("FAKE-PROFILE-SECRET", settings.describe())
        self.assertNotIn("FAKE-PROFILE-SECRET", self.path.read_text())

    def test_same_endpoint_can_inherit_legacy_credentials(self):
        settings = self.settings()
        settings.command("add same another-model --base-url https://old.example.invalid/")
        settings.command("use same")
        self.assertEqual(settings.resolve("planning")["api_key"], "FAKE-LEGACY-SECRET")

    def test_clone_freezes_current_settings_without_disk_reads(self):
        settings = self.settings()
        settings.command("add fast fast-model")
        settings.command("use fast")
        os.environ["AGENT_MODELS_CONFIG"] = str(self.root / "must-not-read.json")
        with patch.object(ModelSettings, "reload", side_effect=AssertionError("must not reload")):
            clone = settings.clone()
        settings.command("reset")
        self.assertEqual(clone.resolve("implementation")["model"], "fast-model")
        self.assertEqual(settings.resolve("implementation")["model"], "legacy-model")

    def test_resolve_is_independent_and_rejects_unknown_task(self):
        settings = self.settings()
        resolved = settings.resolve("codebase")
        resolved["model"] = "changed"
        self.assertEqual(settings.resolve("codebase")["model"], "legacy-model")
        with self.assertRaises(ModelSettingsError):
            settings.resolve("invented-role")

    def test_settings_can_be_managed_without_legacy_model_credentials(self):
        settings = self.settings({})
        settings.command("add fast fast-model --max-tokens 500 --context-limit 6000")
        settings.command("use fast")
        self.assertEqual(settings.resolve("planning")["model"], "fast-model")

    def test_reset_and_remove_refuse_dangling_references(self):
        settings = self.settings()
        settings.command("add fast fast-model")
        settings.command("use fast")
        settings.command("assign codebase fast")
        with self.assertRaisesRegex(ModelSettingsError, "引用"):
            settings.command("remove fast")
        settings.command("reset codebase")
        with self.assertRaises(ModelSettingsError):
            settings.command("remove fast")
        settings.command("reset")
        settings.command("remove fast")
        self.assertEqual(settings.data, self.empty())

    def test_invalid_commands_do_not_save_or_echo_secret_arguments(self):
        settings = self.settings()
        commands = ["add", "add ../escape model", "add fast model --unknown FAKE-SECRET",
                    "add fast model --max-tokens true", "add fast model --max-tokens -1",
                    "add fast model --max-tokens 1.5", "add fast model --context-limit 999",
                    "add fast model --max-tokens 1000 --context-limit 1000",
                    "add fast model --max-tokens 1 --max-tokens 2", "use missing",
                    "assign invented-role fast", "reset invented-role", "remove missing", "'unterminated"]
        for command in commands:
            with self.subTest(command=command), self.assertRaises(ModelSettingsError) as error:
                settings.command(command)
            self.assertNotIn("FAKE-SECRET", str(error.exception))
            self.assertEqual(settings.data, self.empty())
            self.assertFalse(self.path.exists())

    def test_file_schema_rejects_unknown_fields_and_noninteger_budget_values(self):
        invalid = [None, [], {}, {**self.empty(), "version": True}, {**self.empty(), "unexpected": 1},
                   {**self.empty(), "default": "missing"},
                   {**self.empty(), "bindings": {"codebase": "missing"}},
                   {**self.empty(), "profiles": {"fast": {"model": "m", "api_key": "FAKE-SECRET"}}}]
        for name, value in (("max_tokens", True), ("max_tokens", "1000"), ("max_tokens", 0),
                            ("context_token_limit", 999), ("context_token_limit", 1000.0)):
            invalid.append({**self.empty(), "profiles": {"fast": {"model": "m", name: value}}})
        for value in invalid:
            with self.subTest(value=value):
                self.save(value)
                with self.assertRaises(ModelSettingsError) as error:
                    self.settings()
                self.assertNotIn("FAKE-SECRET", str(error.exception))

    def test_file_cannot_bypass_cross_endpoint_credential_check(self):
        self.save({**self.empty(), "default": "other", "profiles": {
            "other": {"model": "m", "base_url": "https://new.example.invalid"}}})
        settings = self.settings()
        with self.assertRaisesRegex(ModelSettingsError, "api_key_env"):
            settings.resolve("implementation")
        #仍能管理全局文件，不能让一个不兼容的默认档案阻止解除绑定和删除。
        settings.command("reset")
        settings.command("remove other")
        self.assertEqual(settings.data, self.empty())

    def test_file_size_and_invalid_json_are_bounded(self):
        self.path.parent.mkdir(parents=True)
        for data in (b"not json", b"\xff", b"x" * (128 * 1024 + 1)):
            with self.subTest(size=len(data)):
                self.path.write_bytes(data)
                with self.assertRaises(ModelSettingsError):
                    self.settings()

    def test_endpoint_validation_rejects_embedded_credentials_and_insecure_remote_http(self):
        settings = self.settings()
        for url in ("http://remote.example.invalid", "https://user:FAKE-SECRET@example.invalid",
                    "https://example.invalid?token=FAKE-SECRET", "https://example.invalid/#fragment",
                    "file:///tmp/model", "https://example.invalid:0", "https://example.invalid:bad",
                    "https://example.invalid/\n", " https://example.invalid"):
            with self.subTest(url=url), self.assertRaises(ModelSettingsError) as error:
                settings.command(f"add fast model --base-url {json.dumps(url)} --api-key-env TOKEN")
            self.assertNotIn("FAKE-SECRET", str(error.exception))
        settings.command("add local local-model --base-url http://127.0.0.1:8000 --api-key-env LOCAL_TOKEN")

    def test_conflicting_writer_is_rejected_without_changing_memory_or_file(self):
        first, second = self.settings(), self.settings()
        first.command("add first first-model")
        original = self.path.read_bytes()
        with self.assertRaisesRegex(ModelSettingsError, "其他进程"):
            second.command("add second second-model")
        self.assertEqual(second.data, self.empty())
        self.assertEqual(self.path.read_bytes(), original)
        second.reload()
        second.command("add second second-model")
        self.assertEqual(set(second.data["profiles"]), {"first", "second"})

    def test_external_file_creation_and_deletion_are_not_silently_overwritten(self):
        settings = self.settings()
        self.save(self.empty())
        with self.assertRaisesRegex(ModelSettingsError, "其他进程"):
            settings.command("add fast fast-model")
        settings.reload()
        self.path.unlink()
        with self.assertRaisesRegex(ModelSettingsError, "其他进程"):
            settings.command("add fast fast-model")

    def test_failed_atomic_replace_keeps_original_and_in_memory_configuration(self):
        settings = self.settings()
        settings.command("add fast fast-model")
        previous, original = deepcopy(settings.data), self.path.read_bytes()
        with patch("coding_agent.runtime.model_settings.os.replace", side_effect=PermissionError):
            with self.assertRaises(ModelSettingsError):
                settings.command("use fast")
        self.assertEqual(settings.data, previous)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_final_symlink_or_fifo_is_rejected_without_reading_or_replacing_target(self):
        self.path.parent.mkdir(parents=True)
        outside = self.root / "outside.json"
        outside.write_text("FAKE-PRIVATE-CONTENT")
        self.path.symlink_to(outside)
        with self.assertRaises(ModelSettingsError):
            self.settings()
        self.assertEqual(outside.read_text(), "FAKE-PRIVATE-CONTENT")
        self.path.unlink()
        os.mkfifo(self.path)
        with self.assertRaises(ModelSettingsError):
            self.settings()

    def test_directory_symlink_and_lock_symlink_are_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        self.path.parent.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ModelSettingsError):
            self.settings()
        self.path.parent.unlink()
        settings = self.settings()
        self.path.parent.mkdir()
        lock_target = self.root / "lock-target"
        lock_target.write_text("keep")
        (self.path.parent / ".models.json.lock").symlink_to(lock_target)
        with self.assertRaises(ModelSettingsError):
            settings.command("add fast fast-model")
        self.assertEqual(lock_target.read_text(), "keep")
        self.assertFalse(self.path.exists())

    def test_help_and_description_do_not_resolve_credentials_or_start_model_calls(self):
        settings = self.settings()
        with patch.dict(os.environ, {"TOKEN": "FAKE-PRIVATE-TOKEN"}):
            settings.command("add fast model --api-key-env TOKEN")
            display = settings.command("") + settings.command("help")
        self.assertNotIn("FAKE-PRIVATE-TOKEN", display)
        self.assertNotIn("FAKE-LEGACY-SECRET", display)
        self.assertTrue(all(task in display for task in MODEL_TASKS))


if __name__ == "__main__":
    unittest.main()
