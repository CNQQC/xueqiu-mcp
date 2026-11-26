"""账号类工具：登录状态、Cookie 登录、退出登录。

这里只放 Cookie 登录，**不提供密码登录工具**：MCP 工具的入参会完整进入模型上下文
和客户端日志，密码不该走这条路。手机号 + 密码登录放在命令行
（``xueqiu-mcp login``），密码用 ``getpass`` 读取，不回显、不进上下文。
"""

from __future__ import annotations

import asyncio
import os

from mcp.types import CallToolResult, ImageContent, TextContent

from .. import auth, browser, login_flow
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


async def login_status(format: str = "markdown") -> str | dict:
    readiness = await browser.browser_status()
    login_help = LOGIN_HELP if readiness["available"] else (
        "可以在服务主机运行 `xueqiu-mcp login --cookie` 登录。要启用扫码，请先处理上方的浏览器提示。"
    )
    state = await _login_state(format, login_help)
    storage = auth.storage_status()
    if format == "json":
        return {**state, "browser": readiness, "credential_storage": storage}
    location = storage["directory"] or "未知"
    writable = "可写" if storage["writable"] else "不可写"
    fallback = ""
    if storage["location"] in ("home_fallback", "temporary"):
        fallback = f"；首选目录 {storage['preferred_directory']} 无法写入"
    if storage["location"] == "temporary":
        fallback += "；临时文件可能被系统清理"
    return (f"扫码浏览器：{'可用' if readiness['available'] else '不可用'}（{readiness['reason']}）\n"
            f"凭证目录：{location}（{writable}{fallback}）\n\n{state}")


async def _login_state(format: str, login_help: str) -> str | dict:
    client = get_client()
    if not client.has_user_cookie:
        attempt = login_flow.get_flow().attempt
        if attempt and attempt.state == "pending" and not attempt.done.is_set():
            state = login_flow.get_flow().lookup(attempt.token)
            if state["state"] == "pending":
                if format == "json":
                    return {"state": "pending"}
                link = f"\n[打开雪球登录页]({attempt.login_url})" if attempt.login_url else ""
                return f"正在等待登录。{attempt.message}{link}\n扫码后凭证会自动接管，无需再次取码。"
        if format == "json":
            return {"state": "anonymous"}
        return (
            "当前为**匿名模式**。\n"
            "行情、K 线、财务、选股器等公开数据都能正常使用；"
            "受风控保护的社区接口和账号相关数据需要登录。\n\n" + login_help
        )

    uid = auth.uid_from_cookies(client.user_cookies)
    head = f"凭证来源：{client.cookie_source}"
    if uid == 0:
        # 常见于只复制了半截 Cookie（漏掉 xq_id_token / xq_is_login）
        head += "\n⚠️ 这份 Cookie 里没有登录身份，可能只复制了一部分，或登录态已被服务端清掉。"

    try:
        user = await auth.fetch_profile(client)
    except auth.LoginError as exc:
        if format == "json":
            return {"state": "invalid", "reason": str(exc)}
        return f"{head}\n\n登录态**已失效**：{exc}\n\n请重新登录。\n\n" + login_help
    if format == "json":
        return {"state": "authenticated", "user": {k: user.get(k) for k in ("id", "screen_name", "followers_count", "friends_count", "status_count")}}
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

    await login_flow.get_flow().cancel()
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
        try:
            path = auth.save_cookies(jar, {"id": user.get("id"), "screen_name": user.get("screen_name")})
        except auth.LoginError as exc:
            return f"登录成功，仅本次会话有效。{exc}"
        client.set_user_cookie(jar, f"凭证文件 {path}")
        tail = f"\n\n凭证已保存到 `{path}`（权限 0600）。"
    else:
        tail = "\n\n未落盘，仅本次进程有效。"
    return f"登录成功 ✅\n\n{_render_user(user)}{tail}"


