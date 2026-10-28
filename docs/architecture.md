# 专用 Coding Agent 架构

本项目围绕代码阅读、最小修改、真实验证和结果交付组织能力，不再把定时任务、常驻队友或共享任务板放进主循环。

```text
CLI
 └─ Application：用户输入、会话、Goal、Plan、运行生命周期
     └─ Orchestrator：理解任务、按需委派、汇总结果
         └─ AgentRunner + RoleSpec：所有角色共用的模型与工具循环
             └─ ToolRuntime：权限 → 参数校验 → 三级审批 → 执行 → 保存证据
                 └─ Environment：本地 / Docker Shell
```

CLI 负责终端输入输出；核心通过交互接口请求确认、提问和发送事件，不直接绑定终端。
会话状态由 Application 管理，本轮目录、模式、取消信号、证据和进程资源由 RunContext 传递。

## 一个内核，五种角色

| 角色 | 职责 | 权限 |
| --- | --- | --- |
| `orchestrator` | 与用户交流，选择角色，汇总结果 | 读取、提问、委派；不能修改 |
| `codebase` | 搜索代码，整理位置和关系 | 只读 |
| `planning` | 分析任务，提出方案与验证方法 | 只读 |
| `implementation` | 修改代码，运行命令，检查并修复失败 | Execute 授权范围内读写、执行 |
| `review_research` | 检查 diff、证据和风险，查阅资料 | 只读，不运行任意 Shell |

不是所有任务都经过五个角色。简单解释可以直接回答，修复任务可以直接委派 Implementation。
角色固定注册，不接受模型自定义权限；第一版串行执行，子角色不能再次委派。
角色默认继承旧模型配置，宿主可通过 `/models` 为角色和辅助任务选择不同档案与预算。
同一 Turn 冻结模型设置，仍共用 AgentRunner 的工具配对、压缩、中断和调用上限，详见 [系统模型设置](models.md)。

委派使用 `AgentTask` 传入任务、限制、验收条件、已有结论和必要上下文引用。
宿主把顶层原始任务传给每个子角色；模型不能用派发参数替换原始请求、取消报告要求或提升权限。
子角色使用独立上下文，通过 `report_result(summary, details)` 提交当前角色的结果；宿主按固定角色 Schema 校验后结束本次委派。
`AgentResult` 返回宿主状态、角色、摘要、专业结果及 `contract_status`；完整记录落盘，主会话不反复复制全部角色历史。
`verification`、代码及证据引用由宿主另外填写，保留真实退出码、状态和有界输出，不能被子角色的成功摘要替代。
结构校验只证明字段合法，不证明结论正确、检查通过或用户已经授权。
格式缺失或非法时，最多增加一次只允许提交报告的修正请求，共用本轮调用预算，不能重新执行文件修改、Shell 或其他工具。
`report_result` 是批次边界；同批其他调用不执行，必要的 `ask_user` 优先转交用户。
各角色的字段及恢复兼容说明见 [Agent 交接格式](agent-handoff.md)。
子任务完成不等于 Goal 完成，失败和缺少用户输入也不能伪装为成功。
子角色需要用户回答时，宿主直接把问题转交 CLI、结束当前回合，并拒绝同批剩余调用；
不等待主模型决定是否展示问题，也不让它代替用户回答。

## 权限在执行层

```text
实际权限 = 本轮用户授权 ∩ 角色能力 ∩ 工作区及工具策略
```

ToolRuntime 同时过滤模型可见工具，并在调用时重新检查。Hook 不能跳过内核权限。
操作审批分 `ask/auto/full`，默认规则自动审批；审批不能扩大角色能力、解除 Plan，亦不恢复旧批准。
审批结果单独记录为运行证据。默认模型调用使用可取消 SSE，文字增量只用于展示；
完整工具调用仍在 SDK 组装响应后经过同一 ToolRuntime，详见 `permissions.md`。
Plan 限制整个运行，不仅限制 Planning Agent；不能派发 Implementation 或通过 MCP 绕过。
MCP 的只读资格来自宿主 `read_only_tools`，与免确认的 `auto_approve` 分开，详见 `mcp.md`。

文件工具和固定参数的只读 Git 查询运行在宿主；任意 Shell 只交给 Implementation，
并通过 Environment 选择本地或 Docker。Docker 只隔离 Shell，不是整个 Agent 的沙箱。

## 状态与资源边界

- Session 保存历史、Todo、Goal、待回答问题和 Plan 状态；恢复只加载历史，等待用户输入。
- Plan 批准关联真实 HumanInput 和当前方案版本；修改方案、中断、恢复后旧批准无效。
- Goal 只由显式 `/goal` 命令启用，Plan 权限优先于 Goal 的续跑要求。
- 后台命令归当前 Turn 管理，保留输出与退出码；结束、中断、退出时清理，不跨回合自动唤醒。
- 前台或后台资源清理无法确认时，无论是否启用 Goal 都停止接收新任务，并明确标记结果不能算成功。
- 用户级与工作区根目录 `AGENTS.md` 提供短规则；Skills 按需加载。
- MemoryManager 在顶层回合入口召回、完整结束后提取；MemoryStore 保存独立 Markdown、索引和回滚快照。
  用户偏好跨项目共享，项目反馈/事实/引用保存在各自 `.memory/`；不使用向量库或新 Agent 循环，详见 `memory.md`。
- `.task_outputs/runs/` 保存执行证据；压缩与会话恢复都不等于文件、进程或 Git 回滚。

旧 Cron、Team、邮箱、任务板和专用 worktree 调度不再注册。历史中的旧调用仅作为记录保留，
不会重放；用户已有数据、Git 分支和 worktree 不由本次重构清除。

本版不增加大型 WorkflowController、Web 服务或云端调度，设计边界见 `workflow-design.md`。
