# Coding Agent

一个面向代码仓库的 CLI Agent 学习项目。重点是：找准代码、最小修改、运行相关检查，并根据真实结果交付。

采用一个执行内核、多个专业角色；保留会话、Goal、中断、上下文压缩、Skills、MCP 和本地/Docker Shell 执行，不建设通用领域的定时或团队平台。

## 架构

```text
CLI / TerminalInteraction
  └─ Application                  用户输入、会话、Plan、Goal、Turn 生命周期
      └─ Orchestrator             按需分配任务、与用户交流、汇总结果
          └─ AgentRunner          所有角色共用的模型与工具循环
              └─ ToolRuntime      参数校验、权限、执行、结果及证据
                  └─ Environment  本地 / Docker Shell
```

| 角色 | 职责 | 权限 |
|---|---|---|
| Orchestrator | 理解任务、提问、派发角色、汇总 | 只读及派发，不能直接修改 |
| Codebase | 搜索代码、定位实现与依赖 | 只读 |
| Planning | 分析需求、制定修改与验证方案 | 永远只读 |
| Implementation | 修改代码、执行 Shell、运行检查并修复失败 | Execute 模式下按用户授权读写和执行 |
| Review / Research | 检查代码和 diff、核对证据、查阅资料 | 只读，不能直接运行测试或任意 Shell |

只有 Implementation 可以使用文件写入、Shell 和有副作用的 MCP 工具。角色调用按需、串行执行，子角色不能再次派发；不是每项任务都必须经过五个角色。各角色默认继承原模型配置，也可以在系统设置中分别选择模型与预算。

角色之间用 `AgentTask` 交接任务、限制、验收条件、已有结论和相关引用；原始用户请求由宿主传递。
专业角色通过 `report_result` 提交各自的结构化结果，宿主校验后返回 `AgentResult`；主 Agent 仍用普通文字与用户交流。
状态、退出码和证据引用由宿主记录，不能由模型在报告中伪造。格式不合格时最多修正一次，修正期间不能重做修改或命令。
字段、示例及边界见 [Agent 交接格式](docs/agent-handoff.md)。

权限既通过工具列表过滤，也在执行时强制检查；伪造工具名、修改提示词或返回一个“已批准”的工具结果不能获得写权限。Hook 用于日志、生命周期扩展或进一步限制，不承担授权职责。

这是应用层权限边界，不是完整的操作系统安全沙箱。

## 安装与启动

支持 Python 3.11+、macOS/Linux。激活已有的 `agent` Python 环境后，在项目根目录安装：

```bash
python -m pip install -e '.[dev]'
coding-agent --workspace "/absolute/path/to/target-project" --config "/absolute/path/to/model.json"
```

也可以只安装运行依赖，继续使用原入口：

```bash
python -m pip install -r requirements.txt
python main.py --workspace "/absolute/path/to/target-project"
python -m coding_agent --workspace "/absolute/path/to/target-project"
```

模型配置参考 `config/model.example.json`。真实 `config.json` 和 `config/mcp.json` 被 Git 忽略，不要将密钥填进样例或提交到仓库。

区分两个目录：

- **宿主配置目录**：保存模型、MCP、沙箱配置。不会自动读取目标仓库携带的 MCP 命令。
- **目标工作区**：需要修改的代码仓库，由 `--workspace` 指定，文件工具不能通过 `..` 或符号链接越界。

源码/可编辑安装时，默认工作区是本 Agent 项目根目录，不随终端 `cd` 改变；默认模型配置是该目录的 `config.json`。wheel 安装时，默认工作区是启动终端的当前目录，宿主配置位于 `~/.coding-agent`。

常用启动参数：

| 参数 | 用途 |
|---|---|
| `--workspace PATH` | 选择目标工作区 |
| `--config PATH` | 指定宿主模型配置 |
| `--mcp-config PATH` | 指定宿主 MCP 配置 |
| `--models-config PATH` | 指定系统模型档案；默认 `~/.coding-agent/models.json` |
| `--models [COMMAND]` | 管理模型档案后退出，不启动模型请求 |
| `--execution local\|docker` | 选择 Shell 执行后端 |
| `--sandbox-config PATH` | 指定宿主沙箱配置 |
| `--memory-home PATH` | 用户规则与记忆目录；默认 `~/.coding-agent` |
| `--approval-mode ask\|auto\|full` | 逐次确认 / 规则审批（默认）/ 跳过操作确认 |
| `--no-stream` | 关闭默认的实时文字输出 |
| `-r [SESSION_ID]` | 选择历史，或恢复指定 ID/唯一前缀 |
| `-c` | 恢复工作区最近一次会话 |
| `--list-sessions` | 只列历史，不启动模型 |

