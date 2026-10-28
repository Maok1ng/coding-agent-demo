"""工程元数据、安装布局和宿主配置边界；不安装依赖或读取真实凭据。"""

import json
import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

import coding_agent
from coding_agent.runtime import environment, workspace


class ProjectTests(unittest.TestCase):
    def test_package_metadata_has_single_dependency_source_and_safe_discovery(self):
        with (workspace.APP_DIR / "pyproject.toml").open("rb") as handle:
            metadata = tomllib.load(handle)
        self.assertEqual(metadata["project"]["scripts"]["coding-agent"], "coding_agent.cli:main")
        self.assertEqual(metadata["tool"]["setuptools"]["dynamic"]["dependencies"]["file"], ["requirements.txt"])
        self.assertEqual(metadata["tool"]["setuptools"]["dynamic"]["version"]["attr"], "coding_agent.__version__")
        self.assertEqual(metadata["tool"]["setuptools"]["packages"]["find"]["include"], ["coding_agent", "coding_agent.*"])
        self.assertIsInstance(coding_agent.__version__, str)

    def test_wheel_layout_uses_dataclass_defaults_without_source_config(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(environment, "APP_DIR", Path(directory)), \
                patch.object(environment, "SOURCE_LAYOUT", False), \
                patch.dict(os.environ, {"AGENT_EXECUTION": "local"}):
            with patch.dict(os.environ):
                os.environ.pop("AGENT_SANDBOX_CONFIG", None)
                execution = environment.get_environment.__wrapped__()
            self.assertIsInstance(execution, environment.LocalEnvironment)
            self.assertEqual(execution.config, environment.ExecutionConfig())

    def test_explicit_missing_sandbox_config_never_silently_falls_back(self):
        with patch.object(environment, "SOURCE_LAYOUT", False), \
                patch.dict(os.environ, {"AGENT_SANDBOX_CONFIG": "/nonexistent-agent-sandbox.json"}):
            with self.assertRaises(FileNotFoundError):
                environment.get_environment.__wrapped__()

    def test_custom_sandbox_config_is_used(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "sandbox.json"
            config.write_text(json.dumps({"backend": "local", "timeout": 9}), encoding="utf-8")
            with patch.dict(os.environ, {"AGENT_SANDBOX_CONFIG": str(config), "AGENT_EXECUTION": "local"}):
                self.assertEqual(environment.get_environment.__wrapped__().config.timeout, 9)

    def test_source_defaults_match_shipped_sample(self):
        values = json.loads((workspace.APP_DIR / "config/sandbox.json").read_text(encoding="utf-8"))
        self.assertEqual(environment.ExecutionConfig(**values), environment.ExecutionConfig())


if __name__ == "__main__":
    unittest.main()
