# 工作目录与 Docker 执行后端

Agent 的宿主配置和用户的目标项目分开管理；目录选择集中在 `runtime/workspace.py`。

| 项目 | 默认位置与覆盖方式 |
| --- | --- |
| 程序目录 | 源码/可编辑安装为项目根目录；wheel 安装的宿主配置目录为 `~/.coding-agent` |
| 模型配置 | 程序目录的 `config.json`；`--config` 或 `AGENT_CONFIG` 覆盖 |
| 工作区 | 源码/可编辑安装默认程序目录；wheel 默认启动终端当前目录；`--workspace` 或 `AGENT_WORKSPACE` 覆盖 |
| MCP 配置 | 宿主的 `config/mcp.json`；`--mcp-config` 或 `AGENT_MCP_CONFIG` 覆盖 |
| 用户规则 / 记忆 | `~/.coding-agent/AGENTS.md` 与 `memory/`；宿主 `--memory-home`、`AGENT_MEMORY_HOME` 或模型配置 `memory_home` 覆盖 |
| 项目约定 / Skills | 只读取工作区根目录 `AGENTS.md`，Skills 从工作区 `skills/` 按需加载 |
| 项目经验记忆 | 当前工作区 `.memory/`，不向父 Git 仓库扩大范围 |
| 会话 / 运行记录 | 工作区的 `.sessions/`、`.transcripts/`、`.task_outputs/` |

显式相对路径以启动终端为基准解析一次。工作区必须存在；进程不调用 `os.chdir()`。
RunContext 向所有专业角色传递同一工作区，文件工具拒绝 `..` 或符号链接逃逸。
角色不能通过委派参数更换宿主配置、工作区或权限。
核心 API 通过宿主 ContextVar 绑定工作区，不要求先调用 CLI 或改写全局目录；
Skills 目录和 Shell 输出也使用当前运行的工作区，后台线程显式继承这一边界。

```bash
python '/path/to/coding-agent-demo/main.py' \
  --workspace /absolute/path/to/target-project \
  --config /absolute/path/to/agent-config.json
```

目标仓库中的同名模型/MCP 配置不会被自动加载。`AGENTS.md` 是项目说明，不是用户授权：
其中的文字不能解除 Plan、提升角色权限，或替用户批准外部工具。
宿主规则目录只由宿主配置指定，不从目标项目的文件中读取；用户级记忆的上级目录须已存在。
自动记忆读写在宿主执行，并不进入 Docker；普通代码搜索默认跳过 `.memory/`，避免把全部私人经验当源码索引。

## Docker 后端

默认使用本地 Shell；显式传入 `--execution docker` 或在宿主沙箱配置中选择 Docker 后才启用。
只有 Implementation 在 Execute 模式中可运行 Shell，所有命令都通过 Environment。
Docker 失败不会悄悄回退到宿主执行。

先在已安装并运行 Docker 的机器上构建镜像：

```bash
docker build -t coding-agent-sandbox:local '/path/to/coding-agent-demo/docker'
python '/path/to/coding-agent-demo/main.py' \
  --workspace /absolute/path/to/target-project \
  --execution docker
```

运行使用 `--pull never`，不自动下载镜像；CLI 检查 Docker CLI、daemon 和镜像是否就绪。
镜像提供 Python、pytest、ruff、git、rg；项目额外依赖需要预装镜像或放在工作区内。
可用 `AGENT_DOCKER_IMAGE` 选择自己的镜像。

每条命令启动临时容器，默认无网络、只读根文件系统、移除 capabilities、禁止提权，
限制为 2 CPU / 2 GiB 内存 / 128 进程，`/tmp` 可写。网络需求由宿主沙箱配置显式决定。
工作区以相同绝对路径可写挂载，文件改动会直接影响用户项目，不是一次性副本。
命令之间不保留容器中的 `cd`、`export` 或系统目录安装结果；容器使用 Linux，不使用宿主 Conda 环境。

## 隔离边界与清理

- Docker 只隔离 Shell。文件编辑、固定参数的 Git 查询、模型请求和 MCP 客户端仍在宿主。
- 只读根文件系统不等于只读工作区；Plan 的禁止写入由 ToolRuntime 保证。
- 容器不继承宿主模型 API 环境变量；工作区包含 Agent 模型配置时拒绝启用 Docker。
  目标项目自身的凭据仍可能位于挂载目录，应由用户管理。
- 后台命令和临时容器归当前 Turn；正常结束、中断或退出时清理，不跨会话恢复。
- 已有 Git worktree 可以作为用户选择的工作区，但不再由 Agent 自动认领、创建或释放。
  若其 Git 元数据位于工作区外，容器内可能无法访问，不能据此扩大挂载范围。
- 旧 `.tasks`、`.mailboxes`、`.scheduled_tasks.json` 和已有 worktree 不自动删除，
  也不再作为旧 Team/Cron 调度状态加载。

这些约束不能把不可信代码变成安全代码；MCP stdio 服务也不自动受 Docker 后端隔离。
