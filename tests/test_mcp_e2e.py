"""端到端测试：以 MCP 客户端身份连接本 Server，列出并调用工具。"""

from __future__ import annotations

import asyncio
import os
import re
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from xueqiu_mcp.symbols import is_resolvable, market_of

# (工具名, 参数, 期望在结果里出现的关键字)
CASES: list[tuple[str, dict, list[str]]] = [
    ("search_stock", {"query": "茅台", "limit": 3}, ["SH600519", "贵州茅台"]),
    ("get_quote", {"symbols": "600519,00700,AAPL"}, ["贵州茅台", "腾讯控股", "苹果", "总市值"]),
    ("get_kline", {"symbol": "600519", "period": "day", "count": 5, "with_valuation": True}, ["K线", "PE"]),
    ("get_minute", {"symbol": "600519"}, ["分时"]),
    ("get_financial_statement", {"symbol": "600519", "statement": "indicator", "count": 3}, ["净资产收益率ROE", "销售毛利率"]),
    ("get_financial_statement", {"symbol": "AAPL", "statement": "income", "count": 2}, ["毛利润", "营业利润"]),
    ("get_financial_statement", {"symbol": "00700", "statement": "balance", "count": 2}, ["资产总额", "股东资金"]),
    ("get_business_breakdown", {"symbol": "600519", "count": 1}, ["主营构成"]),
    ("get_company_profile", {"symbol": "600519"}, ["贵州茅台酒股份有限公司", "所属行业"]),
    ("get_shareholders", {"symbol": "600519", "top_n": 3}, ["股东户数", "十大流通股东"]),
    ("get_dividends", {"symbol": "600519", "count": 3}, ["分红送配", "除权除息日"]),
    ("get_capital_flow", {"symbol": "600519", "days": 5}, ["资金流向", "主力净流入"]),
    ("get_margin_trading", {"symbol": "600519", "days": 3}, ["融资余额"]),
    ("get_block_trades", {"symbol": "600519", "count": 2}, ["大宗交易"]),
    ("screen_stocks", {"market": "CN", "order_by": "mc", "limit": 3, "filters": "pettm:0~15,dy_l:3~"}, ["选股", "市盈率TTM"]),
    ("list_screener_metrics", {"market": "CN", "keyword": "市盈"}, ["pettm"]),
    ("list_industries", {"market": "CN"}, ["行业分类", "S1101"]),
    ("get_hot_stocks", {"limit": 3}, ["A股人气榜 Top"]),
    ("get_hot_stocks", {"market": "CN", "limit": 3}, ["A股人气榜 Top"]),
    ("get_hot_stocks", {"market": "HK", "limit": 3}, ["港股人气榜 Top"]),
    ("get_hot_stocks", {"market": "US", "limit": 3}, ["美股人气榜 Top"]),
    ("get_stock_discussions", {"symbol": "600519", "sort": "hot", "count": 2}, ["雪球讨论", "帖子ID"]),
    ("get_stock_news", {"symbol": "600519", "kind": "notice", "count": 2}, ["公告"]),
    ("get_hot_posts", {"count": 2}, ["雪球热门讨论"]),
    ("search_posts", {"query": "红利", "count": 2}, ["相关帖子"]),
    ("search_users", {"query": "久期财经", "count": 3}, ["相关用户", "4357540281"]),
    ("get_user_posts", {"user_id": "1955602780", "count": 2}, ["动态"]),
    # --- 中文名解析 ---
    ("get_quote", {"symbols": "贵州茅台"}, ["SH600519"]),
    ("get_financial_statement", {"symbol": "宁德时代", "count": 1}, ["宁德时代"]),
    # --- 非股票品种 ---
    ("get_quote", {"symbols": "SH000300,510300,113050"}, ["沪深300"]),
    # --- 参数校验应给出可读提示而非报错 ---
    ("get_financial_statement", {"symbol": "600519", "statement": "xxx"}, ["仅支持"]),
    ("screen_stocks", {"market": "XX"}, ["仅支持"]),
    ("screen_stocks", {"market": "CN", "order_by": "not_a_metric"}, ["不可用"]),
    ("get_minute", {"symbol": "600519", "period": "99d"}, ["仅支持"]),
    ("get_stock_discussions", {"symbol": "600519", "sort": "bad"}, ["仅支持"]),
    ("get_post", {"post_id": "abc"}, ["数字 ID"]),
    ("search_stock", {"query": "不存在的公司名xyz123"}, ["未搜索到"]),
    # --- 账号：只测不改变登录态的路径，别把跑测试的人踢下线 ---
    ("login_status", {}, ["登录"]),
    ("login_with_cookie", {"cookie": "foo=bar"}, ["没有登录身份"]),
    ("login_with_cookie", {"cookie": "  "}, ["没能从这串文本里解析"]),
    # 浏览器登录本身要人扫码，测不了；这里只确认工具注册正确、且没启动时不会抛错
    ("browser_close", {}, ["浏览器"]),
    ("browser_login", {"mode": "冒牌模式"}, ["mode 仅支持"]),
]


