"""客户端性能基准：QPS、延迟分位、上游请求数、内存占用。

全程打本地 mock 上游，不碰雪球，所以可以随便跑、随时复现。用法：

    .venv/bin/python tests/bench.py            # 默认模拟 30ms 网络延迟
    MOCK_RTT=0 .venv/bin/python tests/bench.py # 零延迟，放大纯代码开销

这是**回归检测**工具，不是性能标榜工具。QPS 数字受 mock server 自身的调度开销影响很大
（本地回环上并发升高时 QPS 下降是测试环境的产物，不代表被测代码有问题），
真实表现请看 README 的实测数据。

真正该盯的是这两个确定性计数：

- **上游次数**：开缓存后应当远小于请求次数。这是小机器扛住高并发的关键，
  也是最容易被改坏的地方 —— 只要有参数每次调用都变（比如把当前时间戳传进去），
  cache key 就会次次不同，缓存静默失效而 QPS 看不出异常。
- **峰值并发**：上游同时在途的请求数，应当被闸门压在 ``XUEQIU_MAX_CONCURRENCY`` 以内。
"""

from __future__ import annotations

import asyncio
import json
import os
import resource
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# 保险丝：本基准只该连本地 mock，误连外网直接报错而不是偷偷去打雪球
_real_getaddrinfo = socket.getaddrinfo


def _guard(host, *a, **kw):
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise RuntimeError(f"基准试图连接外网 {host}，base 参数没传对")
    return _real_getaddrinfo(host, *a, **kw)


socket.getaddrinfo = _guard

RTT = float(os.getenv("MOCK_RTT", "0.03"))
QUOTE_PATH = "/v5/stock/batch/quote.json"
KLINE_PATH = "/v5/stock/chart/kline.json"


# ---------------------------------------------------------------- 合成响应
def _quote_body() -> bytes:
    """形状对齐真实 batch/quote.json，约 4.8KB。"""
    q = {f: 1234.5678 for f in (
        "current", "percent", "chg", "open", "high", "low", "last_close", "volume",
        "amount", "turnover_rate", "market_capital", "float_market_capital",
        "pe_ttm", "pe_lyr", "pe_forecast", "pb", "ps", "dividend_yield", "eps",
        "navps", "total_shares", "float_shares", "amplitude", "volume_ratio",
        "limit_up", "limit_down", "high52w", "low52w", "avg_price", "goodwill_in_net_assets",
        "holders_num", "pledge_ratio", "lot_size", "current_year_percent", "traded_amount_ext",
    )}
    q.update({"symbol": "SH600519", "name": "贵州茅台", "currency": "CNY",
              "timestamp": 1750000000000, "exchange": "SH", "status": 1,
              "type": 11, "sub_type": "ASHARE", "issue_date": 1000000000000})
    item = {"market": {"status_id": 5, "region": "CN", "status": "已收盘",
                       "time_zone": "Asia/Shanghai", "delay_tag": 0}, "quote": q,
            "others": {"pankou_ratio": 0.5, "cyb_switch": True},
            "tags": [{"description": "融资融券", "value": 1}]}
    return json.dumps({"data": {"items": [item]}, "error_code": 0,
                       "error_description": ""}, ensure_ascii=False).encode()


def _kline_body() -> bytes:
    """500 根带估值的日 K，约 79KB。"""
    cols = ["timestamp", "volume", "open", "high", "low", "close", "chg", "percent",
            "turnoverrate", "amount", "volume_post", "amount_post", "pe", "pb", "ps",
            "pcf", "market_capital", "balance", "hold_volume_cn", "hold_ratio_cn",
            "net_volume_cn", "dividend", "dividend_yield", "split"]
    base_ts = 1700000000000
    items = [[base_ts + i * 86400000] + [round(100.0 + i * 0.37, 4)] * (len(cols) - 1)
             for i in range(500)]
    return json.dumps({"data": {"symbol": "SH600519", "column": cols, "item": items},
                       "error_code": 0, "error_description": ""}).encode()


BODIES = {QUOTE_PATH: _quote_body(), KLINE_PATH: _kline_body()}
DEFAULT_BODY = json.dumps({"data": {"items": []}, "error_code": 0}).encode()
TOKEN_PAGE = b"<!DOCTYPE html><html>token</html>"


# ---------------------------------------------------------------- mock 上游
class Stats:
    def __init__(self) -> None:
        self.count = 0
        self.live = 0
        self.peak = 0

    def reset(self) -> None:
        self.count = self.peak = 0


STATS = Stats()


