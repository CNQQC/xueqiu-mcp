# 雪球 MCP Server

[![CI](https://github.com/CNQQC/xueqiu-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/CNQQC/xueqiu-mcp/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

**让 AI 直接查询雪球行情、财务报表与社区观点。**

通过 Model Context Protocol（MCP），将[雪球](https://xueqiu.com)数据接入 Claude Code、Claude Desktop、Cherry Studio 等 MCP 客户端。提供 **29 个工具**，覆盖 **A 股、港股、美股**，行情查询还支持指数、ETF 和可转债。

[快速开始](#快速开始) · [客户端接入](#客户端接入) · [使用示例](#使用示例) · [工具一览](#工具一览) · [登录与浏览器](#登录与浏览器) · [服务端部署](#服务端部署) · [配置参考](#配置参考) · [常见问题](#常见问题) · [开发与测试](#开发与测试)

## 项目亮点

- **适合 AI 阅读的结果**：财务字段中文化，金额转换为亿元 / 万元，多期财报按「指标 × 报告期」输出 Markdown 表格。
- **跨市场查询**：一次获取多个市场的行情，支持中文名称解析、财务对比和按指标选股。
- **从讨论找到原帖**：查询热帖、公告和评论；先搜索并核对作者，再限定作者检索，区分作者文字与引用、转发内容。
- **默认匿名使用**：自动获取与续期匿名令牌，公开数据通常无需手动配置 Cookie；受限接口可按需登录。
- **可选扫码登录**：向客户端返回二维码图片与临时登录网页，支持本地和远程部署。
- **减少重复请求**：分级 TTL 缓存、并发请求合并与 HTTP/2，连接数、并发数和缓存容量均可配置。

## 快速开始

需要 **Python 3.10+**、Git，以及能访问雪球的网络。以下命令适用于 macOS / Linux，使用 [uv](https://docs.astral.sh/uv/getting-started/installation/) 管理环境。

### 1. 获取代码并安装

```bash
git clone https://github.com/CNQQC/xueqiu-mcp.git
cd xueqiu-mcp
uv venv --python 3.12
uv pip install -e .
```

<details>
<summary>使用 pip 安装，或在 Windows 上安装</summary>

macOS / Linux：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

Windows PowerShell（先克隆仓库并进入项目目录）：

```powershell
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -e .
```

Windows 下将后续命令中的 `.venv/bin/xueqiu-mcp` 换成 `.venv\Scripts\xueqiu-mcp.exe`。JSON 配置里的反斜杠需要写为 `\\`。

</details>

### 2. 检查命令入口

```bash
.venv/bin/xueqiu-mcp --help
```

此命令只检查本地命令入口。接入客户端后，可让 AI 调用 `get_quote` 查询行情，确认完整链路可用。

### 3. 连接客户端

Claude Code 用户在项目目录执行：

```bash
claude mcp add xueqiu -- "$(pwd)/.venv/bin/xueqiu-mcp"
```

其他客户端见[客户端接入](#客户端接入)。连接后即可提问：

> 对比贵州茅台、腾讯和苹果的最新行情，并标注各自市场与币种。

### 可选依赖

| 需求 | 安装命令 |
| --- | --- |
| 使用 orjson 加速 JSON 解析 | `uv pip install -e ".[fast]"` |
| 扫码登录与帖子正文浏览器兜底 | `uv pip install -e ".[browser]"` |
| 同时启用两项 | `uv pip install -e ".[fast,browser]"` |

启用浏览器支持后，还需下载 Chromium：

```bash
.venv/bin/playwright install chromium
```

不安装浏览器也可以查询公开数据，并通过 Cookie 登录。MCP SDK 的依赖范围为 `>=1.30,<3`，由安装命令自动处理。

## 客户端接入

### Claude Desktop

macOS 用户可以在完成安装后运行仓库脚本：

```bash
./install-claude-desktop.sh
```

按提示使用 **Cmd+Q 完全退出 Claude**。脚本会等待退出、备份配置、更新 `xueqiu` 条目，并重新打开 Claude；其他 MCP 配置保持原样。

> macOS 桌面应用访问 Downloads / Desktop / Documents 下的项目可能受到隐私权限限制。建议在 `~/.local/share/xueqiu-mcp` 等目录克隆并安装，然后运行脚本。

手动配置时，将以下条目合并到客户端的 `mcpServers` 中。macOS Claude Desktop 的配置文件为 `~/Library/Application Support/Claude/claude_desktop_config.json`：

```json
{
  "mcpServers": {
    "xueqiu": {
      "command": "/绝对路径/xueqiu-mcp/.venv/bin/xueqiu-mcp"
    }
  }
}
```

将 `command` 替换成真实的**绝对路径**，路径包含空格或中文时也保留为一个完整字符串。保存后重新启动客户端。

### Cherry Studio / 其他 MCP 客户端

添加本地 **stdio** 服务，将命令设置为上面的可执行文件绝对路径，参数留空。支持 JSON 导入的客户端可使用同一份配置。

远程连接使用 **Streamable HTTP**，地址为 `https://你的域名/mcp`；服务端配置见[服务端部署](#服务端部署)。

## 使用示例

### 直接向 AI 提问

| 场景 | 示例 |
| --- | --- |
| 财务对比 | 对比贵州茅台和五粮液近三年的毛利率与 ROE，注明报告期。 |
| 条件选股 | 筛出市盈率 20 倍以下、股息率 3% 以上、市值 1000 亿以上的 A 股。 |
| 社区观点 | 看看雪球上近期如何讨论宁德时代，区分作者观点和引用内容，并附原帖链接。 |
| 公司公告 | 查询腾讯最近的公司公告。 |

### 股票代码

| 市场 / 类型 | 写法 | 示例 |
| --- | --- | --- |
| A 股 | 交易所前缀 + 6 位数字，或直接 6 位数字 | `SH600519`、`600519`、`SZ000001` |
| 港股 | 5 位数字，不足自动补零 | `00700`、`9988` → `09988` |
| 美股 | 字母代码 | `AAPL`、`BRK.B` |
| 指数 / ETF / 可转债 | 使用完整交易所前缀，避免歧义 | `SH000001`、`SH510300`、`SH113050` |

也可直接传中文名称，例如「贵州茅台」。`get_quote` 支持逗号分隔多个标的，如 `600519,00700,AAPL`。注意：裸代码 `000001` 按深市股票处理，查询上证指数请用 `SH000001`。

### 按指标选股

先调用 `list_screener_metrics` 确认指标名称，再筛选。以下为 MCP 工具调用示意：

```text
list_screener_metrics(market="CN", keyword="市盈")
screen_stocks(market="CN", filters="pettm:0~20,dy_l:3~,mc:100000000000~")
```

筛选语法为 `指标:下限~上限`，多条用逗号分隔，边界留空表示不限。上述条件为市盈率 0–20 倍、股息率 3% 以上、市值 1000 亿元以上；`_l` 后缀表示最新报告期。

### 查找指定作者的一手观点

先搜索并核对账号，再用 `user_id` 限定作者。昵称可能重复，不能直接选择第一项。例如确认目标为「买股票的老木匠」后：

```text
search_users(query="老木匠")
search_posts(query="茅台", user_id="3058599833", count=3)
get_post(post_id="上一步返回的帖子ID", with_comments=false)
```

`search_posts` 和 `get_user_posts` 也接受雪球主页或帖子链接作为 `user_id`。限定作者时会再次核对返回帖子的作者 ID，排除身份不符或未知的结果。

列表提供摘要或关键词附近的摘录，全文和列表均区分作者文字、`//@` 引用和被转发帖。归纳观点前应调用 `get_post` 读取全文，并保留作者、日期和原帖链接；无转发标记不等于原创，单页搜索为空也不等于作者从未讨论过该主题。

帖子列表和详情均显示编辑状态。雪球返回有效的 `edited_at` 时显示“已编辑”和北京时间，缺少有效编辑时间时显示“未知”。转发帖和被转发原帖各自显示自己的编辑状态。

`get_user_posts` 和 `get_stock_discussions` 每页最多请求 30 条，超过 20 条时自动分批读取。两者都显示请求数和实返数。用户动态接口的 `10022` 也会出现在特定分页请求中，仅凭该错误无法确认账号登录状态。

### 导出 JSON、分页与增量采集

所有 24 个只读工具都接受 `format="json"`。MCP 客户端可直接读取 `structuredContent`，只支持文本的客户端可对 `content[0].text` 调用 `JSON.parse`。默认返回 Markdown。JSON 中帖子和作者 ID 是字符串，计数是整数，缺失值为 `null`，时间使用带时区的 ISO 8601。金融数据的 `units` 单独给出金额币种、百分数或小数比率。

```text
search_posts(query="茅台", count=30, since="2026-01-01", until="2026-11-26", original_only=true, min_followers=100, format="json")
get_user_posts(user_id="1422017996", since_id="400000000", count=30, format="json")
get_post(post_id="392261408", comment_count=30, comment_page=2, format="json")
get_kline(symbol="SH600519", period="day", count=20, exclude_today=true, format="json")
get_capital_flow(symbol="SH600519", days=20, exclude_today=true, format="json")
```

- 帖子列表及评论的 `pagination` 给出 `requested`、`returned`、单次上限 `max`、`has_more`、`next_page`。`has_more=null` 表示覆盖范围未知。续读时保持相同筛选和条数，将 `next_page` 原样传给 `page`，评论传给 `comment_page`。读取失败请查看 `note` 和可用的 `retry_page`。帖子头部的评论数可能包含楼中楼，分页针对评论接口实际可见的记录。
- 热帖的 `next_page` 是上游游标。筛选后的搜帖和用户动态每次最多检查 8 个上游页，游标记录页内位置，`scanned` 为本次检查的条数。上游提供时，`upstream_max_page` 给出上游声明的页数。实时排序可能变化，跨调用落库时按帖子 ID 去重。
- `since_id` 为排他下界。`since` 包含起点，`until` 若为时间则不含终点，若为日期则包含当日。无时区输入按北京时间解释。`original_only` 排除带转发标记、内联转述或正文仅有 @ 提及的记录。`min_followers` 指作者粉丝数，粉丝数缺失时无法通过该条件。
- K 线、资金流的 `as_of` 是工具观测时间，`quote_as_of` 是行情快照时间。`is_partial=null` 表示现有市场状态不足以确认。`exclude_today=true` 按交易所当地日期排除当天，周、月、季、年 K 线同时排除覆盖当天的周期。资金累计按日期筛选后重新求和，不足完整窗口时金额为 `null`。

## 工具一览

共 **29 个工具**。不同市场和标的的可用字段取决于雪球上游接口，部分功能仅适用于 A 股。

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
| `get_financial_statement` | 利润表 / 资产负债表 / 现金流量表 / 主要指标，支持 A 股、港股、美股 |
| `get_business_breakdown` | 主营构成：按产品与地区拆分收入、成本、毛利率（仅 A 股） |

港股的每股股息指标 `dps` 保留雪球报告期原值，单位为仙。以腾讯为例，2026 年中报和三季报的数值分别为 265.0 和 397.5，恰与 2025 年度末期股息每股 530 仙的二分之一和四分之三吻合。腾讯公告第 33 页附注 7 载明，2026 年前九个月没有宣派中期股息。核实派息金额和日期应查发行人公告。[腾讯 2026 年第三季度业绩公告](https://www.tencent.com/wp-content/uploads/2026/11/C700_3Qresults.pdf)

### 公司资料

| 工具 | 说明 |
| --- | --- |
| `get_company_profile` | 公司简介、实控人、员工数、所属行业与概念板块 |
| `get_shareholders` | 股东户数走势、十大流通股东、机构持仓 |
| `get_dividends` | 历年分红送配与除权除息日 |

### 资金面

| 工具 | 说明 |
| --- | --- |
| `get_capital_flow` | 每日主力净流入，另列特大、大、中、小单的买入、卖出和净额。上游缺数保留为空 |
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
| `search_posts` | 按关键词搜索帖子，可用 `user_id` 限定作者、`exact_phrase=true` 匹配连续短语。默认每页按内容去重 |
| `search_users` | 按昵称或关键词搜索用户，返回用户 ID、主页、认证和简介 |
| `get_post` | 帖子全文及最新评论，按时间倒序 |
| `get_user_posts` | 某位用户的发帖动态 |

### 账号

| 工具 | 说明 |
| --- | --- |
| `login_status` | 当前登录状态、扫码浏览器可用性、凭证目录及写权限 |
| `get_login_qrcode` | 立即返回登录二维码图片和临时网页链接，后台等待扫码（需装浏览器支持） |
| `browser_login` | 默认同上；也可弹出浏览器窗口完成短信或滑块验证 |
| `login_with_cookie` | 用浏览器 Cookie 登录，可选择是否保存到本地 |
| `logout` | 退出登录，回到匿名模式 |
| `browser_close` | 关掉本机浏览器释放内存，登录态保留 |

## 登录与浏览器

行情、财务、选股及部分社区数据通常可以匿名查询。遇到受限接口时，先调用 `login_status` 检查登录态，再按工具返回的提示登录。

### 扫码登录

安装 `[browser]` 依赖和 Chromium 后，让 AI 调用 `get_login_qrcode`。工具立即返回 **MCP 原生二维码图片**和 **5 分钟有效的临时登录网页链接**，用雪球 App 扫码并确认后，后台自动接管凭证。用 `login_status` 确认结果。

也可在命令行登录：

```bash
.venv/bin/xueqiu-mcp login --browser
```

终端显示二维码，并保存到 `~/.xueqiu-mcp/login-qrcode.png`。需要短信或滑块验证时，可在有图形界面的服务主机上运行窗口模式：

```bash
.venv/bin/xueqiu-mcp login --browser --window
```

### Cookie 登录

在浏览器登录雪球，从开发者工具的 Network 中复制请求的完整 `Cookie` 请求头，再执行以下命令并按提示粘贴：

```bash
.venv/bin/xueqiu-mcp login --cookie
```

macOS 也可以从剪贴板读取，避免 Cookie 写入 shell 历史：

```bash
pbpaste | .venv/bin/xueqiu-mcp login --cookie -
.venv/bin/xueqiu-mcp status
.venv/bin/xueqiu-mcp logout
```

凭证默认保存在 `~/.xueqiu-mcp/cookies.json`，新建的凭证目录权限为 `0700`、文件权限为 `0600`，采用原子写入。可用 `XUEQIU_CREDENTIAL_DIR` 指定目录，或用优先级更高的 `XUEQIU_COOKIE_FILE` 指定完整文件名。首选位置无法写入时，依次尝试用户主目录下的备用目录、当前用户独占的系统临时目录。临时文件可能被系统清理；在容器中需要长期保存时，请指定可写的持久卷。服务启动时优先使用 `XUEQIU_COOKIE`，否则读取对应位置中最近保存的凭证。

<details>
<summary>其他登录方式与凭证处理</summary>

- `xueqiu-mcp login` 提供交互式菜单，也支持实验性的手机号密码登录：`xueqiu-mcp login --phone 13800138000`。密码由终端交互读取；遇到风控时请改用扫码或 Cookie。
- MCP 工具 `login_with_cookie(cookie=..., remember=true)` 会验证 Cookie 后保存，失败时回滚。工具参数可能进入模型上下文和客户端日志，因此优先使用扫码或本地命令行。
- 容器 / 服务器可通过 `XUEQIU_COOKIE` 注入凭证，或在客户端 JSON 的 `env` 对象中配置。不要将真实 Cookie 提交到仓库或 Issue。
- JSON 接口偶发返回「未登录」时，会使用现有凭证重试一次；仍失败才返回登录指引。限流和普通业务错误不会按此规则重试。
- 登录 / 登出会清空响应缓存，避免不同身份的数据混用。

</details>

<details>
<summary>浏览器会话、临时登录网页与正文兜底</summary>

浏览器使用独立 Chromium profile，默认位于 `~/.xueqiu-mcp/browser`，不使用日常浏览器的个人配置。空闲 300 秒后自动关闭，也可调用 `browser_close` 释放资源。

- 重复调用 `get_login_qrcode` 会复用有效会话；`refresh=true` 重新生成，并让旧链接失效。
- `browser_login` 默认也立即返回二维码；显式 `wait` 单次最多等待 30 秒。应先向用户展示二维码，再等待扫码。
- MCP 扫码会话最多等待 5 分钟；完成、超时、刷新、`browser_close` 或 `logout` 会清理等待任务和登录标签页。
- 本地 stdio 模式自动启动仅监听 `127.0.0.1`、使用随机端口的临时网页服务。链接只能在服务所在机器打开，用户也可用手机扫描客户端显示的二维码。
- `remember=false` 仅禁止写入 `cookies.json`，浏览器 profile 仍可能保留登录态。`browser_close` 保留登录态；默认 `logout` 会同时清理凭证文件和浏览器 profile。
- `get_post` 在正文为空或被标记截断时尝试浏览器补全，设置 `full_text=false` 可关闭这次兜底。列表摘要应先通过 `get_post` 查询全文；浏览器补全仍受当前账号的访问权限限制。

无图形界面的服务器可以使用无头扫码模式；只有窗口模式需要 GUI。

</details>

## 服务端部署

默认使用本地 stdio。需要远程访问时，启动 Streamable HTTP 服务：

```bash
XUEQIU_TRANSPORT=streamable-http XUEQIU_HOST=0.0.0.0 XUEQIU_PORT=8000 \
  .venv/bin/xueqiu-mcp
```

客户端连接 `http://<服务器地址>:8000/mcp`。服务默认启用无状态 HTTP 会话，但**同一进程仍共享一个雪球账号和缓存**。公网部署应通过反向代理配置 HTTPS、鉴权和限流；并发上限只控制同时在途的请求数，不是每秒请求配额。

<details>
<summary>远程扫码登录与反向代理</summary>

设置外部可访问的基础地址，不包含 `/mcp`，可带代理路径前缀：

```bash
export XUEQIU_TRANSPORT=streamable-http
export XUEQIU_PUBLIC_BASE_URL=https://mcp.example.com/xueqiu
.venv/bin/xueqiu-mcp
```

临时登录链接为 `https://mcp.example.com/xueqiu/login/<随机token>`。反向代理需将以下请求转发到同一服务进程：

| 外部路径 | 服务端路径 |
| --- | --- |
| `/xueqiu/mcp` | `/mcp` |
| `/xueqiu/login/<token>` | `/login/<token>` |
| `/xueqiu/login/<token>/state` | `/login/<token>/state` |

登录网页的两条 GET 路由使用短期随机 token 授权，不携带 MCP 长期 Bearer 凭证。如果代理统一鉴权，仅为这两种 GET 路径放行，继续保护 `/mcp`；建议关闭登录路径访问日志，避免记录 token。

扫码会话存在进程内存中。使用单 worker，或将同一会话的 MCP 与网页请求固定转发到同一进程。重启会使旧链接失效；未配置外部地址时，仅返回二维码与网页相对路径。

自定义 HTTP 入口需设置 `XUEQIU_TRANSPORT=streamable-http`，并使用 `xueqiu_mcp.server.mcp.streamable_http_app()`，以保留本项目注册的登录路由。

</details>

已有 VPS 的自动更新、健康检查与回滚流程见 [部署文档](deploy/README.md)。

## 配置参考

所有配置均通过环境变量传入。

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `XUEQIU_TRANSPORT` | `stdio` | `stdio` / `streamable-http` / `sse` |
| `XUEQIU_HOST` | `127.0.0.1` | HTTP / SSE 监听地址 |
| `XUEQIU_PORT` | `8000` | HTTP / SSE 监听端口 |
| `XUEQIU_STATELESS` | `1` | Streamable HTTP 无状态会话开关 |
| `XUEQIU_COOKIE` | 空 | 登录 Cookie，优先于本地文件 |
| `XUEQIU_COOKIE_FILE` | `~/.xueqiu-mcp/cookies.json` | 凭证文件位置 |
| `XUEQIU_CREDENTIAL_DIR` | `~/.xueqiu-mcp` | 凭证目录，浏览器 profile 默认随可写目录存放 |
| `XUEQIU_PUBLIC_BASE_URL` | 空 | 远程扫码网页的对外基础地址 |
| `XUEQIU_MCP_LOG` | `INFO` | 日志级别 |

<details>
<summary>连接、缓存与浏览器参数</summary>

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `XUEQIU_MAX_CONNECTIONS` | `32` | HTTP 连接池上限 |
| `XUEQIU_MAX_CONCURRENCY` | `32` | 同时在途的上游请求数上限 |
| `XUEQIU_CACHE_MB` | `16` | 缓存容量估算上限（MB），不等于进程内存硬上限 |
| `XUEQIU_CACHE` | `1` | 设 `0` 关闭缓存 |
| `XUEQIU_HTTP2` | `1` | 设 `0` 关闭 HTTP/2 |
| `XUEQIU_TIMEOUT` | `15` | 单请求超时（秒） |
| `XUEQIU_BROWSER` | `1` | 设 `0` 禁用浏览器能力，仍需安装可选依赖才能启用 |
| `XUEQIU_BROWSER_HEADLESS` | `1` | 设 `0` 让扫码模式显示窗口 |
| `XUEQIU_BROWSER_PROFILE` | `~/.xueqiu-mcp/browser` | 独立浏览器 profile 路径 |
| `XUEQIU_BROWSER_IDLE` | `300` | 空闲回收秒数，设 `0` 关闭自动回收 |
| `XUEQIU_BROWSER_TIMEOUT` | `30` | 单个页面操作超时（秒） |

缓存按接口分级：行情 3 秒、分时 5 秒、K 线 30 秒、财报 1 小时、公司资料 6 小时、行业与选股指标 24 小时。相同请求并发到达时合并为一次上游调用。具体规则见 [cache.py](src/xueqiu_mcp/cache.py)。

</details>

## 常见问题

| 问题 | 处理方式 |
| --- | --- |
| 客户端启动失败或找不到命令 | 确认 `command` 是当前虚拟环境中可执行文件的绝对路径，先运行 `--help` 检查入口。 |
| macOS 提示 `Operation not permitted` | 检查项目是否位于受隐私权限保护的目录，可改在 `~/.local/share/xueqiu-mcp` 安装。 |
| 看不到 `search_users` 或 `search_posts.user_id` | 更新客户端实际启动的安装环境，再重新连接 MCP 或重启客户端。只更新另一份开发目录不会生效。 |
| 提示未登录或被风控拦截 | 先查 `login_status`，再按返回提示扫码或更新 Cookie；登录不保证消除所有风控。 |
| 扫码工具提示浏览器不可用 | 在服务使用的环境中安装 `[browser]` 依赖，并运行 `playwright install chromium`。 |
| 远程登录网页打不开 | 检查 `XUEQIU_PUBLIC_BASE_URL`、代理路径、登录 GET 路由放行及同进程转发。 |
| 帖子列表只有一小段文字 | 用返回的帖子 ID 调用 `get_post` 获取全文。 |
| 搜索结果为空 | 尝试缩短关键词、翻页或查询用户近期动态，也需考虑地区 / IP 和上游接口差异。 |

更新时先进入源码目录，再使用**客户端配置指向的环境**重新安装，例如：

```bash
uv pip install --python /绝对路径/客户端环境/bin/python -e .
```

如果源码目录受 macOS 隐私权限限制，可去掉 `-e` 将包安装到目标环境的 site-packages，随后重启客户端。

## 开发与测试

离线测试不需要访问雪球，浏览器相关测试使用模拟对象：

```bash
.venv/bin/python tests/test_cache.py
.venv/bin/python tests/test_auth.py
.venv/bin/python tests/test_client_social.py
.venv/bin/python tests/test_deploy.py
.venv/bin/python tests/test_browser.py
.venv/bin/python tests/test_login_flow.py
```

真实接口测试需要联网：

```bash
.venv/bin/python tests/test_mcp_e2e.py
.venv/bin/python tests/test_social_e2e.py
```

`test_mcp_e2e.py` 通过 stdio 执行 MCP 工具调用；`test_social_e2e.py` 验证昵称消歧、作者限定搜索与原帖全文。登录和浏览器离线测试使用临时凭证目录；真实 MCP 测试只覆盖不改变登录态的账号路径。

[CI](.github/workflows/ci.yml) 运行上述六组离线测试，覆盖 Python 3.10–3.13、可选 orjson 和 MCP 1.30 兼容性。[每周巡检](.github/workflows/e2e-weekly.yml) 单独运行 `test_mcp_e2e.py`；失败时需同时排查代码、网络与雪球接口变化。巡检设置 `XUEQIU_E2E_GEO_TOLERANT=1`，容忍境外 IP 导致的股票搜索空结果，本地默认严格校验。

<details>
<summary>性能基准与历史测量</summary>

本地基准使用 mock 上游，不访问雪球。重点关注缓存减少的上游请求数，以及峰值并发是否符合配置；mock QPS 不代表真实服务表现。

```bash
.venv/bin/python tests/bench.py
MOCK_RTT=0 .venv/bin/python tests/bench.py
```

以下为项目早期记录的测试结果（A 股交易时段，约 1,500 次真实接口请求），覆盖当时的 22 个工具。原记录未注明日期、硬件和网络配置，不能视为当前版本的性能保证；延迟会随网络、上游状态与缓存命中率变化。

| 场景 | 结果 | 测量条件 |
| --- | --- | --- |
| 22 个工具冷调用延迟 | 中位 **51.0 ms** | 每工具 3 个冷样本取中位，再跨工具取中位 |
| 命中缓存后 | 中位 **1.84 ms** | 每工具 9 个热样本 |
| 本项目自身开销 | 中位 **4.4 ms** | 端到端减去上游墙钟，含 MCP 编解码与格式化 |
| 重复查询（新旧版 A/B） | **快 15.8 倍**，上游请求 **-90%** | 同一标的连查 10 次 |
| 并发 32 | **零失败**，P50 86 ms | 分级加压 1→4→8→16→32，共 193 次请求 |

</details>

<details>
<summary>项目结构</summary>

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

</details>

## 参与贡献

欢迎通过 [Issues](https://github.com/CNQQC/xueqiu-mcp/issues) 报告问题或提出功能建议，也欢迎提交 Pull Request。

- 报告问题时附上 Python 版本、客户端与传输方式、工具名称、脱敏后的参数和错误信息。
- 修复或增加工具时，更新对应文档并运行相关离线测试；涉及上游接口时说明实际验证范围。
- 请勿在日志、截图、Issue 或提交中包含 Cookie、密码和其他账号凭证。

## 许可与数据说明

本项目采用 [MIT License](LICENSE)，是独立开源项目，非雪球官方产品。

数据来自雪球接口，可能延迟、缺失或因接口调整而不可用。仅供学习研究，不构成投资建议；使用时请遵守雪球服务条款并控制请求频率。
