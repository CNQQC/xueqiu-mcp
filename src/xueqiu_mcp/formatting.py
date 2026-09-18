"""把雪球返回的原始数值渲染成紧凑、可读的中文文本。

设计取向：输出对象是大模型，所以
1. 用中文字段名，避免模型猜 ``ncf_from_oa`` 是什么；
2. 大额金额换算成亿/万，而不是 ``1.7205417189091E11``；
3. 用 Markdown 表格承载多行数据，比 JSON 更省 token 也更好读。
"""

from __future__ import annotations

import html as _html
import re
from datetime import datetime, timezone, timedelta
from typing import Any, Iterable, Sequence

CN_TZ = timezone(timedelta(hours=8))

_UNIT_STEPS = (
    (1e12, "万亿"),
    (1e8, "亿"),
    (1e4, "万"),
)

# 雪球用 currency 字段标注计价货币，金额后缀跟着走，避免把美股成交额读成人民币
CURRENCY_SUFFIX = {
    "CNY": "元", "HKD": "港元", "USD": "美元", "EUR": "欧元",
    "GBP": "英镑", "JPY": "日元", "SGD": "新元", "TWD": "新台币",
}


def currency_suffix(currency: str | None) -> str:
    return CURRENCY_SUFFIX.get((currency or "CNY").upper(), currency or "元")


def fmt_ts(ms: Any, fmt: str = "%Y-%m-%d") -> str:
    """毫秒时间戳 → 北京时间字符串。"""
    if ms in (None, "", 0):
        return "-"
    try:
        return datetime.fromtimestamp(float(ms) / 1000, CN_TZ).strftime(fmt)
    except (ValueError, OSError, OverflowError):
        return str(ms)


def fmt_money(v: Any, unit_suffix: str = "元") -> str:
    """金额换算成万亿/亿/万，保留 2~4 位有效小数。"""
    if v is None or v == "":
        return "-"
    try:
        x = float(v)
    except (TypeError, ValueError):
        return str(v)
    sign = "-" if x < 0 else ""
    a = abs(x)
    for step, name in _UNIT_STEPS:
        if a >= step:
            return f"{sign}{a / step:,.2f}{name}{unit_suffix}"
    if a == int(a):
        return f"{sign}{int(a):,}{unit_suffix}"
    return f"{sign}{a:,.2f}{unit_suffix}"


def fmt_share(v: Any) -> str:
    """股数 / 成交量换算成亿股、万股（雪球成交量单位为股）。"""
    return fmt_money(v, unit_suffix="股")


def fmt_num(v: Any, digits: int = 2) -> str:
    if v is None or v == "":
        return "-"
    try:
        x = float(v)
    except (TypeError, ValueError):
        return str(v)
    if x == int(x) and abs(x) < 1e15:
        return f"{int(x):,}"
    return f"{x:,.{digits}f}"


def fmt_pct(v: Any, digits: int = 2, signed: bool = False) -> str:
    """已是百分数的值 → ``12.34%``。"""
    if v is None or v == "":
        return "-"
    try:
        x = float(v)
    except (TypeError, ValueError):
        return str(v)
    sign = "+" if signed and x > 0 else ""
    return f"{sign}{x:.{digits}f}%"


def fmt_rate(v: Any, digits: int = 2, signed: bool = True) -> str:
    """小数比率（0.1234） → ``+12.34%``，用于财报的同比增长率。"""
    if v is None or v == "":
        return "-"
    try:
        x = float(v) * 100
    except (TypeError, ValueError):
        return str(v)
    sign = "+" if signed and x > 0 else ""
    return f"{sign}{x:.{digits}f}%"


def fmt_by_unit(value: Any, unit: str, currency: str | None = None) -> str:
    """按字段映射表里的单位标记渲染单个值。"""
    if value is None or value == "":
        return "-"
    if unit == "money":
        return fmt_money(value, currency_suffix(currency))
    if unit == "share":
        return fmt_share(value)
    if unit == "pct":
        return fmt_pct(value)
    if unit == "rate":
        # 小数比率（0.2142）统一显示成百分数
        return fmt_rate(value, signed=False)
    if unit == "yuan":
        return fmt_num(value, 3)
    if unit == "times":
        return fmt_num(value, 2)
    if unit == "days":
        return f"{fmt_num(value, 1)}天"
    if unit == "date":
        return fmt_ts(value)
    return str(value)


def _prev_period(value: Any, yoy: Any) -> float | None:
    """由当前值和同比增长率反推上年同期值。"""
    try:
        v, y = float(value), float(yoy)
    except (TypeError, ValueError):
        return None
    if abs(1.0 + y) < 1e-9:  # 同比 -100%，反推会除零
        return None
    return v / (1.0 + y)