相对路径按启动终端的位置解析。从其他目录执行 `main.py` 时使用它的绝对路径。也可通过 `AGENT_WORKSPACE`、`AGENT_CONFIG`、`AGENT_MCP_CONFIG`、`AGENT_SANDBOX_CONFIG`、`AGENT_MEMORY_HOME` 和 `AGENT_EXECUTION` 配置相应默认值。

## 系统模型设置

在 CLI 中直接配置不同任务使用的模型；设置跨目标项目共享，只在后续回合生效：

```text
/models
/models add fast YOUR_FAST_MODEL_ID --max-tokens 3000 --context-limit 32000
/models add coding YOUR_CODING_MODEL_ID --max-tokens 8000 --context-limit 64000
/models use coding
/models assign codebase fast
/models assign implementation coding
/models assign memory fast
```

支持五种专业角色、记忆、Goal 验收和压缩摘要的独立绑定；可选不同的 Anthropic Messages API 兼容地址及密钥环境变量。
默认复用旧模型配置，不增加其他供应商协议。凭据不写入档案或会话，更换端点必须显式选择凭据。
`coding-agent --models help` 可以不启动会话就管理设置；完整用法见 [系统模型设置](docs/models.md)。

## Plan 与执行

默认是 Execute 模式，但主 Agent 仍不能直接写代码，需要按需派发 Implementation。

```text
/plan                         进入只读模式，等待任务
/plan 分析登录问题并给出方案    只分析，不实施
/execute                      按当前任务/方案执行；没有任务时等待新输入
/execute 按新要求修改登录逻辑   切换执行并使用指定任务
```

“先给方案”“只分析，不要修改”等明确表达同样进入 Plan。Plan 限制整个任务，不能派发 Implementation，也不能通过子角色、Goal 或 MCP 绕过只读约束。

方案产生后，用户可以明确说“按这个方案开始实施”。“可以”“好的”等简单肯定不会授予执行权限，会先询问并保持只读；需要明确的实施表达或 `/execute`。修改方案会更新版本；中断或恢复后，旧批准失效，必须由用户重新明确确认。

授权只来自交互层本次接收到的真实用户输入。历史消息、仓库文档、工具结果和子 Agent 回复都不能授权；模型也不能自行改变运行模式。新接入的交互适配器必须维护这一来源边界。

## 操作审批与流式输出

审批与 Plan/角色权限独立。默认 `auto` 使用宿主规则自动批准常规读取、文件修改和有限的项目检查命令；
Git 写操作、删除、安装、未知 Shell、敏感文件修改等需要用户确认。`ask` 每次工具调用都问（直接提问除外）；
`full` 跳过操作确认，但不解除 Plan、角色、文件工具工作区边界或危险命令的硬性拒绝。

```text
/permissions          查看当前审批模式
/permissions ask      每次确认
/permissions auto     常规自动批准，高风险或未知操作询问
/permissions full     不再弹操作确认
```

启动也可传 `--approval-mode ask|auto|full`，或通过宿主模型配置 `approval_mode` / 环境变量 `AGENT_APPROVAL_MODE` 设置。
运行时切换和单次批准不保存到会话；恢复时使用本次启动的宿主设置。模型、文档、工具输出不能切换级别。
`full` 风险较高：本机 Shell 可影响工作区外，测试/构建也会执行仓库代码；审批不是操作系统沙箱。

默认实时显示主角色与子角色的文字增量，最终回答不重复打印。SDK 收到完整消息后才交付工具参数，
不会在工具 JSON 尚未完整时执行；中断保留已展示文字，不执行半份响应。
`--no-stream` 或宿主模型配置 `stream: false` 可关闭，适用于不支持 SSE 的兼容接口。
三级审批、MCP 关系、记录与输出细节见 [操作审批与流式输出](docs/permissions.md)。

## Goal 与会话

普通任务在模型不再调用工具时结束，不强制经过 Goal 验收。Goal 只由开头的完整 `/goal` 命令显式启用，不从普通自然语言自动创建。

