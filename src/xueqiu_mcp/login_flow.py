"""短期扫码登录会话：MCP 立即交付图片，后台接管 Cookie，网页只读会话状态。

HTTP 模式把网页挂在 MCP 服务上；stdio 模式按需开一个仅监听回环地址的网页服务。
同一进程共享一个雪球账号，因此也只保留一个有效登录会话。
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import socket
import time
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from . import browser

log = logging.getLogger("xueqiu_mcp")
LOGIN_TTL = 300
POLL_INTERVAL = 2.0
EXPIRED_MESSAGE = "登录链接已失效，请重新调用 get_login_qrcode 获取。"


@dataclass
class LoginAttempt:
    mode: str
    remember: bool
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    expires_at: float = field(default_factory=lambda: time.time() + LOGIN_TTL)
    deadline: float = field(default_factory=lambda: time.monotonic() + LOGIN_TTL)
    state: str = "pending"
    message: str = "请用雪球 App 扫码，并在 App 中确认登录。"
    qr_data: str = ""
    login_url: str = ""
    link_note: str = ""
    done: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def path(self) -> str:
        return f"/login/{self.token}"

    def finish(self, state: str, message: str) -> None:
        self.state, self.message, self.qr_data = state, message, ""
        self.done.set()

    def public_state(self) -> dict:
        # 白名单输出，不能把整个对象（更不能把浏览器 cookies）序列化给网页。
        return {
            "state": self.state, "message": self.message, "qr": self.qr_data,
            "expires_at": int(self.expires_at),
            "expires_in": max(0, int(self.deadline - time.monotonic())),
        }


class LoginFlow:
    def __init__(self) -> None:
        self.attempt: LoginAttempt | None = None
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._web_server = None
        self._web_task: asyncio.Task | None = None
        self._web_socket: socket.socket | None = None
        self._local_url = ""

    async def start(self, mode: str = "qrcode", remember: bool = True,
                    refresh: bool = False) -> LoginAttempt:
        async with self._lock:
            old = self.attempt
            if (old and old.state == "pending" and old.deadline > time.monotonic()
                    and self._task is not None and not self._task.done()
                    and not refresh and old.mode == mode and old.remember == remember):
                return old
            await self._cancel()
            session = browser.get_session()
            try:
                res = await session.open_login(mode)
            except BaseException:
                await session.close_login_page()
                raise
            attempt = LoginAttempt(mode=mode, remember=remember)
            self.attempt = attempt
            if res["logged_in"]:
                await self._adopt(session, attempt)
                await session.close_login_page()
                return attempt
            attempt.qr_data = res.get("qr_data") or ""
            if mode == "qrcode" and not attempt.qr_data:
                attempt.finish("error", "登录页打开了，但未能读取二维码；请重试，或使用 "
                               'browser_login(mode="window") 在服务所在机器完成验证。')
                await session.close_login_page()
                return attempt
            if mode == "window":
                attempt.message = "请在服务所在机器弹出的浏览器窗口中完成登录。"
            try:
                await self._set_link(attempt)
            except asyncio.CancelledError:
                attempt.finish("cancelled", "登录会话已取消，请重新获取登录二维码。")
                await session.close_login_page()
                raise
            except Exception:
                # 网页启动失败也照常交付二维码，扫码后台不能因此丢失。
                log.warning("临时登录网页启动失败，仍可使用 MCP 返回的二维码")
                attempt.link_note = "临时登录网页启动失败，请使用本次返回的二维码图片。"
            self._task = asyncio.create_task(self._watch(session, attempt))
            return attempt

    @staticmethod
    async def _adopt(session, attempt: LoginAttempt) -> None:
        try:
            jar = await session.adopt_cookies(attempt.remember)
            if not jar:
                raise RuntimeError("登录身份已消失")
        except Exception:
            attempt.finish("error", "检测到登录，但接管或保存凭证失败，请重试登录。")
            return
        tail = ("凭证已保存，后续请求可直接使用。" if attempt.remember else
                "凭证已接管，未写入 cookies.json；浏览器 profile 仍会保留登录态。")
        attempt.finish("success", "登录成功。" + tail)

    async def _watch(self, session, attempt: LoginAttempt) -> None:
        try:
            while attempt.deadline > time.monotonic():
                # 每次只观察现有页面，不刷新页面或重建扫码会话。
                remaining = attempt.deadline - time.monotonic()
                res = await asyncio.wait_for(session.login_snapshot(attempt.mode), remaining)
                if res["logged_in"]:
                    await self._adopt(session, attempt)
                    return
                if res.get("qr_data"):
                    attempt.qr_data = res["qr_data"]
                await asyncio.sleep(min(POLL_INTERVAL, max(0, attempt.deadline - time.monotonic())))
            attempt.finish("expired", EXPIRED_MESSAGE)
        except asyncio.CancelledError:
            attempt.finish("cancelled", "登录会话已取消，请重新获取登录二维码。")
            raise
        except asyncio.TimeoutError:
            attempt.finish("expired", EXPIRED_MESSAGE)
        except Exception:
            # 浏览器异常可能含页面内容或 URL，不转发到公共状态接口。
            attempt.finish("error", "登录页面已关闭或读取失败，请重新获取登录二维码。")
        finally:
            await session.close_login_page()

    async def _cancel(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if self.attempt is not None:
            self.attempt.finish("cancelled", "登录会话已取消，请重新获取登录二维码。")
            # task 在第一次调度前被取消时不会进入 _watch 的 finally。
            await browser.get_session().close_login_page()

    async def cancel(self) -> None:
        async with self._lock:
            await self._cancel()

    def lookup(self, token: str) -> dict:
        attempt = self.attempt
        if (not attempt or not token.isascii() or not secrets.compare_digest(attempt.token, token)
                or attempt.deadline <= time.monotonic()):
            return {"state": "expired", "message": EXPIRED_MESSAGE, "qr": ""}
        return attempt.public_state()

    async def _set_link(self, attempt: LoginAttempt) -> None:
        base = os.getenv("XUEQIU_PUBLIC_BASE_URL", "").strip().rstrip("/")
        if base:
            parsed = urlsplit(base)
            if (parsed.scheme not in ("http", "https") or not parsed.netloc
                    or parsed.username or parsed.password or parsed.query or parsed.fragment):
                attempt.link_note = "XUEQIU_PUBLIC_BASE_URL 格式无效，请配置无凭据、查询参数的 HTTP(S) 服务地址。"
                return
            attempt.login_url = base + attempt.path
        elif os.getenv("XUEQIU_TRANSPORT", "stdio").strip().lower() == "stdio":
            attempt.login_url = await self._ensure_local_web() + attempt.path
            attempt.link_note = "此链接仅能在运行 MCP 的机器上打开；手机请扫描对话里的二维码。"
        else:
            attempt.link_note = (
                "服务端尚未配置 XUEQIU_PUBLIC_BASE_URL，无法生成完整链接。"
                f"登录网页相对路径：{attempt.path}；仍可直接扫描本次返回的图片。"
            )

    async def _ensure_local_web(self) -> str:
        if self._web_task is not None and not self._web_task.done():
            return self._local_url
        import uvicorn

        class LocalServer(uvicorn.Server):
            # stdio MCP 自己管理信号；嵌入的 HTTP 服务不能覆盖它的退出处理器。
            def install_signal_handlers(self):
                pass

            @contextmanager
            def capture_signals(self):
                yield

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", 0))
            sock.listen(32)
            sock.setblocking(False)
            app = Starlette(routes=login_routes())
            config = uvicorn.Config(app, log_level="warning", access_log=False, lifespan="off")
            server = LocalServer(config)
            task = asyncio.create_task(server.serve(sockets=[sock]))
            self._web_server, self._web_task, self._web_socket = server, task, sock
            for _ in range(100):
                if server.started:
                    self._local_url = f"http://127.0.0.1:{sock.getsockname()[1]}"
                    return self._local_url
                if task.done():
                    await task
                    break
                await asyncio.sleep(0.01)
            raise RuntimeError("临时网页监听失败")
        except BaseException:
            await self._stop_web()
            sock.close()
            raise

    async def _stop_web(self) -> None:
        if self._web_server is not None:
            self._web_server.should_exit = True
        if self._web_task is not None:
            try:
                await asyncio.wait_for(self._web_task, 3)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        if self._web_socket is not None:
            self._web_socket.close()
        self._web_server = self._web_task = self._web_socket = None
        self._local_url = ""

    async def close(self) -> None:
        async with self._lock:
            await self._cancel()
            await self._stop_web()


_flow: LoginFlow | None = None


def get_flow() -> LoginFlow:
    global _flow
    if _flow is None:
        _flow = LoginFlow()
    return _flow


async def aclose() -> None:
    if _flow is not None:
        await _flow.close()


HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Robots-Tag": "noindex, nofollow, noarchive",
    "Content-Security-Policy": "default-src 'none'; img-src data:; style-src 'unsafe-inline'; "
        "script-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'",
}


async def login_page(request: Request) -> HTMLResponse:
    return HTMLResponse(LOGIN_HTML, headers=HEADERS)


async def login_state(request: Request) -> JSONResponse:
    return JSONResponse(get_flow().lookup(request.path_params["token"]), headers=HEADERS)


def login_routes() -> list[Route]:
    return [Route("/login/{token}", login_page), Route("/login/{token}/state", login_state)]


LOGIN_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer"><title>雪球扫码登录</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#f5f8fb;color:#182b3a;
font:16px/1.7 -apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif}
main{max-width:440px;margin:8vh auto;padding:28px 24px;text-align:center}
.brand{color:#1676c2;font-size:14px;letter-spacing:.14em}h1{font-size:28px;margin:8px 0}
.sub,footer{color:#697b89;font-size:14px}.frame{margin:28px auto 20px;padding:20px;
background:white;border:1px solid #dae4ec;border-radius:16px;width:min(100%,300px)}
img{display:block;width:100%;aspect-ratio:1;image-rendering:pixelated}
[hidden]{display:none!important}#status{font-weight:600}#message{overflow-wrap:anywhere}
body[data-state=success] #status{color:#187749}body[data-state=error] #status{color:#b74434}
footer{margin-top:36px;border-top:1px solid #dae4ec;padding-top:20px}
</style></head><body><main>
<div class="brand">雪球 MCP</div><h1>扫码登录雪球</h1>
<p class="sub">使用雪球 App 扫一扫，并在 App 中确认</p>
<div class="frame" id="frame" hidden><img id="qr" alt="雪球登录二维码"></div>
<div role="status" aria-live="polite"><p id="status">正在读取登录状态…</p>
<p id="message">请稍候</p></div><p class="sub" id="expiry"></p>
<footer>页面会自动更新二维码及登录结果。<br>链接只用于本次登录，请勿转发给他人。</footer>
</main><script>
const qr=document.getElementById('qr'),frame=document.getElementById('frame');
const statusText=document.getElementById('status'),message=document.getElementById('message');
const expiry=document.getElementById('expiry');
const labels={pending:'等待扫码',success:'登录成功',expired:'链接已失效',
cancelled:'登录已取消',error:'登录未完成'};
async function poll(){
 try{
  const response=await fetch(location.pathname.replace(/\\/$/,'')+'/state',
    {cache:'no-store',credentials:'omit',referrerPolicy:'no-referrer'});
  if(!response.ok)throw new Error('network');
  const state=await response.json();
  document.body.dataset.state=state.state;
  statusText.textContent=labels[state.state]||'登录状态';message.textContent=state.message;
  const hasQR=state.state==='pending'&&typeof state.qr==='string'&&
    state.qr.startsWith('data:image/png;base64,');
  frame.hidden=!hasQR;if(hasQR){qr.src=state.qr}else{qr.removeAttribute('src')}
  expiry.textContent=state.state==='pending'?'链接剩余 '+state.expires_in+' 秒':'';
  if(state.state==='pending')setTimeout(poll,2000);
 }catch(error){
  frame.hidden=true;qr.removeAttribute('src');statusText.textContent='连接暂时中断';
  message.textContent='正在重新连接，请保持页面打开。';expiry.textContent='';setTimeout(poll,3000);
 }
}
poll();
</script></body></html>"""
