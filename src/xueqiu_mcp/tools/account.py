"""账号类工具：登录状态、Cookie 登录、退出登录。

这里只放 Cookie 登录，**不提供密码登录工具**：MCP 工具的入参会完整进入模型上下文
和客户端日志，密码不该走这条路。手机号 + 密码登录放在命令行
（``xueqiu-mcp login``），密码用 ``getpass`` 读取，不回显、不进上下文。
"""

from __future__ import annotations

import os

from .. import auth
from ..client import get_client
from ..formatting import fmt_count

# 登录指引只维护 auth.LOGIN_GUIDE 一份：报错里、工具返回里、Server 说明里给模型看到的
# 必须是同一套步骤，三处各写一版迟早会漂移
LOGIN_HELP = auth.LOGIN_GUIDE


def _render_user(user: dict) -> str:
    name = user.get("screen_name") or "（无昵称）"
    lines = [f"**{name}**（用户 ID {user.get('id')}）"]
    meta = []
    if user.get("followers_count") is not None:
        meta.append(f"粉丝 {fmt_count(user.get('followers_count'))}")
    if user.get("friends_count") is not None:
        meta.append(f"关注 {fmt_count(user.get('friends_count'))}")
    if user.get("status_count") is not None:
        meta.append(f"发帖 {fmt_count(user.get('status_count'))}")
    if meta:
        lines.append("　".join(meta))
    if user.get("description"):
        lines.append(str(user["description"]).strip())
    return "\n".join(lines)


async def login_status() -> str:
    client = get_client()
    if not client.has_user_cookie:
        return (
            "当前为**匿名模式**。\n"
            "行情、K 线、财务、选股器等公开数据都能正常使用；"
            "受风控保护的社区接口和账号相关数据需要登录。\n\n" + LOGIN_HELP
        )

    uid = auth.uid_from_cookies(client.user_cookies)
    head = f"凭证来源：{client.cookie_source}"
    if uid == 0:
        # 常见于只复制了半截 Cookie（漏掉 xq_id_token / xq_is_login）
        head += "\n⚠️ 这份 Cookie 里没有登录身份，可能只复制了一部分，或登录态已被服务端清掉。"

    try:
        user = await auth.fetch_profile(client)
    except auth.LoginError as exc:
        return f"{head}\n\n登录态**已失效**：{exc}\n\n请重新登录。\n\n" + LOGIN_HELP
    return f"已登录 ✅\n{head}\n\n{_render_user(user)}"


async def login_with_cookie(cookie: str, remember: bool = True) -> str:
    jar = auth.parse_cookie_string(cookie)
    if not jar:
        return "没能从这串文本里解析出任何 Cookie。请传形如 `xq_a_token=...; u=...` 的完整 Cookie 串。"
    if not auth.is_logged_in(jar):
        return (
            "这份 Cookie 里没有登录身份（缺少 xq_id_token / xq_is_login），拒绝保存。\n"
            "请确认浏览器里确实处于登录状态，并复制**完整**的 Cookie 请求头，不要只截取其中几项。"
        )

    client = get_client()
    before = client.user_cookie  # 校验失败时回滚，避免把好凭证换成坏的
    before_source = client.cookie_source
    client.set_user_cookie(jar, "本次会话登录")
    try:
        user = await auth.fetch_profile(client)
    except auth.LoginError as exc:
        if before:
            client.set_user_cookie(before, before_source)
        else:
            client.clear_user_cookie()
        return f"这份 Cookie 校验没通过：{exc}\n凭证未保存，已保持原状态。"

    tail = ""
    if remember:
        path = auth.save_cookies(jar, {"id": user.get("id"), "screen_name": user.get("screen_name")})
        client.set_user_cookie(jar, f"凭证文件 {path}")
        tail = f"\n\n凭证已保存到 `{path}`（权限 0600），重启后仍然有效。"
    else:
        tail = "\n\n未落盘，仅本次进程有效。"
    return f"登录成功 ✅\n\n{_render_user(user)}{tail}"