async def logout(forget: bool = True) -> str:
    # 先停后台扫码，避免退出后 watcher 又把同一份 Cookie 接管回来。
    await login_flow.aclose()
    client = get_client()
    had = client.has_user_cookie
    client.clear_user_cookie()
    removed = auth.clear_cookies() if forget else False

    parts = ["已退出登录，后续请求回到匿名模式。" if had else "当前本来就没有登录凭证。"]
    if forget:
        parts.append(f"凭证文件已删除（{auth.store_path()}）。" if removed else "凭证文件本来就不存在。")
        # 浏览器 profile 是独立的第二份登录态，不一起清的话下次扫码会「自动登录」
        if await browser.forget_profile():
            parts.append("本地浏览器的登录态也一并清除了。")
    else:
        parts.append("凭证文件保留，下次启动会自动读回。")
    if os.getenv("XUEQIU_COOKIE", "").strip():
        parts.append(
            "⚠️ 环境变量 XUEQIU_COOKIE 仍然设置着，本进程已忽略它，"
            "但重启后会再次被读取 —— 需要彻底退出请把它一并清掉。"
        )
    return "\n".join(parts)


# ------------------------------------------------------------------ 浏览器登录
def _login_result(attempt: login_flow.LoginAttempt) -> CallToolResult:
    lines = [attempt.message]
    state = attempt.public_state()
    if attempt.state == "pending":
        if attempt.login_url:
            lines.append(f"[打开雪球登录页]({attempt.login_url})")
        if attempt.link_note:
            lines.append(attempt.link_note)
        lines.append(
            f"链接剩余 {state['expires_in']} 秒有效。请把链接和二维码图片直接展示给用户。"
            "后台会自动接管登录态，扫码后可调用 login_status 确认；"
            "二维码过期时打开链接查看最新图片，或调用 get_login_qrcode(refresh=true)。"
        )
    content = [TextContent(type="text", text="\n\n".join(lines))]
    if attempt.state == "pending" and attempt.qr_data:
        content.append(ImageContent(type="image", mimeType="image/png",
                                    data=attempt.qr_data.split(",", 1)[1]))
    return CallToolResult(
        content=content,
        structuredContent={
            "state": attempt.state, "message": attempt.message,
            "login_url": attempt.login_url or None,
            "login_path": attempt.path if attempt.state == "pending" else None,
            "expires_at": state["expires_at"], "expires_in": state["expires_in"],
        },
        isError=attempt.state == "error",
    )


async def get_login_qrcode(remember: bool = True, refresh: bool = False) -> CallToolResult:
    """立即交付扫码图片和临时链接，后台等待登录。"""
    return await browser_login(remember=remember, refresh=refresh)


async def browser_login(mode: str = "qrcode", wait: int = 0, remember: bool = True,
                        refresh: bool = False) -> CallToolResult:
    """默认立即返回；显式 wait 仅供已经展示过二维码的客户端短暂等待。"""
    from .. import browser

    if mode not in ("qrcode", "window"):
        return CallToolResult(content=[TextContent(type="text", text=
            "mode 仅支持 qrcode（无头 + 扫码，默认）或 window（弹出窗口自己登录）。")])
    try:
        attempt = await login_flow.get_flow().start(mode, remember, refresh)
    except browser.BrowserUnavailable as exc:
        return CallToolResult(content=[TextContent(type="text", text=
            f"无法启动本地浏览器：\n\n{exc}")], isError=True)
    if wait > 0 and attempt.state == "pending":
        try:
            await asyncio.wait_for(attempt.done.wait(), min(int(wait), 30))
        except asyncio.TimeoutError:
            pass
    return _login_result(attempt)


async def browser_close() -> str:
    """关掉本机浏览器释放内存。登录态留在 profile 里，不会丢。"""
    from .. import browser

    await login_flow.aclose()
    session = browser.get_session()
    if not session.running:
        return "本地浏览器当前没有运行。"
    await session.close()
    return f"本地浏览器已关闭，登录态保留在 {browser.profile_dir()}，下次要用会自动拉起。"
