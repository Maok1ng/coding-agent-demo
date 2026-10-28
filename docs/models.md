# 系统模型设置

系统可以保存多份模型档案，并按任务选择模型。默认位置是当前用户的
`~/.coding-agent/models.json`，跨会话、跨目标项目共享；不会自动读取目标仓库中的同名文件。
模型设置由宿主管理，模型没有修改它的工具，历史消息也不会恢复旧的任务绑定。

## 在 CLI 中操作

```text
/models
/models help
/models add fast YOUR_FAST_MODEL_ID --max-tokens 3000 --context-limit 32000
/models add coding YOUR_CODING_MODEL_ID --max-tokens 8000 --context-limit 64000 --supports-images true
/models use coding
/models assign codebase fast
/models assign implementation coding
/models assign memory fast
/models assign compaction fast
/models reset codebase
/models remove fast
```

`add` 创建新档案，`use` 设置默认档案，`assign` 为一种任务选择专用档案。
`reset 任务` 解除一个任务绑定；`reset` 不带任务时解除默认及所有绑定，恢复旧模型配置。
`remove` 只删除没有被默认或任何任务引用的档案，上例的 fast 仍被 memory/compaction 引用时会拒绝删除。
要修改一个正在使用的档案，可以创建新别名，再通过 use/assign 切换。

命令直接由宿主处理，不调用模型、不修改项目代码，也不会恢复暂停的 Goal 或解除 Plan。
设置成功保存后，下一个真实用户任务生效；同一 Turn 的全部角色和辅助请求使用一份内存快照。
档案文件使用私有权限和原子替换，保存前检查外部修改，拒绝覆盖另一个进程刚写入的设置。

| 任务名 | 用途 |
| --- | --- |
| `orchestrator` | 主角色理解任务和汇总 |
| `codebase` | 查找代码和调用线索 |
| `planning` | 只读分析和规划 |
| `implementation` | 实施修改和运行检查 |
| `review_research` | 只读复核与资料查询 |
| `memory` | 记忆选择、提取和整理 |
| `goal_evaluator` | 判断 Goal 是否完成 |
| `compaction` | 上下文压缩中的模型摘要 |

不同模型沿用相同的角色权限、工具审批、取消和调用总上限。更强的模型不会获得额外工具权限。
单纯机械整理不调用摘要模型；只有需要模型摘要时才选择 compaction 档案。

也可以只管理设置后退出，不启动会话、模型请求、MCP 或 Docker：

```bash
coding-agent --models
coding-agent --models add fast YOUR_FAST_MODEL_ID --max-tokens 3000 --context-limit 32000
coding-agent --models assign codebase fast
coding-agent --models-config /absolute/path/models.json --models list
```

启动选项放在 `--models` 之前，其后的参数都属于管理命令。没有旧 API 配置或没有设置密钥环境变量时，
仍可管理档案；运行模型任务时才验证凭据。`--list-sessions` 仍只查询会话，不加载模型设置。

## 配置来源与兼容

公开样例见 `config/models.example.json`。请替换模型 ID，并按实际模型容量设置预算；样例数值不是容量探测。
可以显式使用 `--models-config PATH`，或在宿主模型配置中指定 `models_config`，或设置环境变量
`AGENT_MODELS_CONFIG`，优先级依次降低，最后才使用用户默认位置。

任务选择顺序为：显式任务绑定 → 旧的 memory_model/goal_evaluator_model → 系统默认档案 → 旧主模型配置。
compaction 没有专用绑定时继承所属角色的模型、端点和预算，保持原来的压缩行为。
未配置任何档案时，原 `config.json` 的 model/max_tokens/context_token_limit 继续生效。
会话中的旧 model 字段只作记录，不覆盖当前宿主设置。

## 图片能力

`supports_images` 声明这份模型档案是否支持图片，只接受 JSON 布尔值 `true` 或 `false`。
CLI 使用 `--supports-images true` 或 `--supports-images false`，`/models` 会显示支持、不支持或未声明。
旧主模型配置也可以加入此字段；切换到一份新档案时，不会继承旧模型的图片能力。
未单独绑定的 compaction 继续继承所属角色的设置。

普通文字任务不要求声明图片能力。浏览器截图等图片进入当前模型上下文时，只有显式设置为 `true`
才会发送请求；设置为 `false` 或未声明都会先停止，并提示选择支持图片的模型档案。
截图和历史仍会保存，不会静默丢掉图片、猜测模型名称或自动切换到其他模型。
声明由宿主配置提供，不代表程序已经探测或验证实际 API 端点的图片能力。

若已有档案缺少此字段，可以创建新别名后重新绑定，例如：

```text
/models add vision YOUR_VISION_MODEL_ID --supports-images true --max-tokens 8000 --context-limit 64000
/models assign implementation vision
```

请按所用服务的实际能力填写；公开样例中的 `false` 只是要求显式配置，不代表某个模型的真实能力。

## 端点与凭据

默认复用原模型配置中的 API 地址和凭据。新档案可以选择另一个 **Anthropic Messages API 兼容端点**：

```text
/models add remote YOUR_MODEL_ID --base-url https://your-gateway.example/v1 --api-key-env CODING_REMOTE_KEY --max-tokens 4000 --context-limit 32000
```

`api_key_env` 只填写环境变量名称，实际密钥由用户在启动终端中设置，不写进档案、命令、会话或运行记录。
切换到另一个地址必须显式指定凭据变量；变量缺失就拒绝请求，绝不把旧端点密钥发送到新端点。
远程地址要求 HTTPS，本机回环服务可以使用 HTTP。不支持在地址中夹带认证信息、查询串或片段。
同一回合中不同端点/凭据使用独立连接池，回合取消和结束时统一清理。

这是现有 API 协议内的模型配置能力，尚未增加 OpenAI Chat Completions 等其他协议适配器。
相同 API 地址能否提供某个模型，由实际服务决定；档案存在不代表服务已验证可用。

## 预算和运行边界

每份档案可设置输出预留 max_tokens 和完整请求预算 context_token_limit；不接受字符串或布尔值。
主角色、子角色按选中档案计算系统提示词、工具、历史和输出预留；摘要助手使用自己的档案预算。
记忆和 Goal 验收仍保留原用途输出上限，档案只能进一步收紧，不能扩大为无限长输出。
预算不足会明确阻塞或按原辅助任务规则降级，不会静默改用其他端点。

本轮没有增加自动选模型、失败后自动切换供应商、计费预测或并行调度。
