"""本地无头浏览器会话：登录取 Cookie，以及 API 拿不全时的正文兜底。

为什么需要浏览器
----------------
雪球的数据有两处是纯 HTTP 客户端够不着的：

**正文残缺的少数帖子**
    列表类接口（讨论区、热帖、搜帖）只给一两百字的 ``description`` 摘要，正文要
    靠 ``/statuses/show.json`` 再取一次。实测匿名下 show.json 对普通帖子和长文都
    给得全（抽查 7 篇长文，与页面渲染结果逐字一致），所以**这条路平时不触发**；
    它兜的是 ``truncated`` 被置位、或正文整个为空的少数帖子 —— 付费专栏、仅粉丝
    可见这类内容匿名拿不到全文，登录后从页面上却能读到。

**主域接口被 WAF 拦**
    ``xueqiu.com`` 主域挂着阿里云 WAF 的 JS 挑战，匿名 httpx 请求拿回来的是挑战
    页而不是 JSON。浏览器天然会执行那段 JS。

所以本模块做两件事：把浏览器当**登录器**（扫码 / 短信 / 微信都由用户在真浏览器里
完成，程序只接手结果 cookie），以及当**兜底渲染器**（只在 API 明显没给全时才开
页面）。两者里前者是主角 —— 登录态本身才是拿到受限内容的钥匙，渲染只是最后一道
保险。日常取数仍然走 :mod:`.client` 的 httpx 快路径。

页面里的数据从哪读
------------------
雪球的帖子页会把服务端渲染的数据原样注入全局变量，结构与 API 一致：

- ``window.SNOWMAN_STATUS`` —— 完整帖子对象，含未截断的 ``text``；
- ``window.SNOWMAN_USER`` —— 当前登录用户，未登录时是空对象。

读全局变量比抠 DOM 稳得多：雪球的 class 名带构建 hash（如
``newLogin_modal__login__main_Qw9``），发一次版就变。本模块凡是绕不开选择器的
地方一律用前缀或文本匹配。

资源
----
Playwright 是可选依赖（``pip install -e ".[browser]"`` 再 ``playwright install
chromium``）。没装时本模块的入口会抛 :class:`BrowserUnavailable`，调用方降级回
纯 API，不影响其余 21 个工具。

浏览器常驻内存约 150~250MB，远高于本项目其余部分，所以默认**空闲 5 分钟自动
关闭**（``XUEQIU_BROWSER_IDLE``），下次要用再拉起来。登录态存在持久化 profile
目录里，关掉浏览器不会丢。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
from pathlib import Path
from typing import Any

from . import auth

log = logging.getLogger("xueqiu_mcp")

HOME_URL = "https://xueqiu.com"
# 帖子页只认 /{uid}/{pid} 这一种写法：/statuses/show/{pid} 会吃 404，
# 所以缺 uid 时得先花一次轻量 API 把作者查出来（见 :meth:`BrowserSession.fetch_status`）
POST_URL = "https://xueqiu.com/{uid}/{pid}"

# 二维码登录的画布。class 带构建 hash，只能前缀匹配
QRCODE_CANVAS = '[class*="index_qrcode__wrapper"] canvas'
# 「二维码登录」这个 tab 是 React 渲染的，domcontentloaded 时往往还不存在
QRCODE_TAB = "text=二维码登录"
DEFAULT_IDLE = 300.0


class BrowserUnavailable(RuntimeError):
    """Playwright 没装、浏览器没下载，或被 ``XUEQIU_BROWSER=0`` 关掉。"""


INSTALL_HINT = (
    "本地浏览器功能需要 Playwright：\n"
    "  1. pip install -e \".[browser]\"      # 或 uv pip install -e \".[browser]\"\n"
    "  2. playwright install chromium        # 下载浏览器内核，约 150MB\n"
    "装完不用改 MCP 配置，重启一下 Server 即可。"
)


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


def profile_dir() -> Path:
    """浏览器 profile 目录。登录态存在这里，与 cookies.json 各自独立。

    两者都留是有意的：cookies.json 给 httpx 快路径用，profile 给浏览器自己用。
    只保一份的话，要么每次开浏览器都得重新登录，要么 API 请求带不上登录态。
    """
    custom = os.getenv("XUEQIU_BROWSER_PROFILE", "").strip()
    if custom:
        return Path(custom).expanduser()
    return auth.store_path().parent / "browser"


def available() -> bool:
    """Playwright 是否可用（只看能不能 import，不启动浏览器）。"""
    if not _env_flag("XUEQIU_BROWSER", True):
        return False
    try:
        import playwright.async_api  # noqa: F401
    except ImportError:
        return False
    return True


# --------------------------------------------------------------------- 二维码
def qr_matrix_to_text(matrix: list[list[bool]], quiet: int = 2) -> str:
    """布尔矩阵渲染成终端能扫的二维码。

    用上下半块字符（``▀``）把两行压成一行 —— 终端字符是高比宽大约 2:1 的长方形，
    一行一模块画出来会被拉成竖长条，手机扫不出来。
    """
    if not matrix:
        return ""
    size = len(matrix)
    pad = [[False] * (size + quiet * 2) for _ in range(quiet)]
    rows = pad + [[False] * quiet + row + [False] * quiet for row in matrix] + pad
    if len(rows) % 2:
        rows.append([False] * len(rows[0]))

    # 黑模块 = 亮块。终端多为深色底，反过来会扫不出
    glyph = {(True, True): "█", (True, False): "▀", (False, True): "▄", (False, False): " "}
    out = []
    for i in range(0, len(rows), 2):
        top, bottom = rows[i], rows[i + 1]
        out.append("".join(glyph[(t, b)] for t, b in zip(top, bottom)))
    return "\n".join(out)


# canvas 像素 → 布尔矩阵。在页面里跑，避免把 230×230 的 RGBA 数组搬回 Python
_READ_QR_JS = r"""
(sel) => {
  const c = document.querySelector(sel);
  if (!c || !c.width) return null;
  const ctx = c.getContext('2d');
  const { data, width: W, height: H } = ctx.getImageData(0, 0, c.width, c.height);
  const dark = (x, y) => {
    const i = (y * W + x) * 4;
    if (data[i + 3] < 128) return false;              // 透明算白
    return (data[i] + data[i + 1] + data[i + 2]) / 3 < 128;
  };
  // 1) 黑像素的外接矩形 = 二维码本体（去掉 canvas 自带的白边）
  let x0 = W, y0 = H, x1 = -1, y1 = -1;
  for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) if (dark(x, y)) {
    if (x < x0) x0 = x; if (x > x1) x1 = x;
    if (y < y0) y0 = y; if (y > y1) y1 = y;
  }
  if (x1 < 0) return null;
  // 2) 左上角 finder pattern 的顶边固定是 7 个模块宽，据此反推模块像素尺寸
  let run = 0;
  for (let x = x0; x <= x1 && dark(x, y0); x++) run++;
  const unit = run / 7;
  if (!(unit >= 1)) return null;
  const n = Math.round((x1 - x0 + 1) / unit);
  if (n < 21 || n > 177) return null;                  // 合法二维码是 21~177 模块
  // 3) 按模块中心采样
  const m = [];
  for (let r = 0; r < n; r++) {
    const row = [];
    for (let q = 0; q < n; q++) {
      row.push(dark(
        Math.min(W - 1, Math.round(x0 + (q + 0.5) * unit)),
        Math.min(H - 1, Math.round(y0 + (r + 0.5) * unit)),
      ));
    }
    m.push(row);
  }
  return m;
}
"""

# 点「二维码登录」。首页登录卡片是常驻的，class 带 hash，只能按文字找
_SWITCH_QR_JS = r"""
() => {
  const tab = [...document.querySelectorAll('a,span,div')]
    .find(e => (e.textContent || '').trim() === '二维码登录');
  if (tab) { tab.click(); return true; }
  return false;
}
"""

_LOGIN_UID_JS = r"""
() => {
  const u = window.SNOWMAN_USER;
  return (u && u.id) ? u.id : 0;
}
"""


class BrowserSession:
    """全局唯一的浏览器会话。懒启动、空闲自动关。

    用 ``launch_persistent_context`` 而不是 ``launch`` + ``new_context``：登录态要
    跨进程活下来，必须落到磁盘 profile 里。
    """

    def __init__(self) -> None:
        self._pw: Any = None
        self._ctx: Any = None
        self._page: Any = None
        self._login_page: Any = None
        self._headless: bool | None = None
        self._lock = asyncio.Lock()
        self._last_used = 0.0
        self._reaper: asyncio.Task | None = None

    # ------------------------------------------------------------- 生命周期
    @property
    def running(self) -> bool:
        return self._ctx is not None

    async def _ensure(self, headless: bool | None = None) -> Any:
        """确保浏览器已启动，返回可用的 page。"""
        if not available():
            raise BrowserUnavailable(INSTALL_HINT)
        if self._ctx is not None and headless is not None and headless != self._headless:
            await self._teardown()
        if self._ctx is not None:
            self._touch()
            return self._page

        from playwright.async_api import async_playwright

        if headless is None:
            headless = _env_flag("XUEQIU_BROWSER_HEADLESS", True)
        path = profile_dir()
        path.mkdir(parents=True, exist_ok=True, mode=0o700)

        self._pw = await async_playwright().start()
        try:
            self._ctx = await self._pw.chromium.launch_persistent_context(
                str(path),
                headless=headless,
                viewport={"width": 1280, "height": 900},
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                # headless 的默认 UA 带 HeadlessChrome，雪球的风控认这个词
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                ),
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception as exc:  # 浏览器内核没下载是最常见的失败
            await self._teardown()
            raise BrowserUnavailable(f"启动 Chromium 失败：{exc}\n\n{INSTALL_HINT}") from exc

        self._page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        self._headless = headless
        self._page.set_default_timeout(float(os.getenv("XUEQIU_BROWSER_TIMEOUT", "30")) * 1000)
        self._touch()
        self._start_reaper()
        log.info("本地浏览器已启动（headless=%s，profile=%s）", headless, path)
        return self._page

    def _touch(self) -> None:
        self._last_used = time.monotonic()

    def _start_reaper(self) -> None:
        idle = float(os.getenv("XUEQIU_BROWSER_IDLE", str(DEFAULT_IDLE)))
        if idle <= 0 or (self._reaper and not self._reaper.done()):
            return

        async def reap() -> None:
            while self._ctx is not None:
                await asyncio.sleep(min(30.0, idle))
                if self._ctx is not None and time.monotonic() - self._last_used > idle:
                    log.info("浏览器空闲 %.0f 秒，自动关闭以释放内存", idle)
                    await self.close()
                    return

        self._reaper = asyncio.create_task(reap())

    async def _teardown(self) -> None:
        reaper, self._reaper = self._reaper, None
        if reaper is not None and reaper is not asyncio.current_task():
            reaper.cancel()
        for obj, name in ((self._ctx, "close"), (self._pw, "stop")):
            if obj is not None:
                try:
                    await getattr(obj, name)()
                except Exception:  # 关闭失败没什么可补救的，别把它抛给调用方
                    pass
        self._ctx = self._pw = self._page = None
        self._login_page = None
        self._headless = None

    async def close(self) -> None:
        async with self._lock:
            await self._teardown()

    # ------------------------------------------------------------- Cookie
    async def cookies(self) -> dict[str, str]:
        if self._ctx is None:
            return {}
        return {
            c["name"]: c["value"] for c in await self._ctx.cookies()
            if c.get("domain", "").lstrip(".") == "xueqiu.com"
            or c.get("domain", "").endswith(".xueqiu.com")
        }

    async def adopt_cookies(self, remember: bool = True) -> dict[str, str]:
        """把浏览器里的登录 cookie 交给 httpx 客户端，并按需落盘。

        没登录成功就原样返回空字典，不去动客户端已有的凭证。
        """
        jar = await self.cookies()
        if not auth.is_logged_in(jar):
            return {}
        from .client import get_client

        source = "本地浏览器登录"
        if remember:
            source = f"凭证文件 {auth.save_cookies(jar)}"
        get_client().set_user_cookie(jar, source)
        return jar

    async def current_uid(self) -> int:
        """页面视角的登录用户 ID。0 表示未登录。"""
        if self._page is None:
            return 0
        try:
            return int(await self._page.evaluate(_LOGIN_UID_JS) or 0)
        except Exception:
            return 0

    # ------------------------------------------------------------- 登录
    async def open_login(self, mode: str = "qrcode") -> dict[str, Any]:
        """打开独立登录标签页，返回 PNG data URI、文件路径和终端字符画。

        登录页与正文渲染页分开，取帖子不会打断正在进行的扫码。
        """
        async with self._lock:
            return await self._open_login(mode)

    async def _open_login(self, mode: str) -> dict[str, Any]:
        headless = mode == "qrcode" and _env_flag("XUEQIU_BROWSER_HEADLESS", True)
        await self._ensure(headless=headless)
        if self._login_page is None or self._login_page.is_closed():
            self._login_page = await self._ctx.new_page()
            self._login_page.set_default_timeout(
                float(os.getenv("XUEQIU_BROWSER_TIMEOUT", "30")) * 1000
            )
        page = self._login_page
        try:
            await page.goto(HOME_URL, wait_until="domcontentloaded")
        except Exception as exc:
            raise BrowserUnavailable("雪球登录页打开失败，请稍后重试。") from exc

        out: dict[str, Any] = {
            "logged_in": False, "qr_text": None, "qr_png": None, "qr_data": None,
        }
        if auth.is_logged_in(await self.cookies()):
            out["logged_in"] = True
            return out
        if mode != "qrcode":
            return out

        try:
            # 先等 React 把登录卡片渲染出来。少了这一步，evaluate 点了个空气，
            # 后面就只能干等 canvas 超时（实测 domcontentloaded 后还要 1~3 秒）
            await page.wait_for_selector(QRCODE_TAB, state="attached", timeout=20000)
            await page.evaluate(_SWITCH_QR_JS)
            # canvas 读的是像素，不要求可见，attached 就够
            await page.wait_for_selector(QRCODE_CANVAS, state="attached", timeout=15000)
            # canvas 是 JS 画的，选择器命中时可能还没画完，给它一拍
            for _ in range(20):
                qr = await self._read_qr(page, with_text=True)
                if qr.get("qr_data"):
                    out.update(qr)
                    break
                await asyncio.sleep(0.3)
        except Exception as exc:
            log.warning("读取登录二维码失败：%s", exc)
        return out

    async def _read_qr(self, page: Any, with_text: bool = False) -> dict[str, Any]:
        """PNG 直接从 canvas 导出，不让字符矩阵识别失败影响图片交付。"""
        out: dict[str, Any] = {}
        data_url = await page.evaluate(
            """(sel) => {
                const c = document.querySelector(sel);
                if (!c || !c.width || !c.height) return null;
                const pixels = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
                for (let i = 0; i < pixels.length; i += 4) {
                    if (pixels[i+3] >= 128 && pixels[i]+pixels[i+1]+pixels[i+2] < 384)
                        return c.toDataURL('image/png');
                }
                return null;
            }""",
            QRCODE_CANVAS,
        )
        if not data_url or not data_url.startswith("data:image/png;base64,"):
            return out
        out["qr_data"] = data_url
        if with_text:
            try:
                matrix = await page.evaluate(_READ_QR_JS, QRCODE_CANVAS)
                if matrix:
                    out["qr_text"] = qr_matrix_to_text(matrix)
            except Exception:
                pass  # MCP 图片交付不依赖终端字符画
            out["qr_png"] = self._save_qr_png(data_url)
        return out

    @staticmethod
    def _save_qr_png(data_url: str) -> str | None:
        """另存一份 PNG 给命令行使用；MCP 直接用内存图片。"""
        try:
            path = (profile_dir().parent / "login-qrcode.png").resolve()
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as f:
                os.fchmod(f.fileno(), 0o600)
                f.write(base64.b64decode(data_url.split(",", 1)[1], validate=True))
            return str(path)
        except Exception:
            return None

    async def login_snapshot(self, mode: str = "qrcode") -> dict[str, Any]:
        """检查同一登录页；页面明确提示二维码失效时才点击换码。"""
        async with self._lock:
            if self._ctx is None or self._login_page is None or self._login_page.is_closed():
                raise BrowserUnavailable("登录浏览器已关闭，请重新调用 get_login_qrcode。")
            self._touch()
            if auth.is_logged_in(await self.cookies()):
                return {"logged_in": True}
            out: dict[str, Any] = {"logged_in": False}
            if mode == "qrcode":
                await self._login_page.evaluate(r"""() => {
                    const root = document.querySelector('[class*="index_qrcode__wrapper"]');
                    if (!root || !/失效|过期|刷新二维码/.test(root.innerText)) return;
                    const target = [...root.querySelectorAll('button,a,span,div')]
                        .reverse().find(e => /刷新|重试|重新获取/.test(e.textContent)
                            && e.getClientRects().length);
                    if (target) target.click();
                }""")
                out.update(await self._read_qr(self._login_page))
            return out

    async def close_login_page(self) -> None:
        async with self._lock:
            if self._login_page is not None:
                try:
                    await self._login_page.close()
                except Exception:
                    pass
                self._login_page = None

    async def wait_login(self, timeout: float = 120.0, poll: float = 2.0) -> dict[str, str]:
        """轮询等待登录完成，返回登录 cookie；超时返回空字典。"""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            jar = await self.cookies()
            if auth.is_logged_in(jar):
                self._touch()
                return jar
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {}
            self._touch()
            await asyncio.sleep(min(poll, remaining))

    async def logout(self) -> bool:
        """清掉浏览器 profile 里的登录态。返回是否清到了东西。"""
        if self._ctx is None:
            return False
        had = auth.is_logged_in(await self.cookies())
        await self._ctx.clear_cookies()
        return had

    # ------------------------------------------------------------- 取数
    async def fetch_status(self, pid: str | int, uid: str | int | None = None) -> dict[str, Any]:
        """渲染帖子页，取回完整的 status 对象。

        取的是 ``window.SNOWMAN_STATUS`` —— 和 API 同结构，但 ``text`` 不截断。
        调用方一般已经从 API 拿到了 ``user_id``，直接传进来能省掉下面那次查询。
        """
        if not uid:
            uid = await self._author_of(pid)
        async with self._lock:
            page = await self._ensure()
            url = POST_URL.format(uid=uid, pid=pid)
            await page.goto(url, wait_until="domcontentloaded")
            st = await page.evaluate("() => window.SNOWMAN_STATUS || null")
            self._touch()
        if not isinstance(st, dict) or not st.get("id"):
            raise BrowserUnavailable(f"页面里没有帖子数据，可能是帖子已删除或需要登录：{url}")
        return st

    @staticmethod
    async def _author_of(pid: str | int) -> str:
        """查帖子作者的 uid —— 帖子页 URL 少了它就是 404。

        走轻量 API 而不是搜索页：这一步只为拼出 URL，多花的时间要尽可能少。
        """
        from .client import get_client

        data = await get_client().get(
            "/statuses/show.json", {"id": str(pid)}, base="https://api.xueqiu.com",
            referer="https://xueqiu.com/",
        )
        author = (data or {}).get("user_id") or ((data or {}).get("user") or {}).get("id")
        if not author:
            raise BrowserUnavailable(f"查不到帖子 {pid} 的作者，无法拼出帖子页地址。")
        return str(author)


_session: BrowserSession | None = None


def get_session() -> BrowserSession:
    global _session
    if _session is None:
        _session = BrowserSession()
    return _session


async def aclose() -> None:
    """进程退出时收好浏览器。没启动过就是个空操作。"""
    if _session is not None:
        await _session.close()


async def forget_profile() -> bool:
    """删掉整个浏览器 profile，返回是否真的删掉了东西。

    退出登录时必须连它一起清 —— profile 里的登录态是独立于 cookies.json 的一份，
    只删凭证文件的话，下次 browser_login 会「莫名其妙」又自动登录上。
    """
    import shutil

    if _session is not None:
        await _session.close()
    path = profile_dir()
    if not path.exists():
        return False
    shutil.rmtree(path, ignore_errors=True)
    return not path.exists()
