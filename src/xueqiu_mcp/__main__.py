"""命令行入口：``python -m xueqiu_mcp`` 或 ``xueqiu-mcp``。

不带子命令时启动 MCP Server（与旧版行为一致，MCP 客户端配置不用改）。另外提供
登录相关的子命令：

==========================  ================================================
``xueqiu-mcp login``        交互式登录（浏览器扫码 / 粘贴 Cookie / 手机号 + 密码）
``xueqiu-mcp status``       查看当前登录态
``xueqiu-mcp logout``       退出登录并删除本地凭证
==========================  ================================================

密码登录只在这里提供，不做成 MCP 工具 —— 工具入参会进入模型上下文和客户端日志，
密码不该走那条路。这里用 ``getpass`` 读取，不回显。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from getpass import getpass

from . import auth
from .client import get_client
from .server import run


def _read_cookie(arg: str | None) -> str:
    """``--cookie`` 的取值：``-`` 表示从标准输入读，省略则交互式提示。

    支持从标准输入读，是为了别让一长串带登录态的 Cookie 出现在 shell 历史里。
    """
    if arg == "-":
        return sys.stdin.read().strip()
    if arg:
        return arg.strip()
    print("请粘贴完整的 Cookie 串（浏览器开发者工具 → 任意请求 → Cookie 请求头），回车结束：")
    return input("Cookie> ").strip()


async def _finish(jar: dict[str, str], remember: bool) -> int:
    """校验凭证并按需落盘。返回进程退出码。"""
    client = get_client()
    client.set_user_cookie(jar, "命令行登录")
    try:
        user = await auth.fetch_profile(client)
    except auth.LoginError as exc:
        print(f"❌ 凭证校验失败：{exc}", file=sys.stderr)
        return 1
    finally:
        await client.aclose()

    print(f"✅ 登录成功：{user.get('screen_name')}（用户 ID {user.get('id')}）")
    if remember:
        path = auth.save_cookies(jar, {"id": user.get("id"), "screen_name": user.get("screen_name")})
        print(f"   凭证已保存到 {path}（权限 0600）")
    else:
        print("   按要求未落盘，本次校验通过但不会被后续进程读到。")
    return 0


async def _login_via_browser(args: argparse.Namespace) -> int:
    """本机浏览器登录：无头出二维码，或直接开窗口让用户自己登。"""
    from . import browser

    session = browser.get_session()
    try:
        res = await session.open_login("window" if args.window else "qrcode")
    except browser.BrowserUnavailable as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1

    try:
        if res["logged_in"]:
            print("浏览器里已经是登录状态，直接接管。")
        else:
            if res.get("qr_text") or res.get("qr_png"):
                if res.get("qr_text"):
                    print(res["qr_text"])
                print("\n请用雪球 App 扫描上面的二维码。", end="")
                if res.get("qr_png"):
                    print(f"（扫不出来可以打开 {res['qr_png']}）", end="")
                print(f"\n最多等 {args.wait} 秒……")
            elif args.window:
                print(f"已弹出浏览器窗口，请在窗口里完成登录，最多等 {args.wait} 秒……")
            else:
                print(
                    "❌ 没读出二维码（雪球页面结构可能变了）。\n"
                    "   请改用窗口模式：xueqiu-mcp login --browser --window",
                    file=sys.stderr,
                )
                return 1
            if not await session.wait_login(timeout=args.wait):
                print("❌ 等待超时，没检测到登录完成。", file=sys.stderr)
                return 1

        jar = await session.cookies()
    finally:
        await session.close()
    return await _finish(jar, not args.no_save)


async def _cmd_login(args: argparse.Namespace) -> int:
    mode = args.mode
    if mode is None:
        print(
            "选择登录方式：\n"
            "  1) 本机浏览器扫码（推荐，用雪球 App 扫一下就完事）\n"
            "  2) 粘贴浏览器 Cookie（不想装浏览器时用这个）\n"
            "  3) 手机号 + 密码（实验性，可能被风控拦）"
        )
        mode = {"2": "cookie", "3": "password"}.get(input("请输入 1 / 2 / 3 [1]> ").strip(), "browser")

    if mode == "browser":
        return await _login_via_browser(args)

    if mode == "cookie":
        jar = auth.parse_cookie_string(_read_cookie(args.cookie))
        if not jar:
            print("❌ 没解析出任何 Cookie。", file=sys.stderr)
            return 1
        if not auth.is_logged_in(jar):
            print(
                "❌ 这份 Cookie 里没有登录身份（缺少 xq_id_token / xq_is_login）。\n"
                "   请确认浏览器处于登录状态，并复制完整的 Cookie 请求头。",
                file=sys.stderr,
            )
            return 1
    else:
        phone = args.phone or input("手机号> ").strip()
        password = getpass("密码（不回显）> ")
        try:
            jar = await auth.login_with_password(phone, password, args.areacode)
        except auth.LoginError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 1
    return await _finish(jar, not args.no_save)


async def _cmd_status(_args: argparse.Namespace) -> int:
    from .tools import account

    try:
        print(await account.login_status())
    finally:
        await get_client().aclose()
    return 0


async def _cmd_logout(args: argparse.Namespace) -> int:
    from .tools import account

    try:
        print(await account.logout(not args.keep_file))
    finally:
        await get_client().aclose()
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="xueqiu-mcp", description="雪球 MCP Server")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("serve", help="启动 MCP Server（不带子命令时的默认行为）")

    lg = sub.add_parser("login", help="登录雪球账号并保存凭证")
    g = lg.add_mutually_exclusive_group()
    g.add_argument("--cookie", nargs="?", const="", metavar="COOKIE",
                   help="Cookie 串；写 - 表示从标准输入读，省略值则交互式输入")
    g.add_argument("--phone", metavar="手机号", help="手机号密码登录（密码交互式输入，不回显）")
    g.add_argument("--browser", action="store_true",
                   help="本机浏览器登录：终端显示二维码，用雪球 App 扫（需要 Playwright）")
    lg.add_argument("--window", action="store_true",
                   help="配合 --browser：弹出浏览器窗口自己登录，短信 / 微信 / 滑块都能用")
    lg.add_argument("--wait", type=int, default=180, help="配合 --browser：等待登录的秒数，默认 180")
    lg.add_argument("--areacode", default="86", help="手机号区号，默认 86")
    lg.add_argument("--no-save", action="store_true", help="只校验不落盘")

    lo = sub.add_parser("logout", help="退出登录")
    lo.add_argument("--keep-file", action="store_true", help="保留本地凭证文件")

    sub.add_parser("status", help="查看当前登录态")
    return p


def main() -> None:
    argv = sys.argv[1:]
    if not argv or argv[0] in ("serve", "-"):
        run()
        return

    # 子命令是给人看的，httpx 每个请求一行 INFO 日志只会盖住真正的提示
    logging.getLogger("httpx").setLevel(logging.WARNING)

    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.cmd in (None, "serve"):
        run()
        return

    if args.cmd == "login":
        # --cookie 与 --phone 互斥，没给就走交互式菜单
        args.mode = (
            "browser" if args.browser
            else "password" if args.phone
            else "cookie" if args.cookie is not None
            else None
        )
        code = asyncio.run(_cmd_login(args))
    elif args.cmd == "logout":
        code = asyncio.run(_cmd_logout(args))
    else:
        code = asyncio.run(_cmd_status(args))
    sys.exit(code)


if __name__ == "__main__":
    main()
