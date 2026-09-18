"""雪球社区（论坛）类工具：个股讨论、新闻公告、热帖、搜帖、帖子详情与评论、用户动态。

列表类接口（讨论区、热帖、搜帖）只给一两百字的 ``description`` 摘要，要读全文得用
:func:`get_post` 再取一次 ``/statuses/show.json``。实测匿名下 show.json 对普通帖子
和长文都给得全，所以正文残缺是少数情况 —— 雪球把 ``truncated`` 置位，或者正文整个
为空（付费专栏、仅粉丝可见这类）。真碰上时 :func:`get_post` 会调本地浏览器从页面里
把正文捞回来（见 :mod:`..browser`），没装浏览器支持就照常返回摘要并附一行提示。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..client import APP_API, get_client
from ..formatting import fmt_count, fmt_ts, post_url, strip_html
from ..resolve import resolve_symbol

log = logging.getLogger("xueqiu_mcp")

REFERER = "https://xueqiu.com/"

SORT_MODES = {
    "time": ("最新", "time"),
    "hot": ("热门", "alpha"),
}
TIMELINE_KINDS = {
    "news": ("新闻", "news"),
    "notice": ("公告", "notice"),
}


def _author(st: dict) -> tuple[str, str]:
    u = st.get("user") or {}
    name = u.get("screen_name") or f"用户{st.get('user_id', '')}"
    fans = u.get("followers_count")
    tag = f"{name}（{fmt_count(fans)}粉）" if fans else name
    return name, tag


def _post_body(st: dict, limit: int) -> str:
    """正文优先取 text，长文可能只有 description 摘要。"""
    body = strip_html(st.get("text"), limit)
    if not body:
        body = strip_html(st.get("description"), limit)
    return body or "（无正文，可能是图片或视频帖）"


def _render_post(st: dict, idx: int | None = None, body_limit: int = 220) -> str:
    _, who = _author(st)
    title = strip_html(st.get("title"))
    num = f"{idx}. " if idx else ""
    # 大量帖子没有标题，此时首行直接用「序号 + 作者」，避免留一行空标题
    head = f"**{num}{title}**" if title else f"**{num}{who}**"
    meta = (
        f"{who}　{fmt_ts(st.get('created_at'), '%Y-%m-%d %H:%M')}　"
        f"赞{fmt_count(st.get('like_count'))} 评{fmt_count(st.get('reply_count'))} "
        f"转{fmt_count(st.get('retweet_count'))}"
    ) if title else (
        f"{fmt_ts(st.get('created_at'), '%Y-%m-%d %H:%M')}　"
        f"赞{fmt_count(st.get('like_count'))} 评{fmt_count(st.get('reply_count'))} "
        f"转{fmt_count(st.get('retweet_count'))}"
    )
    if st.get("view_count"):
        meta += f" 阅{fmt_count(st.get('view_count'))}"
    lines = [head, meta, _post_body(st, body_limit)]
    url = post_url(st)
    if url:
        lines.append(f"帖子ID {st.get('id')}　{url}")
    return "\n".join(x for x in lines if x)


async def get_stock_discussions(
    symbol: str, sort: str = "hot", count: int = 10, page: int = 1
) -> str:
    """个股讨论区。sort=hot 取高热度帖，sort=time 取最新帖。"""
    if sort not in SORT_MODES:
        return f"sort 仅支持：{', '.join(SORT_MODES)}"
    sym = await resolve_symbol(symbol)
    label, api_sort = SORT_MODES[sort]
    count = max(1, min(int(count), 30))

    data = await get_client().get(
        "/query/v1/symbol/search/status",
        {
            "symbol": sym, "count": count, "page": max(1, int(page)),
            "source": "user", "sort": api_sort, "comment": 0, "hl": 0, "q": "",
        },
        base=APP_API,
        referer=f"https://xueqiu.com/S/{sym}",
    )
    posts = data.get("list") or []
    if not posts:
        return f"{sym} 暂无讨论内容。"

    head = f"### {sym} 雪球讨论（{label}，第 {page} 页，共约 {data.get('count', '?')} 条）\n"
    return head + "\n\n---\n\n".join(
        _render_post(p, i) for i, p in enumerate(posts, (page - 1) * count + 1)
    )


async def get_stock_news(symbol: str, kind: str = "news", count: int = 10, page: int = 1) -> str:
    """个股新闻或公告流。kind: news 新闻 / notice 公告。"""
    if kind not in TIMELINE_KINDS:
        return f"kind 仅支持：{', '.join(TIMELINE_KINDS)}"
    sym = await resolve_symbol(symbol)
    label, source = TIMELINE_KINDS[kind]
    count = max(1, min(int(count), 30))

    data = await get_client().get(
        "/statuses/stock_timeline.json",
        {"symbol_id": sym, "count": count, "source": source, "page": max(1, int(page))},
        base=APP_API,
        referer=f"https://xueqiu.com/S/{sym}",
    )
    items = data.get("list") or []
    if not items:
        return f"{sym} 暂无{label}。"

    out = [f"### {sym} {label}（第 {page} 页）"]
    for i, it in enumerate(items, (page - 1) * count + 1):
        body = strip_html(it.get("description") or it.get("text"), 180)
        title = strip_html(it.get("title"))
        if not title:
            # 公告流的标题常放在正文里，取首句当标题，正文就不再重复
            title = body.split("\n")[0].removesuffix(" 网页链接").strip() or "（无标题）"
            body = ""
        url = post_url(it)
        out.append(
            f"**{i}. {title}**\n{fmt_ts(it.get('created_at'), '%Y-%m-%d %H:%M')}"
            + (f"　{body}" if body else "")
            + (f"\n{url}" if url else "")
        )
    return "\n\n".join(out)


async def get_hot_posts(count: int = 10) -> str:
    """雪球首页热门讨论。"""
    count = max(1, min(int(count), 30))
    data = await get_client().get(
        "/statuses/hot/listV2.json",
        {"since_id": -1, "max_id": -1, "size": count},
        base=APP_API,
        referer=REFERER,
    )
    items = data.get("items") or []
    posts = [it.get("original_status") or {} for it in items]
    posts = [p for p in posts if p]
    if not posts:
        return "暂无热门帖子。"
    return "### 雪球热门讨论\n" + "\n\n---\n\n".join(
        _render_post(p, i, body_limit=260) for i, p in enumerate(posts, 1)
    )


async def search_users(query: str, count: int = 10, page: int = 1) -> str:
    """按昵称或关键词查找用户，返回可供帖子搜索使用的 user_id。"""
    query = query.strip()
    if not query:
        return "query 不能为空，请输入用户昵称或关键词。"
    count = max(1, min(int(count), 30))
    page = max(1, int(page))
    data = await get_client().get(
        "/query/v1/search/user.json",
        {"q": query, "count": count, "page": page},
        base=APP_API,
        referer=REFERER,
    )
    users = data.get("list") or []
    if not users:
        return f"未搜到与「{query}」相关的用户。"

    out = [f"### 「{query}」相关用户（第 {page} 页，共约 {data.get('count', '?')} 位）"]
    for i, user in enumerate(users, (page - 1) * count + 1):
        uid = user.get("id")
        name = strip_html(user.get("screen_name")) or f"用户{uid}"
        lines = [f"**{i}. {name}**", f"user_id: {uid}　粉丝：{fmt_count(user.get('followers_count'))}"]
        for label, field in (("认证", "verified_description"), ("简介", "description")):
            detail = strip_html(user.get(field), 200)
            if detail:
                lines.append(f"{label}：{detail}")
        lines.append(f"https://xueqiu.com/{uid}")
        out.append("\n".join(lines))
    out.append("请结合昵称、认证和简介确认目标用户，再将 user_id 传给 search_posts 或 get_user_posts。")
    return "\n\n".join(out)


async def search_posts(
    query: str, count: int = 10, page: int = 1, user_id: str | None = None
) -> str:
    """搜索帖子；指定 user_id 时只搜索该用户的帖子。"""
    uid = str(user_id).strip() if user_id is not None else ""
    if uid and (not uid.isascii() or not uid.isdigit() or int(uid) <= 0):
        return "user_id 需要是雪球用户的正整数数字 ID（见帖子链接 xueqiu.com/<user_id>/<post_id>）。"
    count = max(1, min(int(count), 30))
    page = max(1, int(page))
    params = {"q": query, "count": count, "page": page, "sort": "time"}
    path = "/query/v1/search/status.json"
    if uid:
        # 全站搜索会忽略 uid，必须使用用户专属搜索接口才能正确筛选及分页。
        path = "/query/v1/user/status/search.json"
        params["uid"] = uid
    data = await get_client().get(path, params, base=APP_API, referer=REFERER)
    posts = data.get("list") or []
    scope = f"用户 {uid} 的" if uid else ""
    if not posts:
        return f"未搜到{scope}与「{query}」相关的帖子。"
    head = f"### {scope}「{query}」相关帖子（第 {page} 页，共约 {data.get('count', '?')} 条）\n"
    return head + "\n\n---\n\n".join(
        _render_post(p, i) for i, p in enumerate(posts, (page - 1) * count + 1)
    )


def _looks_truncated(st: dict) -> bool:
    """API 给的正文是不是缺斤少两。

    三个信号，命中任一就算：雪球自己打了 ``truncated`` 标；正文整个是空的（长文
    匿名取常常只剩 ``description``）；或者有标题的长文正文却短得不合常理。
    """
    if st.get("truncated"):
        return True
    text = strip_html(st.get("text"))
    if not text:
        return bool(strip_html(st.get("description")))
    return bool(st.get("title")) and len(text) <= 200


async def _complete_body(st: dict) -> tuple[dict, str]:
    """用本地浏览器把截断的正文补全，返回 ``(帖子, 给用户的说明)``。

    补全失败一律降级 —— 摘要总比报错强，说明里写清楚为什么不全就行。
    """
    from .. import browser

    if not browser.available():
        return st, (
            "\n\n> ℹ️ 雪球接口对这篇长文只给了摘要。装上本地浏览器支持"
            "（`pip install -e \".[browser]\"` + `playwright install chromium`）后，"
            "本工具会自动取回完整正文。"
        )
    try:
        full = await browser.get_session().fetch_status(st.get("id"), st.get("user_id"))
    except Exception as exc:  # 浏览器路径失败不该让整个工具失败
        log.warning("浏览器补全帖子 %s 正文失败：%s", st.get("id"), exc)
        return st, f"\n\n> ⚠️ 正文可能不完整，本地浏览器补全失败：{exc}"

    if len(str(full.get("text") or "")) > len(str(st.get("text") or "")):
        return {**st, "text": full["text"]}, "\n\n> ℹ️ 完整正文由本地浏览器取回。"
    return st, ""


async def get_post(
    post_id: str, with_comments: bool = True, comment_count: int = 10, full_text: bool = True
) -> str:
    """帖子全文及热门评论。post_id 可从讨论 / 热帖列表里取。

    ``full_text`` 为真时，正文被截断就自动走本地浏览器补全。
    """
    pid = str(post_id).strip().rstrip("/").split("/")[-1]
    if not pid.isdigit():
        return "post_id 需要是帖子的数字 ID。"

    client = get_client()
    # 正文和评论只依赖 pid，一起发；评论区可能被关掉，失败不影响正文
    tasks = [client.get("/statuses/show.json", {"id": pid}, base=APP_API, referer=REFERER)]
    if with_comments:
        tasks.append(
            client.get(
                "/statuses/comments.json",
                {"id": pid, "count": max(1, min(int(comment_count), 30)), "page": 1},
                base=APP_API,
                referer=REFERER,
            )
        )
    fetched = await asyncio.gather(*tasks, return_exceptions=True)
    if isinstance(fetched[0], BaseException):
        raise fetched[0]
    st: dict[str, Any] = fetched[0]
    if not st.get("id"):
        return f"未找到 ID 为 {pid} 的帖子。"

    note = ""
    if full_text and _looks_truncated(st):
        st, note = await _complete_body(st)

    _, who = _author(st)
    title = strip_html(st.get("title"))
    out = [
        f"### {title or '（无标题）'}",
        f"作者：{who}　发布：{fmt_ts(st.get('created_at'), '%Y-%m-%d %H:%M')}"
        f"　赞{fmt_count(st.get('like_count'))} 评{fmt_count(st.get('reply_count'))}"
        f" 转{fmt_count(st.get('retweet_count'))}",
        post_url(st),
        "",
        strip_html(st.get("text")) or strip_html(st.get("description")) or "（无正文）",
    ]

    if with_comments:
        cm = fetched[1]
        comments = [] if isinstance(cm, BaseException) else (cm.get("comments") or [])
        if comments:
            out.append(f"\n#### 评论（{len(comments)} 条）")
            for i, c in enumerate(comments, 1):
                cu = (c.get("user") or {}).get("screen_name") or "匿名"
                out.append(
                    f"{i}. **{cu}**（赞{fmt_count(c.get('like_count'))}）"
                    f"　{fmt_ts(c.get('created_at'), '%m-%d %H:%M')}\n"
                    f"   {strip_html(c.get('text'), 200)}"
                )
    return "\n".join(out) + note


async def get_user_posts(user_id: str, count: int = 10, page: int = 1) -> str:
    """某位雪球用户的发帖动态。user_id 是雪球用户的数字 ID。"""
    uid = str(user_id).strip().rstrip("/").split("/")[-1]
    if not uid.isdigit():
        return "user_id 需要是雪球用户的数字 ID（可在帖子链接 xueqiu.com/<user_id>/<post_id> 中找到）。"

    data = await get_client().get(
        "/v4/statuses/user_timeline.json",
        {"user_id": uid, "page": max(1, int(page)), "count": max(1, min(int(count), 30))},
        base=APP_API,
        referer=REFERER,
    )
    posts = data.get("statuses") or []
    if not posts:
        return f"用户 {uid} 暂无公开动态。"
    name, _ = _author(posts[0])
    head = f"### {name}（uid {uid}）的动态（第 {page} 页，共 {data.get('count', '?')} 条）\n"
    return head + "\n\n---\n\n".join(_render_post(p, i) for i, p in enumerate(posts, 1))
