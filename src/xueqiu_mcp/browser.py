"""本地无头浏览器会话：登录取 Cookie，以及 API 拿不全时的正文兜底。

为什么需要浏览器
----------------
雪球的数据有两处是纯 HTTP 客户端够不着的：

**长文正文被截断**
    ``api.xueqiu.com`` 对长帖匿名返回的 ``description`` 只有一两百字的摘要，
    完整的 ``text`` 要么缺失、要么被 ``truncated`` 标记。实测同一篇帖子：
    匿名 API 给 170 字，浏览器渲染后页面里的 ``window.SNOWMAN_STATUS.text``
    有 6938 字 —— 差了 40 倍。

**主域接口被 WAF 拦**
    ``xueqiu.com`` 主域挂着阿里云 WAF 的 JS 挑战，匿名 httpx 请求拿回来的是挑战
    页而不是 JSON。浏览器天然会执行那段 JS。

所以本模块做两件事：把浏览器当**登录器**（扫码 / 短信 / 微信都由用户在真浏览器里
完成，程序只接手结果 cookie），以及当**兜底渲染器**（只在 API 明显没给全时才开
页面）。日常取数仍然走 :mod:`.client` 的 httpx 快路径。

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
import logging
import os
import time
from pathlib import Path
from typing import Any

from . import auth

log = logging.getLogger("xueqiu_mcp")

HOME_URL = "https://xueqiu.com"
POST_URL = "https://xueqiu.com/{uid}/{pid}"
# 帖子页可以只用 pid 访问，雪球会 302 到带 uid 的规范地址
SHORT_POST_URL = "https://xueqiu.com/statuses/show/{pid}"

# 二维码登录的画布。class 带构建 hash，只能前缀匹配
QRCODE_CANVAS = '[class*="index_qrcode__wrapper"] canvas'
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
        for obj, name in ((self._ctx, "close"), (self._pw, "stop")):
            if obj is not None:
                try:
                    await getattr(obj, name)()
                except Exception:  # 关闭失败没什么可补救的，别把它抛给调用方
                    pass
        self._ctx = self._pw = self._page = None

    async def close(self) -> None:
        async with self._lock:
            await self._teardown()

    # ------------------------------------------------------------- Cookie
    async def cookies(self) -> dict[str, str]:
        if self._ctx is None:
            return {}
        return {c["name"]: c["value"] for c in await self._ctx.cookies()}

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
        """打开登录页，返回 ``{"logged_in", "qr_text", "qr_png"}``。

        ``mode="qrcode"`` 走无头 + 二维码：二维码同时以终端字符和 PNG 两种形式给
        出 —— 字符版方便直接显示在对话里，PNG 版是兜底（模型转述字符画有可能把
        它弄坏，扫不出来时让用户打开图片文件）。

        ``mode="window"`` 弹出真实浏览器窗口，扫码 / 短信 / 微信随用户选，程序只
        负责在旁边轮询等结果。滑块、设备校验这些也一并由浏览器处理掉了。
        """
        headless = mode == "qrcode" and _env_flag("XUEQIU_BROWSER_HEADLESS", True)
        page = await self._ensure(headless=headless)
        await page.goto(HOME_URL, wait_until="domcontentloaded")

        out: dict[str, Any] = {"logged_in": False, "qr_text": None, "qr_png": None}
        if auth.is_logged_in(await self.cookies()):
            out["logged_in"] = True
            return out
        if mode != "qrcode":
            return out

        try:
            await page.evaluate(_SWITCH_QR_JS)
            await page.wait_for_selector(QRCODE_CANVAS, timeout=15000)
            # canvas 是 JS 画的，选择器命中时可能还没画完，给它一拍
            for _ in range(20):
                matrix = await page.evaluate(_READ_QR_JS, QRCODE_CANVAS)
                if matrix:
                    out["qr_text"] = qr_matrix_to_text(matrix)
                    out["qr_png"] = await self._save_qr_png(page)
                    break
                await asyncio.sleep(0.3)
        except Exception as exc:
            log.warning("读取登录二维码失败：%s", exc)
        return out

    async def _save_qr_png(self, page: Any) -> str | None:
        """把二维码 canvas 另存成 PNG，返回路径。失败返回 ``None``。"""
        try:
            data_url = await page.evaluate(
                "(sel) => { const c = document.querySelector(sel);"
                " return c ? c.toDataURL('image/png') : null; }",
                QRCODE_CANVAS,
            )
            if not data_url or "," not in data_url:
                return None
            import base64

            path = profile_dir().parent / "login-qrcode.png"
            path.write_bytes(base64.b64decode(data_url.split(",", 1)[1]))
            os.chmod(path, 0o600)
            return str(path)
        except Exception:
            return None

    async def wait_login(self, timeout: float = 120.0, poll: float = 2.0) -> dict[str, str]:
        """轮询等待登录完成，返回登录 cookie；超时返回空字典。"""
        deadline = time.monotonic() + max(1.0, timeout)
        while time.monotonic() < deadline:
            jar = await self.cookies()
            if auth.is_logged_in(jar):
                self._touch()
                return jar
            await asyncio.sleep(poll)
        return {}

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
        """
        async with self._lock:
            page = await self._ensure()
            url = POST_URL.format(uid=uid, pid=pid) if uid else SHORT_POST_URL.format(pid=pid)
            await page.goto(url, wait_until="domcontentloaded")
            st = await page.evaluate("() => window.SNOWMAN_STATUS || null")
            self._touch()
        if not isinstance(st, dict) or not st.get("id"):
            raise BrowserUnavailable(f"页面里没有帖子数据，可能是帖子已删除或需要登录：{url}")
        return st


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
