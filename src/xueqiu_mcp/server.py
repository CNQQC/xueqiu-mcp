"""雪球 MCP Server：把雪球的行情、财务、资金与社区数据暴露成 MCP 工具。

传输方式由 ``XUEQIU_TRANSPORT`` 决定，默认 ``stdio``（Claude Desktop / Claude Code
本地接入）。要在一台机器上同时服务多个客户端，设成 ``streamable-http``，
再用 ``XUEQIU_HOST`` / ``XUEQIU_PORT`` 指定监听地址。
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from mcp.types import CallToolResult

try:  # mcp >= 2.0
    from mcp.server import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server

from . import browser, login_flow
from .client import get_client
from .results import OutputFormat, tool_result
from .tools import account, capital, f10, finance, quote, social
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

数据均为雪球公开数据，行情可能有延迟，不构成投资建议。

默认匿名工作，行情 / 财务 / 选股等公开数据无需登录。
只读工具使用 format=json 返回原始数值和 ISO 时间。翻页请沿 pagination.next_page 续读，评论传 comment_page。

查某个人的一手观点时，先 search_users 找到并核对账号，再 search_posts(query=主题词,
user_id=该账号ID)。不要仅把人名拼进全站搜索的 query，否则会搜到别人讨论他或转载他的帖子。
昵称有歧义时请列出候选，不要默认粉丝最多的人就是目标。已知主页链接也可直接作为 user_id。
结果中的作者 ID 必须属于目标账号；本人发帖也可能引用他人，务必区分「本条作者文字」、
「引用文字」和「被转发帖」。用 get_post 读取全文后再归纳观点，并附作者、时间和帖子原链接。
搜索无结果不等于作者没有说过；可缩短关键词、翻页或用 get_user_posts 查看近期动态。

接口偶发返回「未登录」时，客户端会使用现有凭证自动重试一次，不会清除登录态。
用户动态接口的 10022 同样出现在特定分页请求中，单凭该错误无法确认账号登录状态。
关于登录：少数受风控保护的社区接口和账号相关数据需要登录。自动重试后仍因未登录
而失败，返回的报错里会**直接带上完整的登录步骤** —— 请把那些步骤转述给用户，不要
只回一句「需要登录」就停下。首选 get_login_qrcode（立即返回二维码图片和临时登录链接，
请把图片和链接直接展示给用户，用户用雪球 App 扫码后后台会自动保存凭证）；不要先等待
用户扫码再展示图片。网页会更新二维码和登录结果。那台机器没装浏览器支持时，
再引导用户复制浏览器 Cookie 交给你，由你调用 login_with_cookie。不确定当前登录态时，
用 login_status 查。

另外，讨论区 / 热帖 / 搜帖这些列表接口只给一两百字的摘要，要读全文就对着帖子 ID 调
get_post —— 它取的是完整正文，正文残缺时还会用本机浏览器补一次。所以别因为列表里只有
摘要就下结论，先 get_post。"""

@asynccontextmanager
async def _lifespan(_server):
    """进程退出时收好 HTTP 连接池和本地浏览器，http 模式下同样有效。"""
    try:
        yield {}
    finally:
        await login_flow.aclose()
        await get_client().aclose()
        await browser.aclose()


try:
    mcp = _Server("xueqiu", instructions=INSTRUCTIONS, lifespan=_lifespan)
except TypeError:  # 老版 mcp 不支持 lifespan
    mcp = _Server("xueqiu", instructions=INSTRUCTIONS)

# 短期随机 token 是这两条只读路由的访问凭证，页面不返回账号 Cookie。
mcp.custom_route("/login/{token}", methods=["GET"])(login_flow.login_page)
mcp.custom_route("/login/{token}/state", methods=["GET"])(login_flow.login_state)


# ------------------------------------------------------------------ 搜索与行情
@mcp.tool()
async def search_stock(query: str, limit: int = 10, format: OutputFormat = "markdown") -> CallToolResult:
    """按名称、拼音或代码搜索股票，返回雪球标准代码。

    当用户给的是公司名称而不是代码，且需要确认具体标的时使用。

    Args:
        query: 名称 / 拼音首字母 / 代码，如「茅台」「gzmt」「600519」
        limit: 返回条数，最多 20
    """
    value = await quote.search_stock(query, limit, format=format)
    return tool_result(value, format)