| 会话内命令 | 行为 |
|---|---|
| `/goal 目标描述` | 创建并执行长期目标 |
| `/goal` | 查看当前目标和状态 |
| `/goal edit 完整新目标` | 修改同一个目标；Execute 下立即执行 |
| `/goal resume` | 显式继续未完成目标 |
| `/goal pause` | 暂停但保留目标 |
| `/goal clear` | 解除目标，不删除聊天 |
| `/session`、`/sessions` | 查看当前 ID、历史列表 |

Goal 有 `active`、`paused`、`blocked`、`complete` 四种状态。普通新问题不恢复旧目标；正在执行的目标会先暂停，再回答新问题。已完成目标不能直接 resume，需要 edit 或新建。

**Plan 优先于 Goal。** Plan 下创建、edit 或 resume Goal，只更新目标并保持暂停，不开始执行；生成方案并取得新的执行确认后才可继续。进入 Plan 本身也不会自动恢复此前暂停的 Goal。

默认新建会话，历史和轻量状态保存在目标工作区的 `.sessions/`：

```bash
python -m coding_agent --workspace "/absolute/path/to/project" -r
python -m coding_agent --workspace "/absolute/path/to/project" -r <session-id>
python -m coding_agent --workspace "/absolute/path/to/project" -c
python -m coding_agent --workspace "/absolute/path/to/project" --list-sessions
```

恢复只加载历史，等待用户下一次输入：不重放工具、不恢复进程和 MCP 连接、不恢复审批。旧 `active` Goal 转为 `paused`，原先的 `paused`、`blocked`、`complete` 保留；旧待回答问题取消，新消息不会被当成它的答案。

运行中按 Esc / Ctrl+C 取消当前 Turn 及其子角色、所属进程，保存历史并将未完成 Goal 暂停，然后回到同一个会话的输入提示。取消不会回滚已经完成的文件修改；结果不明确的工具会保留未知状态，不能自动重试。`exit` / `quit` / `q` 或 Ctrl+D 退出 CLI 并清理本应用资源。

交互终端运行中可以直接输入纠偏，按 Enter 后在完整工具批次结束、下一次模型请求前送达，
与原任务共用 Turn 和调用预算；`/steer 内容` 效果相同。`/followup 内容` 排队追加任务，
当前回合正常结束后再开启新 Turn。中断、报错或等待回答时，未送达消息和未发送草稿会预填回输入框，
由用户编辑、确认；已经被模型接收的纠偏保存在历史中。队列只存在于当前进程，不随会话恢复自动执行。
纠偏不切换 Plan、审批模式或 Goal 状态；`/execute` 等宿主命令需要回到普通输入后提交。
管道输入仍采用逐回合读取，不启用运行中的键盘监听。

详情见 [会话保存与恢复](docs/sessions.md) 和 [目标循环](docs/goal-loop.md)。

## 代码检查点与撤销

每个真实用户回合的文件工具修改形成一个代码检查点；同一回合内的多次模型调用和子角色修改合并记录。

```text
/checkpoints     查看检查点、修改摘要和当前版本
/undo            撤销最近一个修改回合
/restore C1      恢复到 C1 完成后的受跟踪文件版本
```

恢复前先展示实际 diff，再确认目标和文件列表；即使 `full` 审批模式也需要这次人工确认。
Plan 下只能查看，不能恢复文件。确认后再次核对文件版本，手工改动冲突时停止，不强制覆盖。
恢复会话后仍可使用原会话的检查点；恢复代码保留聊天，暂停未完成 Goal，旧测试结果需要重新验证。

检查点只覆盖 `write_file` / `edit_file` 触及的文件，保存首次修改前内容、最终内容、权限和存在性。
它不复制整个仓库，不能保证撤销任意 Shell、Git、数据库或远端 MCP 操作。
检测到已跟踪文件的外部修改时，会先保存单独基线，`/undo` 保留该基线；旧分支的记录也会保留。
用法、版本分支和冲突处理见 [代码检查点](docs/checkpoints.md)。

## CLI 提问

主角色或子角色需要用户补充信息时，CLI 会显示问题卡片，区分问题、编号选项和操作提示。
输入编号选择，或直接输入自己的答案；没有选项时直接输入文字。空答案、无效编号会就地提示，不调用模型或丢失待回答问题。

卡片按终端宽度换行并对齐中文；真实终端使用颜色，管道、`NO_COLOR` 或 `TERM=dumb` 使用无颜色文本。
目前是编号输入，不提供上下键选择；Esc / Ctrl+C 取消，Ctrl+D 退出。回答不会自动授予写入权限或恢复暂停的 Goal。

