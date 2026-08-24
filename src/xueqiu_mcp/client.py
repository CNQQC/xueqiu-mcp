"""雪球 HTTP 客户端。

雪球的开放接口需要一个匿名 token（cookie 中的 ``xq_a_token``）。该 token 通过
先访问首页、再访问行情页的方式自动获取，过期后自动重新获取一次。

``xueqiu.com`` 主域下的社区接口（讨论 / 公告 / 自选股）受阿里云 WAF 的 JS 挑战
保护，匿名访问会被拦截。这类接口需要用户在环境变量 ``XUEQIU_COOKIE`` 中提供
自己浏览器里的登录 Cookie。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

import httpx

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


class XueqiuError(RuntimeError):
    """雪球接口返回的业务错误。"""


class NeedCookieError(XueqiuError):
    """接口被 WAF 拦截，需要用户提供自己的 Cookie。"""

    def __init__(self, endpoint: str):
        super().__init__(
            f"接口 {endpoint} 被雪球的风控拦截，匿名访问不可用。\n"
            "请在浏览器登录 xueqiu.com 后，从开发者工具复制完整 Cookie，"
            "配置到环境变量 XUEQIU_COOKIE 后重启本 MCP Server。"
        )


def _parse_cookie_string(raw: str) -> dict[str, str]:
    jar: dict[str, str] = {}
    for piece in raw.split(";"):
        piece = piece.strip()
        if not piece or "=" not in piece:
            continue
        k, _, v = piece.partition("=")
        jar[k.strip()] = v.strip()
    return jar


class XueqiuClient:
    """带自动 token 管理的雪球接口客户端。"""

    def __init__(self, timeout: float = 15.0, user_cookie: str | None = None):
        self._timeout = timeout
        self._user_cookie = user_cookie if user_cookie is not None else os.getenv("XUEQIU_COOKIE", "")
        self._client: httpx.AsyncClient | None = None
        self._token_at: float = 0.0
        self._lock = asyncio.Lock()

    @property
    def has_user_cookie(self) -> bool:
        return bool(self._user_cookie.strip())

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                headers=BASE_HEADERS,
                follow_redirects=True,
            )
            if self.has_user_cookie:
                for k, v in _parse_cookie_string(self._user_cookie).items():
                    self._client.cookies.set(k, v, domain=".xueqiu.com")
                self._token_at = time.time()
        return self._client

    async def _refresh_token(self, force: bool = False) -> None:
        """获取匿名 token。用户自带 Cookie 时不覆盖。"""
        if self.has_user_cookie:
            return
        async with self._lock:
            if not force and time.time() - self._token_at < TOKEN_TTL:
                return
            client = await self._ensure_client()
            client.cookies.clear()
            # 第一步取 acw_tc，第二步行情页才会下发 xq_a_token
            await client.get(MAIN_API + "/", headers={"Accept": "text/html,*/*"})
            await client.get(MAIN_API + "/hq", headers={"Accept": "text/html,*/*"})
            if not client.cookies.get("xq_a_token"):
                raise XueqiuError("未能从雪球获取访问令牌，可能是网络不通或雪球改版。")
            self._token_at = time.time()
            log.info("已刷新雪球匿名 token")

    async def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        base: str = STOCK_API,
        referer: str | None = None,
    ) -> dict[str, Any]:
        """发起 GET 请求并返回解析后的 JSON。"""
        await self._refresh_token()
        client = await self._ensure_client()
        clean = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
        headers = {"Referer": referer} if referer else None

        resp = await client.get(base + path, params=clean, headers=headers)

        # token 失效：强制刷新后重试一次
        if resp.status_code in (400, 401, 403) and not self.has_user_cookie:
            await self._refresh_token(force=True)
            client = await self._ensure_client()
            resp = await client.get(base + path, params=clean, headers=headers)

        body = resp.text
        if body.lstrip().startswith(("<!doctype", "<html", "<textarea", "<!DOCTYPE")):
            raise NeedCookieError(path)

        try:
            data = resp.json()
        except ValueError as exc:  # pragma: no cover - 极少发生
            raise XueqiuError(f"接口 {path} 返回了无法解析的内容：{body[:200]}") from exc

        if isinstance(data, dict):
            code = data.get("error_code")
            if code not in (None, 0, "0"):
                raise XueqiuError(
                    f"雪球接口 {path} 报错：{data.get('error_description') or data.get('error_msg') or code}"
                )
            if data.get("status") and data.get("error") and "data" not in data:
                raise XueqiuError(f"雪球接口 {path} 报错：{data.get('error')}（HTTP {data.get('status')}）")
        return data

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


_client: XueqiuClient | None = None


def get_client() -> XueqiuClient:
    """进程级单例，复用连接与 token。"""
    global _client
    if _client is None:
        _client = XueqiuClient()
    return _client
