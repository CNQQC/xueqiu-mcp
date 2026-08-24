"""资金面工具：主力资金流、成交单结构、融资融券、大宗交易。"""

from __future__ import annotations

from ..client import get_client
from ..formatting import fmt_money, fmt_num, fmt_pct, fmt_share, fmt_ts, md_table
from ..resolve import resolve_symbol


async def get_capital_flow(symbol: str, days: int = 10) -> str:
    """主力资金净流入：历史每日净额 + 当日各类单成交结构。"""
    sym = await resolve_symbol(symbol)
    client = get_client()
    days = max(1, min(int(days), 60))
    out = [f"### {sym} 资金流向\n"]

    hist = (await client.get(
        "/v5/stock/capital/history.json", {"symbol": sym, "count": days}
    )).get("data") or {}
    # 该接口忽略 count，固定返回约 20 个交易日，这里在客户端截断
    items = (hist.get("items") or [])[-days:]
    if items:
        sums = [
            ("近3日", hist.get("sum3")), ("近5日", hist.get("sum5")),
            ("近10日", hist.get("sum10")), ("近20日", hist.get("sum20")),
        ]
        out.append("**主力资金累计净流入**：" + "　".join(
            f"{k} {fmt_money(v)}" for k, v in sums if v is not None
        ))
        rows = [[fmt_ts(it.get("timestamp")), fmt_money(it.get("amount"))] for it in reversed(items)]
        out.append(f"\n**每日主力净流入（近 {len(rows)} 个交易日）**\n")
        out.append(md_table(["日期", "主力净流入"], rows))

    try:
        a = (await client.get("/v5/stock/capital/assort.json", {"symbol": sym})).get("data") or {}
    except Exception:
        a = {}
    if a and a.get("buy_total") is not None:
        levels = [
            ("特大单", "buy_xlarge", "sell_xlarge"),
            ("大单", "buy_large", "sell_large"),
            ("中单", "buy_medium", "sell_medium"),
            ("小单", "buy_small", "sell_small"),
            ("合计", "buy_total", "sell_total"),
        ]
        rows = []
        for label, bk, sk in levels:
            b, s = a.get(bk), a.get(sk)
            if b is None and s is None:
                continue
            net = (b or 0) - (s or 0)
            rows.append([label, fmt_money(b), fmt_money(s), fmt_money(net)])
        if rows:
            out.append(f"\n**成交单结构（{fmt_ts(a.get('timestamp'))}）**\n")
            out.append(md_table(["类型", "流入", "流出", "净额"], rows))

    return "\n".join(out) if len(out) > 1 else f"{sym} 暂无资金流数据。"


async def get_margin_trading(symbol: str, days: int = 10) -> str:
    """融资融券余额与净买入。"""
    sym = await resolve_symbol(symbol)
    data = await get_client().get(
        "/v5/stock/capital/margin.json",
        {"symbol": sym, "page": 1, "size": max(1, min(int(days), 90))},
    )
    d = data.get("data") or {}
    items = d.get("items") or []
    if not items:
        return f"{sym} 无融资融券数据（可能不是两融标的）。"

    rows = [
        [
            fmt_ts(it.get("td_date")),
            fmt_money(it.get("margin_trading_amt_balance")),
            fmt_money(it.get("margin_trading_buy_amt")),
            fmt_money(it.get("margin_trading_net_buy_amt")),
            fmt_money(it.get("short_selling_amt_balance")),
        ]
        for it in items
    ]
    head = f"### {sym} 融资融券（近 {len(rows)} 个交易日）\n"
    if d.get("inclusion_date"):
        head += f"纳入两融标的日期：{fmt_ts(d['inclusion_date'])}\n"
    return head + "\n" + md_table(
        ["日期", "融资余额", "融资买入额", "融资净买入", "融券余额"], rows
    )


async def get_block_trades(symbol: str, count: int = 10) -> str:
    """大宗交易明细。"""
    sym = await resolve_symbol(symbol)
    data = await get_client().get(
        "/v5/stock/capital/blocktrans.json",
        {"symbol": sym, "page": 1, "size": max(1, min(int(count), 50))},
    )
    items = (data.get("data") or {}).get("items") or []
    if not items:
        return f"{sym} 近期无大宗交易记录。"

    rows = [
        [
            fmt_ts(it.get("td_date")),
            fmt_num(it.get("trans_price"), 2),
            fmt_pct(it.get("premium_rat"), signed=True),
            fmt_share(it.get("vol")),
            fmt_money(it.get("trans_amt")),
            (it.get("buy_branch_org_name") or "-")[:20],
            (it.get("sell_branch_org_name") or "-")[:20],
        ]
        for it in items
    ]
    return f"### {sym} 大宗交易（近 {len(rows)} 笔）\n\n" + md_table(
        ["日期", "成交价", "溢价率", "成交量", "成交额", "买方营业部", "卖方营业部"], rows
    )
