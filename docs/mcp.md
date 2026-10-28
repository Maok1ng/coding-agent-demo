# MCP 接入

本项目使用官方 Python SDK `mcp==2.3.0`，不再内置返回固定文本的 docs/deploy 模拟服务。
真实 MCP 服务提供工具实现；本项目只负责连接、发现、权限检查、转发和结果回传。

## GitHub 搜索

1. 在项目 Python 环境执行 `python -m pip install -r requirements.txt`。
2. 使用 `config/mcp.example.json`，或将其内容保存为 `config/mcp.json`。
   当前本机已准备后一份文件；它被 Git 忽略，不应保存或提交真实令牌。
3. 准备 GitHub PAT，并只授予所需仓库和读取权限。不同搜索功能的令牌权限及 GitHub 策略可能不同；
   遇到 401/403 应检查认证或授权，不要直接扩大到全部权限。
4. 在启动 Agent 的同一个终端设置环境变量，再运行 Agent。macOS 默认 zsh 可以隐藏输入：

```zsh
read -rs 'GITHUB_MCP_TOKEN?GitHub token: '
export GITHUB_MCP_TOKEN
printf '\n'
python main.py --mcp-config config/mcp.example.json --workspace "/absolute/path/to/project"
```

不要把令牌粘贴给模型。程序不会自动加载 `.env`，也不会读取其他应用中保存的 GitHub 登录凭据。
程序里的 `${GITHUB_MCP_TOKEN}` 由宿主在连接时展开；缺少变量就停止，不发送空令牌。

进入 CLI 后输入：

```text
连接 github，搜索 Python 写的 coding agent 项目，给我仓库链接和简介。
```

主 Agent 先调用 `connect_mcp(name="github")`；连接成功后，下一次模型请求中出现：
`mcp__github__search_repositories` 等工具。只有服务实际返回且命中白名单的工具才会出现。
工具具体名称、参数和可用性以服务器的发现结果为准，不靠本地硬编码 handler。

示例指向 `https://api.githubcopilot.com/mcp/readonly`，并只暴露搜索/读取工具。
宿主使用 `read_only_tools` 明确标记这些工具可供只读角色使用；工具没有被标记时，
Orchestrator、Codebase、Planning 和 Review / Research 不会获得它，即使它已在 `auto_approve` 中。
`auto_approve` 默认是空列表，默认 `auto` 模式下工具调用仍需用户确认。如果你决定让某个已审核的只读工具直接运行，
可以将其原始工具名加入 `auto_approve`，例如 `"auto_approve": ["search_repositories"]`。
这两个配置互不替代：只读不等于免确认，免确认不等于只读。
不要依据服务器自己声明的 `readOnlyHint` 自动授权；只读声明也不是远端服务绝不写入的沙箱保证。

