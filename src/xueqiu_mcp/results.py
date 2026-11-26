"""MCP Markdown / JSON results and common paging metadata."""
from __future__ import annotations

import json
from datetime import datetime, tzinfo
from typing import Literal

from mcp.types import CallToolResult, TextContent

from .formatting import CN_TZ

OutputFormat = Literal["markdown", "json"]


def tool_result(value: str | dict, format: OutputFormat, *, error: bool = False) -> CallToolResult:
    failed = error or (format == "json" and isinstance(value, str))
    if format == "json":
        payload = {"error": value} if failed else value
        text = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    else:
        text = str(value)
        payload = {"result": text}
    return CallToolResult(content=[TextContent(type="text", text=text)], structuredContent=payload, isError=failed)


def iso_time(ms, zone: tzinfo = CN_TZ) -> str | None:
    if not ms or isinstance(ms, bool):
        return None
    try:
        return datetime.fromtimestamp(float(ms) / 1000, zone).isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def pagination(requested: int | None, returned: int, maximum: int | None, has_more: bool | None,
               next_page: int | str | None, page: int | str, *, total: int | None = None) -> dict:
    return {"requested": requested, "returned": returned, "max": maximum, "page": page,
            "has_more": has_more, "next_page": next_page, "total": total}


def records(items, requested=None, maximum=None, *, page=1, total=None, pageable=False, **fields):
    more = (page * min(max(1, requested), maximum) < total
            if pageable and isinstance(total, int) and requested and maximum else None)
    return {**fields, "items": items, "pagination": pagination(requested, len(items), maximum, more,
            page + 1 if pageable and more is not False else None, page, total=total)}


def select_fields(row, keys, dates=()):
    return {key: iso_time(row[key]) if key in dates else row[key] for key in keys if key in row}


def field_unit(kind, currency=None):
    return {"money": currency, "yuan": currency, "share": "shares", "pct": "percent",
            "rate": "fraction", "date": "ISO8601", "times": "multiple", "days": "days",
            "cent_per_share": "cent_per_share"}.get(kind)


def page_text(meta: dict) -> str:
    def display(value):
        return json.dumps(value, ensure_ascii=False)
    return "　".join(f"{k}={display(meta[k])}" for k in ("requested", "returned", "max", "has_more", "next_page"))
