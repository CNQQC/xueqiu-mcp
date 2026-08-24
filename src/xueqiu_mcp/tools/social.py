"""雪球社区（论坛）类工具：个股讨论、新闻公告、热帖、搜帖、帖子详情与评论、用户动态。"""

from __future__ import annotations

from typing import Any

from ..client import APP_API, get_client
from ..formatting import fmt_count, fmt_ts, post_url, strip_html
from ..resolve import resolve_symbol

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


async def search_posts(query: str, count: int = 10, page: int = 1) -> str:
    """全站搜索帖子。"""
    count = max(1, min(int(count), 30))
    data = await get_client().get(
        "/query/v1/search/status.json",
        {"q": query, "count": count, "page": max(1, int(page)), "sort": "time"},
        base=APP_API,
        referer=REFERER,
    )
    posts = data.get("list") or []
    if not posts:
        return f"未搜到与「{query}」相关的帖子。"
    head = f"### 「{query}」相关帖子（第 {page} 页，共约 {data.get('count', '?')} 条）\n"
    return head + "\n\n---\n\n".join(
        _render_post(p, i) for i, p in enumerate(posts, (page - 1) * count + 1)
    )


async def get_post(post_id: str, with_comments: bool = True, comment_count: int = 10) -> str:
    """帖子全文及热门评论。post_id 可从讨论 / 热帖列表里取。"""
    pid = str(post_id).strip().rstrip("/").split("/")[-1]
    if not pid.isdigit():
        return "post_id 需要是帖子的数字 ID。"

    st: dict[str, Any] = await get_client().get(
        "/statuses/show.json", {"id": pid}, base=APP_API, referer=REFERER
    )
    if not st.get("id"):
        return f"未找到 ID 为 {pid} 的帖子。"

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
        try:
            cm = await get_client().get(
                "/statuses/comments.json",
                {"id": pid, "count": max(1, min(int(comment_count), 30)), "page": 1},
                base=APP_API,
                referer=REFERER,
            )
            comments = cm.get("comments") or []
        except Exception:  # 评论区可能被关闭
            comments = []
        if comments:
            out.append(f"\n#### 评论（{len(comments)} 条）")
            for i, c in enumerate(comments, 1):
                cu = (c.get("user") or {}).get("screen_name") or "匿名"
                out.append(
                    f"{i}. **{cu}**（赞{fmt_count(c.get('like_count'))}）"
                    f"　{fmt_ts(c.get('created_at'), '%m-%d %H:%M')}\n"
                    f"   {strip_html(c.get('text'), 200)}"
                )
    return "\n".join(out)


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
