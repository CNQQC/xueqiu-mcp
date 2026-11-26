"""市场面工具：选股器、指标清单、行业列表、人气榜。"""

from __future__ import annotations

from ..client import get_client
from ..results import field_unit, records, select_fields
from ..formatting import fmt_money, fmt_num, fmt_pct, fmt_share, md_table
from ..screener import CATEGORIES, load_metrics, metric_label, validate
from ..symbols import is_resolvable, market_of, normalize_symbol

MARKETS = {
    "CN": ("A股", "sh_sz"),
    "HK": ("港股", None),
    "US": ("美股", None),
}
# hot_stock/list 的 type=10 是跨市场榜。
HOT_MARKETS = {"CN": 12, "US": 11, "HK": 13}
CN_EXCHANGES = {"sh_sz": "沪深", "sh": "上交所", "sz": "深交所", "bj": "北交所"}

# 指标 → 渲染单位。未列出的按数值渲染。
_UNITS = {
    "mc": "money", "fmc": "money", "amount": "money", "netprofit": "money",
    "total_revenue": "money", "volume": "share",
    "pct": "pct", "pct5": "pct", "pct10": "pct", "pct20": "pct", "pct60": "pct",
    "pct120": "pct", "pct250": "pct", "pct_current_year": "pct", "chgpct": "pct",
    "tr": "pct", "dy_l": "pct", "roediluted": "pct", "roediluted_l": "pct",
    "npay": "pct", "oiy": "pct", "niota": "pct", "gsr_l": "pct", "debt_ratio": "pct",
    "roa": "pct", "pm": "pct",
}
_BASE_COLS = ["symbol", "name", "current", "pct", "mc"]
_BASE_LABELS = {"symbol": "代码", "name": "名称", "current": "现价", "pct": "涨跌幅", "mc": "总市值"}


# 选股接口不返回计价货币，按市场推断，避免把美股市值读成人民币
_MARKET_CURRENCY = {"CN": "CNY", "HK": "HKD", "US": "USD"}


def _render(key: str, value, market: str = "CN") -> str:
    from ..formatting import fmt_by_unit

    if key in ("symbol", "name"):
        return str(value or "-")
    unit = _UNITS.get(key, "times" if key.startswith(("pe", "pb", "ps")) else "plain")
    if unit == "plain":
        return fmt_num(value, 3)
    return fmt_by_unit(value, unit, _MARKET_CURRENCY.get(market, "CNY"))


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
    format: str = "markdown",
) -> str | dict:
    """按指标筛选并排序股票。

    filters 写成 ``指标:下限~上限``，多条用逗号分隔，边界可留空：
    ``pettm:0~20,dy_l:3~,mc:100000000000~``
    指标名用 list_screener_metrics 查询；``_l`` 后缀表示取最新报告期。
    """
    market = (market or "CN").upper()
    if market not in MARKETS:
        return f"market 仅支持：{', '.join(MARKETS)}"
    label, default_exchange = MARKETS[market]

    params: dict = {
        "category": market,
        "page": max(1, int(page)),
        "size": max(1, min(int(limit), 100)),
        "order": "asc" if str(order).lower() == "asc" else "desc",
        "order_by": order_by,
        "only_count": 0,
    }
    ex = exchange or default_exchange
    if market == "CN":
        if ex not in CN_EXCHANGES:
            return f"exchange 仅支持：{', '.join(CN_EXCHANGES)}"
        params["exchange"] = ex
    elif ex:
        params["exchange"] = ex
    if industry:
        params["indcode"] = industry

    err = await validate(market, order_by)
    if err:
        return err

    shown: list[str] = [order_by]
    applied: list[str] = []
    for key, lo, hi in _parse_filters(filters):
        err = await validate(market, key)
        if err:
            return err
        params[key] = f"{lo or '-100000000000000'}_{hi or '100000000000000'}"
        applied.append(f"{await metric_label(market, key)} {lo or '不限'}~{hi or '不限'}")
        shown.append(key)

    extra = [c.strip() for c in (columns or "").replace("，", ",").split(",") if c.strip()]
    for key in extra:
        err = await validate(market, key)
        if err:
            return err
        params.setdefault(key, "-100000000000000_100000000000000")
        shown.append(key)

    data = await get_client().get(
        "/v5/stock/screener/screen.json", params, referer="https://xueqiu.com/hq"
    )
    d = data.get("data") or {}
    rows_raw = d.get("list") or []
    if not rows_raw and format != "json":
        return "没有符合条件的标的，可放宽筛选范围。"

    cols = list(_BASE_COLS)
    for k in shown:
        if k not in cols:
            cols.append(k)
    cols = [c for c in cols if any(c in r for r in rows_raw)]

    if format == "json":
        currency = _MARKET_CURRENCY[market]
        units = {k: field_unit(_UNITS.get(k, "plain"), currency) for k in cols}
        units["current"] = currency
        return records([select_fields(it, cols) for it in rows_raw], limit, 100, page=params["page"], pageable=True,
                       total=d.get("count"), market=market, units=units, currency=currency)
    headers = [_BASE_LABELS.get(c) or await metric_label(market, c) for c in cols]
    rows = [[_render(c, r.get(c), market) for c in cols] for r in rows_raw]

    head = (
        f"### {label}选股（按 {await metric_label(market, order_by)}"
        f"{'升序' if params['order'] == 'asc' else '降序'}，"
        f"第 {page} 页，命中 {fmt_num(d.get('count'))} 只）\n"
    )
    if market == "CN":
        head += f"范围：{CN_EXCHANGES.get(ex, ex)}\n"
    if industry:
        head += f"行业：{industry}\n"
    if applied:
        head += "筛选：" + "；".join(applied) + "\n"
    tail = ""
    if extra:
        tail = (
            "\n\n注：columns 里指定的额外指标会排除该指标为空的标的，"
            "命中数因此可能小于不加 columns 时。"
        )
    return head + "\n" + md_table(headers, rows) + tail


