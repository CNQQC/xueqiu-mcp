# 雪球 MCP Server

[![CI](https://github.com/CNQQC/xueqiu-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/CNQQC/xueqiu-mcp/actions/workflows/ci.yml)

把[雪球](https://xueqiu.com)的行情、财务、资金与**社区论坛**数据接入任何支持 MCP 的客户端（Claude Code、Claude Desktop、Cherry Studio 等）。

覆盖 **A 股 / 港股 / 美股**，外加指数、ETF、可转债。共 **28 个工具**。

## 特点

- **面向大模型的输出**：雪球原始接口返回的是 `ncf_from_oa`、`1.7205417189091E11` 这类字段和数值。本项目把 **600+ 个财务字段翻译成中文**、把金额换算成「亿元 / 万元」、把多期财报转置成「指标 × 报告期」的 Markdown 表格，模型可以直接读懂，token 消耗也远低于原始 JSON。
- **港股字段经过校验**：雪球港股财报用的是 `tto`、`plobtx`、`ploashh` 这类高度缩写的代码。本项目的中文映射是用腾讯控股实际财报数值、通过会计恒等式反向确认的（如 `tto - slgcost == gp`、`ta - tlia == teqy`、`nocf + ninvcf + nfcgcf == icdccceq`），而不是靠猜。
- **论坛可用**：社区接口在 `xueqiu.com` 主域会被风控拦截，本项目走雪球 App 使用的 `api.xueqiu.com`，**无需登录即可**读取个股讨论、公告新闻、热帖、评论。帖子正文的 HTML 会清洗成纯文本。
- **免配置**：匿名令牌自动获取与续期，装完即用，不需要 Cookie、不需要注册。
- **需要时可以登录**：`xueqiu-mcp login` 一条命令，或直接让模型调 `login_with_cookie`。凭证加密权限保存在本地，重启后仍然有效，随时可以 `logout` 撤销。详见[登录](#登录)。
- **扫码登录，不用碰 Cookie**：装上可选的本地浏览器支持后，`get_login_qrcode` 立即返回二维码图片和临时登录网页链接，用雪球 App 扫码并确认后，后台自动接管凭证。网页会更新二维码和登录结果，适用于本地和远程 MCP。详见[本地浏览器](#本地浏览器)。
- **选股指标实时同步**：选股器的指标清单直接读雪球官方元数据接口，雪球调整指标不会导致本项目过时。
- **能在小机器上扛并发**：分级 TTL 缓存 + 并发请求合并 + HTTP/2 多路复用。真实环境实测重复查询快 15.8 倍、打向雪球的请求减少 90%，常驻内存约 75MB。详见[性能与并发](#性能与并发)。

## 安装

```bash
uv venv --python 3.12 && uv pip install -e .
```

MCP SDK 要求 `>=1.30,<3`，支持 1.30 和 2.x 的原生图片返回；从旧版本更新后请重新安装依赖并重启服务。

大 JSON（如 500 根 K 线）的解析想再快 2~3 倍，可以带上 `orjson`：

```bash
uv pip install -e ".[fast]"
```

想用扫码登录（以及正文的浏览器兜底），再装本地浏览器支持：

```bash
uv pip install -e ".[browser]" && .venv/bin/playwright install chromium
```

Chromium 内核约 150MB，不装也不影响其余工具 —— 只是登录得手动贴 Cookie。

## 接入 Claude Code

在项目目录下执行：

```bash
claude mcp add xueqiu -- "$(pwd)/.venv/bin/xueqiu-mcp"
```

## 接入 Claude Desktop / 其他客户端

macOS 可以直接跑安装脚本。它会自动等 Claude 完全退出（运行中的 Claude 会用内存配置覆盖该文件）、
备份原配置，并且只增改 `xueqiu` 一项，不动你已有的其他 MCP：

```bash
./install-claude-desktop.sh
```

手动配置则编辑配置文件（Claude Desktop 在 `~/Library/Application Support/Claude/claude_desktop_config.json`），
把 `command` 换成 `.venv/bin/xueqiu-mcp` 的**绝对路径**：

```json
{
  "mcpServers": {
    "xueqiu": {
      "command": "/绝对路径/.venv/bin/xueqiu-mcp"
    }
  }
}
```

> 若项目路径里含空格或中文，务必使用完整的绝对路径字符串，不要拆成 `args`。

## 工具一览

### 搜索与行情

| 工具 | 说明 |
| --- | --- |
| `search_stock` | 按名称 / 拼音 / 代码搜索标的 |
| `get_quote` | 实时行情，支持一次查多个标的、跨市场混查 |
| `get_kline` | 历史 K 线，可选附带每根 K 线的 PE/PB/PS/市值 |
| `get_minute` | 当日或近 5 日分时（自动抽样约 40 点） |

### 财务

| 工具 | 说明 |
| --- | --- |
| `get_financial_statement` | 利润表 / 资产负债表 / 现金流量表 / 主要指标，A 股港股美股通吃 |
| `get_business_breakdown` | 主营构成：按产品与地区拆分收入、成本、毛利率 |

### 公司资料

| 工具 | 说明 |
| --- | --- |
| `get_company_profile` | 公司简介、实控人、员工数、所属行业与概念板块 |
| `get_shareholders` | 股东户数走势、十大流通股东、机构持仓 |
| `get_dividends` | 历年分红送配与除权除息日 |

### 资金面

| 工具 | 说明 |
| --- | --- |
| `get_capital_flow` | 主力资金每日净流入 + 当日大中小单结构 |
| `get_margin_trading` | 融资融券余额与净买入 |
| `get_block_trades` | 大宗交易明细（含买卖营业部） |

### 市场与选股

| 工具 | 说明 |
| --- | --- |
| `screen_stocks` | 选股器，按估值 / 财务 / 行情指标筛选排序 |
| `list_screener_metrics` | 查询选股器支持的全部指标（官方元数据） |
| `list_industries` | 申万行业分类 |
| `get_hot_stocks` | 雪球人气榜 |

### 社区论坛

| 工具 | 说明 |
| --- | --- |
| `get_stock_discussions` | 个股讨论区，可按热度或时间排序 |
| `get_stock_news` | 个股新闻 / 公司公告流 |
| `get_hot_posts` | 雪球首页热门讨论 |
| `search_posts` | 按关键词搜索帖子，可用 `user_id` 限定作者 |
| `search_users` | 按昵称或关键词搜索用户，返回用户 ID、主页、认证和简介 |
| `get_post` | 帖子全文 + 热门评论 |
| `get_user_posts` | 某位用户的发帖动态 |

### 账号

| 工具 | 说明 |
| --- | --- |
| `login_status` | 当前是匿名还是已登录，登录的是哪个账号 |
| `get_login_qrcode` | 立即返回登录二维码图片和临时网页链接，后台等待扫码（需装浏览器支持） |
| `browser_login` | 默认同上；也可弹出浏览器窗口完成短信或滑块验证 |
| `login_with_cookie` | 用浏览器 Cookie 登录，可选择是否保存到本地 |
| `logout` | 退出登录，回到匿名模式 |
| `browser_close` | 关掉本机浏览器释放内存，登录态保留 |

## 代码写法

| 市场 | 写法 | 例子 |
| --- | --- | --- |
| A 股 | `SH`/`SZ`/`BJ` + 6 位数字，或直接 6 位数字 | `SH600519`、`600519`、`000001` |
| 港股 | 5 位数字，不足补零 | `00700`、`9988` |
| 美股 | 字母代码 | `AAPL`、`BRK.B` |

也可以**直接传中文名称**（如「贵州茅台」），工具会先搜索再取数。

## 使用示例

对模型直接说：

- 「茅台最近的财务指标怎么样？」
- 「帮我筛出市盈率 20 倍以下、股息率 3% 以上、市值千亿以上的 A 股」
- 「看看雪球上大家怎么讨论宁德时代的」
- 「对比一下贵州茅台和五粮液近三年的毛利率和 ROE」
- 「腾讯今天有什么公告吗」

选股器的筛选语法：

```
filters="pettm:0~20,dy_l:3~,mc:100000000000~"
```

即市盈率 0~20 倍、股息率 3% 以上、市值 1000 亿以上。边界可留空表示不限。
指标名可用 `list_screener_metrics` 查询，`_l` 后缀表示取最新报告期。

不知道作者 ID 时，先用 `search_users(query="久期财经")` 按昵称或关键词查找，结合昵称、
认证、简介和主页区分同名用户。得到 `user_id` 后即可限定作者搜索帖子，
例如 `search_posts(query="红利", user_id="4357540281", count=10, page=1)`。
`user_id` 是帖子链接 `xueqiu.com/<user_id>/<post_id>` 中的用户数字 ID；不传则全站搜索。

## 登录

绝大多数功能匿名即可使用 —— 行情、K 线、财务、资金、选股器、以及走 `api.xueqiu.com`
的论坛数据都不需要登录。需要登录的只有两类：

- `xueqiu.com` 主域上受阿里云 WAF 保护的少数接口（匿名访问会拿到挑战页）；
- 属于账号本身的数据（用户资料详情、自选股等）。

### 方式一：扫码登录（最省事，需装浏览器支持）

直接让模型调 `get_login_qrcode`：取到二维码后立即返回 **MCP 原生图片**和 **5 分钟有效的登录网页链接**，服务端在后台等待扫码。用雪球 App 扫码并确认后，凭证自动交给后续请求；可用 `login_status` 确认。

重复取码会复用有效会话，`get_login_qrcode(refresh=true)` 才会重新生成并让旧链接失效。旧工具 `browser_login` 同样默认立即返回。

也可以在命令行执行：

```bash
.venv/bin/xueqiu-mcp login --browser
```

终端会直接画出二维码，用**雪球 App** 扫一下就完成登录，全程不碰 Cookie。
扫不出来可以打开它同时存下的 `~/.xueqiu-mcp/login-qrcode.png`，或者换成窗口模式
`--browser --window` —— 弹出真浏览器，短信验证码、微信登录、滑块验证都能在里面做完。

### 方式二：命令行登录

```bash
.venv/bin/xueqiu-mcp login
```

按提示选择：

1. **粘贴浏览器 Cookie**（不想装浏览器时用这个）：浏览器登录 xueqiu.com → 开发者工具 → Network →
   任意请求的 `Cookie` 请求头 → 复制**整串**。滑块、短信、设备校验全部由浏览器完成，
   本程序只接手结果。
2. **手机号 + 密码**（实验性）：密码用 `getpass` 读取，不回显、不进 shell 历史。
   雪球对该接口有风控，新设备或异地登录可能被拦下，失败时会把雪球的原话透出来 ——
   遇到这种情况请改用第 1 种。

非交互式写法：

```bash
# 从标准输入读，避免 Cookie 落进 shell 历史
pbpaste | .venv/bin/xueqiu-mcp login --cookie -
.venv/bin/xueqiu-mcp login --phone 13800138000   # 密码仍然交互式输入
.venv/bin/xueqiu-mcp status                      # 查看当前登录态
.venv/bin/xueqiu-mcp logout                      # 退出并删除本地凭证
```

登录成功后凭证保存在 `~/.xueqiu-mcp/cookies.json`（目录 0700、文件 0600，
原子写入），MCP Server 启动时自动读取，**不需要改任何客户端配置**。
换个位置存放：设 `XUEQIU_COOKIE_FILE`。

### 方式三：让模型引导你登录

**不需要你先知道要登录。** 任何工具一旦因为没有登录态而失败，返回给模型的报错里会
直接附上完整的登录步骤（去哪复制 Cookie、复制哪一行、之后调哪个工具），模型照着念
给你听就行 —— 不会出现「该接口需要登录」然后双方一起卡住的情况。

JSON 接口偶发返回「未登录」时，会使用现有凭证自动重试一次，不清除 Cookie，也不触发
重新登录。重试仍失败才返回登录指引；限流和普通业务错误不会按此规则重试。

未登录的失败有两种现场：主域接口拿回 WAF 挑战页，以及接口正常返回但雪球说这次请求
没有身份（`用户未登录`、`用户id无效` 等错误码）。两种都会被认出来并带上同一份指引。

你也可以直接说「用这串 Cookie 登录雪球」并附上 Cookie，模型会调 `login_with_cookie`。
校验通过才保存，不通过则回滚到原来的状态，不会把能用的凭证换成坏的。

> 密码登录**不提供** MCP 工具：工具入参会完整进入模型上下文和客户端日志，密码不该
> 走这条路。要用密码请走上面的命令行。

### 方式四：环境变量

适合容器 / 服务器部署，一次性注入、不落盘：

```json
{
  "mcpServers": {
    "xueqiu": {
      "command": "/绝对路径/.venv/bin/xueqiu-mcp",
      "env": { "XUEQIU_COOKIE": "..." }
    }
  }
}
```

优先级：`XUEQIU_COOKIE` 环境变量 > 本地凭证文件。环境变量优先，是为了让部署时注入的
凭证能盖住机器上残留的登录态。

## 本地浏览器

可选能力，装了才有：

```bash
uv pip install -e ".[browser]" && .venv/bin/playwright install chromium
```

它在本机跑一个独立的 Chromium（不碰你日常用的浏览器），干两件事。

### 一、扫码登录

`get_login_qrcode` 打开独立的雪球登录标签页，切到二维码登录，直接把 canvas 导出为
PNG，以 MCP 图片内容返回，同时给出临时登录网页链接。无需模型复制字符画，也无需
客户端访问服务器上的图片文件。登录页面与正文渲染页面分开，查询帖子不会打断扫码。

后台每 2 秒检查登录状态和二维码；页面提示二维码失效时点击刷新。网页也会轮询更新，
登录成功后收起二维码、显示结果。凭证默认写入 `~/.xueqiu-mcp/cookies.json`（0600）。
`remember=false` 只禁止写入该文件，浏览器自身仍使用持久化 profile。

会话最多等待 5 分钟，完成、超时、重新取码、`browser_close` 或 `logout` 都会清理等待任务
和登录标签页。`browser_login(wait=...)` 可显式等待，单次上限 30 秒；第一次应保持默认
`wait=0`，让用户先看到二维码。短信、微信或滑块验证可使用 `browser_login(mode="window")`
在服务所在机器弹出的窗口里操作。

**本地 stdio 接入**：自动启动只监听 `127.0.0.1` 的临时网页服务，使用系统分配的端口。
链接只能在运行 MCP 的机器上打开；手机可扫描对话中的二维码。服务随 MCP 退出关闭，
`browser_close` / `logout` 也会关闭临时网页服务。

**远程 HTTP / SSE 接入**：网页与 MCP 共用服务端口，配置服务对外的基础地址，例如：

```bash
XUEQIU_TRANSPORT=streamable-http
XUEQIU_PUBLIC_BASE_URL=https://mcp.example.com/xueqiu
```

这样返回的链接为 `https://mcp.example.com/xueqiu/login/<随机token>`。反向代理需要把
`/xueqiu/login/<token>` 和 `/xueqiu/login/<token>/state` 转到服务的 `/login/...` 路由。
这两条只读路由用短期随机 token 授权，不附带 MCP 的长期 Bearer 凭证；如果代理统一鉴权，
请仅为这两种 GET 路径放行，继续保护 `/mcp`。建议关闭登录路径的访问日志，避免记录 token。
未配置公网地址时仍返回二维码和相对路径，不会猜一个无法访问的公网链接。

扫码会话存在内存中、同一进程共享一个雪球账号。使用一个 worker，或将同一登录会话的
MCP 请求和网页请求固定转发给同一进程；进程重启会让旧链接失效。服务器若使用自定义
`serve_http.py`，也应设置 `XUEQIU_TRANSPORT=streamable-http`，并使用本项目注册的 HTTP app。

命令行 `xueqiu-mcp login --browser` 继续支持终端字符二维码和本地 PNG 文件。

### 二、正文兜底

`get_post` 发现正文残缺时（雪球把 `truncated` 置位，或正文整个为空），会开页面从
`window.SNOWMAN_STATUS` 里把完整正文捞回来。

> **实测说明**：匿名状态下 `/statuses/show.json` 对普通帖子和长文都给得**全** ——
> 抽查 7 篇带标题的长文，API 正文与页面渲染结果逐字一致（1.0x）。所以这条兜底
> 平时不会触发，它兜的是付费专栏、仅粉丝可见这类匿名拿不到全文的少数内容。
> 列表接口（讨论区 / 热帖 / 搜帖）里那一两百字确实是摘要，但对着帖子 ID 调
> `get_post` 就能拿到全文，不需要浏览器。

### 资源与开关

浏览器常驻约 200MB，远高于本项目其余部分（约 75MB），所以**空闲 5 分钟自动关闭**，
下次要用再拉起来；也可以随时 `browser_close` 手动关。登录态在 profile 目录里，
关掉不会丢。

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `XUEQIU_BROWSER` | 1 | 设 0 彻底关掉浏览器能力（连装了也不用） |
| `XUEQIU_BROWSER_HEADLESS` | 1 | 设 0 则扫码模式也开窗口，方便排查页面问题 |
| `XUEQIU_BROWSER_PROFILE` | `~/.xueqiu-mcp/browser` | profile 目录，登录态存在这里 |
| `XUEQIU_BROWSER_IDLE` | 300 | 空闲多少秒后自动关闭，设 0 关掉这个回收 |
| `XUEQIU_BROWSER_TIMEOUT` | 30 | 单个页面操作的超时（秒） |
| `XUEQIU_PUBLIC_BASE_URL` | 空 | 远程登录网页的对外基础地址，可带反向代理路径前缀 |

profile 是独立于 `cookies.json` 的第二份登录态，所以 `logout` 会把它一起清掉 ——
否则下次扫码会「莫名其妙」直接登录成功。

> 无图形界面的服务器一样能用扫码登录：二维码是纯文本，`window` 模式才需要 GUI。

## 部署到服务器

默认以 stdio 启动，一个进程只服务一个客户端。要在一台机器上同时服务多人多客户端，
改用 streamable-http：

```bash
XUEQIU_TRANSPORT=streamable-http XUEQIU_HOST=0.0.0.0 XUEQIU_PORT=8000 \
  .venv/bin/xueqiu-mcp
```

客户端连 `http://<地址>:8000/mcp`。该模式默认 stateless —— 服务端不为客户端保留会话，
内存不随连接数累积，也方便多副本横向扩展。

> 雪球接口没有官方开放平台，公网暴露前请自行加好鉴权与限流，也别把别人的请求量转嫁到雪球身上。

## 性能与并发

所有资源参数都能用环境变量压低，适配小内存机器：

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `XUEQIU_MAX_CONNECTIONS` | 32 | 连接池上限 |
| `XUEQIU_MAX_CONCURRENCY` | 32 | 同时在途的上游请求数，兼作雪球侧限流 |
| `XUEQIU_CACHE_MB` | 16 | 响应缓存内存上限，已按解析后对象折算，设多少就大致占多少 |
| `XUEQIU_CACHE` | 1 | 设 0 关闭缓存 |
| `XUEQIU_HTTP2` | 1 | 设 0 关闭 HTTP/2 |
| `XUEQIU_TIMEOUT` | 15 | 单请求超时（秒） |

缓存按 endpoint 分级：行情 3 秒、K 线 30 秒、财报 1 小时、公司资料 6 小时、
行业分类与选股指标 24 小时。并发请求同一份数据时只放一个请求出去，其余等它的结果。

### 实测

以下数字来自真实环境实测（打真实雪球接口，A 股交易时段，共约 1,500 次请求）：

| 场景 | 结果 | 测量条件 |
| --- | --- | --- |
| 22 个工具冷调用延迟 | 中位 **51.0 ms** | 每工具 3 个冷样本取中位，再跨工具取中位 |
| 命中缓存后 | 中位 **1.84 ms** | 每工具 9 个热样本 |
| 本项目自身开销 | 中位 **4.4 ms** | 端到端减去上游墙钟，含 MCP 编解码与格式化 |
| 重复查询（新旧版 A/B） | **快 15.8 倍**，上游请求 **-90%** | 同一标的连查 10 次 |
| 并发 32 | **零失败**，P50 86 ms | 分级加压 1→4→8→16→32，共 193 次请求 |

**瓶颈不在本项目**：`stock.xueqiu.com` 单请求中位 40.4 ms，`api.xueqiu.com`（社区类）84.8 ms，
而本项目自身只占 4.4 ms。

**收益主要来自缓存与请求合并**，其次是 HTTP/2 在冷连接突发时的多路复用。
纯管道吞吐（关掉缓存）与优化前基本持平 —— 别指望靠它变快。

`tests/bench.py` 打的是本地 mock 上游（**mock 口径，不等于真实表现**），
用途是回归检测而非标榜性能：

```bash
.venv/bin/python tests/bench.py            # 默认模拟 30ms 网络延迟
MOCK_RTT=0 .venv/bin/python tests/bench.py # 零延迟，放大纯代码开销
```

判读要点：**关注上游请求数和峰值并发是否符合预期**。QPS 数字受 mock server 自身
调度开销影响很大，本地回环上并发升高时 QPS 下降是测试环境的产物，不代表被测代码有问题。

缓存层另有一组零网络的回归测试，覆盖请求合并、取消传播、LRU 淘汰与字节记账：

```bash
.venv/bin/python tests/test_cache.py
```

### 已知边界

- **并发闸门不是限流器**。它只约束「同时在途请求数」，不约束单位时间请求数。
  按实测延迟，32 并发理论上允许约 700 req/s 打向雪球。请在调用侧自行控制节奏。
- 真实环境只验证到 **32 并发**，更高并发没有真实数据。
- **手机号密码登录是实验性的**。雪球对 `/snowman/login` 有风控，新设备、异地、
  频繁尝试都可能被拦成一句笼统的「遇到错误，请刷新页面后重试」。本项目不做重试
  （密码接口连续失败容易触发账号锁定），失败时请改用 Cookie 登录。
- **登录 / 登出会清空响应缓存**。同一个 URL 在匿名态和登录态下可能返回不同内容，
  留着旧条目会让刚登录的人继续读到匿名时的结果。代价是切换身份后的第一批请求
  必然回源。
- `XUEQIU_CACHE_MB` 是偏乐观的估计，实测真实内存增长约为该值的 **1.2~1.9 倍**
  （小条目场景偏高）。小内存机器建议设 8。

## 测试

```bash
.venv/bin/python tests/test_mcp_e2e.py   # 端到端，需要联网
.venv/bin/python tests/test_cache.py     # 缓存层，零网络
.venv/bin/python tests/test_auth.py      # 登录与凭证，零网络
.venv/bin/python tests/test_client_social.py  # 接口重试与用户搜索，零网络
.venv/bin/python tests/test_browser.py   # 本地浏览器层，零网络、不启动浏览器
.venv/bin/python tests/test_login_flow.py # 扫码会话、网页、MCP 原生图片契约，假浏览器
```

`test_mcp_e2e.py` 会以真正的 MCP 客户端身份通过 stdio 连接本 Server，列出全部工具
并逐个实调（含中文名解析、指数/ETF/可转债、以及各类参数校验的错误提示），最后打印
通过数；它只测不改变登录态的账号路径，不会把跑测试的人踢下线。
`test_auth.py` 与 `test_browser.py` 会把凭证文件和浏览器 profile 都指向临时目录，
不碰 `~/.xueqiu-mcp/`。`test_browser.py` 不启动浏览器，只覆盖二维码矩阵怎么变成能扫
的字符画、什么时候判定正文残缺、以及没装 Playwright 时会不会把 `get_post` 拖垮。

`test_login_flow.py` 验证后台保存凭证、链接失效、取消与刷新、网页响应，以及真实 stdio
通信中的图片与结构化结果；只监听本机临时端口，不访问雪球，不需要安装 Playwright。

CI 上跑上述四组离线测试（Python 3.10~3.13，外加 orjson 和 MCP 1.30 兼容性检查）。端到端测试
放在[单独的每周巡检](.github/workflows/e2e-weekly.yml)里，不卡合并 —— 它红了说明
雪球那边变了，而不是这次改动坏了。

巡检在 GitHub runner（境外 IP）上跑，实测 36 项里 35 项正常，只有**搜索接口对境外
IP 返回空结果**；该条在 CI 里记为跳过（`XUEQIU_E2E_GEO_TOLERANT=1`），本地跑不受
影响，仍然严格校验。

## 项目结构

```
src/xueqiu_mcp/
├── client.py        HTTP 客户端：令牌续期、连接池与 HTTP/2、并发闸门、风控识别
├── auth.py          登录：Cookie 解析与登录态识别、凭证落盘、密码登录
├── browser.py       本地浏览器：扫码登录、正文兜底渲染、profile 与空闲回收
├── login_flow.py    临时登录链接、后台扫码会话与登录网页
├── cache.py         响应缓存：分级 TTL、LRU 内存上限、并发请求合并
├── symbols.py       代码规范化（600519 → SH600519）
├── resolve.py       代码解析，中文名走搜索兜底
├── fields.py        A 股字段中文映射表
├── fields_intl.py   港股 / 美股字段映射表（经会计恒等式校验）
├── screener.py      选股器指标元数据（读雪球官方接口并缓存）
├── formatting.py    数值单位换算、Markdown 表格、HTML 正文清洗
├── server.py        MCP 工具注册
└── tools/
    ├── quote.py     行情、K 线、分时
    ├── finance.py   财务报表、主营构成
    ├── f10.py       公司资料、股东、分红
    ├── capital.py   资金流、两融、大宗交易
    ├── market.py    选股器、行业、人气榜
    ├── social.py    论坛：讨论、公告新闻、热帖、评论
    └── account.py   账号：登录状态、扫码登录、Cookie 登录、退出登录
```

## 说明

- 数据全部来自雪球公开接口，行情可能有延迟，**不构成任何投资建议**。
- 本项目仅供学习研究，请遵守雪球的服务条款，避免高频请求。
- 雪球接口非官方开放平台，字段与可用性可能随时调整。
