"""行情类工具：搜索、实时报价、K 线、分时。"""

from __future__ import annotations

import time
from typing import Any

from ..cache import ttl_for
import asyncio

from ..client import get_client
from ..results import field_unit, iso_time, records, select_fields
from ..trading import market_zone, partial_bar, period_end, read_observation
from datetime import datetime, time as wall_time
from ..fields import QUOTE_BRIEF, QUOTE_FIELDS
from ..formatting import (
    fmt_money,
    fmt_num,
    fmt_pct,
    fmt_share,
    fmt_ts,
    kv_block,
    md_table,
    render_fields,
)
from ..resolve import resolve_many, resolve_symbol, search

KLINE_PERIODS = {
    "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "60m": "60m", "120m": "120m",
    "day": "day", "week": "week", "month": "month", "quarter": "quarter", "year": "year",
}
ADJUST_TYPES = {"before": "前复权", "after": "后复权", "normal": "不复权"}

KLINE_PATH = "/v5/stock/chart/kline.json"


async def search_stock(query: str, limit: int = 10, format: str = "markdown") -> str | dict:
    hits = await search(query, size=max(1, min(limit, 20)))
    if format == "json":
        return records(hits, limit, 20, query=query)
    if not hits:
        return f"未搜索到与「{query}」相关的标的。"
    rows = [[h["symbol"], h["name"], h["type"]] for h in hits]
    return f"「{query}」搜索结果：\n\n" + md_table(["代码", "名称", "类型"], rows)


async def get_quote(symbols: str, detail: bool = False, format: str = "markdown") -> str | dict:
    """实时行情。支持一次传多个标的，逗号分隔。"""
    syms = await resolve_many(symbols)
    if not syms:
        return "未提供有效的股票代码。"

    data = await get_client().get(
        "/v5/stock/batch/quote.json", {"symbol": ",".join(syms), "extend": "detail"}
    )
    items = (data.get("data") or {}).get("items") or []
    if format == "json":
        quotes = []
        for item in items:
            q = item.get("quote") or {}
            keys = list(QUOTE_FIELDS) if detail else QUOTE_BRIEF
            values = select_fields(q, keys, dates={"issue_date"})
            quotes.append({**values, "as_of": iso_time(q.get("timestamp")), "currency": q.get("currency"),
                           "market_status": (item.get("market") or {}).get("status"),
                           "units": {k: field_unit(QUOTE_FIELDS[k][1], q.get("currency")) for k in values}})
        return records(quotes, len(syms), None)
    if not items:
        return f"未取到 {', '.join(syms)} 的行情数据，请确认代码是否正确。"

    blocks: list[str] = []
    for it in items:
        q: dict[str, Any] = it.get("quote") or {}
        mk = it.get("market") or {}
        order = None if detail else QUOTE_BRIEF
        pairs = render_fields(q, QUOTE_FIELDS, order, currency=q.get("currency"))
        head = (
            f"### {q.get('name', '?')} ({q.get('symbol', '?')})　"
            f"{fmt_num(q.get('current'), 3)} "
            f"{fmt_pct(q.get('percent'), signed=True)}　"
            f"[{mk.get('status', '-')}　{fmt_ts(q.get('timestamp'), '%Y-%m-%d %H:%M:%S')}]"
        )
        # 头部已展示的字段不再重复
        skip = {"名称", "代码", "现价", "涨跌幅"}
        blocks.append(head + "\n" + kv_block([p for p in pairs if p[0] not in skip]))

    note = ""
    if any((it.get("quote") or {}).get("current_ext") for it in items):
        note = "\n\n注：带「盘后价」的标的处于盘后交易时段。"
    return "\n\n".join(blocks) + note


