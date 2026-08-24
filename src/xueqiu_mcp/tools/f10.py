"""公司资料类工具：公司简介、行业概念、股东结构、分红送配。"""

from __future__ import annotations

import asyncio

from ..client import gather_json, get_client
from ..formatting import fmt_money, fmt_num, fmt_pct, fmt_share, fmt_ts, md_table
from ..resolve import resolve_symbol

_COMPANY_LAYOUT = [
    ("org_name_cn", "公司全称"), ("org_short_name_cn", "简称"), ("org_name_en", "英文名"),
    ("classi_name", "企业性质"), ("actual_controller", "实际控制人"),
    ("legal_representative", "法定代表人"), ("general_manager", "总经理"), ("secretary", "董秘"),
    ("established_date", "成立日期"), ("listed_date", "上市日期"),
    ("reg_asset", "注册资本"), ("staff_num", "员工人数"),
    ("provincial_name", "所属省份"), ("office_address_cn", "办公地址"),
    ("org_website", "公司网站"), ("telephone", "联系电话"), ("email", "邮箱"),
]


async def get_company_profile(symbol: str) -> str:
    """公司基本资料 + 所属行业与概念板块。"""
    sym = await resolve_symbol(symbol)
    client = get_client()

    # 公司资料与行业概念互不依赖，一起发
    data, ind_raw = await asyncio.gather(
        client.get("/v5/stock/f10/cn/company.json", {"symbol": sym}),
        client.get("/v5/stock/f10/cn/industry.json", {"symbol": sym}),
        return_exceptions=True,
    )
    if isinstance(data, BaseException):
        raise data
    c = (data.get("data") or {}).get("company") or {}
    if not c:
        return f"{sym} 无公司资料（该接口仅覆盖 A 股）。"

    lines = [f"### {c.get('org_short_name_cn') or sym}（{sym}）公司资料\n"]
    for key, label in _COMPANY_LAYOUT:
        v = c.get(key)
        if v in (None, "", 0):
            continue
        if key in ("established_date", "listed_date"):
            v = fmt_ts(v)
        elif key == "reg_asset":
            v = fmt_money(v)
        elif key == "staff_num":
            v = f"{fmt_num(v)}人"
        lines.append(f"- **{label}**：{v}")

    if c.get("main_operation_business"):
        lines.append(f"\n**主营业务**：{c['main_operation_business']}")
    if c.get("org_cn_introduction"):
        intro = str(c["org_cn_introduction"]).strip()
        lines.append(f"\n**公司简介**：{intro[:600]}{'…' if len(intro) > 600 else ''}")
    if c.get("operating_scope"):
        scope = str(c["operating_scope"]).strip()
        lines.append(f"\n**经营范围**：{scope[:400]}{'…' if len(scope) > 400 else ''}")

    ind = {} if isinstance(ind_raw, BaseException) else (ind_raw.get("data") or {})
    inds = ind.get("industry") or []
    cons = ind.get("concept") or []
    if inds:
        lines.append("\n**所属行业**：" + "、".join(i.get("ind_name", "") for i in inds))
    if cons:
        lines.append("**概念板块**：" + "、".join(i.get("ind_name", "") for i in cons[:25]))
    return "\n".join(lines)


