"""资金面工具：主力资金流、成交单结构、融资融券、大宗交易。"""

from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal

from ..client import gather_json, get_client
from ..results import iso_time, records, select_fields
from ..trading import partial_bar, read_observation
from ..formatting import fmt_money, fmt_num, fmt_pct, fmt_share, fmt_ts, md_table
from ..resolve import resolve_symbol


async def get_capital_flow(symbol: str, days: int = 10, exclude_today: bool = False, format: str = "markdown") -> str | dict:
    """主力资金净流入：历史每日净额 + 当日各类单成交结构。"""
    sym = await resolve_symbol(symbol)
    client = get_client()
    requested = days
    days = max(1, min(int(days), 60))
    (hist_raw, assort_raw), (snapshot, observed, zone) = await asyncio.gather(gather_json(
        client.get("/v5/stock/capital/history.json", {"symbol": sym, "count": max(20, days + int(exclude_today))}),
        client.get("/v5/stock/capital/assort.json", {"symbol": sym}),
    ), read_observation(client, sym))
    hist = hist_raw.get("data") or {}
    daily = []
    for row in hist.get("items") or []:
        stamp = iso_time(row.get("timestamp"), zone)
        if not stamp or (exclude_today and datetime.fromisoformat(stamp).date() == observed.date()):
            continue
        daily.append({"timestamp": stamp, "amount": row.get("amount"),
                      "is_partial": partial_bar(stamp, "day", observed, snapshot)})
    daily.sort(key=lambda row: row["timestamp"])
    windows = {}
    for n in (3, 5, 10, 20):
        window = daily[-n:]
        amounts = [r["amount"] for r in window if isinstance(r["amount"], (int, float))]
        complete = len(amounts) == n
        flags = [r["is_partial"] for r in window]
        windows[str(n)] = {"amount": float(sum(Decimal(str(v)) for v in amounts)) if complete else None,
                           "available_days": len(amounts), "start": window[0]["timestamp"] if window else None,
                           "end": window[-1]["timestamp"] if window else None,
                           "is_partial": True if True in flags else (None if None in flags or not complete else False)}
    items = daily[-days:]
    a = assort_raw.get("data") or {}
    assort_stamp = iso_time(a.get("timestamp"), zone)
    if exclude_today and (not assort_stamp or datetime.fromisoformat(assort_stamp).date() == observed.date()):
        a = {}
    partial = items[-1]["is_partial"] if items else None
    if format == "json":
        keys = ("timestamp", "buy_total", "sell_total", "buy_xlarge", "sell_xlarge", "buy_large", "sell_large",
                "buy_medium", "sell_medium", "buy_small", "sell_small")
        return records(items, requested, 60, symbol=sym, windows=windows, exclude_today=exclude_today,
                       units={"amount": snapshot["currency"], "order_flow": snapshot["currency"]},
                       order_flow=select_fields(a, keys, dates={"timestamp"}) if a else None,
                       is_partial=partial, **snapshot)
    out = [f"### {sym} 资金流向\n"]
    if items:
        out.append(f"as_of={snapshot['as_of']}　is_partial={partial}　exclude_today={exclude_today}")
        out.append("**主力资金累计净流入**：" + "　".join(
            f"近{n}日 {fmt_money(w['amount']) if w['amount'] is not None else '样本不足'}" for n, w in windows.items()))
        rows = [[it["timestamp"][:10], fmt_money(it["amount"])] for it in reversed(items)]
        out.append(f"\n**每日主力净流入（近 {len(rows)} 个交易日）**\n")
        out.append(md_table(["日期", "主力净流入"], rows))

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


async def get_margin_trading(symbol: str, days: int = 10, format: str = "markdown") -> str | dict:
    """融资融券余额与净买入。"""
    sym = await resolve_symbol(symbol)
    data = await get_client().get(
        "/v5/stock/capital/margin.json",
        {"symbol": sym, "page": 1, "size": max(1, min(int(days), 90))},
    )
    d = data.get("data") or {}
    items = d.get("items") or []
    if format == "json":
        keys = ("td_date", "margin_trading_amt_balance", "margin_trading_buy_amt", "margin_trading_net_buy_amt", "short_selling_amt_balance")
        return records([select_fields(i, keys, dates={"td_date"}) for i in items], days, 90, symbol=sym,
                       units={k: "CNY" for k in keys[1:]})
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


async def get_block_trades(symbol: str, count: int = 10, format: str = "markdown") -> str | dict:
    """大宗交易明细。"""
    sym = await resolve_symbol(symbol)
    data = await get_client().get(
        "/v5/stock/capital/blocktrans.json",
        {"symbol": sym, "page": 1, "size": max(1, min(int(count), 50))},
    )
    items = (data.get("data") or {}).get("items") or []
    if format == "json":
        keys = ("td_date", "trans_price", "premium_rat", "vol", "trans_amt", "buy_branch_org_name", "sell_branch_org_name")
        return records([select_fields(i, keys, dates={"td_date"}) for i in items], count, 50, symbol=sym,
                       units={"trans_price": "CNY", "trans_amt": "CNY", "vol": "shares", "premium_rat": "percent"})
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