@mcp.tool()
async def get_quote(symbols: str, detail: bool = False, format: OutputFormat = "markdown") -> CallToolResult:
    """实时行情快照：价格、涨跌、成交、市值、PE/PB、股息率等。

    支持一次查多个标的，用逗号分隔，也支持 A 股 / 港股 / 美股 / 指数 / ETF / 可转债。

    Args:
        symbols: 一个或多个代码，如 "600519" 或 "600519,00700,AAPL"
        detail: 为 true 时返回全部行情字段（含质押比例、商誉占比等）
    """
    value = await quote.get_quote(symbols, detail, format=format)
    return tool_result(value, format)


@mcp.tool()
async def get_kline(
    symbol: str,
    period: str = "day",
    count: int = 60,
    adjust: str = "before",
    with_valuation: bool = False,
    exclude_today: bool = False, format: OutputFormat = "markdown",
) -> CallToolResult:
    """历史 K 线，可选带上每根 K 线的 PE / PB / PS / 市值。

    Args:
        symbol: 股票代码
        period: 周期，1m/5m/15m/30m/60m/120m/day/week/month/quarter/year
        count: 最近多少根 K 线，最多 500
        exclude_today: 排除交易所当天及覆盖当天的未结束周期
        adjust: 复权方式，before 前复权 / after 后复权 / normal 不复权
        with_valuation: 为 true 时附带估值指标，适合做估值分位分析
    """
    value = await quote.get_kline(
        symbol, period, count, adjust, with_valuation,
        exclude_today=exclude_today, format=format,
    )
    return tool_result(value, format)


@mcp.tool()
async def get_minute(symbol: str, period: str = "1d", format: OutputFormat = "markdown") -> CallToolResult:
    """当日或近 5 日分时走势（自动抽样到约 40 个点）。

    Args:
        symbol: 股票代码
        period: 1d 当日 / 5d 近五日
    """
    value = await quote.get_minute(symbol, period, format=format)
    return tool_result(value, format)


# ------------------------------------------------------------------ 财务
@mcp.tool()
async def get_financial_statement(
    symbol: str,
    statement: str = "indicator",
    report_type: str = "all",
    count: int = 5,
    detail: bool = False,
    format: OutputFormat = "markdown",
) -> CallToolResult:
    """财务报表与核心指标，按「指标 × 报告期」排版便于跨期对比。

    A 股 / 港股 / 美股都支持，字段已翻译成中文并换算单位。

    Args:
        symbol: 股票代码
        statement: indicator 主要指标 / income 利润表 / balance 资产负债表 / cash_flow 现金流量表
        report_type: all 全部报告期 / Q1 一季报 / Q2 中报 / Q3 三季报 / Q4 年报
        count: 取最近几期，最多 20
        detail: 为 true 时输出全部科目，否则只给核心科目
    """
    value = await finance.get_financial_statement(symbol, statement, report_type, count, detail, format=format)
    return tool_result(value, format)


@mcp.tool()
async def get_business_breakdown(symbol: str, count: int = 2, format: OutputFormat = "markdown") -> CallToolResult:
    """主营构成：按产品和地区拆分的收入、成本与毛利率（仅 A 股）。

    Args:
        symbol: 股票代码
        count: 取最近几个报告期，最多 8
    """
    value = await finance.get_business_breakdown(symbol, count, format=format)
    return tool_result(value, format)


# ------------------------------------------------------------------ 公司资料
@mcp.tool()
async def get_company_profile(symbol: str, format: OutputFormat = "markdown") -> CallToolResult:
    """公司资料：简介、主营、实控人、员工数、所属行业与概念板块（仅 A 股）。

    Args:
        symbol: 股票代码
    """
    value = await f10.get_company_profile(symbol, format=format)
    return tool_result(value, format)


@mcp.tool()
async def get_shareholders(symbol: str, top_n: int = 10, format: OutputFormat = "markdown") -> CallToolResult:
    """股东结构：股东户数走势、十大流通股东、基金/社保/QFII 等机构持仓（仅 A 股）。

    Args:
        symbol: 股票代码
        top_n: 展示前几大流通股东，最多 20
    """
    value = await f10.get_shareholders(symbol, top_n, format=format)
    return tool_result(value, format)


@mcp.tool()
async def get_dividends(symbol: str, count: int = 10, format: OutputFormat = "markdown") -> CallToolResult:
    """历年分红送配方案与除权除息日（仅 A 股）。

    Args:
        symbol: 股票代码
        count: 取最近几次，最多 50
    """
    value = await f10.get_dividends(symbol, count, format=format)
    return tool_result(value, format)


