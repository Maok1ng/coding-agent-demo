# 只读网页访问

`read_url` 供所有只读角色和 Implementation 阅读公开文档，Plan 下同样可用。
主工具执行层继续进行角色、参数、审批和取消检查；网页正文只作为外部资料，不能授予权限、
切换模式、回答用户确认或自动写入长期记忆。

```json
{"url": "https://docs.python.org/3/library/ast.html", "max_chars": 12000}
```

参数只有 URL 与输出上限（1,000–20,000 字符）。成功返回 JSON：原始 URL、最终 URL、
页面标题、正文、是否截断，以及外部资料提示。完整工具输出沿用本轮工具记录保存。
它不提供网页搜索；搜索服务仍可通过 MCP 接入。

## 网络边界

- 只发送 HTTP/HTTPS 的 GET，限标准端口 80/443；不接受自定义请求头、请求体、URL 用户名或密码。
- 直接使用 HTTP 连接，不读取系统代理、浏览器身份、Cookie、netrc 或系统登录信息；也不保存响应 Cookie。
- DNS 所有结果都必须是公网地址；私网、回环、链路本地、多播、保留地址、映射 IPv4 和常见 IPv6 过渡地址会被拒绝。
- TCP 连接使用本次检查过的具体 IP，不再按域名做第二次解析。HTTP 保留原 Host，HTTPS 使用默认可信证书链并验证原域名。
- 最多跟随 3 次跳转，每次重新检查 URL 和 DNS；不会把 HTTPS 降级为 HTTP，也不向下一个地址转发凭据。
- DNS、连接、响应头和正文共用 20 秒截止时间；网络连接额外用定时器停止，防止慢速滴流反复延长等待。
- 正文最多 2 MiB；只接受 HTML、纯文本、Markdown 和 JSON，要求服务端使用未压缩内容。体积超限、压缩、非文本和不完整响应会返回错误。

标准库的系统 DNS 解析不能强制取消。等待者可在中断或超时后立即离开；
尚未结束的系统查询最多保留一个 daemon 线程，期间新域名查询返回“DNS resolver is still busy”，
不会反复创建卡住的解析线程。它只完成已开始的 DNS 查询，不会在用户中断后继续发 HTTP 请求。

## 正文和生命周期

HTML 只用标准库解析成文本，去掉 script、style、nav、模板、内嵌对象等内容。
不会运行 JavaScript、点击页面、提交表单、下载资源或写入工作区文件。缺失或未知编码降级为 UTF-8 替换解码。
需要登录或 JavaScript 渲染的文档可能读不到；这里没有浏览器自动化或渲染降级。

连接注册到当前 Turn 的取消回调；Esc / Ctrl+C 会关闭正在连接或读取的 socket。
即使 HTTP 的 `Connection: close` 响应已将 socket 转交给响应正文，宿主仍独立保留传输句柄，
可以主动 shutdown 正在阻塞的正文读取，不必等到原 socket 超时。
请求结束、失败和中断都关闭响应和连接，并解除取消回调；不在会话恢复时重放请求。
“只读”指工具只提供固定 GET 和文本读取，不保证每个第三方网站都正确遵守 GET 无副作用约定。
该工具运行在宿主，不通过 Docker Shell 的网络配置；公网地址限制也不能代替操作系统网络隔离。

实现依据：标准库 [HTTP 连接](https://docs.python.org/3/library/http.client.html)、
[DNS/socket](https://docs.python.org/3/library/socket.html#socket.getaddrinfo) 与
[TLS 默认验证及主机名](https://docs.python.org/3/library/ssl.html#ssl.create_default_context)。
常规测试注入假的 DNS、socket 和响应；取消回归额外使用本机 `socketpair` 验证真实的阻塞读取，
不监听端口，也不访问真实网络、模型或配置。
