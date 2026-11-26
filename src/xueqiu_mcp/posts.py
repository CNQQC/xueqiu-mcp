"""Machine-readable Xueqiu posts and filters, evaluated on the upstream page."""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from .formatting import CN_TZ, post_url, strip_html
from .results import iso_time

_STOCK = re.compile(r"\$([^$()\n（）]+)[(（]([A-Za-z0-9.\-]+)[)）]\$")
_MENTIONS = re.compile(r"(?:@[^@\s:：/,，]+[\s,，]*)+")


def _integer(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        return int(value)
    return None


def post_record(post: dict) -> dict:
    user = post.get("user") or {}
    original = post.get("retweeted_status") or {}
    original_id = original.get("id") or post.get("retweet_status_id")
    if original_id in (0, -1, "0", "-1", ""):
        original_id = None
    author_id = post.get("user_id") or user.get("id")
    text = strip_html(post.get("text")) or strip_html(post.get("description"))
    title = strip_html(post.get("title"))
    symbols = list(dict.fromkeys((name.strip(), code.upper()) for name, code in _STOCK.findall(title + "\n" + text)))
    own, *quoted = re.split(r"//\s*@", text, maxsplit=1)
    return {
        "id": str(post["id"]) if post.get("id") is not None else None,
        "author_id": str(author_id) if author_id is not None else None,
        "author_name": user.get("screen_name"), "followers_count": _integer(user.get("followers_count")),
        "created_at": iso_time(post.get("created_at")), "edited_at": iso_time(post.get("edited_at")),
        "is_retweet": bool(original_id or original), "retweeted_status_id": str(original_id) if original_id else None,
        "title": title, "text": text, "author_text": own.strip(),
        "quoted_text": "//@" + quoted[0] if quoted else None, "url": post_url(post),
        "symbols": [{"name": name, "code": code} for name, code in symbols],
        **{key: _integer(post.get(key)) for key in ("like_count", "reply_count", "retweet_count", "view_count")},
    }


def _bound(value, end):
    if value is None:
        return None
    normalized = value.strip().replace("Z", "+00:00")
    instant = datetime.fromisoformat(normalized)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=CN_TZ)
    if end and re.fullmatch(r"\d{4}-\d{2}-\d{2}", normalized):
        instant += timedelta(days=1)
    return instant.timestamp() * 1000


def filter_posts(rows, since_id, since, until, original_only, min_followers):
    if since_id is not None and not re.fullmatch(r"[0-9]+", str(since_id)):
        raise ValueError("since_id 需要非负整数帖子 ID。")
    if min_followers is not None and min_followers < 0:
        raise ValueError("min_followers 需要非负整数。")
    try:
        start, end = _bound(since, False), _bound(until, True)
    except (ValueError, OverflowError):
        raise ValueError("since 和 until 需要 ISO 日期或带时区的日期时间。") from None
    if start is not None and end is not None and start >= end:
        raise ValueError("since 必须早于 until 的结束边界。")
    selected = []
    for row in rows:
        ts = _integer(row.get("created_at"))
        if start is not None and (ts is None or ts < start):
            continue
        if end is not None and (ts is None or ts >= end):
            continue
        pid = _integer(row.get("id"))
        if since_id is not None and (pid is None or pid <= int(since_id)):
            continue
        fans = _integer((row.get("user") or {}).get("followers_count"))
        if min_followers is not None and (fans is None or fans < min_followers):
            continue
        if original_only:
            rec = post_record(row)
            if rec["is_retweet"] or rec["quoted_text"] or _MENTIONS.fullmatch(rec["text"]):
                continue
        selected.append(row)
    return selected