# 雪球的搜索接口对境外 IP 返回空结果（接口本身 200，只是搜不到东西）。GitHub runner
# 在美国，跑巡检时这一条必然失败。设了 XUEQIU_E2E_GEO_TOLERANT 就把这种**明确可辨认
# 的**地域限制记为跳过而不是失败 —— 让每周巡检红的时候是真的坏了，而不是「又是那条」。
# 本地默认不开，仍然严格。
GEO_TOLERANT = os.getenv("XUEQIU_E2E_GEO_TOLERANT", "").strip() not in ("", "0", "false")
GEO_BLOCKED_HINT = "未搜索到"


def _geo_limited(name: str, body: str) -> bool:
    return GEO_TOLERANT and name == "search_stock" and GEO_BLOCKED_HINT in body


def _is_error(res) -> bool:
    """mcp 1.x 用 isError，2.x 用 is_error。"""
    return bool(getattr(res, "is_error", None) or getattr(res, "isError", None))


def check_hot_stock_rows(body: str, market: str, limit: int) -> None:
    codes = re.findall(r"^\| \d+ \| ([^|]*) \|", body, re.MULTILINE)
    assert 0 < len(codes) <= limit, f"{market} 榜单实际返回 {len(codes)} 行"
    for code in codes:
        assert is_resolvable(code) and market_of(code) == market, f"{market} 榜单包含 {code}"


async def main() -> int:
    params = StdioServerParameters(command=sys.executable, args=["-m", "xueqiu_mcp"])
    ok = fail = skip = 0
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()

            tools = (await session.list_tools()).tools
            print(f"已注册 {len(tools)} 个工具：")
            for t in tools:
                first = (t.description or "").strip().split("\n")[0]
                print(f"  - {t.name}: {first}")
            print()

            names = {t.name for t in tools}
            missing = {c[0] for c in CASES} - names
            if missing:
                print(f"❌ 测试用例引用了未注册的工具：{missing}")
                return 1

            # get_post 需要先从讨论里拿一个真实帖子 ID
            disc = await session.call_tool(
                "get_stock_discussions", {"symbol": "600519", "count": 1}
            )
            text = disc.content[0].text
            pid = None
            for line in text.splitlines():
                if line.startswith("帖子ID"):
                    pid = line.split()[0].removeprefix("帖子ID").strip() or line.split("　")[0].split()[-1]
                    break
            cases = list(CASES)
            if pid and pid.isdigit():
                cases.append(("get_post", {"post_id": pid, "comment_count": 2}, ["作者："]))

            for name, args, expect in cases:
                try:
                    res = await session.call_tool(name, args)
                    body = "\n".join(
                        c.text for c in res.content if getattr(c, "text", None)
                    )
                    missing_kw = [k for k in expect if k not in body]
                    if missing_kw and not _is_error(res) and _geo_limited(name, body):
                        skip += 1
                        print(f"⚠️  {name:26} 跳过：搜索接口对境外 IP 返回空结果")
                    elif _is_error(res) or missing_kw:
                        fail += 1
                        print(f"❌ {name}({args})")
                        print(f"   缺少关键字 {missing_kw}")
                        print(f"   返回: {body[:200]}")
                    else:
                        if name == "get_hot_stocks":
                            check_hot_stock_rows(body, args.get("market", "CN"), args["limit"])
                        ok += 1
                        print(f"✅ {name:26} {len(body):>6} 字符")
                except Exception as e:
                    fail += 1
                    print(f"❌ {name}({args}) 异常: {type(e).__name__}: {e}")

    tail = f"，跳过 {skip}" if skip else ""
    print(f"\n通过 {ok} / {ok + fail}{tail}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
