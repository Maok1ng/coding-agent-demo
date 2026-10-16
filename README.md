# Coding Agent Demo

一个包含上下文压缩、Skills、MCP、后台命令、定时任务和 Agent Team 的编码 Agent 学习项目。

## 目录

```text
test/
├── main.py                 #兼容原启动命令的薄入口
├── coding_agent/
│   ├── cli.py              #参数解析与 CLI 启动
│   ├── agents/             #主循环、一次性子 Agent、持久队友与共享任务板
│   ├── runtime/            #工作区、权限、执行环境、压缩、目标、后台与定时任务
│   ├── tools/              #共享文件/Shell 工具、Todo、提问、Skills
│   └── integrations/       #MCP 适配；当前为内置模拟服务
├── config/                 #非秘密配置与模型配置样例
├── tests/                  #无网络回归测试、可选 Docker 实测
├── docs/                   #使用说明与设计草案
├── docker/                 #沙箱工具镜像，不包含 Agent 凭据
├── requirements.txt        #已验证的运行依赖
└── pyproject.toml          #测试与静态检查配置
```

`config.json` 仍留在项目根目录并被 Git 忽略，不迁移你的真实密钥。
新环境可以参考 `config/model.example.json` 创建自己的 `config.json`，或用 `--config` 指定外部文件。
不要把真实密钥填写进样例。任务、邮箱、归档等运行数据仍保存在所选工作区的原有隐藏目录。

## 启动

Python 需要 3.11 或以上版本。已有 `agent` 环境可直接使用，不需要重新安装依赖。
新建项目环境时，使用该环境的 `python -m pip install -r requirements.txt` 安装依赖。

在项目根目录执行，以下两种方式等价：

```bash
python main.py --workspace "/absolute/path/to/target-project"
python -m coding_agent --workspace "/absolute/path/to/target-project"
```

`--workspace` 不传时仍默认当前 Agent 项目根目录，不跟随终端的 `cd`。
从其他目录启动时，使用 `main.py` 的绝对路径。配置与工作区的相对路径按启动终端的位置解析。

Docker 模式需要 Docker Desktop 正在运行，并已构建工具镜像：

```bash
docker build -t coding-agent-sandbox:local docker
python main.py --workspace "/absolute/path/to/target-project" --execution docker
```

默认仍为本机执行；沙箱配置在 `config/sandbox.json`。
Docker 模式的目标工作区不能包含 Agent 的真实模型配置。
如果 Docker Hub 直连失败，可以只为构建命令临时指定自己的代理，例如本机代理确实监听 7897 时：

```bash
HTTPS_PROXY=http://127.0.0.1:7897 HTTP_PROXY=http://127.0.0.1:7897 \
  docker build -t coding-agent-sandbox:local docker
```

这不会修改 shell 或 Docker 的全局代理，也不会给运行时沙箱开启网络。

## 测试

在项目根目录运行，不读取真实 API 密钥，也不请求模型：

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
```

已安装 Ruff 时可以运行 `ruff check coding_agent tests main.py`。
真实 Docker 测试默认跳过；镜像构建后用 `AGENT_TEST_DOCKER=1` 显式启用。

```bash
AGENT_TEST_DOCKER=1 python -m unittest discover -s tests -p 'test_*.py' -v
```

## 模块边界

主 Agent 负责模型调用、工具分发和会话控制；子 Agent 与队友复用宿主模型客户端。
基础文件工具和 schema 统一在 `coding_agent/tools/files.py`，所有 Shell 统一进入执行环境。
任务板只保留 Team 使用的一套实现，Todo 则是主会话内的轻量步骤列表，二者并不替代。
队友仍需用户授权后才启动，计划审批和工作目录认领逻辑保持不变。

工程整理没有新增数据库、Web 服务或自动启动队友，也没有将整个 Agent 进程放进 Docker。
Docker 仅隔离 Shell；宿主文件工具仍直接读写目标工作区，Git/worktree 管理由宿主负责。
文件替换统一要求非空且唯一匹配；多处命中时会报错，不会只改第一处。

本次整理前的源码备份位于 `.refactor-backups/`，已被 Git 忽略；没有备份真实模型配置。

使用细节见 [工作区与 Docker](docs/workspace.md)、[目标循环](docs/goal-loop.md)。
[工作流设计](docs/workflow-design.md) 是未实现的设计草案，不代表已注册了 workflow 工具。

## 来源与许可证

本项目基于 [shareAI-lab/learn-claude-code](https://github.com/shareAI-lab/learn-claude-code)
学习、扩展并整理，保留原项目的 MIT 许可证及版权声明，详见 [LICENSE](LICENSE)。
