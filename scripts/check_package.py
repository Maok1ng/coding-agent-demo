"""在临时目录验证分发包；不安装到当前环境，也不读取真实模型/MCP 配置。"""

import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def run(arguments: list[str], cwd: Path, env=None) -> subprocess.CompletedProcess:
    """固定使用调用脚本的 Python，超时/失败直接终止检查，不继续宣称构建成功。"""
    return subprocess.run(arguments, cwd=cwd, env=env, check=True, text=True,
                          capture_output=True, timeout=120)


def check_members(names):
    """构建检查额外拒绝敏感路径；项目样例配置可以打包，真实配置不可以。"""
    forbidden = {"config.json", "mcp.json", "models.json", ".sessions", ".transcripts", ".task_outputs", ".memory",
                 ".tasks", ".mailboxes", ".worktrees", ".refactor-backups", ".git"}
    for name in names:
        parts = Path(name).parts
        if any(part in forbidden or part == ".env" or part.startswith(".env.") for part in parts):
            raise RuntimeError(f"分发包包含禁止发布的内容: {name}")


def main():
    with tempfile.TemporaryDirectory(prefix="coding-agent-package-") as directory:
        root = Path(directory).resolve()
        source = root / "source"
        source.mkdir()
        #白名单复制构建输入，不把用户凭据、运行记录或未跟踪的个人文件带进构建目录。
        for name in ("coding_agent", "tests", "scripts", "docs", "docker"):
            shutil.copytree(PROJECT_ROOT / name, source / name,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for name in ("pyproject.toml", "requirements.txt", "README.md", "LICENSE", "main.py", "Makefile", "MANIFEST.in"):
            shutil.copy2(PROJECT_ROOT / name, source / name)
        (source / "config").mkdir()
        for name in ("model.example.json", "models.example.json", "mcp.example.json", "mcp.browser.example.json", "sandbox.json"):
            shutil.copy2(PROJECT_ROOT / "config" / name, source / "config" / name)
        #故意加入假秘密和假会话，验证打包规则确实不会把这些文件包含进分发包。
        (source / "config.json").write_text('{"api_key":"FAKE-PACKAGING-SENTINEL"}', encoding="utf-8")
        (source / "config" / "models.json").write_text('{"private":"FAKE-MODELS-SENTINEL"}', encoding="utf-8")
        (source / ".sessions").mkdir()
        (source / ".sessions" / "fake.json").write_text("{}", encoding="utf-8")
        (source / ".memory").mkdir()
        (source / ".memory" / "fake.md").write_text("FAKE-PRIVATE-MEMORY-SENTINEL", encoding="utf-8")
        #代码检查点含原始代码快照，同样必须被运行数据目录的排除规则挡住。
        checkpoints = source / ".task_outputs" / "checkpoints" / ("0" * 32)
        checkpoints.mkdir(parents=True)
        (checkpoints / "state.json").write_text("FAKE-PRIVATE-CHECKPOINT-SENTINEL", encoding="utf-8")
        run([sys.executable, "-c", "from setuptools.build_meta import build_wheel, build_sdist; "
             "build_wheel('dist'); build_sdist('dist')"], source)
        wheel = next((source / "dist").glob("*.whl"))
        archive = next((source / "dist").glob("*.tar.gz"))
        with zipfile.ZipFile(wheel) as package:
            check_members(package.namelist())
            entry_points = next(name for name in package.namelist() if name.endswith(".dist-info/entry_points.txt"))
            if "coding-agent = coding_agent.cli:main" not in package.read(entry_points).decode():
                raise RuntimeError("wheel 缺少 coding-agent 命令入口")
        with tarfile.open(archive) as package:
            check_members(package.getnames())
        installed = root / "installed"
        run([sys.executable, "-m", "pip", "install", "--no-index", "--no-deps", "--target", str(installed), str(wheel)], root)
        project = root / "target project"
        project.mkdir()
        config = root / "fake-model.json"
        config.write_text(json.dumps({"api_key": "fake", "base_url": "https://example.invalid",
                                      "model": "worker", "max_tokens": 100}), encoding="utf-8")
        #清除调用者的 AGENT_*，避免测试意外读取本机凭据、MCP 服务或 Docker 设置。
        env = {key: value for key, value in os.environ.items() if not key.startswith("AGENT_")}
        mcp_config = root / "empty-mcp.json"
        mcp_config.write_text('{"mcpServers": {}}', encoding="utf-8")
        env.update({"PYTHONPATH": str(installed), "AGENT_MCP_CONFIG": str(mcp_config),
                    "AGENT_MEMORY_HOME": str(root / "fake-memory-home"),
                    "AGENT_MODELS_CONFIG": str(root / "fake-model-settings.json")})
        command = [sys.executable, str(installed / "bin" / "coding-agent")]
        help_result = run([*command, "--help"], project, env)
        if "--resume" not in help_result.stdout:
            raise RuntimeError("安装后的 CLI 未提供会话入口")
        if any(option not in help_result.stdout for option in ("--approval-mode", "--no-stream")):
            raise RuntimeError("安装后的 CLI 缺少审批或流式输出选项")
        configured = run([*command, "--models", "add", "worker", "fake-worker"], project, env)
        if "worker" not in configured.stdout:
            raise RuntimeError("安装后的 CLI 无法管理系统模型档案")
        #使用包的默认沙箱值验证完整启动，不依赖源码 config/sandbox.json。
        result = subprocess.run([*command, "--workspace", str(project), "--config", str(config),
                                 "--execution", "local"], cwd=project, env=env, input="exit\n",
                                text=True, capture_output=True, check=True, timeout=30)
        if f"Workspace: {project}" not in result.stdout:
            raise RuntimeError("安装后的工作区路径不正确")
        if "审批模式：auto" not in result.stdout:
            raise RuntimeError("安装后的默认审批模式不正确")
        if "/checkpoints" not in result.stdout or "/restore" not in result.stdout:
            raise RuntimeError("安装后的 CLI 缺少代码检查点命令提示")
        print("Package check passed: wheel + sdist, no private state, installed CLI startup.")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        print(error.stdout or "", file=sys.stderr)
        print(error.stderr or "", file=sys.stderr)
        raise SystemExit(error.returncode) from None