def _parse_filters(raw: str | None) -> list[tuple[str, str, str]]:
    """解析 ``pettm:0~20,dy_l:3~`` 形式的条件串。"""
    if not raw:
        return []
    out = []
    for part in str(raw).replace("，", ",").split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        key, _, rng = part.partition(":")
        rng = rng.replace("－", "~")
        if "~" not in rng:
            rng = rng.replace("-", "~", 1) if not rng.startswith("-") else rng
        lo, _, hi = rng.partition("~")
        out.append((key.strip(), lo.strip(), hi.strip()))
    return out


async def list_screener_metrics(market: str = "CN", keyword: str | None = None, format: str = "markdown") -> str | dict:
    """列出选股器在该市场支持的全部指标（雪球官方元数据）。"""
    market = (market or "CN").upper()
    if market not in CATEGORIES:
        return f"market 仅支持：{', '.join(CATEGORIES)}"
    metrics = await load_metrics(market)
    if format == "json":
        return records([{"key": key, **m} for key, m in metrics.items()
                        if not keyword or keyword in m["name"] or keyword.lower() in key.lower()], market=market)
    if not metrics:
        return "未取到指标清单。"

    groups: dict[str, list[str]] = {}
    for key, meta in metrics.items():
        if keyword and keyword not in meta["name"] and keyword.lower() not in key.lower():
            continue
        line = f"`{key}`={meta['name']}"
        if meta["has_date"]:
            line += "(需报告期)"
        groups.setdefault(meta["group"], []).append(line)

    if not groups:
        return f"没有匹配「{keyword}」的指标。"
    out = [f"### {MARKETS.get(market, (market,))[0]}选股器可用指标"]
    if keyword:
        out.append(f"（关键词：{keyword}）")
    out.append(
        "\n用法：filters=`指标:下限~上限`，order_by=`指标`。"
        "标注「需报告期」的指标要写成 `指标.YYYYMMDD`（如 `np.20251231`），"
        "或改用带 `_l` 后缀的最新期版本（如 `roediluted_l`）。\n"
    )
    for g, lines in groups.items():
        out.append(f"**{g}**：" + "，".join(lines))
    return "\n".join(out)


async def list_industries(market: str = "CN", format: str = "markdown") -> str | dict:
    """申万行业分类；encode 可作为 screen_stocks 的 industry 参数。"""
    market = (market or "CN").upper()
    if market not in MARKETS:
        return f"market 仅支持：{', '.join(MARKETS)}"
    mtype = MARKETS[market][1] or market.lower()
    data = await get_client().get(
        "/v5/stock/screener/industries.json", {"category": mtype, "type": mtype}
    )
    inds = (data.get("data") or {}).get("industries") or []
    if format == "json":
        return records([select_fields(i, ("encode", "name", "pinyin")) for i in inds], market=market)
    if not inds:
        return "未取到行业列表。"
    rows = [[i.get("encode", ""), i.get("name", ""), i.get("pinyin", "")] for i in inds]
    return (
        f"### {MARKETS[market][0]}行业分类（{len(rows)} 个）\n"
        "把 encode 传给 screen_stocks 的 industry 参数即可按行业筛选。\n\n"
        + md_table(["encode", "行业名称", "拼音"], rows)
    )


async def get_hot_stocks(market: str = "CN", limit: int = 20, format: str = "markdown") -> str | dict:
    """雪球人气榜（按用户关注热度排序）。"""
    market = (market or "CN").strip().upper()
    if market not in HOT_MARKETS:
        return f"market 仅支持：{', '.join(HOT_MARKETS)}"
    data = await get_client().get(
        "/v5/stock/hot_stock/list.json",
        {"size": max(1, min(int(limit), 100)), "type": HOT_MARKETS[market]},
    )
    items = (data.get("data") or {}).get("items") or []
    if not items and format != "json":
        return "未取到人气榜数据。"
    rows = []
    raw_rows = []
    for i, it in enumerate(items, 1):
        symbol = normalize_symbol(str(it.get("symbol") or it.get("code") or ""))
        if not is_resolvable(symbol) or market_of(symbol) != market:
            return f"雪球人气榜第 {i} 名的代码（{symbol or '缺失'}）无法匹配请求的{MARKETS[market][0]}市场。"
        if format == "json":
            raw_rows.append({**select_fields(it, ("name", "current", "percent", "value", "increment")), "symbol": symbol, "rank": i})
            continue
        rows.append([
            str(i),
            symbol,
            it.get("name") or "-",
            fmt_num(it.get("current"), 3),
            fmt_pct(it.get("percent"), signed=True),
            fmt_num(it.get("value")),
            f"{it.get('increment'):+d}" if isinstance(it.get("increment"), int) else "-",
        ])
    if format == "json":
        return records(raw_rows, limit, 100, market=market, units={"current": _MARKET_CURRENCY[market],
                       "percent": "percent", "value": "heat", "increment": "heat"})
    return (
        f"### 雪球{MARKETS[market][0]}人气榜 Top {len(rows)}\n"
        "「热度」为雪球用户关注度指标，「热度变化」为较上一时段增减。\n\n"
        + md_table(["#", "代码", "名称", "现价", "涨跌幅", "热度", "热度变化"], rows)
    )