# ------------------------------------------------------------------ 资金
@mcp.tool()
async def get_capital_flow(
    symbol: str, days: int = 10,
    exclude_today: bool = False, format: OutputFormat = "markdown",
) -> CallToolResult:
    """主力资金流向：每日净流入走势，以及当日大中小单的成交结构。

    Args:
        symbol: 股票代码
        days: 回溯天数，接口最多提供约 20 个交易日
        exclude_today: 排除交易所当天后重新计算累计净额
    """
    value = await capital.get_capital_flow(symbol, days, exclude_today=exclude_today, format=format)
    return tool_result(value, format)


@mcp.tool()
async def get_margin_trading(symbol: str, days: int = 10, format: OutputFormat = "markdown") -> CallToolResult:
    """融资融券余额、融资买入与净买入（仅两融标的）。

    Args:
        symbol: 股票代码
        days: 回溯交易日数，最多 90
    """
    value = await capital.get_margin_trading(symbol, days, format=format)
    return tool_result(value, format)


@mcp.tool()
async def get_block_trades(symbol: str, count: int = 10, format: OutputFormat = "markdown") -> CallToolResult:
    """大宗交易明细：成交价、折溢价率与买卖双方营业部。

    Args:
        symbol: 股票代码
        count: 取最近几笔，最多 50
    """
    value = await capital.get_block_trades(symbol, count, format=format)
    return tool_result(value, format)


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
    format: OutputFormat = "markdown",
) -> CallToolResult:
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
    value = await market_tools.screen_stocks(
        market, order_by, order, limit, page, filters, industry, exchange, columns,
        format=format,
    )
    return tool_result(value, format)


@mcp.tool()
async def list_screener_metrics(
    market: str = "CN", keyword: str | None = None,
    format: OutputFormat = "markdown",
) -> CallToolResult:
    """列出选股器支持的全部指标及其中文名（雪球官方元数据）。

    在使用 screen_stocks 前调用，以确认指标名的正确写法。
    **建议总是带上 keyword**：不带时会列出全部指标，返回内容大约是带 keyword 时的 9 倍。

    Args:
        market: CN / HK / US
        keyword: 按中文名或指标名过滤，如「市盈」「roe」。省略则返回全部指标
    """
    value = await market_tools.list_screener_metrics(market, keyword, format=format)
    return tool_result(value, format)


@mcp.tool()
async def list_industries(market: str = "CN", format: OutputFormat = "markdown") -> CallToolResult:
    """申万行业分类列表，返回的 encode 可作为 screen_stocks 的 industry 参数。

    Args:
        market: CN / HK / US
    """
    value = await market_tools.list_industries(market, format=format)
    return tool_result(value, format)


@mcp.tool()
async def get_hot_stocks(market: str = "CN", limit: int = 20, format: OutputFormat = "markdown") -> CallToolResult:
    """雪球人气榜：按用户关注热度排序的股票及热度变化。

    Args:
        market: CN A股 / HK 港股 / US 美股
        limit: 取前多少名，最多 100
    """
    value = await market_tools.get_hot_stocks(market, limit, format=format)
    return tool_result(value, format)


# ------------------------------------------------------------------ 社区（论坛）
@mcp.tool()
async def get_stock_discussions(
    symbol: str, sort: str = "hot", count: int = 10, page: int = 1,
    format: OutputFormat = "markdown",
) -> CallToolResult:
    """个股讨论区帖子：作者、粉丝数、点赞评论数与正文。

    想了解市场情绪、散户与大 V 观点时使用。

    Args:
        symbol: 股票代码
        sort: hot 按热度（高赞高评论）/ time 按最新
        count: 每页最多 30 条，超过 20 条自动分批获取，结果显示请求数和实返数
        page: 页码
    """
    value = await social.get_stock_discussions(symbol, sort, count, page, format=format)
    return tool_result(value, format)


@mcp.tool()
async def get_stock_news(
    symbol: str, kind: str = "news", count: int = 10, page: int = 1,
    format: OutputFormat = "markdown",
) -> CallToolResult:
    """个股新闻或公告流。

    Args:
        symbol: 股票代码
        kind: news 新闻 / notice 公司公告
        count: 条数，最多 30
        page: 页码
    """
    value = await social.get_stock_news(symbol, kind, count, page, format=format)
    return tool_result(value, format)