## 规则与跨会话记忆

短规则与经验记忆分开：`~/.coding-agent/AGENTS.md` 放用户通用规则，工作区根目录 `AGENTS.md` 放项目规则。
规则在每次模型请求前读取，不自动扫描父仓库或嵌套目录，也不自动改写。

```text
~/.coding-agent/memory/     用户长期偏好，跨项目复用
<workspace>/.memory/       当前项目的反馈、稳定事实和资料线索
  MEMORY.md               自动重建的短索引
  <name>.md               带 YAML 元信息的独立记忆正文
```

每个真实用户回合先用目录选择最多 5 条相关记忆，再加载有界正文，主角色与子角色共享。
选择模型失败时降级为中英文关键词匹配。Execute 的完整回合结束后，宿主从**本次真实用户输入**中提取明确的长期信息，
校验持久性、原文依据和字段后写入独立记忆文件；不从工具输出、子角色猜测或恢复的历史自动学习，不自动改 `AGENTS.md`。
Plan、等待回答、中断和未完整结束的回合不提取。记忆只是背景，不能授权执行或改变权限。

```text
记住，我长期偏好使用 tab 缩进。     正常对话，回合结束后尝试保存用户偏好
本项目使用 unittest 运行测试。     正常对话，尝试保存项目事实
/memory list                      查看用户和当前项目的记忆
/memory show user/名称.md          阅读一条记忆；名称以 list 输出为准
/memory forget project/名称.md     删除一条记忆；Plan 下拒绝修改
```

默认复用当前模型：已有记忆时召回通常增加 1 次模型请求，合格回合的提取增加 1 次；达到 10 条后可能再整理 1 次。
辅助请求共用本轮调用上限，失败不影响已完成的编码结果。模型配置中 `memory_auto_extract: false` 可关闭自动提取，
`memory_enabled: false` 可关闭自动召回和提取；规则加载不受这两个开关影响。

记忆和本地备份可能包含私人信息，不要提交或上传。删除记忆会保留可恢复快照，**不等于彻底擦除**；
聊天历史也不会因此删除。完整设计、预算和恢复边界见 [记忆系统](docs/memory.md)。

## 压缩时保留工作代码

每次请求先计算完整预算：系统提示词、当前角色的工具定义、历史消息、输出预留和协议余量。
宿主配置 `context_token_limit` 默认 16,000，`max_tokens` 是其中的输出预留；升级前的限制只统计历史，现在需要按实际模型容量设置总预算。
估算用于提前压缩，实际用量以 API 返回为准；固定内容或当前输入放不下时会明确停止，不反复发送超限请求。

压缩先判断能否腾出有效空间，再机械整理或生成摘要；软阈值压不下去时延后处理，避免反复总结。
当前请求和近期成对的工具消息会保留，并额外保留有界的代码工作集。每次角色运行独立记录成功 `read_file` / `read_files` 时实际返回的片段和当时版本；
压缩后优先保留最近仍有效的代码，其他文件留下路径、旧行号范围及需要重读的提示。不会为补齐工作集自动读取新的文件正文。

近期段从尾部累计 token 选取，默认使用历史可用预算的一半、最多 8,000 tokens，并尽量对齐用户消息。
本轮纠偏和工具调用/结果始终成对保护。反复压缩时，旧摘要作为独立参考与新历史合并；
机械摘要不把旧摘要计入新消息的省略预算，模型摘要明确保留用户限制、已有决定、文件状态及真实检查结果。

工作集最多展示 8 个文件，总新增内容不超过 10,000 字符，且不能挤掉当前请求和新工具结果。
编辑、Shell、子角色或用户改动导致版本不匹配时，旧片段不再作为有效代码提供。
恢复会话或开启下一轮时，旧工作集仅作未验证的路径线索，不恢复版本、审批或执行。
完整流程、预算及与 Freebuff 的适配区别见 [上下文压缩](docs/context-compaction.md)。

## 代码工具与验证

