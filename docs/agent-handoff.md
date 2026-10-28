# Agent 交接格式

交接分成两件事：主 Agent 说明要做什么，专业角色说明实际发现或完成了什么。
结构由宿主校验，权限和执行事实仍由原运行时负责，不新增模型循环或工作流引擎。

```text
Orchestrator
  → dispatch_agent：任务说明 + 限制 + 验收条件 + 已有结论 + 引用
  → 宿主补充顶层原始任务，检查可信角色与模式
  → 专业角色独立阅读、分析或实施
  → report_result：摘要 + 当前角色的专业结果
  → 宿主校验格式，附上真实执行记录和证据引用
  → Orchestrator 汇总并回答用户
```

## 任务输入

`AgentTask` 保留原来的 `role`、`instruction`、`context_refs`，增加明确的上下文栏位。
`dispatch_agent` 的模型参数如下：

| 字段 | 含义 |
| --- | --- |
| `role` | 已注册的专业角色，不能自定义或再次派发 |
| `instruction` | 本次子任务的具体目标 |
| `context_refs` | 相关代码或工作区内证据的路径 |
| `constraints` | 本次任务需要遵守的限制 |
| `acceptance_criteria` | 用什么条件判断结果满足要求 |
| `known_facts` | 已有结论与线索，仍需按当前代码核实 |

三个新增数组可以省略；每个最多 10 项，每项最多 1,000 字符。
`original_request` 和 `require_report` 由宿主填写，不是模型可提交的派发参数。
子角色收到顶层原始任务，但不会复制整个父会话。

例如：

```json
{
  "role": "codebase",
  "instruction": "定位会话恢复的入口和相关状态处理，暂不修改代码",
  "context_refs": ["coding_agent/runtime/session.py"],
  "constraints": ["保留当前会话恢复语义"],
  "acceptance_criteria": ["说明恢复入口、调用关系和未完成 Goal 的处理位置"],
  "known_facts": ["用户希望恢复后等待下一次输入"]
}
```

这些内容是任务上下文，不是新的授权来源。`known_facts` 可能过期；限制或验收条件也不能解除 Plan、角色权限、审批或工作区边界。
重要需求不能只藏在代码注释或自由文本摘要里，主 Agent 应放进相应任务字段。

## 各角色的结果

正式委派的专业角色通过同一个工具提交结果：

```text
report_result(summary="简短结论", details={当前角色规定的字段})
```

`summary` 为 1–4,000 字符；整个报告 JSON 最多 16,000 字符。
每个角色的 `details` 结构由宿主固定，模型不能通过参数选用另一个角色的结构。
需要用户回答时使用 `ask_user`，不能用一份“已完成”报告代替必要提问。

| 角色 | `details` 字段 | 要表达的内容 |
| --- | --- | --- |
| Codebase | `locations`、`relationships`、`uncertainties` | 代码位置、相关原因、调用关系和未确认事项 |
| Planning | `steps`、`risks`、`open_questions` | 修改步骤、涉及文件、验证办法、风险和待确认问题 |
| Implementation | `changes`、`unverified`、`risks` | 实际修改说明、未验证事项和剩余风险 |
| Review / Research | `findings`、`sources`、`conclusion`、`uncertainties` | 可行动问题、资料来源、结论和不确定性 |

各数组可以为空，但所列栏位不能省略。没有问题的 Review 应提交空 `findings`，无需编造问题。
每类条目的形状如下：

```text
locations[] = {path, start_line, end_line?, reason}       最多 12 项
steps[] = {description, files: [path], validation}        最多 20 项
changes[] = {path, description}                          最多 30 项
findings[] = {
  severity: "P0" | "P1" | "P2" | "P3",
  description, basis, path?, start_line?, end_line?
}                                                       最多 20 项
```

`?` 表示可选。位置行号从 1 开始，结束行不能早于开始行；Review 提供行号时也必须给出对应路径。
代码路径和 Planning 的 `files` 必须位于工作区内，不能用绝对路径、`..` 或越界符号链接绕过边界。
资料来源与问题依据是模型报告的引用或说明，结构合法不代表宿主已经核实其内容。

