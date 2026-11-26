"""Trading dates and finality for bars and daily capital flows."""
from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from .client import XueqiuError
from .results import iso_time
from .symbols import market_of


def market_zone(symbol):
    return ZoneInfo({"CN": "Asia/Shanghai", "HK": "Asia/Hong_Kong", "US": "America/New_York"}[market_of(symbol)])


def period_end(day: date, period: str) -> date:
    if period == "week":
        return day + timedelta(days=4 - day.weekday())
    if period in ("month", "quarter", "year"):
        month = day.month if period == "month" else (3 * ((day.month - 1) // 3 + 1) if period == "quarter" else 12)
        return date(day.year, month, monthrange(day.year, month)[1])
    return day


def observation(symbol, quote_data):
    zone = market_zone(symbol)
    observed = datetime.now(zone)
    rows = (quote_data.get("data") or {}).get("items") or []
    snapshot = next((r for r in rows if (r.get("quote") or {}).get("symbol") == symbol), {})
    quote = snapshot.get("quote") or {}
    return {"as_of": observed.isoformat(), "quote_as_of": iso_time(quote.get("timestamp"), zone),
            "market_status": (snapshot.get("market") or {}).get("status"),
            "currency": quote.get("currency")}, observed, zone


async def read_observation(client, symbol):
    try:
        data = await client.get("/v5/stock/batch/quote.json", {"symbol": symbol, "extend": "detail"})
    except (XueqiuError, httpx.RequestError):
        data = {}
    return observation(symbol, data)


def partial_bar(stamp, period, observed, snapshot):
    if not stamp:
        return None
    day = datetime.fromisoformat(stamp).date()
    today = observed.date()
    if period_end(day, period) < today:
        return False
    quote_time = snapshot["quote_as_of"]
    if not quote_time or datetime.fromisoformat(quote_time).date() != today:
        return None
    status = snapshot["market_status"] or ""
    if status in ("交易中", "午间休市", "午间休盘", "盘中休市"):
        if period.endswith("m"):
            return datetime.fromisoformat(stamp) > datetime.fromisoformat(quote_time)
        return True
    if status in ("已收盘", "盘后交易") and period_end(day, period) == today:
        return False
    # A calendar boundary alone cannot identify an early close or a market holiday.
    return None
