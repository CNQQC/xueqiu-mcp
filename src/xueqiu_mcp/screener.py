"""选股器指标元数据。

指标清单直接取自雪球官方的 ``screener/fields.json``（带中文名），进程内缓存，
因此不会随雪球调整指标而过时。

指标名带 ``_l`` 后缀表示"最新报告期"（如 ``roediluted_l`` 净资产收益率）；
财务报表类指标需要指定报告期，写成 ``指标.YYYYMMDD``（如 ``np.20251231``）。
"""

from __future__ import annotations

import asyncio

from .client import get_client

_CACHE: dict[str, dict[str, dict]] = {}
_LOCK = asyncio.Lock()

CATEGORIES = ("CN", "HK", "US")


async def load_metrics(category: str = "CN") -> dict[str, dict]:
    """返回 ``{指标名: {"name": 中文名, "group": 分类, "has_date": 0/1}}``。"""
    category = (category or "CN").upper()
    if category in _CACHE:
        return _CACHE[category]
    async with _LOCK:
        if category in _CACHE:
            return _CACHE[category]
        data = await get_client().get(
            "/v5/stock/screener/fields.json",
            {"category": category},
            referer="https://xueqiu.com/hq",
        )
        out: dict[str, dict] = {}
        for group in (data.get("data") or {}).get("items") or []:
            gname = group.get("field_category") or ""
            _collect(group.get("fields"), gname, out)
            for sub in group.get("category2_list") or []:
                _collect(
                    sub.get("fields"),
                    f"{gname}/{sub.get('field_category2', '')}",
                    out,
                )
        _CACHE[category] = out
        return out


def _collect(fields: dict | None, group: str, out: dict) -> None:
    for key, meta in (fields or {}).items():
        out[key] = {
            "name": (meta or {}).get("name") or key,
            "group": group,
            "has_date": (meta or {}).get("has_date", 0),
        }


def base_metric(key: str) -> str:
    """把 ``np.20251231`` / ``roediluted_l`` 还原成元数据表里的键。"""
    k = key.split(".")[0]
    return k


async def metric_label(category: str, key: str) -> str:
    """指标名 → 中文名，未知时原样返回。"""
    metrics = await load_metrics(category)
    k = base_metric(key)
    meta = metrics.get(k) or metrics.get(k.removesuffix("_l"))
    label = meta["name"] if meta else key
    if "." in key:
        label += f"({key.split('.', 1)[1]})"
    return label


async def validate(category: str, key: str) -> str | None:
    """校验指标名，返回错误提示或 None。"""
    metrics = await load_metrics(category)
    k = base_metric(key)
    if k in metrics or k.removesuffix("_l") in metrics:
        return None
    return (
        f"指标「{key}」在 {category} 市场不可用。"
        "可先调用 list_screener_metrics 查看该市场支持的全部指标。"
    )