- `list_files` 返回有深度和条目上限的目录树。Codebase 先定位最多 12 个候选文件，再用内容搜索、范围读取核实，报告代码位置、相关原因和调用关系。
- `list_files`、`glob`、`search_code` 共用文件发现规则：默认排除依赖和运行记录，并读取根目录及子目录的 `.gitignore`、可选 `.agentignore`；不跟随符号链接。每次调用重新扫描，文件和规则变化会在下一次调用生效。
- `search_code` 按字面内容搜索，返回文件、行号及附近代码；`read_file` 支持范围读取和行号。输出有界，截断时需要缩小查询或继续读取。
- `search_many` / `read_files` 一次提交最多 12 个查询或文件范围，合并重复项，整批共用 24,000 字符输出预算；复用现有权限、文件树和读取快照，不增加模型循环。
- `list_symbols` 列出单文件的 Python 类、函数和调用位置；`find_symbol` 按名称有界查找定义及调用线索。复用忽略规则，初版支持 `.py` / `.pyi` 的 AST，不解析类型、导入或动态调用；其他语言继续用 `search_code`。符号查询不会替代编辑前的 `read_file`。
- `read_url` 读取公开 HTTP(S) 文档，返回有界文本和最终来源 URL；不运行 JavaScript、不带登录身份，拒绝私网并逐跳核对地址。所有角色及 Plan 可用，网页内容只作资料。详见 [只读网页访问](docs/web-access.md)。
- 忽略规则只影响自动发现；显式 `read_file` 仍按原工作区边界处理，不因文件被忽略而禁止读取。完整流程、忽略示例和扫描上限见 [代码定位](docs/code-discovery.md)。
- 修改已有文件前必须读取。工具记录文件摘要，拒绝覆盖读取后发生的并发修改；`edit_file` 仍要求非空且唯一匹配。
- 同一文件、相同候选与原因的编辑失败达到 3 次时提示重新读取和修正，达到 6 次时停止整个回合；子角色、压缩和换工具 ID 不能清空计数。Goal 进入 `blocked`，等待真实用户决定下一步；此前已成功的修改保留。详见 [编辑失败保护](docs/edit-recovery.md)。
- `git_status`、`git_diff` 只有固定只读参数，不接受任意 Git 命令；读取 Git 状态时禁用外部 diff、textconv 和内容过滤器。
- Implementation 应阅读相关实现、最小修改、运行相关检查、根据失败修复，最后核对 diff。没有实际检查证据时必须说明未验证，不能仅凭模型判断宣称通过。
- `verification_status` 可随时读取宿主检查记录。回合收尾后自动汇总真实退出码、超时、取消和文件版本；最后修改后的旧检查会标记过期，没有检查会明确显示未验证。同命令修复重跑成功后，仍保留旧失败。结果与完整引用落盘，详见 [最终检查记录](docs/verification.md)。
- 文件写入和精确编辑先展示候选 diff，再按当前审批模式应用；`ask` 等你确认，`auto` 只对敏感文件询问，`full` 自动应用。等待期间文件变化就拒绝旧候选。新文件同样有 diff，完整对比和审批证据保存在运行记录中。
- 用户与工作区根目录的 `AGENTS.md` 提供短规则；经验记忆按相关性召回，不自动加载嵌套规则。两者都不能授予额外权限。

后台命令由当前 Turn 管理。Implementation 通过 `process_status` / `wait_process` 获取实际输出、退出码和状态；命令“已启动”不代表测试通过。Turn 结束、中断或退出时清理未完成进程，取消和超时都不能计作通过。没有跨回合通知队列，也不会自动唤醒模型。
资源清理失败时停止接收新任务；子角色提出必要问题时由宿主直接展示并等待用户，不能被主角色跳过。

角色完整记录、工具结果和进程输出保存在 `.task_outputs/`，压缩归档在 `.transcripts/`。这些文件及会话可能含敏感代码和输出；在其他目标仓库中也应忽略它们，不要提交或上传。

## Docker 边界

默认本机执行。Docker 模式需要运行 Docker Desktop/兼容运行时，并构建工具镜像：

```bash
docker build -t coding-agent-sandbox:local docker
python -m coding_agent --workspace "/absolute/path/to/project" --execution docker
```

沙箱配置参考 `config/sandbox.json`，可使用 `--sandbox-config` 覆盖。默认容器网络关闭；Docker 缺失或初始化失败时会报错，不静默降级到宿主 Shell。

**Docker 只隔离 Shell，不是把整个 Agent 放进容器。** 文件读写、固定 Git 查询、模型请求和 MCP 连接仍在宿主执行；工作区是可写绑定挂载，容器的只读根文件系统不代表工作区只读。Docker 目标工作区不能包含 Agent 的真实模型配置。完整边界见 [工作区与 Docker](docs/workspace.md)。

