"""本地浏览器层的回归测试。零网络、不启动浏览器，随便跑。

真正需要浏览器的路径（扫码、渲染取正文）在 tests/test_mcp_e2e.py 里联网跑；这里
只覆盖不依赖浏览器就能出错的部分：二维码矩阵怎么变成能扫的字符画、什么时候该判定
正文被截断、以及浏览器缺席时会不会把整个工具拖垮。

每个用例都对应一个真实的判断难点或踩过的坑，注释里写了是什么 —— 别删。
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# 同 test_auth：绝不能让测试碰到用户真实的 ~/.xueqiu-mcp
_TMP = Path(tempfile.mkdtemp())
os.environ["XUEQIU_COOKIE_FILE"] = str(_TMP / "cookies.json")
os.environ["XUEQIU_BROWSER_PROFILE"] = str(_TMP / "browser")
os.environ.pop("XUEQIU_COOKIE", None)

from xueqiu_mcp import browser  # noqa: E402
from xueqiu_mcp.tools import social  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"　{detail}" if detail and not cond else ""))


def t_qrcode_render() -> None:
    """二维码字符画错一个模块就扫不出来，所以逐项验形状与取字。"""
    m = [[True, False], [False, True]]
    art = browser.qr_matrix_to_text(m, quiet=0)
    # 两行压成一行：上黑下白是 ▀，上白下黑是 ▄
    check("上下半块压行", art == "▀▄", repr(art))

    art2 = browser.qr_matrix_to_text([[True]], quiet=1)
    lines = art2.split("\n")
    # 1 模块 + 上下各 1 圈静区 = 3 行 → 压成 2 行；宽度 3 列
    check("静区按圈数补齐", len(lines) == 2 and all(len(x) == 3 for x in lines), repr(art2))
    check("静区是空白", lines[0][0] == " " and lines[-1] == "   ", repr(lines))

    # 行数为奇数时必须补一行白，否则 zip 会把最后一行模块整行吞掉 —— 二维码就废了
    odd = browser.qr_matrix_to_text([[True], [False], [True]], quiet=0)
    check("奇数行不丢最后一行", odd.split("\n") == ["▀", "▀"], repr(odd))

    check("空矩阵不炸", browser.qr_matrix_to_text([]) == "")


def t_availability() -> None:
    os.environ["XUEQIU_BROWSER"] = "0"
    check("XUEQIU_BROWSER=0 时关闭", not browser.available())
    os.environ.pop("XUEQIU_BROWSER")
    # 装没装 playwright 都不该抛错，只返回真假
    check("available() 只返回布尔", isinstance(browser.available(), bool))


def t_profile_dir() -> None:
    check("profile 目录跟随环境变量", browser.profile_dir() == _TMP / "browser")
    os.environ.pop("XUEQIU_BROWSER_PROFILE")
    from xueqiu_mcp import auth

    # 默认值必须落在凭证目录旁边，而不是当前工作目录
    check("默认 profile 在凭证目录下", browser.profile_dir() == auth.store_path().parent / "browser")
    os.environ["XUEQIU_BROWSER_PROFILE"] = str(_TMP / "browser")


def t_forget_profile() -> None:
    path = browser.profile_dir()
    path.mkdir(parents=True, exist_ok=True)
    (path / "Cookies").write_text("x")
    check("profile 能被清掉", asyncio.run(browser.forget_profile()) and not path.exists())
    # 退出登录会无条件调它，目录不存在时必须安静返回 False 而不是抛错
    check("重复清除不报错", asyncio.run(browser.forget_profile()) is False)


def t_truncation_detection() -> None:
    """判早了白开一次浏览器，判晚了用户拿到的就是残缺正文。"""
    check("雪球标了 truncated", social._looks_truncated({"truncated": True, "text": "短"}))
    check(
        "只有摘要没有正文",
        social._looks_truncated({"text": "", "description": "一段摘要"}),
    )
    # 有标题的长文正文却只有一两百字，多半是被砍过
    check(
        "长文正文短得离谱",
        social._looks_truncated({"title": "深度分析", "text": "太长不看" * 10}),
    )
    check(
        "正常长文不误判",
        not social._looks_truncated({"title": "深度分析", "text": "正文" * 500}),
    )
    # 短评论帖没有标题，本来就该短，不能拿去开浏览器
    check("无标题短帖不误判", not social._looks_truncated({"text": "今天大盘不错"}))
    check("空帖子不误判", not social._looks_truncated({}))


def t_degrade_without_browser() -> None:
    """没装 playwright 时，补全必须降级成一句提示，而不是让 get_post 整个失败。"""
    os.environ["XUEQIU_BROWSER"] = "0"
    try:
        st, note = asyncio.run(social._complete_body({"id": 1, "text": "摘要"}))
    finally:
        os.environ.pop("XUEQIU_BROWSER")
    check("降级不抛错且不动原帖", st == {"id": 1, "text": "摘要"})
    check("降级带安装提示", "browser" in note and "playwright" in note, note)


def main() -> None:
    print("== 二维码渲染")
    t_qrcode_render()
    print("== 可用性开关")
    t_availability()
    print("== profile 目录")
    t_profile_dir()
    t_forget_profile()
    print("== 正文截断识别")
    t_truncation_detection()
    print("== 浏览器缺席时的降级")
    t_degrade_without_browser()
    print(f"\n{len(PASS)} 通过，{len(FAIL)} 失败")
    if FAIL:
        print("失败：" + "、".join(FAIL))
        sys.exit(1)


if __name__ == "__main__":
    main()