async def get_kline(
    symbol: str,
    period: str = "day",
    count: int = 60,
    adjust: str = "before",
    with_valuation: bool = False,
    exclude_today: bool = False, format: str = "markdown",
) -> str | dict:
    """历史 K 线。count 为最近 N 根，可选带上 PE/PB/市值。"""
    sym = await resolve_symbol(symbol)
    period = KLINE_PERIODS.get(period, period)
    if period not in KLINE_PERIODS:
        return f"不支持的周期「{period}」，可选：{', '.join(KLINE_PERIODS)}"
    requested = count
    count = max(1, min(int(count), 500))

    indicator = "kline"
    if with_valuation:
        indicator += ",pe,pb,ps,market_capital"

    # begin 若用毫秒级当前时间，每次调用的 cache key 都不一样，K 线缓存会 100% 落空，
    # 还会把大条目不断塞进缓存挤掉别的端点。量化到 TTL 边界即可，返回数据完全相同。
    step = max(1.0, ttl_for(KLINE_PATH))
    begin = int(time.time() // step * step * 1000)

    zone = market_zone(sym)
    today = datetime.now(zone).date()
    if exclude_today:
        begin = int(datetime.combine(today, wall_time.min, zone).timestamp() * 1000) - 1
    client = get_client()
    data, (snapshot, observed, zone) = await asyncio.gather(client.get(
        KLINE_PATH,
        {
            "symbol": sym,
            "begin": begin,
            "period": period,
            "type": adjust,
            "count": -min(500, count + int(exclude_today)),
            "indicator": indicator,
        },
    ), read_observation(client, sym))
    d = data.get("data") or {}
    cols: list[str] = d.get("column") or []
    items: list[list] = d.get("item") or []
    if exclude_today and "timestamp" in cols:
        ts_index = cols.index("timestamp")
        items = [row for row in items if (stamp := iso_time(row[ts_index], zone))
                 and period_end(datetime.fromisoformat(stamp).date(), period) < today]
    items = items[-count:]

    idx = {c: i for i, c in enumerate(cols)}
    show = ["timestamp", "open", "high", "low", "close", "percent", "volume", "amount", "turnoverrate"]
    if with_valuation:
        show += ["pe", "pb", "ps", "market_capital"]
    show = [c for c in show if c in idx]
    labels = {
        "timestamp": "日期", "open": "开盘", "high": "最高", "low": "最低", "close": "收盘",
        "percent": "涨跌幅", "volume": "成交量", "amount": "成交额", "turnoverrate": "换手率",
        "pe": "PE", "pb": "PB", "ps": "PS", "market_capital": "总市值",
    }

    last_stamp = iso_time(items[-1][idx["timestamp"]], zone) if items else None
    partial = partial_bar(last_stamp, period, observed, snapshot)
    if format == "json":
        bars = [{**{c: row[idx[c]] for c in show}, "timestamp": iso_time(row[idx["timestamp"]], zone),
                 "is_partial": partial if i == len(items) - 1 else False} for i, row in enumerate(items)]
        money = snapshot["currency"]
        units = {c: ("percent" if c in ("percent", "turnoverrate") else "shares" if c == "volume" else
                     "multiple" if c in ("pe", "pb", "ps") else money) for c in show if c != "timestamp"}
        return records(bars, requested, 500, symbol=sym, period=period, adjust=adjust, exclude_today=exclude_today,
                       units=units, is_partial=partial, **snapshot)
    if not items:
        return f"{sym} 无符合条件的 K 线数据。"
    tf = "%Y-%m-%d" if period in ("day", "week", "month", "quarter", "year") else "%m-%d %H:%M"
    rows = []
    for row in items:
        cells = []
        for c in show:
            v = row[idx[c]]
            if c == "timestamp":
                cells.append(fmt_ts(v, tf))
            elif c == "percent":
                cells.append(fmt_pct(v, signed=True))
            elif c == "volume":
                cells.append(fmt_share(v))
            elif c in ("amount", "market_capital"):
                cells.append(fmt_money(v))
            elif c == "turnoverrate":
                cells.append(fmt_pct(v))
            else:
                cells.append(fmt_num(v, 3))
        rows.append(cells)

    first, last = items[0][idx["close"]], items[-1][idx["close"]]
    chg = (last - first) / first * 100 if first else 0
    title = (
        f"### {d.get('symbol', sym)} {period} K线（{ADJUST_TYPES.get(adjust, adjust)}，最近 {len(rows)} 根）\n"
        f"区间：{rows[0][0]} ~ {rows[-1][0]}　收盘 {fmt_num(first, 3)} → {fmt_num(last, 3)}"
        f"　区间涨跌 {fmt_pct(chg, signed=True)}\n"
    )
    state = {True: "盘中未完成", False: "已完成", None: "完成状态未知"}[partial]
    title += f"最后一根：{state}　as_of={snapshot['as_of']}　exclude_today={exclude_today}\n"
    return title + "\n" + md_table([labels[c] for c in show], rows)


async def get_minute(symbol: str, period: str = "1d", format: str = "markdown") -> str | dict:
    """分时走势。period: 1d 当日 / 5d 五日。"""
    sym = await resolve_symbol(symbol)
    if period not in ("1d", "5d"):
        return "period 仅支持 1d（当日）或 5d（五日）。"

    data = await get_client().get(
        "/v5/stock/chart/minute.json", {"symbol": sym, "period": period}
    )
    d = data.get("data") or {}
    items = (d.get("items") or []) + (d.get("after") or [])
    if format == "json":
        from ..symbols import market_of
        currency = d.get("currency") or {"CN": "CNY", "HK": "HKD", "US": "USD"}[market_of(sym)]
        keys = ("timestamp", "current", "avg_price", "percent", "volume", "amount")
        return records([select_fields(i, keys, dates={"timestamp"}) for i in items], symbol=sym, period=period,
                       last_close=d.get("last_close"), units={"current": currency, "avg_price": currency,
                       "volume": "shares", "percent": "percent", "amount": currency})
    if not items:
        return f"{sym} 暂无分时数据（可能未开盘或代码有误）。"

    last_close = d.get("last_close")
    # 分时点位很多，等距抽样到 ~40 个点，够模型判断走势又不撑爆上下文
    step = max(1, len(items) // 40)
    sampled = items[::step]
    if sampled[-1] is not items[-1]:
        sampled.append(items[-1])

    rows = [
        [
            fmt_ts(p.get("timestamp"), "%m-%d %H:%M"),
            fmt_num(p.get("current"), 3),
            fmt_pct(p.get("percent"), signed=True),
            fmt_num(p.get("avg_price"), 3),
            fmt_share(p.get("volume")),
        ]
        for p in sampled
    ]
    highs = [p.get("current") for p in items if p.get("current") is not None]
    head = (
        f"### {sym} 分时（{'当日' if period == '1d' else '近5日'}，共 {len(items)} 点，已抽样 {len(rows)} 点）\n"
        f"昨收 {fmt_num(last_close, 3)}　最高 {fmt_num(max(highs), 3)}　最低 {fmt_num(min(highs), 3)}"
        f"　最新 {fmt_num(highs[-1], 3)}\n"
    )
    return head + "\n" + md_table(["时间", "价格", "涨跌幅", "均价", "分钟成交量"], rows)
