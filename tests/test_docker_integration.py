"""显式启用的真实容器测试；默认不依赖 Docker，也不下载镜像。"""

import os
import json
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coding_agent.runtime import environment, workspace


@unittest.skipUnless(os.getenv("AGENT_TEST_DOCKER") == "1", "设置 AGENT_TEST_DOCKER=1 启用真实 Docker 测试")
class DockerIntegrationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="agent-docker-test-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.task = self.root / "project with spaces"
        self.task.mkdir()
        for module in (workspace, environment):
            change = patch.object(module, "WORKDIR", self.root)
            change.start()
            self.addCleanup(change.stop)
        self.env = environment.DockerEnvironment(environment.ExecutionConfig(backend="docker"))
        self.env.prepare()

    def test_workspace_and_container_security(self):
        checks = '''import errno, os, platform, shutil
from pathlib import Path
assert platform.system() == "Linux"
assert os.getenv("AGENT_TEST_HOST_ONLY") is None
for name in ("python", "git", "rg", "pytest", "ruff"):
    assert shutil.which(name), name
status = Path("/proc/self/status").read_text()
assert "CapEff:\\t0000000000000000" in status
assert "NoNewPrivs:\\t1" in status
assert len(Path("/proc/net/route").read_text().splitlines()) == 1
try:
    Path("/outside-workspace-check").write_text("no")
except OSError as error:
    assert error.errno == errno.EROFS, error
else:
    raise AssertionError("root filesystem is writable")
Path("result.txt").write_text("persisted")
print(Path.cwd())
'''
        with patch.dict(os.environ, {"AGENT_TEST_HOST_ONLY": "fake-test-marker"}):
            output, code = self.env.execute("python -c " + shlex.quote(checks), self.task)
        self.assertEqual(code, 0, output)
        self.assertEqual(output, str(self.task))
        self.assertEqual((self.task / "result.txt").read_text(), "persisted")
        self.assertEqual(self.env.execute("cat result.txt; exit 7", self.task), ("persisted", 7))

    def test_timeout_removes_its_container(self):
        self.env.config.timeout = 2
        removed = []
        original = environment._remove_container

        def remove(name):
            removed.append(name)
            original(name)

        with patch.object(environment, "_remove_container", side_effect=remove):
            output, code = self.env.execute("sleep 30", self.task)
        self.assertIsNone(code)
        self.assertIn("Timeout", output)
        self.assertEqual(len(removed), 1)
        self.assertNotIn(removed[0], environment._containers)
        result = environment.subprocess.run(
            ["docker", "ps", "-aq", "--filter", f"name=^{removed[0]}$"],
            text=True, capture_output=True, timeout=10, check=True,
        )
        self.assertEqual(result.stdout.strip(), "")

    def test_both_cli_paths_start_in_docker_mode_without_model_requests(self):
        config = self.root / "fake-model.json"
        config.write_text(json.dumps({"api_key": "fake", "base_url": "https://example.invalid",
                                      "model": "worker", "max_tokens": 100}), encoding="utf-8")
        for entry in ([str(workspace.APP_DIR / "main.py")], ["-m", "coding_agent"]):
            with self.subTest(entry=entry):
                result = subprocess.run(
                    [sys.executable, *entry, "--workspace", str(self.task), "--config", str(config),
                     "--execution", "docker"], cwd=workspace.APP_DIR,
                    input="exit\n", text=True, capture_output=True, timeout=20,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Execution: docker", result.stdout)


if __name__ == "__main__":
    unittest.main()