官方资料：[GitHub MCP Server](https://github.com/github/github-mcp-server)、
[GitHub 远程服务和只读地址](https://github.com/github/github-mcp-server/blob/main/docs/remote-server.md)。

## 浏览器检查本地应用

新增公开样例 `config/mcp.browser.example.json`，通过原有官方 MCP Python SDK 连接
[Chrome DevTools MCP](https://github.com/ChromeDevTools/chrome-devtools-mcp)。
浏览器工具由该外部服务实现，本项目继续使用同一条连接、发现、权限、执行和证据记录路径。

```bash
python -m coding_agent --workspace "/absolute/path/to/project" --mcp-config config/mcp.browser.example.json
```

进入 CLI 后可以输入：

```text
请 Implementation 连接 browser，检查 http://localhost:3000 的保存功能。
先读取页面快照，按快照中的真实 uid 操作；需要检查布局时拍摄截图，不传 filePath。
修改后再次截图检查页面变化，并检查控制台及网络错误；报告每一步的实际结果。
```

应用需要先启动。可以自行启动，也可以明确让 Implementation 使用已有 Shell/后台命令工具启动，
同一 Turn 的后台进程仍会在回合结束时清理。浏览器工具连接不等于已经启动或测试通过；
Chrome 通常在第一个需要浏览器的工具调用时启动。

样例需要可执行的 `npx`、官方支持的 Node.js LTS 和 Google Chrome；其中 URL 白名单选项要求 Chrome 149+。
首次连接时 `npx -y chrome-devtools-mcp@latest` 可能下载包到 npm 缓存；`ask` / `auto` 仍会先确认启动外部程序。
本项目不会在启动 CLI 时自动安装或连接它；没有依赖时安全报错，没有模拟结果或静默降级。
如需要固定版本，可由宿主将 `@latest` 替换为自行审核的发布版本。
参数与版本要求以官方[配置文档](https://github.com/ChromeDevTools/chrome-devtools-mcp/blob/main/docs/configuration.md)为准。

样例的选择如下：

- 使用 `--isolated` 临时浏览器资料目录与 headless 模式，不复用日常浏览器的登录和标签页。
- 使用服务的 URL 白名单，仅允许 `http://localhost:*/*` 和 `http://127.0.0.1:*/*`，
  本地应用引用的远端 CDN/接口也可能因此被阻止。其他来源需要用户审核后显式修改宿主配置；
  不会因页面加载失败自动扩大范围。
- 关闭调用方 JavaScript 求值、`file:` 导航、服务配置自动发现、遥测及 CrUX 请求，启用网络头脱敏。
  这仍是浏览器服务配置，不是完整的操作系统或出站网络沙箱；页面自身脚本和点击触发的请求仍会执行。
- 仅暴露页面导航、文本快照、截图、点击/填表、等待以及控制台/网络读取；不暴露文件上传、
  任意 `evaluate_script`、扩展安装、性能追踪等工具。完整参数通过 SDK 实际发现，不在本地复制工具 schema。
- `read_only_tools` 与 `auto_approve` 均为空。即便是快照/日志工具，也可能首次启动浏览器或触发页面运行，
  所以默认只交给 Execute 下的 Implementation；Plan 和其它只读角色无法直接调用。
  服务启动、工具调用各自确认，一次连接批准不会自动批准点击或提交。

`--no-javascript-evaluation` 不会禁止网页本身的 JavaScript。
浏览器点击、表单提交可能修改本地或远端数据，必须限定在用户真实授权的测试范围内。
`full` 宿主审批模式仍按已有语义跳过确认，不改变角色和 Plan 限制。

**文本快照**用于页面结构、元素 uid 和交互结果；`take_screenshot` 用于实际画面。
截图调用应省略 `filePath`，让 MCP 直接返回内嵌图片；传入 `filePath` 时，Chrome DevTools MCP 会改为落盘而不附图，
本项目不会依据返回的文件路径再读取宿主文件，也不会自动下载资源链接或图片 URL。
参数以官方[截图工具说明](https://github.com/ChromeDevTools/chrome-devtools-mcp/blob/main/docs/tool-reference.md#take_screenshot)为准。

图片经过宿主校验后以独立 `image` 协议块放入 `tool_result.content`，不再转成文字占位。
Implementation 当前选择的模型及其 Anthropic Messages 兼容端点必须支持图片；不支持时如实报告，不能声称看过画面。
模型档案须显式声明 `supports_images: true`，CLI 可用 `/models add vision 模型ID --supports-images true`
创建后再 `/models assign implementation vision`。声明为 `false` 或未声明时，宿主在发送图片请求前阻塞并保留截图记录；
不会按模型名称猜测能力、静默丢图或自动切换端点，详见 [模型图片能力](models.md#图片能力)。
UI 操作仍优先使用快照中的 uid，不能从缩放图片自行推算原始页面坐标。

宿主使用 Pillow 校验实际图片格式、字节数和像素数；PNG/JPEG/GIF/WebP 会重编码为 PNG 或 JPEG，动画只取第一帧。
每张输入最多 8 MiB、解码前最多 2500 万像素；归一后长边不超过 1568、像素不超过 110 万，
同时限制标准视觉模型的 patch 数，单张归一图片最多 3 MiB。采用保守的统一窗口，
符合官方[图片处理约定](https://platform.claude.com/docs/en/build-with-claude/vision)，不按模型名称猜测更大的视觉分辨率。
每个工具结果最多处理 4 张图片；整个请求最多保留最近 8 张、合计 16 MiB Base64 数据，
超出的旧图移出请求并留下引用和明确提示；无效图片标记工具错误，不转发原始二进制。
需要阅读长页面细节时，优先按元素截图或分段检查，避免整页图片缩小后文字难以辨认。

归一后的截图以仅当前用户可读写的文件保存到本轮 `.task_outputs/runs/`，CLI 展示截图路径，工具证据记录引用；
会话中保存归一后的图片协议块，恢复历史可继续查看当时画面，不重拍或重放操作。
上下文压缩保留最近受保护的图片；旧图片归档后只以引用进入摘要，摘要不能把占位文字当成视觉证据。
不会保留未经处理的原始截图。截图只证明拍摄时的页面画面，恢复旧图也不代表页面目前仍然如此。

控制台和网络结果按原样留入工具证据。
MCP 的 `is_error` 只表示工具执行是否报错；控制台错误或失败的页面步骤，需要根据实际内容报告，
不能因工具返回成功就认定应用测试通过。

浏览器样例只包含 `browser`。要同时接入 GitHub，可将两个公开样例的 `mcpServers` 条目合并到你选定的宿主配置；
不会自动覆盖现有 `config/mcp.json`，配置调整后重启 Agent。

## 配置字段

顶层使用 `mcpServers` 对象，键是模型连接时使用的服务名。以下是本项目支持的宿主配置约定，
不是承诺兼容所有产品的 MCP 配置格式。

- `type`：`http` / `streamable-http`，或 `stdio`。不支持旧式 SSE。
- `url`、`headers`：HTTP 服务地址和请求头；正式远程服务要求 HTTPS，HTTP 只允许 localhost/回环地址。
- `command`、`args`、`env`、`cwd`：stdio 子进程的命令、参数、显式环境和工作目录。
  相对 `cwd` 以配置文件所在目录为基准；相对脚本路径再相对于该 `cwd`。
- `allowed_tools`：只暴露列出的原始工具名。省略或 null 表示全部；空列表表示不开放任何工具。
- `read_only_tools`：经宿主审核、允许只读角色调用的原始工具名，默认空列表；不接受 null。
  只起分类作用，不能绕过 `allowed_tools`、运行模式或逐次确认。
- `auto_approve`：宿主审核过、可以免确认调用的原始工具名，默认空列表。
- `connect_timeout`：连接和发现整个阶段的秒数，默认 15。
- `timeout`：单次工具调用的秒数，默认 60。
- `enabled`：是否启用，默认 true。

`url`、请求头、命令、参数、显式环境和 cwd 支持 `${变量名}`。只在连接时展开，
不会自动将宿主的所有环境变量传给子进程；stdio 仅继承 SDK 的最小环境和配置中的 `env`。
配置在进程内缓存，修改后需要重启 Agent。配置文件错误在 CLI 启动时提示，服务故障在连接/调用时提示。

默认配置位于 Agent 自己的 `config/mcp.json`，不跟随 `--workspace` 去加载目标仓库里的命令。
可以用 `--mcp-config /absolute/path/mcp.json` 显式选择，优先于 `AGENT_MCP_CONFIG`。
没有默认配置时，普通本地工具仍可使用；显式选择不存在的配置则报错。

## 本地 stdio 示例

stdio 服务也是真实的 MCP，只是用本机子进程而不是 HTTP。服务脚本需要已由可信来源安装或开发。
下面命令和路径仅为示例，必须替换成你自己的文件，程序不会自动安装这些服务：

```json
{
  "mcpServers": {
    "my_local_service": {
      "type": "stdio",
      "command": "/absolute/path/to/python",
      "args": ["/absolute/path/to/server.py"],
      "cwd": "/absolute/path/to/project",
      "env": {"SERVICE_TOKEN": "${MY_SERVICE_TOKEN}"},
      "allowed_tools": ["search"],
      "read_only_tools": ["search"],
      "auto_approve": []
    }
  }
}
```

`ask` / `auto` 模式启动 stdio 服务前会独立要求确认；无法交互确认时拒绝启动。
MCP 工具自己的确认是另一道权限检查，不等于启动服务的授权。
把全部工具标记为只读或免确认，也不能跳过启动外部程序的确认。
用户显式选择宿主审批 `full` 时可以跳过上述确认；`ask` 则比 `auto_approve` 更严格。
三级审批均不放宽工具白名单、角色能力或 Plan 限制，详见 [操作审批](permissions.md)。

## 如何进入主循环

`connect_mcp` 连接 → SDK `list_tools`（含分页）→ 按角色过滤动态工具池 → 模型选择工具 →
ToolRuntime 校验参数、运行模式、角色和宿主策略 → SDK `call_tool` →
文本/JSON、图片协议块和 `is_error` → messages / 执行记录 → 模型继续。

连接和发现目录在角色之间共享，但角色的可用工具表互相独立。`tool_catalog()` 返回宿主元数据快照，
`get_mcp_tool_metadata(name)` 提供 `read_only`、`policy`、`server`、`raw_name` 和 `available`。
这些分类直接来自已连接服务的宿主配置，不依赖最后哪个角色组装过工具池；未知或已断开工具默认
不可用、不具备只读资格、需要确认。工具执行层会再次检查，隐藏工具不等于安全边界。

每个服务有一个专用线程和长期存在的异步事件循环，同一个协程进入和退出 SDK 上下文。
主循环仍是同步的，不为每次调用创建新连接，也不在多次 `asyncio.run` 之间搬运 SDK 会话。
退出 CLI 时先清理当前运行，再关闭 MCP 连接和本进程启动的 stdio 服务。

连接失败不会登记到工具池，调用失败会保留 `is_error`。网络异常的原文不直接交给模型，
避免请求头和令牌泄漏；当前会返回安全的错误类型。超时或断连可能发生在服务器已执行操作之后，
程序不自动重试调用，尤其不能盲目重试写操作。

## 当前边界

- 只接入 MCP tools；尚未开放 resources/prompts 的独立发现入口、OAuth 登录流程、工具热更新或自动重连。
- PAT/Header 认证可用；HTTP 的认证失败会报连接错误，用户需要自行检查令牌和服务配置。
- 文本和结构化输出保留；资源链接和内嵌文本显示为文字，MCP 直接返回的图片以独立协议块传给视觉模型。
  音频和内嵌二进制资源尚未开放；资源链接不会触发文件读取或网络请求，Base64 不会混入文字预览和摘要。
- 专业角色按宿主的 `read_only_tools` 和当前运行模式筛选外部工具；只有 Implementation
  在执行授权范围内可使用有副作用或未分类工具，不能因为父角色有权限就自动继承写入能力。
- MCP 不受现有 Bash Docker 后端自动隔离；stdio 服务默认运行在宿主。
  工具白名单和确认不是操作系统沙箱，服务代码必须可信，必要时自己配置容器启动命令。

## 验证

```bash
python -m unittest tests.test_mcp -v
python -m unittest tests.test_mcp_browser -v
python -m unittest discover -s tests -p 'test_*.py' -v
```

测试通过官方 SDK 启动独立的本地 stdio 服务，以及仅监听回环地址的真实 HTTP 服务，
检查连接、参数、工具白名单、只读分类与免确认的区别、角色间目录不互相污染、认证头、超时、
错误标记、子进程环境和退出清理。
不需要 GitHub 凭据、不访问 GitHub、不调用模型。测试用服务仅位于 tests/fixtures，
不会注册到产品默认工具池；生产 MCP 客户端没有模拟回退。

浏览器接入测试同样通过官方 SDK 和独立 stdio fixture，验证快照、交互参数、控制台错误证据、
截图图片协议、私有文件引用、坏图失败后的继续调用、工具白名单、拒绝启动/调用以及 Plan/角色隔离；
它们不安装或启动 Chrome，不代替真实浏览器验收。