@mcp.tool()
async def get_hot_posts(count: int = 10, page: int = 1, format: OutputFormat = "markdown") -> CallToolResult:
    """雪球首页热门讨论。首页 page=1，续读传入上次返回的 next_page 游标。

    Args:
        count: 条数，最多 30
    """
    value = await social.get_hot_posts(count, page=page, format=format)
    return tool_result(value, format)


@mcp.tool()
async def search_users(query: str, count: int = 10, page: int = 1, format: OutputFormat = "markdown") -> CallToolResult:
    """按昵称或关键词搜索雪球用户，返回用户 ID、昵称、主页及可用的认证和简介。

    不知道作者 ID 时先调用本工具；支持昵称片段，例如「老木匠」可找到「买股票的老木匠」。
    结果第一项可能是其他同名账号，简称不能直接当作完整昵称。结合返回信息区分用户，确认目标后，将
    user_id 传给 search_posts 搜索该作者的帖子，或 get_user_posts 查看动态。

    Args:
        query: 用户昵称或关键词，如「久期财经」
        count: 条数，最多 30
        page: 页码
    """
    value = await social.search_users(query, count, page, format=format)
    return tool_result(value, format)


@mcp.tool()
async def search_posts(
    query: str, count: int = 10, page: int | str = 1, user_id: str | None = None,
    since_id: str | int | None = None, since: str | None = None, until: str | None = None,
    original_only: bool = False, min_followers: int | None = None, format: OutputFormat = "markdown",
) -> CallToolResult:
    """搜索雪球帖子及关键词附近原文摘录，可限定作者并校验作者 ID。

    since_id 排除边界 ID；since/until 按发布时间筛选，纯日期包含当日。original_only 排除转发和纯 @ 提及。
    min_followers 过滤作者粉丝数。页内游标从 pagination.next_page 获取。

    查某人一手观点必须先 search_users 确认账号，再传 user_id；只在 query 里写人名会
    搜到他人的转述。结果区分作者文字和引用/转发；引用观点前用 get_post 获取全文。

    Args:
        query: 关键词，如「宇树」「红利策略」
        count: 条数，最多 30
        page: 页码
        user_id: 雪球用户数字 ID 或主页/帖子链接，可先用 search_users 查找；不填则全站搜索
    """
    value = await social.search_posts(
        query, count, page, user_id,
        since_id=since_id, since=since, until=until, original_only=original_only, min_followers=min_followers, format=format,
    )
    return tool_result(value, format)


@mcp.tool()
async def get_post(
    post_id: str, with_comments: bool = True, comment_count: int = 10, full_text: bool = True,
    comment_page: int = 1, format: OutputFormat = "markdown",
) -> CallToolResult:
    """按帖子 ID 取全文及热门评论。ID 可从讨论列表或热帖列表中获得。

    返回发布时间和编辑状态。雪球提供编辑时间时显示“已编辑”和北京时间，否则显示“未知”。
    正文残缺时（雪球置了 truncated，或正文为空的付费 / 仅粉丝可见内容）会用本机
    浏览器把完整正文取回来，需要装浏览器支持，没装就照常返回并给出提示。

    Args:
        post_id: 帖子数字 ID，或完整帖子链接
        with_comments: 是否附带评论
        comment_count: 每页评论条数，最多 30
        comment_page: 评论页码，从 1 起，按 pagination.next_page 继续
        full_text: 正文被截断时是否用本地浏览器补全；设 false 可省掉这次开销
    """
    value = await social.get_post(
        post_id, with_comments, comment_count, full_text,
        comment_page=comment_page, format=format,
    )
    return tool_result(value, format)


@mcp.tool()
async def get_user_posts(
    user_id: str, count: int = 10, page: int | str = 1,
    since_id: str | int | None = None, since: str | None = None, until: str | None = None,
    original_only: bool = False, min_followers: int | None = None, format: OutputFormat = "markdown",
) -> CallToolResult:
    """查看某位雪球用户的发帖动态，用于跟踪特定作者的观点。

    since_id 排除边界 ID；since/until 按发布时间筛选，纯日期包含当日。original_only 排除转发和纯 @ 提及。
    min_followers 过滤作者粉丝数。续读请传回 pagination.next_page。

    Args:
        user_id: 雪球用户数字 ID 或主页/帖子链接（xueqiu.com/<user_id>/<post_id>）
        count: 每页最多 30 条，超过 20 条自动分批获取，结果显示请求数和实返数
        page: 页码
    """
    value = await social.get_user_posts(
        user_id, count, page,
        since_id=since_id, since=since, until=until, original_only=original_only, min_followers=min_followers, format=format,
    )
    return tool_result(value, format)