def fmt_metric(cell: Any, unit: str, currency: str | None = None) -> str:
    """财报里的 ``[数值, 同比增长率]`` → ``1,720.54亿元 (同比-1.20%)``。

    指标本身就是百分比时（ROE、毛利率、增长率等）不能这么写：``12.08% (同比+3.87%)``
    里两个百分号语义完全不同 —— 前者是值，后者是**相对变化率**，但最自然的读法是
    「上升了 3.87 个百分点」。按真实数据算，这一误读会把 ROE 的实际变化（+0.45pp）
    夸大 8.6 倍。所以这类指标改为直接给出上年同期值，让对比基数一目了然。
    """
    if isinstance(cell, (list, tuple)):
        value = cell[0] if cell else None
        yoy = cell[1] if len(cell) > 1 else None
    else:
        value, yoy = cell, None
    body = fmt_by_unit(value, unit, currency)
    if yoy is None or body == "-":
        return body
    if unit in ("pct", "rate"):
        prev = _prev_period(value, yoy)
        if prev is not None:
            return f"{body}（上年同期 {fmt_by_unit(prev, unit, currency)}）"
    return f"{body} (同比{fmt_rate(yoy)})"


def md_table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    """生成 Markdown 表格；无数据时返回提示文本。"""
    rows = [[("-" if c is None else str(c)) for c in r] for r in rows]
    if not rows:
        return "（无数据）"
    head = "| " + " | ".join(headers) + " |"
    sep = "| " + " | ".join("---" for _ in headers) + " |"
    body = "\n".join("| " + " | ".join(r) + " |" for r in rows)
    return f"{head}\n{sep}\n{body}"


def kv_block(pairs: Iterable[tuple[str, Any]], per_line: int = 3) -> str:
    """把键值对排成每行若干列的紧凑文本。"""
    items = [f"{k}: {v}" for k, v in pairs if v not in (None, "", "-")]
    lines = ["　".join(items[i : i + per_line]) for i in range(0, len(items), per_line)]
    return "\n".join(lines)


def render_fields(
    data: dict[str, Any],
    mapping: dict[str, tuple[str, str]],
    order: Sequence[str] | None = None,
    currency: str | None = None,
) -> list[tuple[str, str]]:
    """按映射表把一条记录渲染成 ``[(中文名, 格式化值)]``。"""
    keys = list(order) if order else [k for k in mapping if k in data]
    out: list[tuple[str, str]] = []
    for k in keys:
        if k not in data:
            continue
        label, unit = mapping.get(k, (k, "plain"))
        out.append((label, fmt_by_unit(data[k], unit, currency)))
    return out


# ---------------------------------------------------------------- 帖子正文
_BR = re.compile(r"<\s*(br|/p|/div|/li)\s*/?>", re.I)
_IMG = re.compile(r"<img[^>]*>", re.I)
_TAG = re.compile(r"<[^>]+>")
_BLANK = re.compile(r"\n{3,}")
_SPACES = re.compile(r"[ \t\u3000]{2,}")


def strip_html(raw: str | None, limit: int | None = None) -> str:
    """把雪球帖子的 HTML 正文清洗成纯文本。

    雪球正文里嵌了大量 ``<a href>`` 股票链接和图片标签，直接喂给模型既费 token
    又难读。这里保留可读文字与 ``$贵州茅台(SH600519)$`` 这类股票标记，丢掉标签。
    """
    if not raw:
        return ""
    t = _IMG.sub("[图片]", raw)
    t = _BR.sub("\n", t)
    t = _TAG.sub("", t)
    t = _html.unescape(t)
    t = t.replace("\u200b", "").replace("\xa0", " ")
    t = _SPACES.sub(" ", t)
    t = _BLANK.sub("\n\n", t).strip()
    if limit and len(t) > limit:
        t = t[:limit].rstrip() + "…"
    return t


def fmt_count(v: Any) -> str:
    """点赞/评论数：过万折算成 ``1.2万``。"""
    try:
        n = int(v or 0)
    except (TypeError, ValueError):
        return str(v)
    if n >= 10000:
        return f"{n / 10000:.1f}万"
    return str(n)


def post_url(status: dict) -> str:
    """拼出帖子的雪球网页地址。"""
    uid = status.get("user_id") or (status.get("user") or {}).get("id")
    sid = status.get("id")
    if uid and sid:
        return f"https://xueqiu.com/{uid}/{sid}"
    target = status.get("target") or ""
    if target.startswith("http"):
        return target
    if target:
        return "https://xueqiu.com" + target
    return ""
