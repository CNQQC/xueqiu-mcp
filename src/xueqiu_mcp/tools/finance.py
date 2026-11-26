"""财务类工具：三大报表、主要指标、主营构成。"""

from __future__ import annotations

from ..client import get_client
from ..fields import STATEMENTS
from ..fields_intl import HK_STATEMENTS, US_STATEMENTS
from ..formatting import fmt_metric, fmt_money, fmt_rate, fmt_ts, md_table
from ..resolve import resolve_symbol
from ..results import field_unit, iso_time, records
from ..symbols import market_of

REPORT_TYPES = {
    "Q1": "一季报", "Q2": "中报", "Q3": "三季报", "Q4": "年报", "all": "全部报告期",
}
_MARKET_PATH = {"CN": "cn", "HK": "hk", "US": "us"}
# 三个市场的财报字段名完全不同，各用各的映射表
_MARKET_STATEMENTS = {"CN": STATEMENTS, "HK": HK_STATEMENTS, "US": US_STATEMENTS}


async def get_financial_statement(
    symbol: str,
    statement: str = "indicator",
    report_type: str = "all",
    count: int = 5,
    detail: bool = False,
    format: str = "markdown",
) -> str | dict:
    """拉取财务报表，按「指标 × 报告期」转置成表格便于跨期对比。"""
    if statement not in STATEMENTS:
        return f"statement 仅支持：{', '.join(STATEMENTS)}"
    if report_type not in REPORT_TYPES:
        return f"report_type 仅支持：{', '.join(REPORT_TYPES)}"

    sym = await resolve_symbol(symbol)
    region = market_of(sym)
    market = _MARKET_PATH.get(region, "cn")
    title, mapping, brief = _MARKET_STATEMENTS[region][statement]
    count = max(1, min(int(count), 20))

    data = await get_client().get(
        f"/v5/stock/finance/{market}/{statement}.json",
        {"symbol": sym, "type": report_type, "is_detail": "true", "count": count},
    )
    d = data.get("data") or {}
    rows = d.get("list") or []
    if format == "json":
        keys = list(mapping) if detail else brief
        items = [{"report_name": r.get("report_name"), "report_date": iso_time(r.get("report_date")),
                  "metrics": {k: r[k] for k in keys if k in r}} for r in rows]
        return records(items, count, 20, symbol=sym, statement=statement, currency=d.get("currency"),
                       units={k: field_unit(mapping[k][1], d.get("currency")) for k in keys},
                       metric_columns=["value", "yoy_fraction"])
    if not rows:
        return f"{sym} 未取到{title}数据（该市场或该报告类型可能无数据）。"

    currency = d.get("currency")
    periods = [r.get("report_name") or fmt_ts(r.get("report_date")) for r in rows]

    keys = list(mapping) if detail else list(brief)
    # 只保留至少有一期有值的指标，避免整行都是 "-"
    keys = [k for k in keys if any(_has_value(r.get(k)) for r in rows)]
    if not keys:
        return f"{sym} 的{title}在所选报告期内无有效数据。"

    table_rows = []
    for k in keys:
        label, unit = mapping[k]
        table_rows.append([label] + [fmt_metric(r.get(k), unit, currency) for r in rows])

    head = (
        f"### {d.get('quote_name') or sym} ({sym}) {title}\n"
        f"报告类型：{REPORT_TYPES[report_type]}　计价货币：{d.get('currency_name') or currency or '-'}"
        f"　最新报告期：{d.get('last_report_name') or '-'}\n"
    )
    if d.get("tip"):
        head += f"提示：{d['tip']}\n"
    tail = "" if detail else f"\n\n（当前为精简视图，共 {len(mapping)} 个科目，传 detail=true 查看全部）"
    return head + "\n" + md_table(["指标"] + periods, table_rows) + tail


def _has_value(cell) -> bool:
    if cell is None:
        return False
    if isinstance(cell, (list, tuple)):
        return bool(cell) and cell[0] is not None
    return True


# 雪球用 class_standard 区分主营构成的拆分口径
_BIZ_CLASS = {1: "按行业", 2: "按产品", 3: "按地区", 4: "按业务", 5: "按销售模式"}


async def get_business_breakdown(symbol: str, count: int = 2, format: str = "markdown") -> str | dict:
    """主营构成：按产品 / 地区 / 销售模式拆分的收入占比与毛利率。"""
    sym = await resolve_symbol(symbol)
    data = await get_client().get(
        "/v5/stock/finance/cn/business.json",
        {"symbol": sym, "type": "all", "count": max(1, min(int(count), 8))},
    )
    d = data.get("data") or {}
    periods = d.get("list") or []
    if format == "json":
        items = [{"report_date": iso_time(p.get("report_date")), "report_name": p.get("report_name"),
                  "groups": [{"class_standard": g.get("class_standard"),
                              "items": [{k: it.get(k) for k in ("project_announced_name", "prime_operating_income",
                                         "income_ratio", "gross_profit_rate")} for it in g.get("business_list") or []]}
                             for g in p.get("class_list") or []]} for p in periods]
        return records(items, count, 8, symbol=sym, units={"prime_operating_income": d.get("currency"),
                       "income_ratio": "fraction", "gross_profit_rate": "fraction"})
    if not periods:
        return f"{sym} 无主营构成数据（该接口仅覆盖 A 股）。"

    currency = d.get("currency")
    ind = d.get("industry") or {}
    out = [
        f"### {d.get('quote_name') or sym} 主营构成",
        f"主营业务：{d.get('main_operation_business') or '-'}",
        f"产品类型：{d.get('product_type') or '-'}　所属行业：{ind.get('ind_name') or '-'}",
    ]

    for period in periods:
        pname = period.get("report_name") or fmt_ts(period.get("report_date"))
        out.append(f"\n**{pname}**")
        for group in period.get("class_list") or []:
            items = group.get("business_list") or []
            if not items:
                continue
            gname = _BIZ_CLASS.get(group.get("class_standard"), "其他口径")
            rows = [
                [
                    it.get("project_announced_name") or "-",
                    fmt_money(it.get("prime_operating_income"), _suffix(currency)),
                    fmt_rate(it.get("income_ratio"), signed=False),
                    fmt_rate(it.get("gross_profit_rate"), signed=False),
                ]
                for it in items
            ]
            out.append(f"\n*{gname}*\n")
            out.append(md_table(["项目", "营业收入", "收入占比", "毛利率"], rows))
    return "\n".join(out)


def _suffix(currency):
    from ..formatting import currency_suffix

    return currency_suffix(currency)
