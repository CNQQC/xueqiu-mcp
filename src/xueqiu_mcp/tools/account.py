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
