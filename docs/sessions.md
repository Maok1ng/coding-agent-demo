# 会话保存与恢复

会话是一个工作区内可恢复的对话，不是长期记忆、Git 快照或进程快照。
检查点使用原子替换的 JSON 和进程锁，不引入数据库。

## 使用

```bash
#新建会话。
python -m coding_agent --workspace /absolute/path/to/project

#选择历史；也可指定完整 ID 或唯一前缀。
python -m coding_agent --workspace /absolute/path/to/project -r
python -m coding_agent --workspace /absolute/path/to/project -r <session-id>

#继续最近更新的会话，或仅列出历史。
python -m coding_agent --workspace /absolute/path/to/project -c
python -m coding_agent --workspace /absolute/path/to/project --list-sessions
```

`-r` / `--resume`、`-c` / `--continue`、`--list-sessions` 互斥，两个 Python 入口和安装后的 CLI 均支持。
仅列出历史不加载模型配置、不启动 Docker、不连接 MCP。
对话内用 `/session` 查看当前 ID，`/sessions` 查看列表；运行中不切换会话。

请保持 `--workspace` 与原会话一致。源码默认程序目录，wheel 默认启动目录，详见 `workspace.md`。

## 保存什么

每个会话保存为 `<workspace>/.sessions/<id>.json`：

- 元数据：格式版本、ID、工作区、首条用户消息标题和 UTC 时间。
- 可恢复上下文：用户消息、模型响应、工具调用及结果；结构化保存 SDK block。
- `state`：Todo、Goal、待回答问题，以及新增的 `state.plan`。
- Plan 保存方案标识、版本、内容和模式；批准事件不作为可恢复权限保存。

会话仍兼容 v1。旧快照缺少 Plan 字段时使用默认值，不要求批量迁移。
旧 Team/Cron 等工具调用只作为历史保留，不重新注册或执行。

关键输入、模型返回、工具结果、压缩和退出边界保存检查点。
实际工具执行前先保存调用；如果执行和结果保存之间中断，恢复时必须保守处理，不能伪造成功。
临时文件写入、同步后原子替换；保存失败停止继续执行。独占锁防止同时写同一会话。

压缩过的会话恢复“摘要和保留消息”，不自动重新塞回全部历史。
角色完整记录、工具证据及归档引用需要连同 `.task_outputs/`、`.transcripts/` 保留。
会话快照不是永久事件审计日志。

代码检查点单独保存在 `.task_outputs/checkpoints/<session-id>/`，保存文件工具修改的前后版本。
同一会话恢复后可通过 `/checkpoints` 查看，用 `/undo` 或 `/restore C编号` 主动恢复代码。
会话恢复本身仍只加载历史，绝不自动恢复文件；代码恢复也保留聊天历史。
检查点范围、外部修改基线和人工确认见 [代码检查点](checkpoints.md)。

## 恢复只加载，不执行

恢复后等待真实用户输入，不请求模型、不重放工具、不恢复进程或旧权限批准。

- 保留历史和 Todo；旧 active Goal 转为 paused，paused / blocked / complete 和原因保持。
- 旧待回答问题标记为 cancelled，不能把下一条新问题自动当作旧问题答案。
- 恢复或中断会使旧 Plan 批准失效；继续方案需要重新明确授权。
- 普通问题开启普通 Turn，不自动恢复旧 Goal；`/goal resume` 才请求继续目标。
- Plan 下 `/goal resume` 不解除只读；`/goal edit` 更新目标但保持暂停。
- 不恢复 Shell、后台命令、子角色运行或 MCP 连接；使用本次启动的配置与策略。

历史 Goal 的 waiting_user / error 兼容为 blocked，limit 兼容为 paused，
waiting_background 转换后也遵守 active 恢复为 paused 的规则。

## 中断、退出与结果未知

真实 TTY 下，运行中输入文字并按 Enter 会作为 steering 纠偏，不另开 Turn。
Runner 只在完整 assistant/工具批次的边界注入它；主角色和正在工作的子角色各接收一次，
仍共用本轮调用预算。宿主用 `USER_PROMPT` / `STEERING` 标签记录来源，标签不发送给模型 API，
也不能用来恢复权限、批准 Plan 或重新激活 Goal。

`/followup 内容` 将追加任务排到当前回合之后；只有正常完成且没有待回答问题时，
CLI 才通过正常用户提交入口开始新 Turn。运行中的 `/steer 内容` 等同直接输入纠偏，
其他宿主命令不会在纠偏入口执行，而是退回普通输入框等待确认。

取消优先于注入。模型还没完整接收的纠偏、尚未执行的 follow-up、未按 Enter 的草稿，
都会在监听线程停止后退回编辑器；已经接收的纠偏保留在会话历史中。
正常结束后未能送达的纠偏同样退回，不会悄悄变成新任务。返回的多条消息逐条预填，
需要用户确认；原本的 follow-up 只有满足正常结束条件才自动提交。
排队消息不落成恢复后自动执行的任务，也不恢复进程或授权。

交互终端运行时按 Esc 或 Ctrl+C 取消当前 Turn，返回同一会话的输入提示：

- 当前模型、压缩、验收、角色调用和所属 Shell / Docker 共用取消信号。
- 未完成 Goal 进入 paused；Plan 批准和待回答问题失效。
- 已完成结果保留，未分发工具标记未执行，已进入分发但未确认结果的工具保守记为结果未知。
- 保存修复后的历史，不执行正常续跑或等待后台通知自动唤醒。

后台命令属于当前 Turn，不再有独立常驻队友或跨回合队列；Turn 结束也会清理所属命令。
输入 `exit` / `quit` / `q` 或 EOF 才退出 CLI，清理本轮资源和 MCP 连接。
权限确认时 EOF 同样退出，而不是当作失败交给模型重试。

中断不是回滚：文件可能已经写入，远端 MCP 操作可能已经生效。
恢复后先核实磁盘与外部状态，不自动重试副作用。
历史中间缺配对、重复调用 ID、工作区不匹配或损坏 JSON 会明确拒绝或提示，不伪造成功。

## 数据与隐私

旧任务板、邮箱、Cron 文件、Git 分支和 worktree 保留在磁盘，但不参与新运行。
本次重构不回滚、不迁移或删除这些用户数据。

`.sessions/` 的新目录权限为 0700、文件为 0600；这是本机访问限制，不是加密。
快照和工具输出可能包含敏感数据，不要上传。
浏览器截图以独立图片块保存在当前会话中，归一后的图片文件保存在 `.task_outputs/runs/`，CLI 会显示路径。
恢复可保留尚未压缩的图片，仍只加载历史，不重新截图或启动浏览器；后续模型必须支持图片输入。
已归档或因请求大小限制移出的旧图只保留引用；需要重新观察当前页面时由正常工具调用再次截图。
目标工作区为其他仓库时，应自行忽略 `.sessions/`、`.task_outputs/` 和归档目录。
清理历史前先退出持锁进程；程序不自动删除会话或引用的证据。
