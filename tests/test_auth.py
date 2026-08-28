"""登录 / 凭证层回归测试。零网络，随便跑。

每个用例都对应一个真实的判断难点或踩过的坑，注释里写了是什么 —— 别删。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# 必须在 import 之前指向临时文件：绝不能让测试碰到用户真实的 ~/.xueqiu-mcp/cookies.json
_TMP = Path(tempfile.mkdtemp()) / "cookies.json"
os.environ["XUEQIU_COOKIE_FILE"] = str(_TMP)
os.environ.pop("XUEQIU_COOKIE", None)

from xueqiu_mcp import auth  # noqa: E402
from xueqiu_mcp.client import (  # noqa: E402
    NeedCookieError,
    NeedLoginError,
    XueqiuError,
    XueqiuClient,
    raise_business_error,
)

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"　{detail}" if detail and not cond else ""))


def _id_token(uid: int) -> str:
    """伪造一个结构与雪球一致的 xq_id_token（JWT，载荷里带 uid）。"""
    payload = base64.urlsafe_b64encode(json.dumps({"uid": uid, "iss": "uc"}).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def t_cookie_string() -> None:
    jar = auth.parse_cookie_string(" a=1; b = 2 ;;bad;c=3=4 ")
    check("Cookie 串解析容忍空格与空段", jar == {"a": "1", "b": "2", "c": "3=4"}, str(jar))
    # 值里带 = 的很常见（JWT 的 padding），不能按第一个 = 之后全丢
    check("值里的 = 不会被截断", jar["c"] == "3=4")
    check("往返一致", auth.parse_cookie_string(auth.format_cookie_string(jar)) == jar)


def t_login_detection() -> None:
    """匿名访客同样有 xq_a_token，光看它在不在会把匿名误判成已登录。"""
    anon = {"xq_a_token": "abc", "u": "651787924543095", "xq_id_token": _id_token(-1)}
    check("匿名 token 不算已登录", not auth.is_logged_in(anon))
    check("匿名的 uid 为 0", auth.uid_from_cookies(anon) == 0)

    logged = {"xq_a_token": "abc", "xq_id_token": _id_token(1234567)}
    check("登录 token 认得出来", auth.is_logged_in(logged))
    check("能读出用户 ID", auth.uid_from_cookies(logged) == 1234567)

    # JWT 结构变了就靠 xq_is_login 兜底，不能直接判成未登录
    check("JWT 坏掉时靠 xq_is_login 兜底",
          auth.is_logged_in({"xq_id_token": "not-a-jwt", "xq_is_login": "1", "u": "888"}))
    check("兜底路径也能读出 ID",
          auth.uid_from_cookies({"xq_id_token": "x", "xq_is_login": "1", "u": "888"}) == 888)
    check("空 Cookie 不算登录", not auth.is_logged_in({}))


def t_store() -> None:
    jar = {"xq_a_token": "abc", "xq_id_token": _id_token(42)}
    path = auth.save_cookies(jar, {"id": 42, "screen_name": "测试"})
    check("凭证文件落到了指定位置", path == _TMP and path.exists())
    # 凭证等同密码，权限必须是 0600
    check("文件权限 0600", oct(path.stat().st_mode)[-3:] == "600", oct(path.stat().st_mode))
    check("读回来一致", auth.load_saved_cookies() == jar)
    check("uid 一并存下", auth.load_store().get("uid") == 42)

    path.write_text("{ 坏掉的 json", "utf-8")
    # 凭证文件损坏不该让 Server 起不来，退回匿名即可
    check("损坏的凭证文件退化为空而不是抛错", auth.load_saved_cookies() == {})

    check("clear 删得掉", auth.clear_cookies())
    check("重复 clear 不抛错", auth.clear_cookies() is False)


def t_resolve_priority() -> None:
    auth.save_cookies({"xq_a_token": "from-file", "xq_id_token": _id_token(7)})
    cookie, src = auth.resolve_cookie()
    check("没有环境变量时用凭证文件", "from-file" in cookie and "凭证文件" in src, src)

    os.environ["XUEQIU_COOKIE"] = "xq_a_token=from-env"
    try:
        cookie, src = auth.resolve_cookie()
        # 环境变量优先，服务器部署才能用一次性注入的凭证盖住机器上残留的登录态
        check("环境变量优先于凭证文件", cookie == "xq_a_token=from-env" and "环境变量" in src, src)
        cookie, src = auth.resolve_cookie("xq_a_token=explicit")
        check("构造参数优先级最高", cookie == "xq_a_token=explicit" and src == "构造参数")
    finally:
        os.environ.pop("XUEQIU_COOKIE", None)
    auth.clear_cookies()


def t_error_guidance() -> None:
    """未登录的失败必须自带完整登录步骤。

    模型不会自己去猜怎么登录；如果报错只说「需要登录」，它就只会把这句话转述给
    用户，然后双方一起卡住。所以这几个出口都得带上可执行的步骤。
    """
    def raised(code, desc=""):
        try:
            raise_business_error("/x.json", code, desc)
        except XueqiuError as e:
            return e
        return None

    e = raised(60201, "用户id无效")
    check("自选股的『用户id无效』判为未登录", isinstance(e, NeedLoginError))
    check("未登录报错带上了登录步骤", "login_with_cookie" in str(e) and "开发者工具" in str(e))
    check("会话失效（10020）也判为未登录", isinstance(raised("10020", "请重新登录"), NeedLoginError))
    check("靠描述里的『未登录』也能认出来", isinstance(raised(99999, "用户未登录"), NeedLoginError))

    other = raised(400, "参数错误")
    check("与登录无关的错误仍是普通错误", type(other) is XueqiuError, type(other).__name__)
    check("普通错误不塞登录步骤", "login_with_cookie" not in str(other))

    # WAF 挑战页走的是另一条现场，但出路一样，指引也得在
    check("WAF 拦截同样带登录步骤", "login_with_cookie" in str(NeedCookieError("/y.json")))


def t_client_switch() -> None:
    c = XueqiuClient(user_cookie="xq_a_token=t1; xq_id_token=" + _id_token(9))
    check("客户端认到了传入的凭证", c.has_user_cookie and c.cookie_source == "构造参数")
    check("能反查登录用户 ID", auth.uid_from_cookies(c.user_cookies) == 9)

    # 登录/登出必须清缓存：同一 URL 在匿名态和登录态下返回的内容可能不同
    asyncio.run(c.cache.fetch("k", 60, lambda: _fake_load()))
    check("缓存里先有东西", c.cache.stats()["entries"] == 1)
    c.set_user_cookie({"xq_a_token": "t2"}, "本次会话登录")
    check("换凭证会清空缓存", c.cache.stats()["entries"] == 0)
    check("换完凭证来源同步更新", c.cookie_source == "本次会话登录")

    asyncio.run(c.cache.fetch("k", 60, lambda: _fake_load()))
    c.clear_user_cookie()
    check("登出后回到匿名", not c.has_user_cookie and c.cookie_source == "")
    check("登出也会清空缓存", c.cache.stats()["entries"] == 0)
    # 登出后必须重新去拿匿名 token，否则后续请求带着一个已经不存在的身份
    check("登出后 token 被判为过期", c._token_at == 0.0)


async def _fake_load():
    return {"v": 1}, 100


def t_client_default_source() -> None:
    """不传参数时，客户端应当自己从凭证文件里把登录态读起来。"""
    auth.save_cookies({"xq_a_token": "saved", "xq_id_token": _id_token(5)})
    try:
        c = XueqiuClient()
        check("默认构造会读凭证文件", c.has_user_cookie and "凭证文件" in c.cookie_source, c.cookie_source)
    finally:
        auth.clear_cookies()
    c2 = XueqiuClient()
    check("没有凭证时保持匿名", not c2.has_user_cookie and c2.cookie_source == "")


def main() -> None:
    print("== Cookie 串")
    t_cookie_string()
    print("== 登录态识别")
    t_login_detection()
    print("== 凭证文件")
    t_store()
    print("== 凭证优先级")
    t_resolve_priority()
    print("== 未登录时的引导")
    t_error_guidance()
    print("== 客户端切换凭证")
    t_client_switch()
    t_client_default_source()
    print(f"\n{len(PASS)} 通过，{len(FAIL)} 失败")
    if FAIL:
        print("失败：" + "、".join(FAIL))
        sys.exit(1)


if __name__ == "__main__":
    main()
