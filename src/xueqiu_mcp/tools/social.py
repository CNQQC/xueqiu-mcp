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
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from ..client import APP_API, XueqiuError, get_client
from ..formatting import CN_TZ, fmt_count, fmt_ts, post_url, strip_html
from ..posts import content_hash, filter_posts, post_record, search_text
from ..results import pagination, page_text, records
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


def _user_id(value: str) -> str:
    """接受数字 ID 或雪球主页/帖子链接，拒绝昵称及其他站点链接。"""
    value = str(value).strip()
    if value.startswith(("https://", "http://")):
        url = urlparse(value)
        if url.hostname not in ("xueqiu.com", "www.xueqiu.com"):
            return ""
        value = url.path.strip("/").split("/")[0]
    return str(int(value)) if value.isascii() and value.isdigit() and int(value) > 0 else ""


def _is_author(st: dict, uid: str) -> bool:
    # 不信任上游是否真的应用了 uid；未知作者或互相矛盾的作者字段也不能当作一手来源。
    ids = [str(v) for v in (st.get("user_id"), (st.get("user") or {}).get("id")) if v is not None]
    return bool(ids) and all(v == uid for v in ids)


def _excerpt(text: str, limit: int | None, query: str = "") -> str:
    if not limit or len(text) <= limit:
        return text
    # 不再一律截开头：长文命中词经常在几千字之后。
    terms = [query] + query.split() if query else []
    positions = [text.lower().find(term.lower()) for term in terms if term]
    matches = [p for p in positions if p >= 0]
    start = max(0, min(matches) - limit // 3) if matches else 0
    return ("…" if start else "") + text[start:start + limit] + ("…" if start + limit < len(text) else "")


def _edit_status(st: dict) -> str:
    """雪球用毫秒时间戳 edited_at 记录编辑，editable 和 canEdit 表示权限。"""
    try:
        millis = int(str(st.get("edited_at")))
        if millis > 0:
            edited = datetime.fromtimestamp(millis / 1000, CN_TZ)
            return f"编辑状态：已编辑（{edited:%Y-%m-%d %H:%M:%S}）"
    except (ValueError, OverflowError, OSError):
        pass
    return "编辑状态：未知"


def _post_body(st: dict, limit: int | None, query: str = "") -> str:
    """将当前作者文字、内联引用、被转发帖分开，避免把他人的观点算给当前作者。"""
    body = strip_html(st.get("text")) or strip_html(st.get("description"))
    own, *quoted = re.split(r"//\s*@", body, maxsplit=1)
    parts = ["本条作者文字：\n" + _excerpt(own.strip(), limit, query)] if own.strip() else []
    if quoted:
        parts.append("引用文字（不代表本条作者的新观点）：\n" + _excerpt("//@" + quoted[0], limit, query))
    original = st.get("retweeted_status") or {}
    if original:
        name, _ = _author(original)
        text = strip_html(original.get("text")) or strip_html(original.get("description"))
        parts.append(f"被转发帖（作者：{name}）：\n{_edit_status(original)}\n{_excerpt(text, limit, query)}\n{post_url(original)}")
    elif st.get("retweet_status_id"):
        parts.append(f"被转发帖 ID：{st['retweet_status_id']}（正文未返回，可用 get_post 查阅）")
    return "\n\n".join(parts) or "（无正文，可能是图片或视频帖）"


def _render_post(st: dict, idx: int | None = None, body_limit: int = 220, query: str = "") -> str:
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
    meta += f"　{_edit_status(st)}"
    uid = st.get("user_id") or (st.get("user") or {}).get("id")
    kind = "转发/回复，需区分本人文字与引用" if st.get("retweet_status_id") or st.get("retweeted_status") else "本人发布（无转发标记，不保证内容原创）"
    lines = [head, meta, f"作者 user_id: {uid or '未知'}　类型：{kind}", _post_body(st, body_limit, query)]
    url = post_url(st)
    if url:
        lines.append(f"帖子ID {st.get('id')}　{url}")
    return "\n".join(x for x in lines if x)


async def _fetch_post_page(
    path: str, params: dict, key: str, size: int, page: int, referer: str,
    fixed_batch: int | None = None,
) -> tuple[list[dict], dict, str]:
    """将工具页映射到不超过 20 条的上游页。短页保留自己的位置，避免下页重读。"""
    batch_size = fixed_batch or min(size, 20)
    start = (page - 1) * size
    stop = start + size
    posts = []
    seen = set()
    data = {}
    note = ""
    more = None
    retry = False
    client = get_client()
    for batch_index in range(start // batch_size, (stop - 1) // batch_size + 1):
        upstream_page = batch_index + 1
        try:
            response = await client.get(
                path, {**params, "count": batch_size, "page": upstream_page},
                base=APP_API, referer=referer,
            )
        except (XueqiuError, httpx.RequestError) as exc:
            if not posts:
                raise
            retry = True
            note = f"上游第 {upstream_page} 页读取失败，已保留取到的帖子。{exc}"
            break
        if isinstance(response.get("data"), list) and isinstance(response.get("meta"), dict):
            response = {**response["meta"], key: response["data"]}
        if response.get("page") not in (None, upstream_page):
            note = f"雪球未确认上游第 {upstream_page} 页，无法确认后续覆盖范围。"
            retry = True
            break
        data = response
        batch = data.get(key) or []
        batch_start = batch_index * batch_size
        for post in batch[max(0, start - batch_start):min(batch_size, stop - batch_start)]:
            pid = post.get("id")
            if pid is not None:
                if str(pid) in seen:
                    continue
                seen.add(str(pid))
            posts.append(post)
        last_page = data.get("maxPage")
        if isinstance(last_page, int):
            more = upstream_page < last_page or stop < batch_start + len(batch)
            if upstream_page >= last_page:
                break
        elif len(batch) < batch_size:
            more = True if stop < batch_start + len(batch) else None
            if not batch:
                break
        else:
            more = True if stop < batch_start + len(batch) else None
    if len(posts) < size and not note:
        note = "雪球在本页范围内返回的不同帖子数少于请求数。"
    total = data.get("total") if key == "statuses" else data.get("count")
    meta = pagination(size, len(posts), 30, None if retry else more,
                      None if retry or more is False else page + 1, page, total=total)
    if retry:
        meta["retry_page"] = page
    meta["upstream_max_page"] = data.get("maxPage")
    return posts, meta, note


async def get_stock_discussions(
    symbol: str, sort: str = "hot", count: int = 10, page: int = 1, format: str = "markdown",
) -> str | dict:
    """个股讨论区。sort=hot 取高热度帖，sort=time 取最新帖。"""
    if sort not in SORT_MODES:
        return f"sort 仅支持：{', '.join(SORT_MODES)}"
    sym = await resolve_symbol(symbol)
    label, api_sort = SORT_MODES[sort]
    size = max(1, min(int(count), 30))
    page = max(1, int(page))

    posts, meta, note = await _fetch_post_page(
        "/query/v1/symbol/search/status",
        {"symbol": sym, "source": "user", "sort": api_sort, "comment": 0, "hl": 0, "q": ""},
        "list", size, page, f"https://xueqiu.com/S/{sym}",
    )
    meta["requested"] = count
    if format == "json":
        return {"symbol": sym, "sort": sort, "pagination": meta, "items": [post_record(p) for p in posts], "note": note}
    head = (
        f"### {sym} 雪球讨论（{label}，第 {page} 页，请求 {count} 条，实际返回 {len(posts)} 条，"
        f"共约 {meta['total'] if meta['total'] is not None else '?'} 条）\n{page_text(meta)}"
    )
    if note:
        head += "\n" + note
    body = "\n\n---\n\n".join(
        _render_post(p, i) for i, p in enumerate(posts, (page - 1) * size + 1)
    )
    return head + "\n" + (body or "本页暂无讨论内容。")


async def get_stock_news(symbol: str, kind: str = "news", count: int = 10, page: int = 1, format: str = "markdown") -> str | dict:
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
    if format == "json":
        return records([post_record(it) for it in items], count, 30, page=page, pageable=True,
                       total=data.get("count"), symbol=sym, kind=kind)
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
            f"**{i}. {title}**\n{fmt_ts(it.get('created_at'), '%Y-%m-%d %H:%M')}　{_edit_status(it)}"
            + (f"　{body}" if body else "")
            + (f"\n{url}" if url else "")
        )
    return "\n\n".join(out)


async def get_hot_posts(count: int = 10, page: int = 1, format: str = "markdown") -> str | dict:
    """page=1 取首页，后续传上次返回的 next_page 游标。"""
    if page < 1:
        return "page 需要是 1 或上次返回的 next_page 游标。"
    size = max(1, min(int(count), 30))
    cursor = -1 if page == 1 else page
    posts = []
    seen = set()
    note = ""
    more = None
    next_page = None
    # 首页中有推荐卡片，预取少量余量。只消费到最后一条入选帖。
    for _ in range(3):
        try:
            data = await get_client().get("/statuses/hot/listV2.json", {
                "since_id": -1, "max_id": cursor, "size": min(30, size - len(posts) + 5),
            }, base=APP_API, referer=REFERER)
        except (XueqiuError, httpx.RequestError) as exc:
            if not posts:
                raise
            note = f"后续热帖读取失败。{exc}"
            next_page, more = cursor, None
            break
        cards = data.get("items") or []
        for index, card in enumerate(cards):
            if cursor > 0 and card.get("id") == cursor:
                continue
            post = card.get("original_status") or {}
            pid = post.get("id")
            if pid is None or pid in seen:
                continue
            seen.add(pid)
            posts.append(post)
            if len(posts) == size and index + 1 < len(cards):
                next_page, more = card.get("id"), True
                break
        else:
            next_page = data.get("next_max_id")
            if next_page in (0, -1) or not cards:
                more, next_page = False, None
            elif not isinstance(next_page, int) or (cursor > 0 and next_page >= cursor):
                more, next_page = None, None
                note = "雪球未提供可继续推进的热帖游标。"
            else:
                more = True
        if len(posts) >= size or next_page is None:
            break
        cursor = next_page
    if len(posts) < size:
        note = (note + " 本次热帖流可用的不同帖子少于请求数。").strip()
    meta = pagination(count, len(posts), 30, more, next_page, page)
    if format == "json":
        return {"pagination": meta, "items": [post_record(p) for p in posts], "note": note}
    head = "### 雪球热门讨论\n" + page_text(meta)
    if note:
        head += "\n" + note
    return head + "\n" + "\n\n---\n\n".join(_render_post(p, i, body_limit=260) for i, p in enumerate(posts, 1))


async def search_users(query: str, count: int = 10, page: int = 1, format: str = "markdown") -> str | dict:
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
    if format == "json":
        items = [{"id": str(u["id"]), "name": strip_html(u.get("screen_name")), "followers_count": u.get("followers_count"),
                  "description": strip_html(u.get("description")), "verified_description": strip_html(u.get("verified_description"))}
                 for u in users if u.get("id") is not None]
        return records(items, count, 30, page=page, pageable=True, total=data.get("count"), query=query)
    if not users:
        return f"未搜到与「{query}」相关的用户。"

    out = [f"### 「{query}」相关用户（第 {page} 页，共约 {data.get('count', '?')} 位）"]
    if len(users) > 1:
        out.append("有多个候选账号：简称不一定等于完整昵称，不要直接选第一项或仅凭粉丝数认定身份。请结合用户提供的完整昵称、主页及简介核对。")
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


async def _read_filtered(path, params, key, size, page, uid, filters):
    """Scan bounded upstream pages. The token resumes at the first unconsumed row."""
    filter_posts([], *filters)  # validate before issuing any requests
    batch = 20 if key == "statuses" else 10
    token = re.fullmatch(r"p(\d+)r(\d+)", str(page))
    if token:
        source, offset = map(int, token.groups())
        if source < 1 or offset >= batch:
            raise ValueError("筛选游标无效，请使用返回的 next_page。")
    else:
        if not str(page).isdecimal():
            raise ValueError("page 需要页码或筛选结果返回的 next_page。")
        source, offset = divmod((max(1, int(page)) - 1) * size, batch)
        source += 1
    selected, seen, notes = [], set(), []
    more = None
    next_page = None
    last_source = None
    scanned = 0
    retry_page = None
    for _ in range(8):
        try:
            rows, upstream, note = await _fetch_post_page(path, params, key, batch, source, REFERER, fixed_batch=batch)
        except (XueqiuError, httpx.RequestError) as exc:
            if not scanned:
                raise
            more, next_page, retry_page = None, None, f"p{source}r{offset}"
            notes.append(f"上游第 {source} 页读取失败。{exc}")
            break
        if upstream.get("retry_page"):
            more, next_page, retry_page = None, None, f"p{source}r{offset}"
            notes.append(note)
            break
        last_source = upstream.get("upstream_max_page")
        eligible = {id(p) for p in filter_posts(rows[offset:], *filters)}
        for i, post in enumerate(rows[offset:], offset):
            scanned += 1
            pid = post.get("id")
            if id(post) not in eligible or (uid and not _is_author(post, uid)) or pid in seen:
                continue
            selected.append(post)
            seen.add(pid)
            if len(selected) == size:
                if i + 1 < len(rows):
                    more, next_page = True, f"p{source}r{i+1}"
                else:
                    more = upstream["has_more"]
                    next_page = None if more is False else f"p{source+1}r0"
                break
        if len(selected) == size:
            break
        more = upstream["has_more"]
        if more is False or (more is None and not rows):
            next_page = None
            break
        source += 1
        offset = 0
        next_page = f"p{source}r0"
    if len(selected) < size:
        notes.append(f"本次扫描 {scanned} 条候选，取得 {len(selected)} 条符合条件的帖子。")
    meta = pagination(size, len(selected), 30, more, next_page, page)
    meta.update(scanned=scanned, upstream_max_page=last_source)
    if retry_page:
        meta["retry_page"] = retry_page
    return selected, meta, " ".join(notes)


async def search_posts(
    query: str, count: int = 10, page: int | str = 1, user_id: str | None = None,
    since_id: str | int | None = None, since: str | None = None, until: str | None = None,
    original_only: bool = False, min_followers: int | None = None, format: str = "markdown",
    exact_phrase: bool = False, deduplicate: bool = True,
) -> str | dict:
    query = query.strip()
    if not query:
        return "query 不能为空；查看作者最新动态请使用 get_user_posts。"
    uid = _user_id(user_id) if user_id is not None else ""
    if user_id is not None and not uid:
        return "user_id 需要是雪球用户的正整数数字 ID。"
    size = max(1, min(int(count), 30))
    params = {"q": query, "sort": "time"}
    path = "/query/v1/search/status.json"
    if uid:
        params["uid"] = uid
        path = "/query/v1/user/status/search.json"
    filters = (since_id, since, until, original_only, min_followers)
    if any(x is not None for x in (since_id, since, until, min_followers)) or original_only:
        try:
            raw, meta, note = await _read_filtered(path, params, "list", size, page, uid, filters)
        except ValueError as exc:
            return str(exc)
    else:
        page = max(1, int(page))
        raw, meta, note = await _fetch_post_page(path, params, "list", size, page, REFERER, fixed_batch=10)
    scope = f"用户 {uid} 的" if uid else ""
    title = f"{scope}「{query}」相关帖子"
    verified = [p for p in raw if _is_author(p, uid)] if uid else raw
    rejected = len(raw) - len(verified)
    if rejected:
        note += f" 已排除 {rejected} 条无法通过作者身份校验的结果。"
    note += " 以下为关键词附近摘录；引用前请用 get_post 获取全文并核对上下文。"
    return _filtered_page(verified, meta, note, title, count, query, format,
                          exact_phrase=exact_phrase, deduplicate=deduplicate)


def _filtered_page(posts, meta, note, title, requested, query, format, *, exact_phrase=False, deduplicate=False):
    if exact_phrase:
        candidate_count = len(posts)
        phrase = search_text(query)
        posts = [p for p in posts if any(phrase in search_text(text) for text in (
            strip_html(p.get("title")), strip_html(p.get("text")) or strip_html(p.get("description"))
        ))]
        if len(posts) < candidate_count:
            note += f" 本页有 {candidate_count - len(posts)} 条未匹配完整短语。"
    if deduplicate:
        unique = []
        seen = set()
        for post in posts:
            digest = content_hash(post)
            if digest and digest in seen:
                continue
            seen.add(digest)
            unique.append(post)
        if len(unique) < len(posts):
            note += f" 本页已合并 {len(posts) - len(unique)} 条同文结果，保留首次命中。"
        posts = unique
    meta.update(requested=requested, returned=len(posts))
    if format == "json":
        items = [post_record(p) for p in posts]
        if query:
            for item, post in zip(items, posts):
                item["content_hash"] = content_hash(post)
        return {"pagination": meta, "items": items, "note": note.strip()}
    page = meta["page"]
    head = (f"### {title}（第 {page} 页，本页 {len(posts)} 条，请求 {requested} 条，实际返回 {len(posts)} 条）"
            f"\n{page_text(meta)}")
    if note.strip():
        head += "\n" + note.strip()
    start = 1 if not isinstance(page, int) else (page - 1) * max(1, min(int(requested), 30)) + 1
    body = "\n\n---\n\n".join(_render_post(p, i, 600 if query else 220, query) for i, p in enumerate(posts, start))
    return head + "\n" + (body or "本页没有符合条件的结果。")


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
    post_id: str, with_comments: bool = True, comment_count: int = 10, full_text: bool = True,
    comment_page: int = 1, format: str = "markdown",
) -> str | dict:
    """帖子全文及最新评论（按时间倒序）。post_id 可从讨论 / 热帖列表里取。

    ``full_text`` 为真时，正文被截断就自动走本地浏览器补全。
    """
    pid = str(post_id).strip().rstrip("/").split("/")[-1]
    if not pid.isdigit():
        return "post_id 需要是帖子的数字 ID。"

    size, comment_page = max(1, min(int(comment_count), 30)), max(1, int(comment_page))
    client = get_client()
    # 正文和评论只依赖 pid，一起发；评论区可能被关掉，失败不影响正文
    tasks = [client.get("/statuses/show.json", {"id": pid}, base=APP_API, referer=REFERER)]
    if with_comments:
        tasks.append(
            client.get(
                "/statuses/comments.json",
                {"id": pid, "count": size, "page": comment_page},
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

    comments = []
    comment_note = ""
    meta = None
    if with_comments:
        cm = fetched[1]
        if isinstance(cm, BaseException) and not isinstance(cm, Exception):
            raise cm
        if isinstance(cm, BaseException):
            comment_note = f"评论第 {comment_page} 页读取失败。{cm}"
            meta = pagination(comment_count, 0, 30, None, None, comment_page)
            meta["retry_page"] = comment_page
        else:
            comments = (cm.get("comments") or [])[:size]
            total = cm.get("count", cm.get("total"))
            last = cm.get("maxPage")
            if isinstance(last, int):
                more = comment_page < last
            elif isinstance(total, int):
                more = comment_page * size < total
            else:
                more = None if len(comments) == size else False
            meta = pagination(comment_count, len(comments), 30, more,
                              None if more is False else comment_page + 1, comment_page, total=total)
            if len(comments) < size:
                comment_note = "当前评论页返回的评论少于请求数。"
    if format == "json":
        return {"post": post_record(st), "comments": [post_record(c) for c in comments],
                "comment_sort": "time_desc" if with_comments else None,
                "pagination": meta, "note": (note.strip() + " " + comment_note).strip()}

    _, who = _author(st)
    title = strip_html(st.get("title"))
    out = [
        f"### {title or '（无标题）'}",
        f"作者：{who}　发布：{fmt_ts(st.get('created_at'), '%Y-%m-%d %H:%M')}"
        f"　赞{fmt_count(st.get('like_count'))} 评{fmt_count(st.get('reply_count'))}"
        f" 转{fmt_count(st.get('retweet_count'))}　{_edit_status(st)}",
        post_url(st),
        "",
        f"作者 user_id: {st.get('user_id') or (st.get('user') or {}).get('id') or '未知'}",
        _post_body(st, None),
    ]

    if with_comments:
        out.append(page_text(meta))
        if comment_note:
            out.append(comment_note)
        if comments:
            out.append(f"\n#### 最新评论（按时间倒序，{len(comments)} 条）")
            for i, c in enumerate(comments, 1):
                cu = (c.get("user") or {}).get("screen_name") or "匿名"
                out.append(
                    f"{i}. **{cu}**（赞{fmt_count(c.get('like_count'))}）"
                    f"　{fmt_ts(c.get('created_at'), '%m-%d %H:%M')}\n"
                    f"   {strip_html(c.get('text'), 200)}"
                )
    return "\n".join(out) + note


async def get_user_posts(
    user_id: str, count: int = 10, page: int | str = 1, since_id: str | int | None = None,
    since: str | None = None, until: str | None = None, original_only: bool = False,
    min_followers: int | None = None, format: str = "markdown",
) -> str | dict:
    uid = _user_id(user_id)
    if not uid:
        return "user_id 需要是雪球用户的数字 ID（可在帖子链接 xueqiu.com/<user_id>/<post_id> 中找到）。"
    size = max(1, min(int(count), 30))
    path, params = "/v4/statuses/user_timeline.json", {"user_id": uid}
    filters = (since_id, since, until, original_only, min_followers)
    if any(x is not None for x in (since_id, since, until, min_followers)) or original_only:
        try:
            raw, meta, note = await _read_filtered(path, params, "statuses", size, page, uid, filters)
        except ValueError as exc:
            return str(exc)
    else:
        page = max(1, int(page))
        raw, meta, note = await _fetch_post_page(path, params, "statuses", size, page, REFERER)
    posts = [p for p in raw if _is_author(p, uid)]
    if len(posts) < len(raw):
        note += f" 已排除 {len(raw) - len(posts)} 条作者不符或身份不明的动态。"
    name = _author(posts[0])[0] if posts else "雪球用户"
    return _filtered_page(posts, meta, note, f"{name}（uid {uid}）的动态", count, "", format)
