# 开发与工程约定

## 安装与入口

支持 Python 3.11+ 的 macOS/Linux。当前进程锁、进程组回收使用 POSIX 接口，未宣称支持 Windows。
先激活自己的 `agent` 环境，在本项目目录执行：

```bash
python -m pip install -e '.[dev]'
coding-agent --workspace /absolute/path/to/project --config /absolute/path/to/model.json
coding-agent --workspace /absolute/path/to/project -r
```

安装命令只作用于所选 Python 环境；不需要改 PATH、全局 Python 或重新创建 Conda 环境。
仍可使用 `python main.py` 与 `python -m coding_agent`。
`.[dev]` 在运行依赖之外增加 Ruff 和构建工具；测试仍使用标准库 unittest。

`pyproject.toml` 声明构建后端、包名、Python 版本、许可证、开发依赖和命令入口。
运行依赖统一维护在 `requirements.txt`，版本统一维护在 `coding_agent.__version__`。
本版本只固定了直接运行依赖，不把它称为包含全部传递依赖的跨平台 lockfile。

## 源码与安装包的目录区别

| 方式 | 默认工作区 | 默认宿主配置根目录 |
| --- | --- | --- |
| 源码/可编辑安装 | Agent 项目目录，保持原行为 | Agent 项目目录 |
| wheel 安装 | 启动终端当前目录 | `~/.coding-agent` |

建议始终显式传 `--workspace` 与 `--config`，避免误操作错误目录。
wheel 安装不会将 site-packages 当作工作区，也不携带真实模型、MCP 或用户沙箱配置。
wheel 未提供沙箱文件时使用 `ExecutionConfig` 的默认值；源码配置缺失仍报错。
需要自定义沙箱参数时使用 `--sandbox-config /absolute/path/to/sandbox.json` 或 `AGENT_SANDBOX_CONFIG`。
显式指定但不存在的文件一定报错；不会悄悄切换回默认参数。

## 统一检查

```bash
make help
make test
make lint
make check
make package-check
```

也可用 `make PYTHON=/absolute/path/to/agent/bin/python test` 显式选择环境。
`make check` 只检查，不自动格式化或更改用户注释。
单元测试不使用真实模型凭据；MCP HTTP 用例会在本机监听临时回环端口，受限环境可能需要额外许可。
Docker 实测仍需 `AGENT_TEST_DOCKER=1` 显式启用，默认跳过。

`package-check` 在临时目录进行：

1. 白名单复制源码、公开样例与文档，不读取真实 config.json/config/mcp.json。
2. 构建 wheel 与 sdist，检查命令入口和禁止发布的文件路径。
3. 将 wheel 安装到临时 target，复用当前环境的运行依赖，不改变当前环境的已安装项目。
4. 离开源码目录，以假模型配置验证安装后的帮助与完整 CLI 启动；不发送模型请求。

它会故意加入假秘密文件和假会话，检查发布规则是否将其排除。
此检查是打包边界回归，不是通用秘密扫描器，提交前仍要人工检查变更。

## CI 与发布范围

`.github/workflows/ci.yml` 为 Python 3.11/3.12 提供安装、lint、测试与打包检查。
只有该目录作为 GitHub 仓库根目录发布时，配置才会生效。
当前本地目录仍位于教程父仓库内，本次没有新建 Git 仓库、移动 Git 历史、推送或发布版本。
没有配置自动发布到 PyPI、自动推送或仓库 secrets。

wheel 只包含 `coding_agent` 代码和必要元数据；sdist 额外包含公开文档、测试、样例和构建资料。
`MANIFEST.in` 使用明确范围，不用整个工作区的 `graft .`。
凭据和 `.sessions/.tasks/.transcripts` 等运行数据不属于分发包；保留原位置，不迁移或清理。

## 修改边界

- `agents/` 决定模型执行和协作，不负责读取任意全局配置。
- `runtime/` 提供执行、状态、会话、压缩和权限机制。
- `tools/` 维护模型可见 schema 与基础操作。
- `integrations/` 适配外部服务；MCP 不承担工作流阶段控制。
- 变更保持现有中文注释与局部排版，不为风格统一重写整个文件。
- 新能力添加相应无真实模型请求的回归测试，避免导入时启动线程或访问网络。

详细调用关系见 [架构说明](architecture.md)，Hook 与工作流的区别见 [Hook 说明](hooks.md)。