# ------------------------------------------------------------------ 账号
@mcp.tool()
async def login_status(format: OutputFormat = "markdown") -> CallToolResult:
    """查看当前是匿名还是已登录，以及登录的是哪个雪球账号。

    某个接口报「被风控拦截」或「需要登录」时，先用它确认登录态。
    """
    value = await account.login_status(format=format)
    return tool_result(value, format)


@mcp.tool()
async def login_with_cookie(cookie: str, remember: bool = True) -> str:
    """用浏览器里的 Cookie 登录雪球，解锁受风控保护的社区接口与账号数据。

    获取方式：浏览器登录 xueqiu.com → 开发者工具 Network → 任意请求的 Cookie
    请求头 → 复制整串。必须是完整的一串，只截几项会缺少登录身份。
    校验不通过时不会保存，也不会破坏已有的登录态。

    注意：不要用这个工具传手机号密码。需要密码登录请在服务器命令行执行
    `xueqiu-mcp login`，密码不会经过这里。

    Args:
        cookie: 完整 Cookie 串，形如 `xq_a_token=...; xq_id_token=...; u=...`
        remember: 为 true 时保存到本地凭证文件（权限 0600），重启后仍有效
    """
    return await account.login_with_cookie(cookie, remember)


@mcp.tool()
async def logout(forget: bool = True) -> str:
    """退出登录，回到匿名模式。

    Args:
        forget: 为 true 时同时删除本地凭证文件，false 则只在本次进程内失效
    """
    return await account.logout(forget)


@mcp.tool()
async def get_login_qrcode(remember: bool = True, refresh: bool = False) -> CallToolResult:
    """获取雪球登录二维码图片和临时登录网页链接，取码后立即返回，不等待扫码。

    首选登录方式。请直接展示返回的图片和链接，用户用雪球 App 扫码并确认后，后台
    自动接管凭证。链接 5 分钟有效，页面更新二维码和结果；用 login_status 确认登录。
    重复调用复用有效会话，避免打断正在扫码的用户。

    Args:
        remember: 是否保存到 cookies.json；浏览器自身仍使用持久化 profile
        refresh: 为 true 时取消旧登录会话并生成新二维码及链接
    """
    return await account.get_login_qrcode(remember, refresh)


@mcp.tool()
async def browser_login(mode: str = "qrcode", wait: int = 0, remember: bool = True,
                        refresh: bool = False) -> CallToolResult:
    """通过浏览器登录雪球，默认立即返回二维码图片和临时链接，后台等待扫码。

    Args:
        mode: qrcode 无头模式显示二维码（默认）；window 弹出浏览器窗口自己登录，
              短信验证码、微信登录、滑块验证都能在窗口里完成
        wait: 默认 0 立即交付；已展示二维码后可显式等待，单次最多 30 秒
        remember: 是否保存到 cookies.json；浏览器自身仍使用持久化 profile
        refresh: 是否取消已有扫码会话并重新取码，默认复用有效会话
    """
    return await account.browser_login(mode, wait, remember, refresh)


@mcp.tool()
async def browser_close() -> str:
    """关掉本机浏览器释放内存（约 200MB）。登录态保留，下次需要时会自动拉起。"""
    return await account.browser_close()


def _run_http(transport: str, **options) -> None:
    # 1.x 从 settings 读监听配置；2.x 改为 run() 参数。
    if hasattr(mcp.settings, "host"):
        for name, value in options.items():
            setattr(mcp.settings, name, value)
        mcp.run(transport)
    else:
        mcp.run(transport, **options)


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
        _run_http(
            "streamable-http",
            host=host,
            port=port,
            stateless_http=stateless,
            json_response=True,  # 纯请求-响应型工具，不需要 SSE 流式开销
        )
    elif transport == "sse":
        _run_http("sse", host=os.getenv("XUEQIU_HOST", "127.0.0.1"),
                  port=int(os.getenv("XUEQIU_PORT", "8000")))
    else:
        mcp.run()
