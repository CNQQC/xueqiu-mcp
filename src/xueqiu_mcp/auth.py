"""登录与凭证管理。

雪球的数据分三档：

1. **匿名可取**：行情、K 线、财务、选股器等，靠自动获取的匿名 token 就够（见
   :mod:`.client`），本模块不参与；
2. **需要登录态**：``xueqiu.com`` 主域上的一部分社区接口被阿里云 WAF 的 JS 挑战
   拦截，匿名请求拿回来的是挑战页而不是 JSON；
3. **属于账号本身**：用户资料详情、自选股分组等，没有登录身份根本无从谈起。

本模块负责第 2、3 档所需要的那份登录凭证：怎么拿到、存在哪、怎么校验、怎么撤销。

拿到凭证有两条路
----------------
**粘贴浏览器 Cookie（推荐）**
    在浏览器里登录 xueqiu.com，从开发者工具复制 Cookie 串。这条路最稳：登录时的
    滑块、短信、设备校验全由浏览器完成，本程序只接手结果。

**手机号 + 密码（实验性）**
    直接调雪球的 ``/snowman/login``。雪球对该接口有风控，新设备、异地、频繁尝试
    都可能被拦成一个笼统的「遇到错误，请刷新页面后重试」。失败时本模块会把雪球的
    原话透出来，并提示改用 Cookie 方式，不做无谓的重试。

凭证存哪
--------
默认 ``~/.xueqiu-mcp/cookies.json``，目录 0700、文件 0600，写入走临时文件 +
``os.replace``，避免写到一半进程挂掉留下半个文件。可用 ``XUEQIU_COOKIE_FILE``
指定别的位置。

优先级：构造参数 > ``XUEQIU_COOKIE`` 环境变量 > 凭证文件。环境变量优先于文件，
是为了让服务器部署能用一次性注入的凭证覆盖机器上残留的登录态。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("xueqiu_mcp")

# 登录态相关的 cookie。保存时全量落盘（WAF 有时会认 acw_tc 之类的辅助 cookie），
# 这里只用于判断「这串 cookie 到底有没有登录身份」。
AUTH_COOKIE_KEYS = ("xq_a_token", "xqat", "xq_r_token", "xq_id_token", "xq_is_login", "u")

STORE_VERSION = 1

# 面向「正在使用本 MCP 的大模型」写的登录指引。凡是因为没登录而失败的地方都把它原样
# 带上 —— 模型不会主动去猜怎么登录，得在它撞墙的那一刻就把完整步骤递到手里，
# 否则它只会回一句「该接口需要登录」然后停在那，用户还是不知道该干什么。
LOGIN_GUIDE = """需要登录雪球账号才能继续。请**主动把下面的步骤转述给用户**，
不要只告诉用户「需要登录」就停下：

**最省事的办法**：直接调用 `get_login_qrcode` 工具 —— 服务端拉起浏览器后立即返回
二维码图片和临时登录链接。请直接把图片和链接展示给用户，不要先等待扫码。用户用
雪球 App 扫码并确认后，后台自动接管并保存凭证，再用 `login_status` 确认即可。
链接 5 分钟有效，网页会更新二维码和登录结果；需要短信或滑块时可调用
`browser_login(mode="window")` 在服务所在机器的浏览器中完成。该工具返回「无法启动
本地浏览器」时，说明这台机器没装浏览器支持，再按下面的 Cookie 步骤来：

1. 请用户在浏览器里登录 xueqiu.com；
2. 按 F12 打开开发者工具 → 切到 Network（网络）标签 → 刷新页面 →
   点任意一条发往 xueqiu.com 的请求 → 在 Request Headers 里找到 `Cookie:` 那一行
   → 复制**整行**内容（很长，形如 `xq_a_token=...; xq_id_token=...; u=...`）；
3. 用户把这串 Cookie 发给你之后，调用 `login_with_cookie` 工具传进去，登录即完成。
   凭证会以 0600 权限保存在本地，之后重启也不用再登录。

用户如果不方便动浏览器，也可以在运行本服务的机器上执行 `xueqiu-mcp login`
（支持手机号 + 密码），登录完成后重试即可。

