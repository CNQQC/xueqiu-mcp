"""雪球 HTTP 客户端。

雪球的开放接口需要一个匿名 token（cookie 中的 ``xq_a_token``）。该 token 通过
先访问首页、再访问行情页的方式自动获取，过期后自动重新获取一次。

``xueqiu.com`` 主域下的社区接口（讨论 / 公告 / 自选股）受阿里云 WAF 的 JS 挑战
保护，匿名访问会被拦截。这类接口需要一份登录凭证，来源见 :mod:`.auth`：命令行
``xueqiu-mcp login``、MCP 工具 ``login_with_cookie``，或环境变量 ``XUEQIU_COOKIE``。
凭证可以在运行中随时切换（:meth:`XueqiuClient.set_user_cookie`），不必重启进程。

并发与资源
----------
本客户端面向「小机器扛住高并发」设计，具体做法：

- **token 校验走无锁快速路径**。token 没过期时直接返回，不碰锁；只有真的需要
  刷新才进临界区，进去后再确认一次（double-check）。早期版本每个请求都要抢一次
  ``asyncio.Lock``，把并发请求全串行化了。
- **连接池显式设限并开启 HTTP/2**。``stock.xueqiu.com`` 支持 h2，多路复用后几条
  连接就能承载上百个并发请求，省下大量 TLS 握手和 socket 内存；不支持 h2 的域会
  由 ALPN 自动回落到 HTTP/1.1。
- **上游并发闸门**。在途请求数有上限，多出来的排队等待。这既是保护小机器，也是
  避免把雪球打出风控。
- **响应缓存 + 请求合并**。见 :mod:`.cache`。
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import os
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from .auth import LOGIN_GUIDE, format_cookie_string, parse_cookie_string, resolve_cookie
from .cache import ResponseCache, ttl_for

try:  # orjson 可选：大 JSON 解析快 2~3 倍，装了就用
    import orjson

    def _loads(raw: bytes) -> Any:
        return orjson.loads(raw)
except ImportError:  # pragma: no cover
    def _loads(raw: bytes) -> Any:
        return _json.loads(raw)

try:  # 没装 h2 时 httpx(http2=True) 会直接抛错，这里探测后自动回落 HTTP/1.1
    import h2  # noqa: F401

    HTTP2_AVAILABLE = True
except ImportError:  # pragma: no cover
    HTTP2_AVAILABLE = False

log = logging.getLogger("xueqiu_mcp")

STOCK_API = "https://stock.xueqiu.com"
MAIN_API = "https://xueqiu.com"
# 雪球 App 使用的接口域。社区类接口（讨论、公告、评论）在 xueqiu.com 主域上会被
# 阿里云 WAF 的 JS 挑战拦下，但在该域可以正常匿名访问。
APP_API = "https://api.xueqiu.com"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

BASE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://xueqiu.com/",
    "Origin": "https://xueqiu.com",
}

# token 有效期按 30 分钟保守估计，到期主动刷新
TOKEN_TTL = 25 * 60
# 强制刷新的最小间隔：并发请求同时撞上 token 失效时，只放一个出去刷新，
# 其余在这个窗口内直接复用刚拿到的新 token，避免惊群。
FORCE_REFRESH_COOLDOWN = 5.0

# 解析成 Python 对象后的内存大约是原始 JSON 的 4 倍（实测 500 根 K 线：77KB → 322KB，
# 4.2×）。缓存按这个倍率折算体积，好让 XUEQIU_CACHE_MB 直接对应真实内存占用，
# 而不是一个需要用户自己换算的原始字节数。
OBJECT_SIZE_FACTOR = 4


def _env_int(name: str, default: int) -> int:
    try:
        v = int(os.getenv(name, ""))
        return v if v > 0 else default
    except ValueError:
        return default


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


class XueqiuError(RuntimeError):
    """雪球接口返回的业务错误。"""


class PostNotFoundError(XueqiuError):
    """雪球帖子详情端点明确回复帖子不存在。"""

    def __init__(self, code, desc):
        self.upstream_code = str(code)
        super().__init__(desc or "您访问的页面不存在")


class RateLimitedError(XueqiuError):
    """被上游限流。

    单独分出来是因为限流响应体往往也是 HTML，会被误判成 WAF 挑战页，
    进而提示用户去配 Cookie —— 那是完全错误的处置方向：限流时该做的是降速，
    配上自己的 Cookie 反而把限流从匿名 IP 维度升级到自己的账号维度。
    """

    def __init__(self, endpoint: str):
        super().__init__(
            f"接口 {endpoint} 被雪球限流（HTTP 429）。请降低请求频率后重试。\n"
            "可以调小 XUEQIU_MAX_CONCURRENCY，或在调用侧加间隔。"
            "不要为此配置 XUEQIU_COOKIE —— 那会把限流升级到你的账号维度。"
        )


class NeedCookieError(XueqiuError):
    """接口被 WAF 拦截，需要用户提供自己的 Cookie。"""

    def __init__(self, endpoint: str):
        super().__init__(
            f"接口 {endpoint} 被雪球的风控拦截，匿名访问不可用。\n\n{LOGIN_GUIDE}"
        )


# 雪球用来表示「这次请求没有登录身份」的错误码。
# 10022 用户未登录；10020 会话失效（描述里写「重新登录」）；60201 用户 id 无效
# —— 自选股一类接口在匿名时就报这个。
NEED_LOGIN_CODES = {"10020", "10022", "60201"}
NEED_LOGIN_HINTS = ("未登录", "重新登录", "请登录", "登录后")


class NeedLoginError(XueqiuError):
    """接口本身通了，但雪球说这次请求没有登录身份（或登录态已失效）。

    和 :class:`NeedCookieError` 分开，是因为两者的现场完全不同：那个拿回来的是
    WAF 挑战页，这个拿回来的是一个正常的 JSON 错误。但对用户来说出路是同一条，
    所以两者都把完整的登录指引带上。
    """

    def __init__(self, endpoint: str, desc: str = "", code: Any = None):
        if endpoint == "/v4/statuses/user_timeline.json" and str(code) == "10022":
            # 该接口同一会话 count=20 可成功，count=25/30 却返回 10022。
            super().__init__(
                f"雪球用户动态接口返回 10022（{desc or '用户未登录'}）。"
                "该响应也可能与请求条数或页码有关，无法据此确认账号登录状态。"
            )
            return
        detail = f"：{desc}" if desc else ""
        super().__init__(f"接口 {endpoint} 需要登录{detail}\n\n{LOGIN_GUIDE}")


def raise_business_error(endpoint: str, code: Any, desc: str, status: int = 200) -> None:
    """把雪球的业务错误码翻成异常。永远抛，不返回。

    先认「没登录」再落到通用报错：这类失败有明确的出路（去登录），不该被混在一句
    干巴巴的「雪球接口报错」里 —— 那样模型只会把错误原样转述给用户，然后双方都
    停在原地。
    """
    if str(code) in NEED_LOGIN_CODES or any(h in desc for h in NEED_LOGIN_HINTS):
        raise NeedLoginError(endpoint, desc, code)
    if endpoint == "/statuses/show.json":
        missing = str(code) == "20210" or (
            str(code) == "404" and desc.strip("。 ") in ("您访问的页面不存在", "帖子不存在", "帖子已删除")
        )
        if missing:
            if status in (401, 403):
                raise NeedLoginError(endpoint, desc, code)
            raise PostNotFoundError(code, desc)
    raise XueqiuError(f"雪球接口 {endpoint} 报错：{desc or code}")


class XueqiuClient:
    """带自动 token 管理、响应缓存与并发控制的雪球接口客户端。

    资源相关参数都能用环境变量覆盖，方便在小内存机器上压低占用：

    ==========================  ======  ================================
    环境变量                     默认    说明
    ==========================  ======  ================================
    ``XUEQIU_MAX_CONNECTIONS``   32     连接池上限
    ``XUEQIU_MAX_CONCURRENCY``   32     同时在途的上游请求数上限
    ``XUEQIU_CACHE_MB``          16     响应缓存上限。已按解析后对象折算并计入
                                        条目自身开销，但仍是偏乐观的估计：
                                        实测真实 RSS 增长约为该值的 1.2~1.9 倍
                                        （小条目场景偏高）。小内存机器建议设 8
    ``XUEQIU_CACHE``             1      设 0 关闭缓存
    ``XUEQIU_HTTP2``             1      设 0 关闭 HTTP/2
    ``XUEQIU_TIMEOUT``           15     单请求超时（秒）
    ``XUEQIU_COOKIE``            -      登录 Cookie，优先于凭证文件
    ``XUEQIU_COOKIE_FILE``       -      凭证文件路径，默认 ``~/.xueqiu-mcp/cookies.json``
    ==========================  ======  ================================
    """

    def __init__(
        self,
        timeout: float | None = None,
        user_cookie: str | None = None,
        *,
        max_connections: int | None = None,
        max_concurrency: int | None = None,
        cache_bytes: int | None = None,
        cache_enabled: bool | None = None,
        http2: bool | None = None,
    ):
        self._timeout = timeout if timeout is not None else float(_env_int("XUEQIU_TIMEOUT", 15))
        self._user_cookie, self._cookie_source = resolve_cookie(user_cookie)
        self._max_connections = max_connections or _env_int("XUEQIU_MAX_CONNECTIONS", 32)
        want_h2 = http2 if http2 is not None else _env_flag("XUEQIU_HTTP2", True)
        self._http2 = want_h2 and HTTP2_AVAILABLE

        self._client: httpx.AsyncClient | None = None
        self._token_at: float = 0.0
        self._lock = asyncio.Lock()        # 只保护 token 刷新
        self._init_lock = asyncio.Lock()   # 只保护 client 首次创建
        self._gate = asyncio.Semaphore(max_concurrency or _env_int("XUEQIU_MAX_CONCURRENCY", 32))

        mb = cache_bytes if cache_bytes is not None else _env_int("XUEQIU_CACHE_MB", 16) * 1024 * 1024
        enabled = cache_enabled if cache_enabled is not None else _env_flag("XUEQIU_CACHE", True)
        self._cache = ResponseCache(max_bytes=mb, enabled=enabled)
        self._upstream_calls = 0

    @property
    def has_user_cookie(self) -> bool:
        return bool(self._user_cookie.strip())

    @property
    def cookie_source(self) -> str:
        """当前登录凭证的来源描述，未登录时是空串。"""
        return self._cookie_source

    @property
    def user_cookie(self) -> str:
        return self._user_cookie

    @property
    def user_cookies(self) -> dict[str, str]:
        return parse_cookie_string(self._user_cookie)

    def set_user_cookie(self, cookie: str | dict[str, str], source: str = "本次会话登录") -> None:
        """换上一份登录凭证，立即对后续请求生效。

        缓存会被清空：同一个 URL 在匿名态和登录态下可能返回不同内容（社区接口尤其
        明显），留着旧条目会让刚登录的用户继续读到匿名时的结果。
        """
        self._user_cookie = cookie if isinstance(cookie, str) else format_cookie_string(cookie)
        self._cookie_source = source
        client = self._client
        if client is not None:
            client.cookies.clear()
            for k, v in parse_cookie_string(self._user_cookie).items():
                client.cookies.set(k, v, domain=".xueqiu.com")
        self._token_at = time.monotonic()
        self._cache.clear()

    def clear_user_cookie(self) -> None:
        """丢掉登录凭证，退回匿名模式。下一个请求会重新去拿匿名 token。"""
        self._user_cookie = ""
        self._cookie_source = ""
        if self._client is not None:
            self._client.cookies.clear()
        self._token_at = 0.0
        self._cache.clear()

    @property
    def cache(self) -> ResponseCache:
        return self._cache

    async def _ensure_client(self) -> httpx.AsyncClient:
        # 快速路径：绝大多数调用走这里，不碰锁
        client = self._client
        if client is not None:
            return client
        async with self._init_lock:
            if self._client is None:  # double-check，避免并发建出多个连接池
                keepalive = max(4, self._max_connections // 2)
                self._client = httpx.AsyncClient(
                    timeout=self._timeout,
                    headers=BASE_HEADERS,
                    follow_redirects=True,
                    http2=self._http2,
                    limits=httpx.Limits(
                        max_connections=self._max_connections,
                        max_keepalive_connections=keepalive,
                        keepalive_expiry=60.0,
                    ),
                )
                if self.has_user_cookie:
                    for k, v in parse_cookie_string(self._user_cookie).items():
                        self._client.cookies.set(k, v, domain=".xueqiu.com")
                    self._token_at = time.monotonic()
            return self._client

    async def _refresh_token(self, force: bool = False) -> None:
        """获取匿名 token。用户自带 Cookie 时不覆盖。"""
        if self.has_user_cookie:
            return
        # 无锁快速路径：token 还新鲜就直接走，高并发下这是热路径
        if not force and time.monotonic() - self._token_at < TOKEN_TTL:
            return
        async with self._lock:
            age = time.monotonic() - self._token_at
            # 进临界区后再确认一次：force 时用短冷却窗口，避免并发失败触发的惊群
            if age < (FORCE_REFRESH_COOLDOWN if force else TOKEN_TTL):
                return
            client = await self._ensure_client()
            client.cookies.clear()
            # 行情页一次就能同时下发 acw_tc 和 xq_a_token，不必先访问首页。
            # （首页那一跳曾经是必须的，实测已不需要，省下约 95ms 和 110KB 传输。）
            self._upstream_calls += 1
            await client.get(MAIN_API + "/hq", headers={"Accept": "text/html,*/*"})
            if not client.cookies.get("xq_a_token"):
                # 兜底：雪球若改回「必须先过首页」的行为，这条路仍然走得通
                self._upstream_calls += 1
                await client.get(MAIN_API + "/", headers={"Accept": "text/html,*/*"})
                self._upstream_calls += 1
                await client.get(MAIN_API + "/hq", headers={"Accept": "text/html,*/*"})
            if not client.cookies.get("xq_a_token"):
                raise XueqiuError("未能从雪球获取访问令牌，可能是网络不通或雪球改版。")
            self._token_at = time.monotonic()
            log.info("已刷新雪球匿名 token")

    async def _fetch(
        self, path: str, clean: dict[str, Any], base: str, referer: str | None
    ) -> tuple[Any, int]:
        """真正发请求。返回 ``(解析后的 JSON, 原始字节数)``。"""
        await self._refresh_token()
        client = await self._ensure_client()
        headers = {"Referer": referer} if referer else None

        # JSON「未登录」可能只是上游偶发错误。先原样重试，不能据此清 Cookie
        # 或刷新 token；每次逻辑请求最多发两次，重试也经过并发闸门和请求合并。
        for attempt in range(2):
            async with self._gate:
                self._upstream_calls += 1
                resp = await client.get(base + path, params=clean, headers=headers)

            if resp.status_code == 429:
                raise RateLimitedError(path)
            if path == "/statuses/show.json" and resp.is_server_error:
                resp.raise_for_status()

            try:
                data = self._decode_response(path, resp)
            except PostNotFoundError:
                raise
            except NeedLoginError:
                if attempt:
                    raise
                log.info("接口 %s 返回未登录，使用现有凭证重试一次", path)
                continue
            except XueqiuError:
                if attempt or self.has_user_cookie or resp.status_code not in (400, 401, 403):
                    raise
            else:
                if attempt or self.has_user_cookie or resp.status_code not in (400, 401, 403):
                    return data, len(resp.content) * OBJECT_SIZE_FACTOR

            # 保留匿名 HTTP 鉴权失败的 token 刷新兜底，与 JSON 未登录重试分开。
            await self._refresh_token(force=True)

        raise AssertionError("unreachable")

    @staticmethod
    def _decode_response(path: str, response: httpx.Response) -> Any:
        # 只检查开头，避免为大 JSON 复制和解码整个响应体。
        raw = response.content
        if raw[:32].lstrip()[:1] == b"<":
            raise NeedCookieError(path)

        try:
            data = _loads(raw)
        except ValueError as exc:  # pragma: no cover - 极少发生
            if path == "/statuses/show.json" and response.status_code == 404:
                response.raise_for_status()
            raise XueqiuError(
                f"接口 {path} 返回了无法解析的内容：{raw[:200].decode('utf-8', 'replace')}"
            ) from exc

        if isinstance(data, dict):
            code = data.get("error_code")
            if code not in (None, 0, "0"):
                raise_business_error(
                    path, code, str(data.get("error_description") or data.get("error_msg") or ""), response.status_code
                )
            if path == "/statuses/show.json" and response.is_error:
                response.raise_for_status()
            if data.get("status") and data.get("error") and "data" not in data:
                raise XueqiuError(f"雪球接口 {path} 报错：{data.get('error')}（HTTP {data.get('status')}）")
        return data

    async def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        base: str = STOCK_API,
        referer: str | None = None,
        cache: bool = True,
    ) -> dict[str, Any]:
        """发起 GET 请求并返回解析后的 JSON。

        默认按 endpoint 的分级 TTL 走缓存，并把并发的相同请求合并成一次上游调用。
        传 ``cache=False`` 可绕过缓存。

        .. warning::
           命中缓存时返回的是**共享对象**，多个调用方拿到的是同一份数据。
           调用方只许读，不许原地改（``sort`` / ``append`` / 改键值都不行），
           否则会污染后续所有命中该缓存的请求。
        """
        clean = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
        ttl = ttl_for(path) if cache else 0.0
        if ttl <= 0:
            data, _ = await self._fetch(path, clean, base, referer)
            return data

        key = f"{base}{path}?{urlencode(sorted(clean.items()))}"
        return await self._cache.fetch(key, ttl, lambda: self._fetch(path, clean, base, referer))

    def stats(self) -> dict[str, Any]:
        """运行时指标，便于压测和线上观测。"""
        return {"upstream_calls": self._upstream_calls, **self._cache.stats()}

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._cache.clear()


async def gather_json(*coros: Any) -> list[Any]:
    """并发跑多个取数协程，单个失败返回 ``{}``，不牵连其他。

    多数工具要拼几个互不依赖的接口（如股东结构要同时取股东户数、十大流通股东、
    机构持仓）。串行 await 会把延迟叠加成 3 倍，这里一次性发出去。
    """
    results = await asyncio.gather(*coros, return_exceptions=True)
    out: list[Any] = []
    for r in results:
        if isinstance(r, asyncio.CancelledError):
            raise r
        out.append({} if isinstance(r, BaseException) else r)
    return out


_client: XueqiuClient | None = None


def get_client() -> XueqiuClient:
    """进程级单例，复用连接、token 与缓存。"""
    global _client
    if _client is None:
        _client = XueqiuClient()
    return _client