class MockUpstream(asyncio.Protocol):
    def connection_made(self, transport):
        self.transport = transport
        self.buf = b""

    def data_received(self, data: bytes) -> None:
        self.buf += data
        while b"\r\n\r\n" in self.buf:
            head, _, self.buf = self.buf.partition(b"\r\n\r\n")
            line = head.split(b"\r\n", 1)[0].decode("latin1")
            parts = line.split(" ")
            if len(parts) >= 2:
                asyncio.create_task(self._respond(parts[1].split("?", 1)[0]))

    async def _respond(self, path: str) -> None:
        STATS.count += 1
        STATS.live += 1
        STATS.peak = max(STATS.peak, STATS.live)
        try:
            if RTT:
                await asyncio.sleep(RTT)
            if path in ("/", "/hq"):
                body, ctype = TOKEN_PAGE, b"text/html"
                extra = (b"Set-Cookie: xq_a_token=mock; Path=/\r\n"
                         b"Set-Cookie: acw_tc=mock; Path=/\r\n")
            else:
                body, ctype, extra = BODIES.get(path, DEFAULT_BODY), b"application/json", b""
            if not self.transport.is_closing():
                self.transport.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: " + ctype +
                    b"\r\nContent-Length: " + str(len(body)).encode() +
                    b"\r\n" + extra + b"Connection: keep-alive\r\n\r\n" + body)
        finally:
            STATS.live -= 1


# ---------------------------------------------------------------- 压测
def _rss_mb() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r / 1e6 if sys.platform == "darwin" else r / 1e3


def _pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))]


async def _case(client, base, path, params, concurrency, total, label, distinct=1, cache=False):
    STATS.reset()
    lat: list[float] = []
    state = {"i": 0, "err": 0}

    async def worker():
        while state["i"] < total:
            i = state["i"]
            state["i"] = i + 1
            p = dict(params)
            if distinct > 1:
                p["symbol"] = f"SH{600000 + i % distinct}"
            t0 = time.perf_counter()
            try:
                await client.get(path, p, base=base, cache=cache)
            except Exception:
                state["err"] += 1
            lat.append(time.perf_counter() - t0)

    t0 = time.perf_counter()
    await asyncio.gather(*[worker() for _ in range(concurrency)])
    dur = time.perf_counter() - t0
    print(f"  {label:22} 并发{concurrency:>4} {total/dur if dur else 0:>9.1f} QPS  "
          f"P50 {_pct(lat,.5)*1000:>7.1f}  P95 {_pct(lat,.95)*1000:>7.1f}  "
          f"P99 {_pct(lat,.99)*1000:>7.1f} ms  上游 {STATS.count:>5}  峰值并发 {STATS.peak:>4}"
          + (f"  ❌{state['err']}" if state["err"] else ""))


async def main() -> int:
    loop = asyncio.get_running_loop()
    server = await loop.create_server(MockUpstream, "127.0.0.1", 0)
    base = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"

    import xueqiu_mcp.client as C

    C.MAIN_API = base  # _refresh_token 在函数体内取模块全局，替换有效

    print("=" * 104)
    print(f"mock 上游 RTT={RTT*1000:.0f}ms   Python {sys.version.split()[0]}   "
          f"httpx {__import__('httpx').__version__}   HTTP/2 可用={C.HTTP2_AVAILABLE}")
    print("=" * 104)

    client = C.XueqiuClient()
    qp = {"symbol": "SH600519", "extend": "detail"}
    kp = {"symbol": "SH600519", "period": "day", "count": -500}

    STATS.reset()
    t0 = time.perf_counter()
    await client.get(QUOTE_PATH, qp, base=base)
    print(f"\n冷启动（含 token 获取）: {(time.perf_counter()-t0)*1000:.0f} ms  上游 {STATS.count} 次\n")

    print("【关缓存 —— 纯管道能力】")
    for c in (1, 8, 32, 128, 512):
        await _case(client, base, QUOTE_PATH, qp, c, max(200, c * 4), "quote 4.8KB")
    for c in (8, 128):
        await _case(client, base, KLINE_PATH, kp, c, max(100, c * 2), "kline500 79KB")
    await _case(client, base, QUOTE_PATH, qp, 128, 800, "quote×200标的", distinct=200)

    print("\n【开缓存 —— 生产实际表现】")
    client.cache.clear()
    await _case(client, base, QUOTE_PATH, qp, 128, 2000, "同标的×2000", cache=True)
    client.cache.clear()
    await _case(client, base, QUOTE_PATH, qp, 128, 2000, "200标的×2000", distinct=200, cache=True)
    print(f"\n  缓存指标: {client.stats()}")
    print(f"  进程 RSS 峰值: {_rss_mb():.1f} MB")

    await client.aclose()
    server.close()
    await server.wait_closed()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
