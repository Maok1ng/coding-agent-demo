# Goal gate 接入说明

## 使用

在原有运行环境中，从 `test` 目录启动 `python main.py`，然后输入：

```text
/goal 修复登录失败问题，复现检查成功，相关测试全部通过，保持接口兼容
/goal
/goal clear
```

第一条设置目标并立即执行。第二条只显示状态。第三条清除目标。
目标保持到验收成功、手动清除或替换；等待用户或遇到阻碍时保留目标。
普通用户输入会回答待处理问题，并恢复原目标；阻碍解除后可输入“条件已恢复，请继续”。
`stop/off/reset/none/cancel` 是 `clear` 的别名。暂不支持 pause/resume 命令。

`config.json` 可选增加以下字段，无需修改原有凭据字段：

```json
{
  "goal_block_cap": 8,
  "goal_max_turns": 40
}
```

`goal_evaluator_model` 可指定验收模型；缺省使用现有 `model`。
验收模型最大输出为 512 tokens，不传 tools。默认拦截上限为 8 次，
主模型调用上限为每次进入 agent_loop 40 次；两项只约束当前一次执行，不是全会话预算。

## 代码如何连接

1. `main.submit_query()` 处理 `/goal`，设置 `GOAL.active`，把目标正文作为用户请求加入历史。
2. `main.agent_loop()` 调用 `GOAL.begin_query(messages)`，绑定主会话列表。
3. 每次调用工作模型，把 active 目标追加到 system 上下文；即使历史压缩，目标仍然可见。
4. 有 tool_use 时，执行原有工具分发与权限 hooks，继续原循环。
5. 没有 tool_use 时，先收集响应生成期间完成的后台结果；有新结果则让 worker 读取。
6. 调用现有 `trigger_hook("Stop", messages)`；其中注册了 `GOAL.stop_hook`。
7. gate 用独立模型调用读取目标和最近约 24,000 字符的对话，包括工具结果。
8. 未完成时 hook 返回反馈字符串；原有循环将其作为 user 消息追加，然后继续。
9. gate 返回 complete 时清除 active；continue 注入反馈并续跑；wait_user 展示问题；
   blocked 展示阻碍并保留目标。异常、后台等待和次数超限也保留目标。

子 agent 也使用 `coding_agent/runtime/hooks.py`，但传入不同的 messages 列表。
gate 检查列表身份，因此子 agent 结束不会验收或续跑主目标。
验收调用直接请求 API，不进入 agent_loop，不会递归触发自身。

这是模型辅助验收，不能保证判断正确；独立调用不代表错误完全独立。
反馈标记为控制器生成，并明确它不能增加用户授权。user role 是当前 API 的消息载体。

## 已实现：ask_user 与四种 gate 决策

工作模型现在能调用主会话工具：

```json
{
  "name": "ask_user",
  "input": {
    "question": "你希望使用哪种数据库？",
    "options": ["SQLite", "PostgreSQL"]
  }
}
```

终端展示问题和编号，用户输入 `2` 会映射为 PostgreSQL，也可以直接输入自由文本。
选项最多 6 个；没有 options 时按自由文本回答。空输入不解除等待，越界编号会提示重选。
`/goal` 只查状态，不充当回答；`/goal clear` 或设置新目标会取消原问题。

ask_user 直接进入等待，跳过模型验收，普通任务与 goal 任务都支持。
同一响应只允许一个 ask_user；整批包含提问时，其他工具一律不执行，
并为每个 tool_use 生成配对结果。提问结果先记录 waiting_user，
收到真实回答后更新为 answered，取消则更新为 cancelled。
被跳过的工具返回错误结果，需要模型在用户回答后重新调用，不会自动重放。

gate 的协议已经从 ok/impossible 改为：

```json
{
  "decision": "wait_user",
  "reason": "数据库偏好需要用户决定",
  "question": "你希望使用哪种数据库？",
  "options": ["SQLite", "PostgreSQL"]
}
```

| 决策 | 行为 |
| --- | --- |
| complete | 验收成功，清除 active 目标，记录 completed |
| continue | 追加验收反馈，继续执行 |
| wait_user | 必须提供具体问题，保留目标，等待真实用户回答 |
| blocked | 保留目标和阻碍原因，停止自动执行，等待用户确认条件变化后继续 |

