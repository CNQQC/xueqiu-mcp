"""雪球 MCP Server：把雪球的行情、财务、资金与社区数据暴露成 MCP 工具。

传输方式由 ``XUEQIU_TRANSPORT`` 决定，默认 ``stdio``（Claude Desktop / Claude Code
本地接入）。要在一台机器上同时服务多个客户端，设成 ``streamable-http``，
再用 ``XUEQIU_HOST`` / ``XUEQIU_PORT`` 指定监听地址。
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

try:  # mcp >= 2.0
    from mcp.server import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server

from .client import get_client
from .tools import capital, f10, finance, quote, social
from .tools import market as market_tools

logging.basicConfig(
    level=os.getenv("XUEQIU_MCP_LOG", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("xueqiu_mcp")

INSTRUCTIONS = """雪球(xueqiu.com)数据服务，覆盖 A 股 / 港股 / 美股。

代码写法：A 股用 SH600519、SZ000001（或直接写 600519，会自动补交易所前缀），
港股用 5 位数字如 00700，美股用字母代码如 AAPL。也可以直接传中文名称
（如「贵州茅台」），工具会先走搜索再取数。

数据均为雪球公开数据，行情可能有延迟，不构成投资建议。"""

@asynccontextmanager
async def _lifespan(_server):
    """进程退出时收好 HTTP 连接池，http 模式下同样有效。"""
    try:
        yield {}
    finally:
        await get_client().aclose()


try:
    mcp = _Server("xueqiu", instructions=INSTRUCTIONS, lifespan=_lifespan)
except TypeError:  # 老版 mcp 不支持 lifespan
    mcp = _Server("xueqiu", instructions=INSTRUCTIONS)


# ------------------------------------------------------------------ 搜索与行情
@mcp.tool()
async def search_stock(query: str, limit: int = 10) -> str:
    """按名称、拼音或代码搜索股票，返回雪球标准代码。

    当用户给的是公司名称而不是代码，且需要确认具体标的时使用。

    Args:
        query: 名称 / 拼音首字母 / 代码，如「茅台」「gzmt」「600519」
        limit: 返回条数，最多 20
    """
    return await quote.search_stock(query, limit)


@mcp.tool()
async def get_quote(symbols: str, detail: bool = False) -> str:
    """实时行情快照：价格、涨跌、成交、市值、PE/PB、股息率等。

    支持一次查多个标的，用逗号分隔，也支持 A 股 / 港股 / 美股 / 指数 / ETF / 可转债。

    Args:
        symbols: 一个或多个代码，如 "600519" 或 "600519,00700,AAPL"
        detail: 为 true 时返回全部行情字段（含质押比例、商誉占比等）
    """
    return await quote.get_quote(symbols, detail)


@mcp.tool()
async def get_kline(
    symbol: str,
    period: str = "day",
    count: int = 60,
    adjust: str = "before",
    with_valuation: bool = False,
) -> str:
    """历史 K 线，可选带上每根 K 线的 PE / PB / PS / 市值。

    Args:
        symbol: 股票代码
        period: 周期，1m/5m/15m/30m/60m/120m/day/week/month/quarter/year
        count: 最近多少根 K 线，最多 500
        adjust: 复权方式，before 前复权 / after 后复权 / normal 不复权
        with_valuation: 为 true 时附带估值指标，适合做估值分位分析
    """
    return await quote.get_kline(symbol, period, count, adjust, with_valuation)


@mcp.tool()
async def get_minute(symbol: str, period: str = "1d") -> str:
    """当日或近 5 日分时走势（自动抽样到约 40 个点）。

    Args:
        symbol: 股票代码
        period: 1d 当日 / 5d 近五日
    """
    return await quote.get_minute(symbol, period)


# ------------------------------------------------------------------ 财务
@mcp.tool()
async def get_financial_statement(
    symbol: str,
    statement: str = "indicator",
    report_type: str = "all",
    count: int = 5,
    detail: bool = False,
) -> str:
    """财务报表与核心指标，按「指标 × 报告期」排版便于跨期对比。

    A 股 / 港股 / 美股都支持，字段已翻译成中文并换算单位。

    Args:
        symbol: 股票代码
        statement: indicator 主要指标 / income 利润表 / balance 资产负债表 / cash_flow 现金流量表
        report_type: all 全部报告期 / Q1 一季报 / Q2 中报 / Q3 三季报 / Q4 年报
        count: 取最近几期，最多 20
        detail: 为 true 时输出全部科目，否则只给核心科目
    """
    return await finance.get_financial_statement(symbol, statement, report_type, count, detail)


@mcp.tool()
async def get_business_breakdown(symbol: str, count: int = 2) -> str:
    """主营构成：按产品和地区拆分的收入、成本与毛利率（仅 A 股）。

    Args:
        symbol: 股票代码
        count: 取最近几个报告期，最多 8
    """
    return await finance.get_business_breakdown(symbol, count)


# ------------------------------------------------------------------ 公司资料
@mcp.tool()
async def get_company_profile(symbol: str) -> str:
    """公司资料：简介、主营、实控人、员工数、所属行业与概念板块（仅 A 股）。

    Args:
        symbol: 股票代码
    """
    return await f10.get_company_profile(symbol)


@mcp.tool()
async def get_shareholders(symbol: str, top_n: int = 10) -> str:
    """股东结构：股东户数走势、十大流通股东、基金/社保/QFII 等机构持仓（仅 A 股）。

    Args:
        symbol: 股票代码
        top_n: 展示前几大流通股东，最多 20
    """
    return await f10.get_shareholders(symbol, top_n)


@mcp.tool()
async def get_dividends(symbol: str, count: int = 10) -> str:
    """历年分红送配方案与除权除息日（仅 A 股）。

    Args:
        symbol: 股票代码
        count: 取最近几次，最多 50
    """
    return await f10.get_dividends(symbol, count)


# ------------------------------------------------------------------ 资金
@mcp.tool()
async def get_capital_flow(symbol: str, days: int = 10) -> str:
    """主力资金流向：每日净流入走势，以及当日大中小单的成交结构。

    Args:
        symbol: 股票代码
        days: 回溯天数，接口最多提供约 20 个交易日
    """
    return await capital.get_capital_flow(symbol, days)


@mcp.tool()
async def get_margin_trading(symbol: str, days: int = 10) -> str:
    """融资融券余额、融资买入与净买入（仅两融标的）。

    Args:
        symbol: 股票代码
        days: 回溯交易日数，最多 90
    """
    return await capital.get_margin_trading(symbol, days)


@mcp.tool()
async def get_block_trades(symbol: str, count: int = 10) -> str:
    """大宗交易明细：成交价、折溢价率与买卖双方营业部。

    Args:
        symbol: 股票代码
        count: 取最近几笔，最多 50
    """
    return await capital.get_block_trades(symbol, count)


# ------------------------------------------------------------------ 市场
@mcp.tool()
async def screen_stocks(
    market: str = "CN",
    order_by: str = "mc",
    order: str = "desc",
    limit: int = 20,
    page: int = 1,
    filters: str | None = None,
    industry: str | None = None,
    exchange: str | None = None,
    columns: str | None = None,
) -> str:
    """选股器：按估值、财务、行情等指标筛选并排序股票。

    filters 写成 `指标:下限~上限`，多条用逗号分隔，边界可留空。
    例：`pettm:0~20,dy_l:3~,mc:100000000000~` 表示市盈率 0~20 倍、
    股息率 3% 以上、市值 1000 亿以上。
    指标名请先用 list_screener_metrics 查询；`_l` 后缀表示取最新报告期。

    Args:
        market: CN A股 / HK 港股 / US 美股
        order_by: 排序指标，如 mc 总市值、pct 涨跌幅、pettm 市盈率
        order: desc 降序 / asc 升序
        limit: 每页条数，最多 100
        page: 页码
        filters: 筛选条件串
        industry: 行业代码，用 list_industries 查询
        exchange: A 股范围，sh_sz 沪深 / sh / sz / bj
        columns: 额外展示的指标，逗号分隔；注意会排除该指标为空的标的
    """
    return await market_tools.screen_stocks(
        market, order_by, order, limit, page, filters, industry, exchange, columns
    )


@mcp.tool()
async def list_screener_metrics(market: str = "CN", keyword: str | None = None) -> str:
    """列出选股器支持的全部指标及其中文名（雪球官方元数据）。

    在使用 screen_stocks 前调用，以确认指标名的正确写法。
    **建议总是带上 keyword**：不带时会列出全部指标，返回内容大约是带 keyword 时的 9 倍。

    Args:
        market: CN / HK / US
        keyword: 按中文名或指标名过滤，如「市盈」「roe」。省略则返回全部指标
    """
    return await market_tools.list_screener_metrics(market, keyword)


@mcp.tool()
async def list_industries(market: str = "CN") -> str:
    """申万行业分类列表，返回的 encode 可作为 screen_stocks 的 industry 参数。

    Args:
        market: CN / HK / US
    """
    return await market_tools.list_industries(market)


@mcp.tool()
async def get_hot_stocks(market: str = "CN", limit: int = 20) -> str:
    """雪球人气榜：按用户关注热度排序的股票及热度变化。

    Args:
        market: CN A股 / HK 港股 / US 美股
        limit: 取前多少名，最多 100
    """
    return await market_tools.get_hot_stocks(market, limit)


# ------------------------------------------------------------------ 社区（论坛）
@mcp.tool()
async def get_stock_discussions(
    symbol: str, sort: str = "hot", count: int = 10, page: int = 1
) -> str:
    """个股讨论区帖子：作者、粉丝数、点赞评论数与正文。

    想了解市场情绪、散户与大 V 观点时使用。

    Args:
        symbol: 股票代码
        sort: hot 按热度（高赞高评论）/ time 按最新
        count: 条数，最多 30
        page: 页码
    """
    return await social.get_stock_discussions(symbol, sort, count, page)


@mcp.tool()
async def get_stock_news(symbol: str, kind: str = "news", count: int = 10, page: int = 1) -> str:
    """个股新闻或公告流。

    Args:
        symbol: 股票代码
        kind: news 新闻 / notice 公司公告
        count: 条数，最多 30
        page: 页码
    """
    return await social.get_stock_news(symbol, kind, count, page)


@mcp.tool()
async def get_hot_posts(count: int = 10) -> str:
    """雪球首页热门讨论，用于了解当前全市场关注的话题。

    Args:
        count: 条数，最多 30
    """
    return await social.get_hot_posts(count)


@mcp.tool()
async def search_posts(query: str, count: int = 10, page: int = 1) -> str:
    """全站搜索雪球帖子。

    Args:
        query: 关键词，如「宇树」「红利策略」
        count: 条数，最多 30
        page: 页码
    """
    return await social.search_posts(query, count, page)


@mcp.tool()
async def get_post(post_id: str, with_comments: bool = True, comment_count: int = 10) -> str:
    """按帖子 ID 取全文及热门评论。ID 可从讨论列表或热帖列表中获得。

    Args:
        post_id: 帖子数字 ID，或完整帖子链接
        with_comments: 是否附带评论
        comment_count: 评论条数，最多 30
    """
    return await social.get_post(post_id, with_comments, comment_count)


@mcp.tool()
async def get_user_posts(user_id: str, count: int = 10, page: int = 1) -> str:
    """查看某位雪球用户的发帖动态，用于跟踪特定作者的观点。

    Args:
        user_id: 雪球用户数字 ID（见帖子链接 xueqiu.com/<user_id>/<post_id>）
        count: 条数，最多 30
        page: 页码
    """
    return await social.get_user_posts(user_id, count, page)


def run() -> None:
    """启动 MCP Server。

    ==========================  ==================  ================================
    环境变量                     默认                说明
    ==========================  ==================  ================================
    ``XUEQIU_TRANSPORT``         stdio               stdio / streamable-http / sse
    ``XUEQIU_HOST``              127.0.0.1           http 模式监听地址
    ``XUEQIU_PORT``              8000                http 模式监听端口
    ``XUEQIU_STATELESS``         1                   http 模式不保留会话状态
    ==========================  ==================  ================================

    ``stateless`` 模式下每个请求彼此独立，服务端不为客户端保留会话，内存不随
    连接数累积，也方便多副本横向扩展 —— 大并发场景应保持开启。
    """
    transport = os.getenv("XUEQIU_TRANSPORT", "stdio").strip().lower()
    if transport in ("http", "streamable-http", "streamable_http"):
        host = os.getenv("XUEQIU_HOST", "127.0.0.1")
        port = int(os.getenv("XUEQIU_PORT", "8000"))
        stateless = os.getenv("XUEQIU_STATELESS", "1").strip().lower() not in ("0", "false", "no", "off")
        log.info("以 streamable-http 启动：http://%s:%s/mcp（stateless=%s）", host, port, stateless)
        mcp.run(
            "streamable-http",
            host=host,
            port=port,
            stateless_http=stateless,
            json_response=True,  # 纯请求-响应型工具，不需要 SSE 流式开销
        )
    elif transport == "sse":
        mcp.run("sse", host=os.getenv("XUEQIU_HOST", "127.0.0.1"), port=int(os.getenv("XUEQIU_PORT", "8000")))
    else:
        mcp.run()