## MCP

使用官方 MCP Python SDK，支持 stdio 与远程 Streamable HTTP；服务按需连接，启动 Agent 不会自动连接全部服务。

浏览器接入提供独立的 [Chrome DevTools MCP 样例](config/mcp.browser.example.json)，可通过
`--mcp-config config/mcp.browser.example.json` 选择。Implementation 可以对用户授权的本地应用读取文本快照、
点击、填表、截图及检查控制台/网络结果；启动服务和调用工具沿用现有审批，Plan 不能操作浏览器。
截图以独立图片块传给支持视觉的模型，并保存可打开的图片文件；大图先等比缩小，旧图压缩后保留证据引用。
所选模型须显式声明 `supports_images: true`（CLI 添加档案时用 `--supports-images true`）；未声明或不支持时先阻止图片请求，保留截图并提示配置，详见 [模型设置](docs/models.md#图片能力)。
需要 Node.js 22.12+ 与 Chrome 149+；本项目不会自动安装它们。截图处理使用 requirements.txt 中的 Pillow。
样例只含 browser，可按 [MCP 文档](docs/mcp.md) 与 GitHub 的服务条目合并。

默认读取宿主 `config/mcp.json`，也可用 `--mcp-config` 或 `AGENT_MCP_CONFIG` 指定。GitHub 搜索配置参考 `config/mcp.example.json`：

```bash
#先在当前终端安全设置并 export GITHUB_MCP_TOKEN；不要把令牌写入源码。
python -m coding_agent --mcp-config config/mcp.example.json --workspace "/absolute/path/to/project"
```

随后可以请求“连接 github，搜索 Python 写的 coding agent 项目”。Agent 先调用 `connect_mcp`，发现工具后再按角色权限调用，不返回假搜索结果。

宿主配置分别控制：`allowed_tools` 是工具白名单，`read_only_tools` 是只读分类，`auto_approve` 是免确认列表。免确认不等于只读；未分类的工具不能提供给只读角色，也不信任服务端 `readOnlyHint` 来授权。示例使用 GitHub 只读端点并逐项分类，外部调用默认仍需确认。

启动 stdio MCP 是在宿主启动外部程序，即使只准备使用只读工具，`ask` / `auto` 下也需要独立确认；
`full` 可以跳过该确认，但不是给只读角色开放任意 Shell。更多配置与授权细节见 [MCP 接入](docs/mcp.md)。

## 工程与测试

```text
coding_agent/
├── application.py       #用户任务与会话生命周期
├── cli.py               #终端适配及唯一组合入口
├── agents/
│   ├── roles.py         #可信角色、任务和结果
│   └── runner.py        #共用 AgentRunner
├── runtime/
│   ├── tools.py         #唯一 ToolRuntime
│   ├── context.py       #共享工作区、权限模式、取消和证据
│   └── ...              #会话、Goal、压缩、执行环境、Turn 所属进程
├── tools/               #文件、搜索、Git、Skills、Todo、提问
└── integrations/        #MCP 连接与工具目录
```

```bash
make check
make package-check
python -m unittest discover -s tests -p 'test_*.py' -v
```

`make check` 运行 Ruff 与 unittest，不自动改写源码；可通过 `make PYTHON=/absolute/path/to/python check` 选择已有环境。`make package-check` 在临时目录构建 wheel/sdist，检查不包含真实凭据和运行状态，并验证安装后的 CLI。运行依赖维护在 `requirements.txt`。

测试不调用真实模型。真实 Docker 测试默认跳过，构建镜像后可显式启用：

```bash
AGENT_TEST_DOCKER=1 python -m unittest discover -s tests -p 'test_*.py' -v
```

旧 Cron、常驻 Team、邮箱、共享任务板和专用 worktree 调度已经退出主流程；不加载或恢复它们。已有历史、隐藏数据目录、Git 分支和 worktree 不因重构被删除，旧会话中的调用只作为历史保留。

更多说明：[架构](docs/architecture.md)、[开发](docs/development.md)、[Hook](docs/hooks.md)。`docs/workflow-design.md` 是未实现的历史设计草案，不代表存在 workflow 工具。

## 来源与许可证

本项目基于 [shareAI-lab/learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) 学习、扩展并整理，保留原项目的 MIT 许可证及版权声明，详见 [LICENSE](LICENSE)。