gate 也能将普通文本提问转换成 wait_user；问题会写入历史，确保下一轮理解用户回答。
这仍取决于模型判断；ask_user 的等待行为由程序保证，更明确。

运行状态与 gate 决策分开：目标的 status 为 active、waiting_user、blocked、
waiting_background、error 或 limit。error/limit 停止自动请求，不当作成功。
只有真实用户提交路径能回答问题；cron、队友事件不能解除等待。
队列线程在 waiting_user、blocked、error、limit 时保留事件，恢复后再交付。

ask_user 目前只提供给主模型；子 agent 的文本疑问需要先交回主模型处理。
工具负责收集回答，已有工具权限规则仍然执行，回答不会自动扩大工具权限。

## 下一步二：增加程序验收与证据记录

当前 gate 看的是工具输出、文本产物和可能被压缩的摘要，不能自行检查工作区。
为可检验的目标设置程序 validator，例如测试退出码、失败数量、实际执行的测试数量，
复现步骤的结果或接口兼容检查。模型负责解释缺口，controller 根据证据约束 complete。

记录 `criterion_id / tool_use_id / command / exit_code / artifact_path / revision`，
将每条验收条件与证据关联。修改代码后，旧版本的测试成功不能证明新版本成功。
文件已经写入与内容正确是两种验收条件，不能混为一谈。

不要只让 worker 声称“测试通过”，也不要让 worker 通过删除测试改变验收标准。
当前同步 Bash 在非零退出时提供明确错误，但成功时没有独立结构化退出码；
下一步应统一同步和后台结果格式，为 validator 提供一致字段。
后台通知只保留结果前 500 字符，完整输出应落盘并通过证据引用读取。

## 下一步三：增加实际预算和停滞检测

现有上限避免一次执行无限续跑，但每次新的用户输入或队列回合会重置计数。
应持久累计 worker、evaluator、compactor 等调用的 input/output tokens、费用、耗时，
在每次请求前检查预算，并给调用配置超时。子 agent 用量也要汇总到同一任务预算。

停滞不能只看有没有调用工具：重复读取同一个文件也是无进展。
比较证据、产物和待完成条件的变化，连续无变化时要求改变策略，达到上限则停止并报告。
纯文本任务用产物变化衡量进展；代码任务用 diff、验证结果和剩余失败数等信号。
API 失败时采用有界重试，重试也计入预算；预算耗尽应保留未完成状态。

## 下一步四：连接后台完成事件

当前有后台 Shell 任务尚未交付结果时 gate 会暂缓验收，保留 active 目标。
如果结果在 worker 响应期间完成，当前回合立即收集结果并继续。
如果结果在本轮返回以后完成，现有队列线程仅监听 cron 和队友邮箱；
需要下一次用户输入才会收集它，不会独立唤醒主模型。

增加“后台结果就绪”事件，交给现有 queue_processor_loop，获取 agent_lock 后统一交付。
不要每 0.2 秒仅因目标 active 就请求模型，也不要在后台线程直接修改 history。
事件交付需有确认与失败恢复：只有 worker 成功收到输出后才能消费事件，
否则模型请求失败后可能丢失唯一唤醒信号。
队友任务运行与等待审批也应纳入等待状态，避免 gate 强迫 Lead 重复轮询。

## 下一步五：持久化与用户控制

当前目标和状态均只存在内存中，退出程序后不会恢复。
保存目标 ID、版本、条件、状态、用量、最后验收和证据引用；记录 worker 回合与工具结果。
恢复时重新核查工作区和证据，不能直接重复可能已经完成的写入、提交或发布操作。

增加 pause/resume 和取消信号。当前 CLI 在同步 agent_loop 返回以前无法读取新输入，
所以运行中的目标不能靠再次输入 `/goal clear` 及时打断。
需要输入事件队列或可取消的运行任务，在安全边界响应暂停。
验收期间若目标已被替换，必须检查 goal_version，丢弃旧判定。

## 测试

```text
python -m unittest -v tests.test_goal
```

测试使用假客户端和验收结果，检查真实 agent_loop 与 Stop hook 的连接。
不调用网络、不读取真实 config.json、不执行实际工具命令。
这些测试验证控制流程；真实模型的判断质量还需用成功、缺证据、失败、等待用户等案例评估。
