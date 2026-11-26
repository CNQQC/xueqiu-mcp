"""股票代码规范化。

雪球的 symbol 规则：
- A 股 / 沪深基金 / 可转债：交易所前缀 + 6 位数字，如 SH600519、SZ000001、BJ430047
- 港股：5 位数字，如 00700
- 美股：字母代码，如 AAPL、BRK.A
"""

from __future__ import annotations

import re

# 沪市：主板 60、科创板 688/689、B 股 900、基金 50/51/52/56/58、可转债 110/111/113/118、国债逆回购 204
_SH_PREFIXES = ("60", "68", "69", "90", "50", "51", "52", "56", "58", "11", "20", "00")
# 深市：主板 000/001/002/003、创业板 300/301、B 股 200、基金 15/16/18、可转债 12、逆回购 131
_SZ_PREFIXES = ("00", "30", "20", "15", "16", "18", "12", "13", "39")
# 北交所：43、83、87、88、92
_BJ_PREFIXES = ("43", "83", "87", "88", "92")

_DIGITS_6 = re.compile(r"^\d{6}$")
_PREFIXED = re.compile(r"^(SH|SZ|BJ)(\d{6})$", re.IGNORECASE)
_HK = re.compile(r"^(?:HK)?(\d{1,5})$", re.IGNORECASE)
_US = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
BOARD_CODE = re.compile(r"BK[0-9]{4,}", re.I)
FUND_CODE = re.compile(r"F[0-9]{6}", re.I)


def _exchange_for_6digit(code: str) -> str:
    """按 6 位数字代码推断交易所前缀。"""
    p2, p3 = code[:2], code[:3]
    if p3 in ("204", "110", "111", "113", "118", "600", "601", "603", "605", "688", "689"):
        return "SH"
    if p3 in ("000", "001", "002", "003", "300", "301", "123", "127", "128", "131"):
        return "SZ"
    if p2 in _BJ_PREFIXES:
        return "BJ"
    if p2 in ("60", "68", "69", "90", "50", "51", "52", "56", "58", "11"):
        return "SH"
    if p2 in ("00", "30", "20", "15", "16", "18", "12", "39"):
        return "SZ"
    # 兜底：6/9 开头归沪市，其余归深市
    return "SH" if code[0] in "69" else "SZ"


def normalize_symbol(raw: str) -> str:
    """把用户输入的代码转成雪球 symbol。

    无法判定的输入（例如中文名称）会原样返回，由调用方走搜索兜底。
    """
    s = (raw or "").strip().replace(" ", "")
    if not s:
        return s

    m = _PREFIXED.match(s)
    if m:
        return m.group(1).upper() + m.group(2)

    # 600519.SH / 000001.SZ 这类后缀写法
    if "." in s:
        head, _, tail = s.rpartition(".")
        if tail.upper() in ("SH", "SS", "SZ", "BJ") and _DIGITS_6.match(head):
            ex = "SH" if tail.upper() in ("SH", "SS") else tail.upper()
            return ex + head
        if tail.upper() == "HK" and head.isdigit():
            return head.zfill(5)

    if _DIGITS_6.match(s):
        return _exchange_for_6digit(s) + s

    # 港股：1~5 位数字补零到 5 位（09988、00700、01810）
    m = _HK.match(s)
    if m and len(m.group(1)) <= 5:
        return m.group(1).zfill(5)

    if _US.match(s.upper()):
        return s.upper()

    return s


def is_resolvable(symbol: str) -> bool:
    """判断规范化结果看起来是否已经是一个合法 symbol（否则需要走搜索）。"""
    return bool(
        _PREFIXED.match(symbol)
        or re.match(r"^\d{5}$", symbol)
        or _US.match(symbol.upper())
    )


def parse_symbols(raw: str | list[str]) -> list[str]:
    """把逗号 / 空格 / 顿号分隔的多标的输入拆成 symbol 列表。"""
    if isinstance(raw, list):
        parts = raw
    else:
        parts = re.split(r"[,，、;；\s]+", raw or "")
    return [normalize_symbol(p) for p in parts if p and p.strip()]


def market_of(symbol: str) -> str | None:
    """BK 板块覆盖多个市场，归属需要行情快照确认。"""
    if BOARD_CODE.fullmatch(symbol):
        return None
    if _PREFIXED.match(symbol) or FUND_CODE.fullmatch(symbol):
        return "CN"
    if re.match(r"^\d{5}$", symbol):
        return "HK"
    return "US"
