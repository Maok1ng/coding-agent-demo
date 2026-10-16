# 工作目录与可选 Docker 沙箱

程序目录和目标项目目录分开管理，目录选择集中在 `coding_agent/runtime/workspace.py`。

| 目录或文件 | 解析规则 |
| --- | --- |
| `APP_DIR` | `main.py` 所在目录，不受终端 `cd` 影响 |
| `CONFIG_PATH` | 默认 `APP_DIR/config.json`；可用 `--config` 或 `AGENT_CONFIG` 指定 |
| `WORKDIR` | 默认 `APP_DIR`，兼容已有数据；可用 `--workspace` 或 `AGENT_WORKSPACE` 指定 |
| `.tasks`、`.mailboxes`、`.transcripts`、`.scheduled_tasks.json` 等 | 仍保存在所选 `WORKDIR` 中，没有迁移或删除原数据 |
| Skills | 读取所选工作区的 `skills/` |
| Task/worktree 目录 | 认领后由 assignment 指定；同步命令、文件工具、后台 Bash、一次性子 Agent 都继承它 |

显式传入的相对路径按启动终端的位置解析一次，此后全部使用绝对路径。
线程不会调用 `os.chdir()`；子 Agent 使用 `ContextVar` 临时继承目录，避免影响其他队友。
环境变量应在启动进程前设置；运行过程中不能更换工作区。

## 选择工作区

下面命令可以从任意终端目录运行（先激活自己的 `agent` 环境）：

```bash
python '/path/to/coding-agent-demo/main.py' \
  --workspace /absolute/path/to/target-project
```

工作区必须是已经存在的目录；不提供参数时仍操作本 demo 目录。
模型配置继续读取 Agent 程序目录的 `config.json`，不会误读目标仓库的同名文件。

## Docker 后端

`config/sandbox.json` 默认使用 `local`，仅在显式传入 `--execution docker` 或修改后端配置后启用 Docker。
Shell 统一经过 `coding_agent/runtime/environment.py`；Docker 启动失败不会回退到本机。
模型 API、权限确认、任务板和 worktree 管理由宿主进程负责。

先在已经安装并启动 Docker 的机器上构建工具镜像：

```bash
docker build -t coding-agent-sandbox:local \
  '/path/to/coding-agent-demo/docker'
```

然后选择一个独立目标项目：

```bash
python '/path/to/coding-agent-demo/main.py' \
  --workspace /absolute/path/to/target-project \
  --execution docker
```

运行时使用 `--pull never`，不会自动下载镜像。CLI 在接收任务前检查 Docker CLI、daemon 和镜像是否就绪；
执行期间出现错误仍会返回给工具调用。
基础镜像提供 Python、pytest、ruff、git、rg；目标项目所需的额外依赖需要提前加入镜像。
`AGENT_DOCKER_IMAGE` 可选择自己的镜像。

每条 Shell 命令启动一个临时容器。默认无网络、只读根文件系统、移除 capabilities、禁止提权，
限制为 2 CPU / 2 GiB 内存 / 128 个进程；`/tmp` 可写。
如任务确实需要网络，可以由宿主修改 `config/sandbox.json` 的 `network` 为 `bridge`。

工作区以**相同绝对路径**可写挂载到容器，所以宿主文件工具和容器里的 Shell 操作同一份文件，
也无需把模型生成的路径在 `/workspace` 与宿主路径之间来回替换。
写入工作区会直接改变目标项目文件；该方案不是“一次性副本”。工作区以外的宿主目录不挂载。
容器读取不到宿主进程的模型 API 环境变量；工作区若包含 Agent 的模型配置文件会拒绝启用 Docker。
目标仓库自身的凭据文件仍属于挂载内容，应由宿主决定放入哪些文件。

文件修改在命令间保留，`cd`、`export`、`pip install` 到容器系统目录的结果和后台服务不会保留。
需要跨命令使用的依赖应预装镜像或安装到工作区内的虚拟环境。
容器内运行的是 Linux，不会使用宿主 macOS 的 Conda Python。

Git 项目建议把 `--workspace` 指向仓库根目录：内部创建的 `.worktrees` 和主仓库 `.git` 会一起挂载。
已有 worktree 的 Git 元数据若在工作区外，容器内 Git 无法访问它；宿主管理的 Git 工具仍使用宿主环境。
所有队友共享所选工作区这一挂载边界，Task 工作目录不构成额外的容器隔离边界。

Docker 的 `run`、bind mount 和网络选项见官方说明：
[docker run](https://docs.docker.com/reference/cli/docker/container/run/)、
[bind mounts](https://docs.docker.com/engine/storage/bind-mounts/)、
[none network](https://docs.docker.com/engine/network/drivers/none/)。