async def get_shareholders(symbol: str, top_n: int = 10) -> str:
    """股东结构：股东户数走势、十大流通股东、机构持仓概览。"""
    sym = await resolve_symbol(symbol)
    client = get_client()
    out: list[str] = [f"### {sym} 股东结构\n"]

    # 三个接口互不依赖，并发取，省掉两个 RTT
    h_raw, t_raw, o_raw = await gather_json(
        client.get("/v5/stock/f10/cn/holders.json", {"symbol": sym}),
        client.get("/v5/stock/f10/cn/top_holders.json", {"symbol": sym, "circula": 1}),
        client.get("/v5/stock/f10/cn/org_holding/detail.json", {"symbol": sym, "page": 1, "size": 10}),
    )

    # 股东户数
    items = (h_raw.get("data") or {}).get("items") or []
    if items:
        rows = [
            [
                fmt_ts(it.get("timestamp")),
                fmt_num(it.get("ashare_holder")),
                fmt_pct(it.get("chg"), signed=True),
                fmt_share(it.get("per_float")),
                fmt_money(it.get("per_amount")),
                fmt_pct(it.get("top_holder_ratio")),
            ]
            for it in items[:8]
        ]
        out.append("**股东户数**\n")
        out.append(
            md_table(
                ["报告期", "股东户数", "较上期", "户均持股", "户均市值", "十大股东持股比"], rows
            )
        )

    # 十大流通股东
    t = t_raw.get("data") or {}
    holders = (t.get("items") or [])[: max(1, min(int(top_n), 20))]
    if holders:
        period = (t.get("times") or [{}])[0].get("name", "")
        rows = [
            [
                str(i),
                it.get("holder_name") or "-",
                fmt_share(it.get("held_num")),
                fmt_pct(it.get("held_ratio")),
                fmt_pct(it.get("chg"), signed=True) if it.get("chg") else "不变",
            ]
            for i, it in enumerate(holders, 1)
        ]
        out.append(f"\n**十大流通股东（{period}）**\n")
        out.append(md_table(["#", "股东名称", "持股数", "占流通股比", "较上期变动"], rows))
        total = t.get("total") or {}
        if total:
            out.append(
                f"\n合计持股比例 {fmt_pct(total.get('held_ratio'))}"
                f"　较上期 {fmt_pct(total.get('chg'), signed=True) if total.get('chg') else '不变'}"
            )

    # 机构持仓
    o = o_raw.get("data") or {}
    groups = [
        ("fund_items", "基金"), ("qfii_items", "QFII"), ("social_items", "社保"),
        ("insurance_items", "保险"), ("broker_items", "券商"), ("other_items", "其他机构"),
    ]
    # 每组第一行是雪球给出的「全部合计」，家数则在 types 里按机构类型给出
    type_cnt = {t.get("org_name"): t.get("org_cnt") for t in (o.get("types") or [])}
    rows = []
    for key, label in groups:
        arr = o.get(key) or []
        summary = next((x for x in arr if x.get("org_name_or_fund_name") == "全部合计"), None)
        if summary is None:
            continue
        cnt = type_cnt.get(label)
        rows.append(
            [
                label,
                f"{fmt_num(cnt)}家" if cnt else f"{max(len(arr) - 1, 0)}家(明细)",
                fmt_share(summary.get("held_num")),
                fmt_pct(summary.get("to_float_shares_ratio")),
            ]
        )
    if rows:
        out.append(f"\n**机构持仓（{o.get('chg_date', '')}）**\n")
        out.append(md_table(["机构类型", "家数", "合计持股", "占流通股比"], rows))

    return "\n".join(out) if len(out) > 1 else f"{sym} 暂无股东数据（该接口仅覆盖 A 股）。"


async def get_dividends(symbol: str, count: int = 10) -> str:
    """历年分红送配方案。"""
    sym = await resolve_symbol(symbol)
    data = await get_client().get(
        "/v5/stock/f10/cn/bonus.json",
        {"symbol": sym, "page": 1, "size": max(1, min(int(count), 50)), "extend": "true"},
    )
    d = data.get("data") or {}
    items = d.get("items") or []
    if not items:
        return f"{sym} 无分红记录。"

    rows = [
        [
            it.get("dividend_year") or "-",
            it.get("plan_explain") or "-",
            fmt_ts(it.get("equity_date")),
            fmt_ts(it.get("ashare_ex_dividend_date") or it.get("ex_dividend_date")),
            fmt_ts(it.get("dividend_date")),
        ]
        for it in items
    ]
    out = [f"### {sym} 分红送配（近 {len(rows)} 次）\n",
           md_table(["方案年度", "分配方案", "股权登记日", "除权除息日", "派息日"], rows)]

    for key, label in (("allots", "配股"), ("addtions", "增发")):
        arr = d.get(key) or []
        if arr:
            out.append(f"\n**{label}记录**：共 {len(arr)} 次，最近一次 "
                       f"{fmt_ts((arr[0] or {}).get('listing_date') or (arr[0] or {}).get('ex_dividend_date'))}")
    return "\n".join(out)
