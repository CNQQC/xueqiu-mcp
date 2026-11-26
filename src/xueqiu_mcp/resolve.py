"""把用户输入解析成雪球 symbol：先规则规范化，再用搜索兜底。"""

from __future__ import annotations

import asyncio
import re

from .client import get_client
from .symbols import is_resolvable, normalize_symbol

_SPLIT_RE = re.compile(r"[,，、;；\s]+")

_STOCK_TYPE_NAME = {
    11: "A股", 12: "A股", 30: "港股", 31: "港股", 82: "科创板",
    0: "美股", 1: "美股", 2: "美股", 3: "美股", 5: "美股",
    23: "美股", 6: "指数", 18: "指数", 26: "基金/ETF",
}


async def search(query: str, size: int = 10) -> list[dict]:
    """按名称 / 拼音 / 代码搜索标的。"""
    data = await get_client().get(
        "/query/v1/suggest_stock.json",
        {"q": query, "count": size},
        base="https://xueqiu.com",
        referer="https://xueqiu.com/",
    )
    items = data.get("data") or []
    out = []
    for it in items[:size]:
        code = it.get("code") or ""
        out.append(
            {
                "symbol": code,
                "name": it.get("query") or "",
                "type": _STOCK_TYPE_NAME.get(it.get("stock_type"), "其他"),
            }
        )
    return out


async def resolve_symbol(raw: str) -> str:
    """解析单个标的。规则判不出来时（如中文名）走雪球搜索取第一条。"""
    s = normalize_symbol(raw)
    if is_resolvable(s):
        return s
    hits = await search(raw, size=1)
    if not hits:
        raise ValueError(f"未找到与「{raw}」匹配的标的，请换用股票代码或更准确的名称。")
    return hits[0]["symbol"]


async def resolve_many(raw: str | list[str], *, unresolved: list[str] | None = None) -> list[str]:
    """解析多个标的，保持输入顺序并去重。

    能按规则判定的代码直接就地规范化；只有中文名之类需要走搜索的才发请求，
    且这些请求并发发出 —— 否则查 5 个中文名就要串行等 5 个 RTT。
    """
    if isinstance(raw, list):
        parts = raw
    else:
        parts = [p for p in _SPLIT_RE.split(raw or "") if p.strip()]

    resolved: list[str | None] = []
    pending: list[tuple[int, str]] = []
    for i, p in enumerate(parts):
        s = normalize_symbol(p)
        if is_resolvable(s):
            resolved.append(s)
        else:
            resolved.append(None)
            pending.append((i, p))

    if pending:
        hits = await asyncio.gather(*(resolve_symbol(p) for _, p in pending), return_exceptions=True)
        for (i, name), hit in zip(pending, hits):
            if isinstance(hit, ValueError) and unresolved is not None:
                unresolved.append(name)
                continue
            if isinstance(hit, BaseException):
                raise hit
            resolved[i] = hit

    out: list[str] = []
    for sym in resolved:
        if sym and sym not in out:
            out.append(sym)
    return out