请顺带提醒用户：Cookie 等同于账号凭证，只应在自己信任的环境里提供。"""


class LoginError(RuntimeError):
    """登录失败。

    不继承 :class:`.client.XueqiuError`：本模块被 :mod:`.client` 导入，反向导入会
    形成环。需要用到 client 里的常量时一律在函数体内延迟导入。
    """


# --------------------------------------------------------------------- Cookie 串
def parse_cookie_string(raw: str) -> dict[str, str]:
    """``"a=1; b=2"`` → ``{"a": "1", "b": "2"}``。"""
    jar: dict[str, str] = {}
    for piece in (raw or "").split(";"):
        piece = piece.strip()
        if not piece or "=" not in piece:
            continue
        k, _, v = piece.partition("=")
        jar[k.strip()] = v.strip()
    return jar


def format_cookie_string(jar: dict[str, str]) -> str:
    """``{"a": "1"}`` → ``"a=1"``。"""
    return "; ".join(f"{k}={v}" for k, v in jar.items() if k)


def uid_from_cookies(jar: dict[str, str]) -> int:
    """从 cookie 里读出登录用户 ID，未登录返回 0。

    ``xq_id_token`` 是一个 JWT，载荷里的 ``uid`` 匿名时是 ``-1``、登录后是真实用户
    ID。这是唯一一个不用发请求就能区分「匿名 token」和「登录 token」的信号 ——
    ``xq_a_token`` 匿名访客同样会有，光看它在不在会把匿名当成已登录。
    """
    token = jar.get("xq_id_token") or ""
    parts = token.split(".")
    if len(parts) == 3:
        try:
            payload = parts[1]
            payload += "=" * (-len(payload) % 4)  # base64url 补齐 padding
            uid = json.loads(base64.urlsafe_b64decode(payload)).get("uid")
            if isinstance(uid, int) and uid > 0:
                return uid
        except (ValueError, TypeError):
            pass
    # 兜底：雪球换掉 JWT 时还能靠这两个字段判断
    if str(jar.get("xq_is_login", "")).strip() not in ("", "0", "false"):
        try:
            return int(jar.get("u") or jar.get("uid") or 0)
        except ValueError:
            return -1  # 有登录标记但读不出 ID，仍然算已登录
    return 0


def is_logged_in(jar: dict[str, str]) -> bool:
    return uid_from_cookies(jar) != 0


# --------------------------------------------------------------------- 凭证文件
def store_path() -> Path:
    custom = os.getenv("XUEQIU_COOKIE_FILE", "").strip()
    if custom:
        return Path(custom).expanduser()
    return Path.home() / ".xueqiu-mcp" / "cookies.json"


def load_store() -> dict[str, Any]:
    """读取凭证文件。文件不存在或损坏都返回 ``{}``，不抛错。

    凭证文件坏掉不该让整个 MCP Server 起不来 —— 大不了退回匿名模式。
    """
    path = store_path()
    try:
        data = json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("凭证文件 %s 读取失败（%s），按未登录处理", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def load_saved_cookies() -> dict[str, str]:
    jar = load_store().get("cookies")
    return jar if isinstance(jar, dict) else {}


def save_cookies(jar: dict[str, str], user: dict[str, Any] | None = None) -> Path:
    """把凭证写入文件（0600），返回文件路径。"""
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {
        "version": STORE_VERSION,
        "saved_at": int(time.time()),
        "uid": uid_from_cookies(jar),
        "user": user or {},
        "cookies": jar,
    }
    # 临时文件 + 原子替换：中途失败不会留下半份凭证覆盖掉原来能用的那份
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path


def clear_cookies() -> bool:
    """删除凭证文件。返回是否真的删掉了东西。"""
    path = store_path()
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:  # pragma: no cover
        raise LoginError(f"无法删除凭证文件 {path}：{exc}") from exc


# --------------------------------------------------------------------- 凭证来源
def resolve_cookie(explicit: str | None = None) -> tuple[str, str]:
    """按优先级挑一份凭证，返回 ``(cookie 串, 来源描述)``。都没有则返回 ``("", "")``。"""
    if explicit:
        return explicit, "构造参数"
    env = os.getenv("XUEQIU_COOKIE", "").strip()
    if env:
        return env, "环境变量 XUEQIU_COOKIE"
    saved = load_saved_cookies()
    if saved:
        return format_cookie_string(saved), f"凭证文件 {store_path()}"
    return "", ""


# --------------------------------------------------------------------- 登录动作
async def fetch_profile(client: Any) -> dict[str, Any]:
    """取当前登录用户的资料。未登录时抛 :class:`LoginError`。

    走 ``api.xueqiu.com`` 而不是主域：主域的同名接口在 WAF 后面，未登录时返回的是
    挑战页 HTML，反而看不出「未登录」这个真正的原因。
    """
    from .client import APP_API, XueqiuError  # 延迟导入，避免与 client 形成导入环

    try:
        data = await client.get("/user/show.json", base=APP_API, cache=False)
    except XueqiuError as exc:
        raise LoginError(f"登录态校验失败：{exc}") from exc
    user = data.get("user") if isinstance(data, dict) and isinstance(data.get("user"), dict) else data
    if not isinstance(user, dict) or not user.get("id"):
        raise LoginError("雪球没有返回用户信息，登录态可能已失效。")
    return user


async def login_with_password(
    telephone: str, password: str, areacode: str = "86", timeout: float = 15.0
) -> dict[str, str]:
    """手机号 + 密码登录，成功返回 cookie 字典。

    实验性：雪球对该接口有风控，失败原因常常只是一句笼统的提示。这里不做重试 ——
    密码类接口连续失败很容易触发账号级的锁定，重试只会让情况更糟。
    """
    import httpx

    from .client import BASE_HEADERS, MAIN_API  # 延迟导入，理由同上

    if not telephone.strip() or not password:
        raise LoginError("手机号和密码都不能为空。")

    async with httpx.AsyncClient(
        timeout=timeout, headers=BASE_HEADERS, follow_redirects=True
    ) as client:
        # 先过一次行情页：拿到匿名 token 和 acw_tc，否则登录接口直接判为异常请求
        await client.get(MAIN_API + "/hq", headers={"Accept": "text/html,*/*"})
        resp = await client.post(
            MAIN_API + "/snowman/login",
            data={
                "areacode": areacode,
                "telephone": telephone.strip(),
                "password": password,
                "remember_me": "true",
            },
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        try:
            data = resp.json()
        except ValueError:
            raise LoginError(
                "雪球返回了非 JSON 内容，通常意味着请求被风控拦截。"
                "请改用 Cookie 登录（浏览器登录后复制 Cookie）。"
            ) from None

        if isinstance(data, dict) and data.get("error_code") not in (None, 0, "0"):
            desc = data.get("error_description") or data.get("error_msg") or data.get("error_code")
            raise LoginError(
                f"雪球拒绝了这次登录：{desc}（error_code={data.get('error_code')}）\n"
                "若确认手机号和密码无误，多半是被风控拦下了 —— 请改用 Cookie 登录。"
            )

        jar = dict(client.cookies)
        if not is_logged_in(jar):
            raise LoginError(
                "登录请求没报错，但没拿到登录态 cookie，可能需要在浏览器里完成"
                "滑块或短信验证。请改用 Cookie 登录。"
            )
        return jar
