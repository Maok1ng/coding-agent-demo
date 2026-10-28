# 代码定位与共用文件树

Codebase 使用现有只读工具，先找路径，再阅读相关实现，最后返回有依据的简短报告。
这一流程借鉴 Freebuff 的 [file-picker](https://github.com/CodebuffAI/freebuff/blob/e98f7304b8ed55d63662f0bb4cb477afe2636de6/agents/file-explorer/file-picker.ts#L26)
和 [共用文件树](https://github.com/CodebuffAI/freebuff/blob/e98f7304b8ed55d63662f0bb4cb477afe2636de6/common/src/project-file-tree.ts#L89)，
在现有 Python 工具层实现，不增加 Agent 类型、模型循环或向量索引。

## 定位流程

```text
用户问题和相关路径
  → list_files：查看有界目录树
  → glob / search_code / search_many：选出最多 12 个候选文件
  → read_file / read_files：阅读必要行范围，核实符号及调用关系
  → 报告文件、行号、相关原因、调用关系和未确认事项
```

已有准确路径时可以直接读取。目录结构只说明文件存在，Codebase 需要阅读内容才能判断其作用。
候选文件数量和报告格式属于角色工作约定；扫描上限、工作区边界及只读权限由宿主执行。

工具调用示例：

```text
list_files(path=".", max_depth=3, max_entries=200)
list_files(path="coding_agent/runtime", max_depth=2, max_entries=100)
glob(pattern="coding_agent/**/session*.py")
search_code(query="class SessionStore", path="coding_agent", context_lines=2, max_results=20)
read_file(path="coding_agent/runtime/session.py", start_line=1, end_line=80)
search_many(queries=[{"query": "class SessionStore", "path": "coding_agent"}, {"query": "class GoalController", "path": "coding_agent"}])
read_files(files=[{"path": "coding_agent/runtime/session.py", "start_line": 1, "end_line": 80}, {"path": "coding_agent/runtime/goal.py", "start_line": 1, "end_line": 60}])
```

这些是模型工具参数示例，不是终端命令。输出被截断时，应缩小 `path` 或查询范围；
目录树达到深度上限时，可以查看对应子目录或增加 `max_depth`。

`search_many` 和 `read_files` 是确定性的本地工具，没有增加模型调用或 Agent 类型。
每批最多 12 项，按首次出现顺序去重；相同目录的批量搜索复用一次文件发现结果。
每项仍保留原工具的范围、命中数和文件大小限制，整批标题、正文和截断说明共用 24000 字符，
按剩余项数分摊，避免第一个大文件占满输出。不同范围可以分别读取同一文件，重叠范围不自动合并。
单项读取失败会明确显示错误，其余项继续；没有实际展示的文件不登记编辑前置版本。
成功展示的读取片段及其 SHA256 会分别进入运行记录和代码工作集，压缩后仍可保留正在工作的代码。
只读角色和 Plan 可调用它们；`ask` 模式仍需用户批准每次批量调用，拒绝后整批不执行。

## 共用文件发现层

```text
RunContext.project_files
  └─ ProjectFiles
      ├─ list_files：目录树
      ├─ glob：路径匹配
      └─ search_code / search_many：同一候选文件集 → rg / Python 降级搜索
```

主角色和子角色共享同一个工作区及 `ProjectFiles` 对象。
每次工具调用重新扫描，忽略规则仅在本次扫描中复用；用户编辑、Shell 修改和规则变更会在下一次调用生效。
不启动索引线程，也不将整棵文件树固定注入每次模型请求。

`rg` 只负责在选定文件中查找字面内容；没有 `rg` 时，Python 搜索使用同一批候选文件。
搜索结果保留文件路径、行号及附近代码，读取与输出继续遵守现有大小限制。

## 忽略规则

默认排除 `.git`、`node_modules`、虚拟环境、构建目录，以及 `.sessions`、`.memory`、
`.task_outputs`、`.transcripts` 等运行记录。默认硬排除不能通过否定规则解除。
遍历不跟随文件或目录符号链接，也不越出当前工作区。

工作区根目录及子目录可以放置以下文件，规则使用 Gitignore 风格，由 `pathspec` 解析：

| 文件 | 用途 |
| --- | --- |
| `.gitignore` | 从自动发现中排除 Git 忽略的文件和目录 |
| `.agentignore` | 为本 Agent 额外排除不需要发现的内容 |

例如在工作区根目录创建 `.agentignore`：

```gitignore
# 忽略整个生成目录，扫描不会进入它。
/generated/

# 忽略 logs 内的条目，但保留说明文件；logs 目录本身仍可进入。
/logs/*
!/logs/README.md

# 忽略任意目录下的临时文件。
*.scratch
```

子目录规则相对于所在目录生效；`!` 可以撤销同一类规则中先前的匹配。
`.gitignore` 与 `.agentignore` 分别判定，只要任意一类仍排除该路径，它就不会出现在发现结果中。
因此 `.agentignore` 中的否定规则不会解除 `.gitignore` 的排除。
父目录已整体被排除时，扫描不会进入它，内部文件也不能通过子目录规则重新出现。

忽略只控制自动发现。显式 `read_file`、`read_files` 保持当前工作区权限，可读取已知路径的忽略文件。
这与 Freebuff 对显式读取继续保留 Agent 自有忽略规则的处理有区别；本次沿用项目原有读取行为。
`.agentignore` 不承担访问控制或操作系统沙箱职责，也不会修改 Plan、角色或审批权限。

## 扫描与输出上限

| 项目 | 上限 |
| --- | --- |
| `list_files` 默认深度 / 条目 | 3 层 / 200 条 |
| `list_files` 可指定深度 / 条目 | 10 层 / 1,000 条 |
| 公共扫描返回条目 | 10,000 条 |
| 单次扫描目录 / 实际检查条目 | 2,000 个 / 20,000 条 |
| 单份忽略文件 | 64 KiB |
| 工具输出 | 24,000 字符 |

达到扫描、深度或输出上限时会提示结果不完整，不能将当前列表当作全仓库清单。
普通目录不可读或扫描时被删除等情况也需要关注输出提示；忽略规则文件无法安全读取时报告错误。
遍历与搜索沿用当前 Turn 的取消信号。

## Python 符号定位

`list_symbols` 与 `find_symbol` 使用 Python 标准库 AST 按需解析源码，帮助先找定义和调用位置，
再用 `read_file` 核实附近实现。不会执行源码、建立常驻索引或加载整个仓库到模型上下文。

```text
list_symbols(path="coding_agent/runtime/session.py", max_results=50)
list_symbols(path="coding_agent/runtime/session.py", query="SessionStore")
find_symbol(symbol="SessionStore", path="coding_agent", max_results=30)
find_symbol(symbol="SessionStore.open", path="coding_agent")
```

`list_symbols` 面向单个 `.py` / `.pyi` 文件，`query` 是区分大小写的子串筛选。
`find_symbol` 在指定文件或目录下精确匹配简单名称或源码限定名称，不使用正则或模糊匹配。
结果分别列出 `definitions` 和 `calls`，包含路径、起止行号、种类及限定名称；方法、嵌套定义、
异步函数均有区分。装饰器的行号与定义行号分开，定义位置指向 `class` / `def` 那一行。

调用位置只是语法线索：`self.run()` 会被记录成 `self.run`，不能据此断言它调用了哪一个类的 `run`。
工具不解析导入别名、继承、接收者类型或动态调用；复杂的 `factory().run()` 仅保留 `run`，
并标记接收者无法解析。当前版本只解析 Python，其他语言会明确提示使用 `search_code`，
不会把跳过的文件报告为“没有符号”。

符号工具复用 `RunContext.project_files` 的工作区、忽略和链接规则，显式传入被忽略文件也不会解析。
扫描后的读取再次逐层拒绝链接，使用非阻塞普通文件读取，避免路径换链或命名管道导致越界和等待。
这类解析不计作 `read_file`：不更新编辑前置版本，也不向压缩工作集添加源码。

| 项目 | 上限 |
| --- | --- |
| 每次解析 Python 文件 | 100 个 |
| 每份源码 / 单次源码总量 | 2 MiB / 8 MiB |
| 每份源码 AST 节点 / 符号条目 | 100,000 / 2,000 |
| 默认单文件 / 查找结果 | 50 / 30 条，定义和调用合计 |
| 可指定结果数 / JSON 输出 | 最多 100 条 / 24,000 字符 |

达到上限、语法错误、读取中变化或文件无法安全读取时，结果包含 `truncated` 和 `warnings`。
缩小路径或名称后重试，不能从不完整结果推断整个仓库不存在某个符号。

## 回归检查

在已安装项目依赖的 `agent` 环境中执行：

```bash
python -m unittest tests.test_project_files tests.test_file_tools tests.test_symbol_tools tests.test_coding_runtime
```

这些测试在临时目录创建文件、忽略规则和符号链接，检查嵌套规则、否定规则、候选一致性、扫描上限、刷新与取消。
不读取用户真实会话或记忆，也不调用真实模型。