以下为假设项目的 Codebase 格式示例，路径和行号仅用于演示：

```json
{
  "summary": "会话加载入口位于 session 模块。",
  "details": {
    "locations": [
      {"path": "src/session.py", "start_line": 42, "reason": "load_session 负责读取历史记录"}
    ],
    "relationships": [],
    "uncertainties": ["尚未确认加载失败后的 CLI 提示行为"]
  }
}
```

## 宿主填写的字段

模型只填写 `summary` 和 `details`。`AgentResult` 中以下字段由宿主生成：

- `role`、`status`：实际执行角色与运行状态。
- `contract_status`：报告是否通过结构校验；正常交接为 `validated`，缺失或非法为 `missing` / `invalid`，无需报告的调用为 `not_required`。
- `evidence_refs`、`code_refs`：实际运行中记录的证据和代码引用。
- `verification`：真实命令状态、退出码、有界输出及代码版本记录。
- `verification_summary`：顶层应用收尾后生成的最终检查汇总；子角色不会填写，原始证据仍单独保留。
- `question`：通过提问流程产生、需要交给真实用户的问题。

报告参数不接受这些宿主字段，也不接受修改权限、模式或 Goal 的字段。
`details.changes` 是模型对修改的说明；实际 diff 和检查结果仍以工具证据为准。
`validated` 只说明结构通过校验；子任务结束、检查通过和 Goal 完成是不同的判断。

## 格式错误、提问与中断

宿主不会把普通文字直接当作合格报告。报告缺失或非法时，最多请求一次格式修正；这次请求计入原有模型调用上限。
修正期间只允许 `report_result`，不能再次编辑文件、执行 Shell、读取工具或调用 MCP，避免为修正格式重做工作。
再次失败时保留相应状态和记录，父 Agent 不能把它视作合格交接。
在 `ask` 审批模式下，用户拒绝提交报告会直接停止该委派，不会当作格式错误再次请求同意。

`report_result` 是批次边界，同批其他调用不会执行；`ask_user` 优先处理。
工具调用标识无效或在同批重复时，整批工具在执行前被拒绝，原始响应另存审计记录。
需要用户输入、达到调用上限、取消或异常退出时，宿主保留真实状态，不要求模型补一份“成功”报告。
Esc / Ctrl+C、进程清理、Goal 暂停和会话保存继续使用现有流程。

旧会话中的历史委派和文字摘要继续作为历史读取，不重新注册、重放工具或强制转换成新报告格式。
主 Agent 的最终用户回答仍是自然语言，无需把内部 JSON 直接展示在 CLI 中。

## 借鉴来源

参考 Freebuff 固定提交 `e98f7304b8ed55d63662f0bb4cb477afe2636de6` 的三个机制：

- [Thinker 的输出 Schema](https://github.com/CodebuffAI/freebuff/blob/e98f7304b8ed55d63662f0bb4cb477afe2636de6/agents/thinker/thinker.ts#L22)：明确角色结果结构。
- [set_output 的宿主校验](https://github.com/CodebuffAI/freebuff/blob/e98f7304b8ed55d63662f0bb4cb477afe2636de6/packages/agent-runtime/src/tools/handlers/tool/set-output.ts#L44)：校验失败不保存结构化结果。
- [父角色输出选择](https://github.com/CodebuffAI/freebuff/blob/e98f7304b8ed55d63662f0bb4cb477afe2636de6/packages/agent-runtime/src/util/agent-output.ts#L72)：按输出模式返回必要内容。

Freebuff 并非所有角色都使用结构化输出；它的 `set_output` 本身也不结束 Agent 步骤。
本项目按已有 Runner 适配：成功的 `report_result` 结束本次专业角色委派，用户对话继续由主 Agent 管理。
上述源码可以通过 GitHub 公开内容 API 阅读，不要求用户 clone，也不依赖 GitHub MCP。