async def logout(forget: bool = True) -> str:
    client = get_client()
    had = client.has_user_cookie
    client.clear_user_cookie()
    removed = auth.clear_cookies() if forget else False

    parts = ["已退出登录，后续请求回到匿名模式。" if had else "当前本来就没有登录凭证。"]
    if forget:
        parts.append(f"凭证文件已删除（{auth.store_path()}）。" if removed else "凭证文件本来就不存在。")
    else:
        parts.append("凭证文件保留，下次启动会自动读回。")
    if os.getenv("XUEQIU_COOKIE", "").strip():
        parts.append(
            "⚠️ 环境变量 XUEQIU_COOKIE 仍然设置着，本进程已忽略它，"
            "但重启后会再次被读取 —— 需要彻底退出请把它一并清掉。"
        )
    return "\n".join(parts)


# ------------------------------------------------------------------ 浏览器登录
def _qr_block(res: dict) -> str:
    """把二维码渲染成一段「请原样输出」的说明。

    字符画被模型改掉一个字符就扫不出来了，所以既明确要求原样输出，又给一份 PNG
    路径兜底 —— 真被转述坏了，用户还能直接打开图片扫。
    """
    lines = [
        "请用**雪球 App** 扫描下面的二维码登录。",
        "⚠️ 下面这段字符画请**原样、完整**输出给用户，不要改写、缩排或转述，否则扫不出来：",
        "",
        "```",
        res["qr_text"],
        "```",
    ]
    if res.get("qr_png"):
        lines.append(f"\n扫不出来就让用户直接打开这张图片：`{res['qr_png']}`")
    return "\n".join(lines)


async def browser_login(mode: str = "qrcode", wait: int = 90, remember: bool = True) -> str:
    """用本机浏览器登录雪球，成功后登录态自动接管给后续所有请求。"""
    from .. import browser

    if mode not in ("qrcode", "window"):
        return "mode 仅支持 qrcode（无头 + 扫码，默认）或 window（弹出窗口自己登录）。"

    session = browser.get_session()
    try:
        res = await session.open_login(mode)
    except browser.BrowserUnavailable as exc:
        return f"无法启动本地浏览器：\n\n{exc}"

    # profile 里还留着上次的登录态，直接接管，不用再扫一次
    if res["logged_in"]:
        await session.adopt_cookies(remember)
        return "浏览器里已经是登录状态，登录态已接管 ✅\n\n" + await login_status()

    if mode == "qrcode" and not res.get("qr_text"):
        return (
            "登录页打开了，但没读出二维码（雪球页面结构可能变了）。\n"
            '请改用窗口模式：browser_login(mode="window")，在弹出的浏览器里自己完成登录。'
        )

    head = _qr_block(res) if mode == "qrcode" else (
        "已弹出浏览器窗口，请在窗口里完成登录（扫码 / 短信验证码 / 微信都行）。"
    )
    jar = await session.wait_login(timeout=max(0, int(wait)))
    if not jar:
        return (
            f"{head}\n\n⏳ 等了 {int(wait)} 秒还没等到登录完成。浏览器仍开着，"
            "用户扫完码后再调用一次 browser_login 就能接管（不会重新扫码）。"
        )

    await session.adopt_cookies(remember)
    tail = "凭证已落盘，重启后仍然有效。" if remember else "仅本次进程有效，未落盘。"
    return f"登录成功 ✅ {tail}\n\n" + await login_status()


async def browser_close() -> str:
    """关掉本机浏览器释放内存。登录态留在 profile 里，不会丢。"""
    from .. import browser

    session = browser.get_session()
    if not session.running:
        return "本地浏览器当前没有运行。"
    await session.close()
    return f"本地浏览器已关闭，登录态保留在 {browser.profile_dir()}，下次要用会自动拉起。"
