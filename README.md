# 雪球 MCP Server

把[雪球](https://xueqiu.com)的行情、财务、资金与**社区论坛**数据接入任何支持 MCP 的客户端（Claude Code、Claude Desktop、Cherry Studio 等）。

覆盖 **A 股 / 港股 / 美股**，外加指数、ETF、可转债。共 **22 个工具**。

## 特点

- **面向大模型的输出**：雪球原始接口返回的是 `ncf_from_oa`、`1.7205417189091E11` 这类字段和数值。本项目把 **600+ 个财务字段翻译成中文**、把金额换算成「亿元 / 万元」、把多期财报转置成「指标 × 报告期」的 Markdown 表格，模型可以直接读懂，token 消耗也远低于原始 JSON。
- **港股字段经过校验**：雪球港股财报用的是 `tto`、`plobtx`、`ploashh` 这类高度缩写的代码。本项目的中文映射是用腾讯控股实际财报数值、通过会计恒等式反向确认的（如 `tto - slgcost == gp`、`ta - tlia == teqy`、`nocf + ninvcf + nfcgcf == icdccceq`），而不是靠猜。
- **论坛可用**：社区接口在 `xueqiu.com` 主域会被风控拦截，本项目走雪球 App 使用的 `api.xueqiu.com`，**无需登录即可**读取个股讨论、公告新闻、热帖、评论。帖子正文的 HTML 会清洗成纯文本。
- **免配置**：匿名令牌自动获取与续期，装完即用，不需要 Cookie、不需要注册。
- **选股指标实时同步**：选股器的指标清单直接读雪球官方元数据接口，雪球调整指标不会导致本项目过时。
- **能在小机器上扛并发**：分级 TTL 缓存 + 并发请求合并 + HTTP/2 多路复用，512 并发下 P99 稳定在 800ms 以内，常驻内存约 75MB。详见[性能与并发](#性能与并发)。

## 安装

```bash
uv venv --python 3.12 && uv pip install -e .
```

大 JSON（如 500 根 K 线）的解析想再快 2~3 倍，可以带上 `orjson`：

```bash
uv pip install -e ".[fast]"
```

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
| `search_posts` | 全站搜索帖子 |
| `get_post` | 帖子全文 + 热门评论 |
| `get_user_posts` | 某位用户的发帖动态 |

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

## 可选：配置自己的 Cookie

绝大多数功能匿名即可使用。少数需要登录态的接口（如用户资料详情）可以配置环境变量：

```bash
export XUEQIU_COOKIE="从浏览器开发者工具复制的完整 Cookie"
```

在 MCP 配置里则写成：

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

`tests/bench.py` 打的是本地 mock 上游（不碰雪球），模拟 30ms 网络延迟，
在 12 核 macOS 上的结果：

| 场景 | QPS | P99 | 上游请求 |
| --- | --- | --- | --- |
| 512 并发 · 关缓存 | 695 | 760 ms | 2048 / 2048 |
| 128 并发 · 200 个标的 · 开缓存 | 5823 | 212 ms | 200 / 2000 |
| 128 并发 · 同一标的 · 开缓存 | 47191 | 41 ms | **1** / 2000 |

空闲常驻内存约 75MB，压测峰值约 83MB。

```bash
.venv/bin/python tests/bench.py            # 默认模拟 30ms 网络延迟
MOCK_RTT=0 .venv/bin/python tests/bench.py # 零延迟，放大纯代码开销
```

判读要点：**并发升高时 QPS 应当上升或持平**。如果不升反降，说明请求在代码里被
串行化了 —— 这正是早期版本的问题，每个请求都要抢一次 token 锁，并发 32 时的吞吐
反而只有并发 1 的三分之一。

## 测试

```bash
.venv/bin/python tests/test_mcp_e2e.py
```

该脚本会以真正的 MCP 客户端身份通过 stdio 连接本 Server，列出全部工具并逐个实调
（含中文名解析、指数/ETF/可转债、以及各类参数校验的错误提示），最后打印通过数。

## 项目结构

```
src/xueqiu_mcp/
├── client.py        HTTP 客户端：令牌续期、连接池与 HTTP/2、并发闸门、风控识别
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
    └── social.py    论坛：讨论、公告新闻、热帖、评论
```

## 说明

- 数据全部来自雪球公开接口，行情可能有延迟，**不构成任何投资建议**。
- 本项目仅供学习研究，请遵守雪球的服务条款，避免高频请求。
- 雪球接口非官方开放平台，字段与可用性可能随时调整。
