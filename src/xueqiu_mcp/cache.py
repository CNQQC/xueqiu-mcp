"""响应缓存：TTL + LRU + 内存上限，并对并发的相同请求做合并。

小服务器上最贵的资源是内存和上游配额，所以这里同时做三件事：

1. **分级 TTL**：行情几秒就过期，财报、行业分类这类慢变数据可以缓存几小时，
   免得每次都打雪球。
2. **内存上限**：按响应体字节数累计，超过上限就按 LRU 淘汰。只限条数是不够的
   —— 一条 500 根 K 线就有 79KB，几百条就能吃掉几百兆。
3. **请求合并（single-flight）**：N 个协程同时问同一支股票时，只放一个请求出去，
   其余等它的结果。这是高并发下对上游最有效的减压手段。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Awaitable

# 按 endpoint 前缀分级的 TTL（秒）。匹配时取最长前缀。
TTL_RULES: tuple[tuple[str, float], ...] = (
    # 盘中高频变动
    ("/v5/stock/batch/quote.json", 3.0),
    ("/v5/stock/chart/minute.json", 5.0),
    ("/v5/stock/chart/kline.json", 30.0),
    ("/v5/stock/realtime/pankou.json", 3.0),
    # 每日更新
    ("/v5/stock/capital/", 300.0),
    ("/v5/stock/f10/cn/blocktrans.json", 600.0),
    ("/v5/stock/hot_stock/", 120.0),
    # 季度 / 低频更新
    ("/v5/stock/finance/", 3600.0),
    ("/v5/stock/f10/", 21600.0),
    # 元数据，基本不变
    ("/v5/stock/screener/fields.json", 86400.0),
    ("/v5/stock/screener/industries.json", 86400.0),
    # 搜索与社区
    ("/query/v1/suggest_stock.json", 600.0),
    ("/statuses/", 60.0),
    ("/v5/stock/screener/quote/list.json", 60.0),
)
DEFAULT_TTL = 30.0


class _LeaderCancelled(Exception):
    """发起上游请求的那个协程被取消了。

    合并进来的其他协程自己并没有被取消，不该跟着一起死 —— 更要命的是，把
    ``CancelledError`` 传给它们会被 asyncio 当成「这些任务也被取消了」，
    杂散取消逃出各自的 cancel scope 后会让 MCP 的 TaskGroup 直接拆掉整个会话，
    客户端必须重连。所以这里换一个普通异常，收到的人重新发起请求即可。
    """


def ttl_for(path: str) -> float:
    """按最长前缀匹配取 TTL。"""
    best, best_len = DEFAULT_TTL, -1
    for prefix, ttl in TTL_RULES:
        if path.startswith(prefix) and len(prefix) > best_len:
            best, best_len = ttl, len(prefix)
    return best


class ResponseCache:
    """TTL + LRU + 字节上限的异步缓存，内置 single-flight。"""

    __slots__ = ("_data", "_bytes", "_max_bytes", "_inflight", "hits", "misses",
                 "merged", "oversized", "enabled")

    def __init__(self, max_bytes: int = 32 * 1024 * 1024, enabled: bool = True):
        # key -> (到期时间, 值, 体积)；dict 保序，用作 LRU
        self._data: dict[str, tuple[float, Any, int]] = {}
        self._bytes = 0
        self._max_bytes = max_bytes
        self._inflight: dict[str, asyncio.Future] = {}
        self.hits = self.misses = self.merged = self.oversized = 0
        self.enabled = enabled

    def _evict(self) -> None:
        while self._bytes > self._max_bytes and self._data:
            oldest = next(iter(self._data))
            self._bytes -= self._data.pop(oldest)[2]

    def get(self, key: str) -> tuple[bool, Any]:
        """返回 (是否命中, 值)。用元组而非 None 以便区分「缓存了 None」。"""
        if not self.enabled:
            return False, None
        item = self._data.get(key)
        if item is None:
            return False, None
        expire, value, size = item
        if time.monotonic() >= expire:
            del self._data[key]
            self._bytes -= size
            return False, None
        # 命中即刷新 LRU 位置
        self._data[key] = self._data.pop(key)
        self.hits += 1
        return True, value

    def put(self, key: str, value: Any, ttl: float, size: int) -> None:
        if not self.enabled or ttl <= 0:
            return
        # 记账要把条目自身的开销算进去（key 字符串 + dict 槽位 + 元组），实测约 273B/条。
        # 否则小条目场景下真实内存会显著超过 XUEQIU_CACHE_MB。
        size += len(key) * 2 + 200
        if size > self._max_bytes:
            # 单条就超上限：存进去也会被 _evict 立刻淘汰掉自己，白白挤走别人。
            # 直接不收，并记一笔，免得在 stats 里表现为「entries 恒为 0」这种查不出的怪事。
            self.oversized += 1
            return
        old = self._data.pop(key, None)
        if old is not None:
            self._bytes -= old[2]
        self._data[key] = (time.monotonic() + ttl, value, size)
        self._bytes += size
        self._evict()

    async def fetch(
        self,
        key: str,
        ttl: float,
        loader: Callable[[], Awaitable[tuple[Any, int]]],
        _depth: int = 0,
    ) -> Any:
        """取缓存；未命中则调用 loader，并合并同 key 的并发请求。

        loader 需返回 ``(值, 体积字节数)``。
        """
        hit, value = self.get(key)
        if hit:
            return value

        # 已有同 key 在途：等它的结果，不再打上游
        fut = self._inflight.get(key)
        if fut is not None:
            self.merged += 1
            try:
                return await asyncio.shield(fut)
            except _LeaderCancelled:
                # 发起请求的那个协程被取消了，但我们自己没有。重来一次 ——
                # 此时 inflight 已清空，这一轮多半由我们自己去打上游。
                if _depth >= 2:
                    raise RuntimeError(f"上游请求连续被取消，放弃：{key}") from None
                return await self.fetch(key, ttl, loader, _depth + 1)

        self.misses += 1
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._inflight[key] = fut
        try:
            value, size = await loader()
        except asyncio.CancelledError:
            # 只有自己该被取消，合并进来的人不该受牵连。详见 _LeaderCancelled。
            if not fut.done():
                fut.set_exception(_LeaderCancelled())
            fut.exception()
            raise
        except BaseException as exc:
            if not fut.done():
                fut.set_exception(exc)
            # 取回异常，避免 "Future exception was never retrieved" 噪声
            fut.exception()
            raise
        else:
            self.put(key, value, ttl, size)
            if not fut.done():
                fut.set_result(value)
            return value
        finally:
            self._inflight.pop(key, None)

    def clear(self) -> None:
        self._data.clear()
        self._bytes = 0

    def stats(self) -> dict[str, Any]:
        total = self.hits + self.misses
        return {
            "entries": len(self._data),
            "bytes": self._bytes,
            "hits": self.hits,
            "misses": self.misses,
            "merged": self.merged,
            "oversized": self.oversized,
            "hit_rate": round(self.hits / total, 4) if total else 0.0,
            "inflight": len(self._inflight),
        }
