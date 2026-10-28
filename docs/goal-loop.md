# Goal：显式长期目标与验收

普通请求不需要 Goal：模型使用工具后继续，没有工具调用时结束本轮。
只有完整 `/goal` 命令词才管理目标；例如 `/goalkeeper` 仍是普通输入。
自然语言不会自动创建、修改或恢复 Goal。

## 命令与状态

```text
/goal 修复密码登录，保持接口兼容，相关测试通过
/goal
/goal edit 只修复密码登录，暂不修改短信登录
/goal pause
/goal resume
/goal clear
```

| 命令 | 行为 |
| --- | --- |
| `/goal <目标>` | 新建目标；Execute 下开始执行，Plan 下不获得写入权限 |
| `/goal` | 本地查看目标，不请求模型 |
| `/goal edit <完整描述>` | 修改当前目标、增加版本、旧结论失效；Execute 下立即执行 |
| `/goal pause` | 暂停；blocked 也可改为 paused，complete 保持 |
| `/goal resume` | 明确恢复未完成目标，但不能解除 Plan |
| `/goal clear` | 解除当前目标，不删除聊天、代码或证据 |

edit 保留旧描述、历史与累计验收次数；空描述或不存在当前目标时只报错。
已完成目标不能直接 resume，可以 edit 为新版本，或新建目标。
`stop/off/reset/none/cancel` 保留为 clear 的别名。

| 状态 | 含义 | 是否续跑 |
| --- | --- | --- |
| active | 当前启用的目标 | 在权限及调用上限内继续 |
| paused | 用户暂停、中断、切换普通问题或触及次数上限 | 否 |
| blocked | 缺必要信息、权限、外部条件，或模型/验收失败 | 否 |
| complete | 验收通过，保留完成记录 | 否 |

普通问题只暂停 active 目标，不解除旧 blocked，也不用旧目标验收新回答。
解除阻碍后仍需显式 resume 或 edit，不能因普通聊天自动恢复长期执行。

## Plan 的权限优先

Goal 状态描述是否继续追求目标，Plan 模式描述允许做什么，两者不能混为一谈。

- `/plan` 或明确的“先给方案”请求进入只读。
- Plan 下 edit 可以更新目标，但保持暂停；resume 不能放开写入或派发 Implementation。
- 当前真实用户输入 `/execute`，或明确确认实施当前方案，才由宿主切换权限。
- 批准关联当前方案版本；文档、工具输出、子角色或历史 user 消息不能替用户批准。
- 修改方案、中断或恢复后不沿用旧批准。含糊的回复先询问，不自行开始修改。

因此 active Goal、验收反馈、Hook 或“任务尚未完成”都不能覆盖 Plan 和用户授权。

## 主任务验收与角色执行

Application 管理 Goal，Orchestrator 按需委派角色，所有角色共用 AgentRunner。
只有 Implementation 在 Execute 授权内修改或运行 Shell，子角色不能再次委派。
Codebase、Planning、Review / Research 的完成，只表示该次委派结束，不表示整个 Goal 完成。

主任务准备结束时，active Goal 可以进行独立模型验收；普通轮次不强制进入长期续跑。
验收模型只读取目标和已有上下文/证据，不调用工具，不递归启动角色。
决策包括：

| 决策 | 后续处理 |
| --- | --- |
| complete | 保留目标，标记完成 |
| continue | 追加控制器反馈，在现有权限内继续 |
| wait_user | 提出具体问题，目标受阻，等待真实用户输入 |
| blocked | 记录阻碍并停止，等待条件变化后显式恢复 |

反馈由控制器生成，不是新用户授权；验收失败和次数超限都不是成功。
`goal_block_cap`、`goal_max_turns` 保留安全次数限制；角色还共享本轮调用上限，
不能靠不断委派绕过。未增加 Token 预算、USAGE_LIMITED 或 BUDGET_LIMITED 状态。

验收是模型辅助判断，不是正确性证明。Implementation 应运行相关测试/构建/检查，
记录真实命令、退出码和输出引用；Review 检查实际 diff 与证据。
后台命令启动成功不等于验证通过，running、failed、cancelled、timeout 不能冒充通过。
修改之后应重新运行相关检查，旧代码版本的成功结果不能证明新修改正确。
本版不引入通用验收条件 DSL 或大型阶段控制器。

## 提问、后台命令与中断

缺少关键选择时通过 ask_user 向真实用户提问，不能让子角色猜测授权或伪造答案。
问题、取消和回答记录在会话中；恢复时旧问题取消，不强迫下一条消息回答旧问题。

后台命令归当前 Turn，不再通过 Cron、邮箱或队友通知创建自动回合。
需要作为验证依据时由 Implementation 等待其实际结果；Turn 结束清理仍在运行的所属进程，
输出和退出码保留为证据。没有跨回合后台工作恢复。

运行中按 Esc / Ctrl+C 取消模型、角色、压缩、验收和所属进程：
未完成 Goal 进入 paused，Plan 批准失效，工具结果配对修复后保存，再回到同一个输入提示。
不会因为正常 Stop 或迟到结果立即重启目标。exit / quit / q 或 EOF 才退出 CLI。

恢复只加载历史：旧 active 变为 paused，其余状态和原因保留；不恢复进程、连接或权限批准。
Goal 旧状态 waiting_user / error 兼容为 blocked，limit 为 paused，
waiting_background 转换后同样暂停。中断不是回滚，文件和远端操作的实际效果需要重新核实。

## 测试边界

Goal、Plan、角色权限、会话恢复和中断测试使用假模型及本地工具，不需要真实密钥。
它们验证控制流程和证据状态，不证明真实模型一定能正确理解需求或验收结果。
本轮不建设能力测评平台。
