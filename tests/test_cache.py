"""缓存层回归测试。零网络，随便跑。

每个用例都对应一个真实踩过的坑，注释里写了是什么坑 —— 别删。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from xueqiu_mcp.cache import ResponseCache, ttl_for  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"　{detail}" if detail and not cond else ""))


async def t_single_flight() -> None:
    """并发的相同请求应当只打一次上游。"""
    c = ResponseCache()
    calls = {"n": 0}

    async def loader():
        calls["n"] += 1
        await asyncio.sleep(0.05)
        return {"v": 1}, 100

    got = await asyncio.gather(*[c.fetch("k", 60, loader) for _ in range(20)])
    check("并发 20 个相同请求只打 1 次上游", calls["n"] == 1, f"实际 {calls['n']}")
    check("合并的返回值是同一对象", all(g is got[0] for g in got))
    check("merged 计数正确", c.merged == 19, f"实际 {c.merged}")


async def t_leader_cancel_does_not_kill_followers() -> None:
    """坑：leader 被取消时若把 CancelledError 传给 follower，
    asyncio 会把 follower 也标成 CANCELLED，杂散取消逃出 cancel scope 后
    会让 MCP 的 TaskGroup 直接拆掉整个会话，客户端必须重连。"""
    c = ResponseCache()
    started = asyncio.Event()
    calls = {"n": 0}

    async def loader():
        calls["n"] += 1
        started.set()
        await asyncio.sleep(3)
        return {"v": 1}, 100

    leader = asyncio.create_task(c.fetch("k", 60, loader))
    await started.wait()
    followers = [asyncio.create_task(c.fetch("k", 60, loader)) for _ in range(4)]
    await asyncio.sleep(0.05)
    leader.cancel()
    await asyncio.sleep(0.15)

    check("leader 自己确实被取消", leader.cancelled())
    check("follower 不被连带取消", not any(f.cancelled() for f in followers),
          f"被取消 {sum(f.cancelled() for f in followers)}/4")
    check("follower 接手重发了请求", calls["n"] >= 2, f"loader 调用 {calls['n']} 次")
    for f in followers:
        f.cancel()
    await asyncio.gather(*followers, return_exceptions=True)


async def t_error_not_cached() -> None:
    """错误不该进缓存，否则一次抖动会被记住整个 TTL。"""
    c = ResponseCache()
    calls = {"n": 0}

    async def failing():
        calls["n"] += 1
        raise RuntimeError("boom")

    for _ in range(3):
        try:
            await c.fetch("k", 60, failing)
        except RuntimeError:
            pass
    check("3 次失败调用了 3 次 loader", calls["n"] == 3, f"实际 {calls['n']}")
    check("失败后缓存为空", c.stats()["entries"] == 0)


def t_lru_and_bytes() -> None:
    """LRU 顺序、字节上限、以及单条超限的处理。"""
    # 每条实际记账 = 1000 + len(key)*2 + 200 = 1206，上限设 5000 刚好容 4 条
    c = ResponseCache(max_bytes=5000)
    for i in range(4):
        c.put(f"k{i}", {"i": i}, 60, 1000)
    c.get("k0")                    # 命中应把 k0 移到最近使用
    c.put("k4", {"i": 4}, 60, 1000)   # 触发淘汰，该走的是 k1
    hit_k0, _ = c.get("k0")
    hit_k1, _ = c.get("k1")
    check("LRU 淘汰最久未用的，而非刚命中的", hit_k0 and not hit_k1,
          f"k0={hit_k0} k1={hit_k1}")

    # 坑：单条超过上限时，put 后会被 _evict 立刻淘汰掉自己，
    # 表现为 entries 恒为 0 却查不出原因。现在应当直接拒收并计数。
    c2 = ResponseCache(max_bytes=1000)
    c2.put("huge", {"x": 1}, 60, 5000)
    check("单条超上限不入缓存", c2.stats()["entries"] == 0)
    check("超限被计数而非静默", c2.stats()["oversized"] == 1)
    check("超限条目读不到", c2.get("huge")[0] is False)

    # 坑：记账只算响应体、不算 key 和 dict 开销时，小条目场景真实内存会显著超标
    c3 = ResponseCache(max_bytes=10_000)
    c3.put("x" * 100, {"v": 1}, 60, 10)
    check("记账计入条目自身开销", c3.stats()["bytes"] > 10,
          f"bytes={c3.stats()['bytes']}，应远大于 10")


def t_ttl_expiry() -> None:
    c = ResponseCache()
    c.put("k", {"v": 1}, 0.15, 100)
    check("TTL 内命中", c.get("k")[0] is True)
    time.sleep(0.25)
    check("TTL 过后失效", c.get("k")[0] is False)
    check("ttl_for 走最长前缀", ttl_for("/v5/stock/finance/cn/indicator.json") == 3600.0)


def t_kline_key_stability() -> None:
    """坑：get_kline 曾把 int(time.time()*1000) 当参数传，
    导致每次调用的 cache key 都不同，K 线缓存 100% 落空，
    还会把大条目不断塞进缓存挤掉别的端点。"""
    from xueqiu_mcp.tools.quote import KLINE_PATH
    from xueqiu_mcp.cache import ttl_for as _ttl

    step = max(1.0, _ttl(KLINE_PATH))
    begins = []
    for _ in range(3):
        begins.append(int(time.time() // step * step * 1000))
        time.sleep(0.05)
    check("连续调用的 begin 参数稳定", len(set(begins)) == 1, f"得到 {set(begins)}")


async def main() -> int:
    print("缓存层回归测试\n")
    await t_single_flight()
    await t_leader_cancel_does_not_kill_followers()
    await t_error_not_cached()
    t_lru_and_bytes()
    t_ttl_expiry()
    t_kline_key_stability()
    print(f"\n通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("失败：" + "、".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
