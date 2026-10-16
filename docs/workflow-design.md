# 工作流接入设计草案

本文是接入建议，尚未注册可执行 workflow 或修改现有执行循环。

## 现有机制与工作流的职责

| 机制 | 当前职责 | 引入工作流后的职责 |
| --- | --- | --- |
| Agent loop | 模型选择工具并执行 | 完成当前阶段内的具体任务 |
| Todo | 模型维护步骤清单 | 显示阶段中的细项，不作为阶段通过证据 |
| Task/team | 任务认领、依赖、队友协作 | 作为阶段执行方式，由 Lead 汇总结果 |
| Goal gate | 检查整个会话目标是否完成 | 最终验收，并提供阶段未完成的反馈 |
| ask_user | 主会话等待真实用户回答 | 暂停当前阶段和工作流，回答后恢复 |
| Workflow controller | 尚未存在 | 选择阶段、校验产物、控制跳转、记录运行状态 |

## 建议先实现一个串行的 bugfix 工作流

顺序为 clarify → reproduce → fix → verify。

- clarify：明确问题、限制和验收条件，缺少必要信息时调用 ask_user。
- reproduce：执行复现步骤，记录真实失败证据；未复现时不直接进入 fix。
- fix：根据已确认原因修改代码，记录变更和说明。
- verify：重新运行复现与相关测试；通过才进入最终 goal 验收。
- verify 失败：携带失败证据回到 fix，并累计重试预算。

有需要时，clarify 还可以回到用户；所有阶段都可以进入 blocked。
这些转移由程序定义，模型提供产物和建议，不可自行宣布任意跳转。

## 定义与运行实例分开

WorkflowDefinition 保存固定的阶段、输入/输出 schema、允许的执行方式和转移规则。
WorkflowRun 保存本次 run_id、实际参数、当前阶段、状态、产物与证据引用、预算、待回答问题。
同一个 bugfix 定义可以产生多个相互独立的运行实例。

第一版建议只允许一个 active workflow，避免同时引入并发运行与问题路由。
状态可以使用 running / waiting_user / blocked / completed / failed / cancelled。
blocked 表示可恢复的外部阻碍，failed 表示本次执行已终止；都不等于验收成功。

## 与当前代码的连接位置

1. 增加 workflow.py：定义 registry、WorkflowRun、阶段验证和状态转移。
2. 主入口增加显式启动、状态与取消入口；之后再提供给模型的启动工具。
3. 把 agent_loop 的会话状态收敛到 RunContext：messages、goal、user_input、workflow。
4. 每轮向 worker 注入当前阶段、输入、期望产物和约束，复用原工具权限路径。
5. worker 准备停止时，先核查 ask_user 等待，再检查阶段产物与通过条件。
6. 阶段完成后由 controller 选择下一阶段；最后一个阶段通过后再做整体 goal 验收。
7. 整体 goal 即使发现测试通过，也不能跳过尚未通过的必需阶段。

阶段完成不应调用当前的 set_goal 来覆盖整个用户目标，也不应清除全局目标。
可以使用阶段 validator 与独立的阶段评估上下文，复用验收器协议，但保留各自状态。
Todo 标记完成、Task 标记 completed 和 Workflow 阶段验收通过也要分别处理。

## ask_user 必须能让出执行权

阶段调用 ask_user 后，保存当前阶段、消息、工具结果和待回答问题，返回终端。
工作流保持 waiting_user；真实用户回答后恢复原阶段。
不能把等待实现成一个持锁的 Workflow 工具内部循环，否则 CLI 无法读取回答。
同一批的其他工具依然跳过，回答后重新调用；不要自动重放已执行过的副作用。

## 普通 agent 与 team 共用工作流定义

Workflow 的阶段与执行者分开。一个阶段可以由主 agent、一次性子 agent 或 team 执行。
例如分析由 Lead 完成，实现分配给队友，验证由主 agent 运行命令并汇总证据。
第一版仍可全部使用主 agent；之后只替换阶段 runner，保留阶段与验收规则。

队友的疑问先路由到 Lead；如果允许队友提出结构化问题，还需要 question_id、run_id、
stage_id 和 requester，确保用户回答回到正确的任务。目前尚未实现这条路由。
并行阶段涉及代码写入时还要明确文件归属或独立 worktree，不能只启动多个线程。

## s16 示例可借鉴的部分与适配边界

可借鉴可信 registry、结构化输出、run_id、journal、运行锁与阶段事件。
s16 的 AnthropicAgentRunner 不传工具，只根据输入文本生成结果，不能直接修复工作区。
其 install_workflow_tool 面向 s15 的宿主接口，和 `coding_agent/agents/loop.py` 的工具组装函数签名不同。
应通过明确的 runner 适配层复用思想，避免直接复制安装函数。

缓存证据要关联实际代码版本和输入。不能仅因提示词相同就复用旧测试或执行结果。
恢复时先检查已有操作和产物，防止重复写入、提交或发布。

## 验证重点

- 复现未通过时不能进入修改阶段。
- 验证失败时回到 fix，携带新的失败证据。
- ask_user 能返回终端，回答后恢复同一阶段。
- 阶段完成不能被误当成整个目标完成。
- 取消、异常、超预算分别记录，不自动标记 completed。
- 如果接入队友，任务完成事件必须能关联原运行与阶段。
