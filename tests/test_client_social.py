"""偶发未登录重试与用户关键词搜索回归，使用假 HTTP 响应，不访问雪球。"""
from __future__ import annotations

import asyncio
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from xueqiu_mcp.client import APP_API, NeedCookieError, NeedLoginError, RateLimitedError, XueqiuClient, XueqiuError
from xueqiu_mcp.tools import social


class ClientTests(unittest.IsolatedAsyncioTestCase):
    def client(self, responses, cookie=""):
        with patch("xueqiu_mcp.client.resolve_cookie", return_value=(cookie, "test")):
            client = XueqiuClient(cache_enabled=True)
        requests = []

        async def respond(request):
            requests.append(request)
            await asyncio.sleep(0)
            return responses[min(len(requests) - 1, len(responses) - 1)]

        client._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        if cookie:
            client._client.cookies.set("xq_a_token", "test-token", domain=".xueqiu.com")
        client._refresh_token = AsyncMock()
        self.addAsyncCleanup(client.aclose)
        return client, requests

    async def test_login_error_retries_same_request_without_refresh(self):
        for cookie in ("", "xq_a_token=test-token"):
            for status in (200, 400, 401, 403):
                for code, key, desc in [(10022, "error_description", "用户未登录"),
                                        ("10020", "error_msg", "请重新登录"),
                                        (60201, "error_description", "用户id无效"),
                                        (99999, "error_description", "请登录后访问")]:
                    with self.subTest(cookie=bool(cookie), status=status, code=code):
                        c, requests = self.client([
                            httpx.Response(status, json={"error_code": code, key: desc}),
                            httpx.Response(200, json={"list": [{"id": 1}]}),
                        ], cookie)
                        result = await c.get("/statuses/search.json", {"q": "红利", "uid": "123"},
                                             base=APP_API, referer="https://xueqiu.com/123")
                        self.assertEqual(result, {"list": [{"id": 1}]})
                        self.assertEqual(len(requests), 2)
                        self.assertEqual(requests[0].url, requests[1].url)
                        self.assertEqual(requests[0].headers, requests[1].headers)
                        self.assertEqual(c.user_cookie, cookie)
                        c._refresh_token.assert_awaited_once_with()
                        self.assertEqual(c.stats()["upstream_calls"], 2)

    async def test_persistent_login_error_is_bounded_and_not_cached(self):
        c, requests = self.client([
            httpx.Response(200, json={"error_code": 10022, "error_description": "用户未登录"})
        ])
        for _ in range(2):
            with self.assertRaises(NeedLoginError):
                await c.get("/statuses/show.json")
        self.assertEqual(len(requests), 4)
        self.assertEqual(c.cache.stats()["entries"], 0)

    async def test_retry_is_shared_and_success_is_cached(self):
        c, requests = self.client([
            httpx.Response(200, json={"error_code": 10022}),
            httpx.Response(200, json={"id": 1}),
        ])
        result = await asyncio.gather(*[c.get("/statuses/show.json") for _ in range(10)])
        self.assertTrue(all(d == {"id": 1} for d in result))
        self.assertEqual(await c.get("/statuses/show.json"), {"id": 1})
        self.assertEqual(len(requests), 2)

    async def test_other_errors_are_not_login_retried(self):
        for response, error in [
            (httpx.Response(429, text="<html>rate limited</html>"), RateLimitedError),
            (httpx.Response(200, json={"error_code": 999, "error_msg": "参数错误"}), XueqiuError),
            (httpx.Response(200, text="<html>challenge</html>"), NeedCookieError),
        ]:
            with self.subTest(error=error):
                c, requests = self.client([response])
                with self.assertRaises(error):
                    await c.get("/statuses/show.json")
                self.assertEqual(len(requests), 1)

    async def test_anonymous_http_refresh_still_works(self):
        c, requests = self.client([
            httpx.Response(401, json={"error_code": 400, "error_msg": "token invalid"}),
            httpx.Response(200, json={"id": 1}),
        ])
        self.assertEqual(await c.get("/statuses/show.json"), {"id": 1})
        self.assertEqual(len(requests), 2)
        self.assertEqual(c._refresh_token.await_count, 2)
        c._refresh_token.assert_awaited_with(force=True)

    async def test_retry_rate_limit_is_reported(self):
        c, requests = self.client([
            httpx.Response(200, json={"error_code": 10022}), httpx.Response(429),
        ])
        with self.assertRaises(RateLimitedError):
            await c.get("/statuses/show.json")
        self.assertEqual(len(requests), 2)

    async def test_timeline_10022_keeps_code_without_prescribing_login(self):
        c, requests = self.client([httpx.Response(400, json={
            "error_code": 10022, "error_description": "用户未登录",
        })])
        with patch.object(social, "get_client", return_value=c):
            with self.assertRaises(NeedLoginError) as caught:
                await social.get_user_posts("123", count=25)
        self.assertEqual([r.url.params["count"] for r in requests], ["20", "20"])
        self.assertIn("10022（用户未登录）", str(caught.exception))
        self.assertIn("无法据此确认", str(caught.exception))
        self.assertNotIn("login_with_cookie", str(caught.exception))

    async def test_search_scopes_have_separate_cache_entries(self):
        c, requests = self.client([httpx.Response(200, json={"list": [], "count": 0})])
        with patch.object(social, "get_client", return_value=c):
            for uid in (None, "123", "456", "123", None):
                await social.search_posts("红利", user_id=uid)
        self.assertEqual(len(requests), 3)
        self.assertEqual(requests[0].url.path, "/query/v1/search/status.json")
        self.assertNotIn("uid", requests[0].url.params)
        self.assertEqual(requests[1].url.path, "/query/v1/user/status/search.json")
        self.assertEqual(requests[1].url.params["uid"], "123")
        self.assertEqual(requests[2].url.params["uid"], "456")


class PostPaginationTests(unittest.IsolatedAsyncioTestCase):
    async def test_counts_and_consecutive_pages_for_both_feeds(self):
        async def fetch(path, params, **kwargs):
            size, page = params["count"], params["page"]
            self.assertLessEqual(size, 20)
            offset = (page - 1) * size
            key = "statuses" if "user_id" in params else "list"
            return {key: [{"id": i, "user_id": 123} for i in range(offset + 1, offset + size + 1)]}

        client = AsyncMock()
        client.get.side_effect = fetch
        with patch.object(social, "get_client", return_value=client):
            for tool, arg in [(social.get_user_posts, "123"), (social.get_stock_discussions, "SH600519")]:
                for size in (20, 25, 30):
                    for page in (1, 2):
                        result = await tool(arg, count=size, page=page)
                        self.assertIn(f"请求 {size} 条，实际返回 {size} 条", result)
                        ids = list(map(int, re.findall(r"^帖子ID (\d+)", result, re.M)))
                        self.assertEqual(ids, list(range((page - 1) * size + 1, page * size + 1)))

    async def test_short_page_and_duplicates_do_not_shift_following_window(self):
        def rows(ids):
            return {"list": [{"id": i, "user_id": 123} for i in ids]}

        second = rows([19, *range(21, 40)])
        client = AsyncMock()
        client.get.side_effect = [rows(range(1, 20)), second, second, rows(range(41, 61))]
        with patch.object(social, "get_client", return_value=client):
            first = await social.get_stock_discussions("SH600519", count=30)
            following = await social.get_stock_discussions("SH600519", count=30, page=2)
        self.assertIn("请求 30 条，实际返回 28 条", first)
        self.assertIn("少于请求数", first)
        first_ids = re.findall(r"^帖子ID (\d+)", first, re.M)
        next_ids = re.findall(r"^帖子ID (\d+)", following, re.M)
        self.assertFalse(set(first_ids) & set(next_ids))
        self.assertEqual(len(set(next_ids)), 30)

    async def test_partial_fetch_retains_verified_posts_and_reason(self):
        client = AsyncMock()
        client.get.side_effect = [
            {"statuses": [{"id": 1, "user_id": 123}, {"id": 2, "user_id": 456}]},
            XueqiuError("访问频率太快"),
        ]
        with patch.object(social, "get_client", return_value=client):
            result = await social.get_user_posts("123", count=25)
        for detail in ("请求 25 条，实际返回 1 条", "上游第 2 页读取失败", "访问频率太快", "已排除 1 条", "帖子ID 1"):
            self.assertIn(detail, result)
        self.assertNotIn("帖子ID 2", result)


class SearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_edit_time_is_shown_in_results_and_detail(self):
        post = {"id": 1, "user_id": 123, "text": "正文", "edited_at": 1780363943000}
        client = AsyncMock()
        client.get.side_effect = [{"list": [post]}, post]
        with patch.object(social, "get_client", return_value=client):
            results = await social.search_posts("正文")
            detail = await social.get_post("1", with_comments=False)
        for output in (results, detail):
            self.assertIn("编辑状态：已编辑（2026-06-02 09:32:23）", output)

    def test_missing_edit_evidence_stays_unknown(self):
        permissions = {"editable": True, "canEdit": True, "updated_at": 1780363943000}
        for extra in ({}, {"edited_at": 0}, {"edited_at": True}, {"edited_at": "bad"}):
            with self.subTest(extra=extra):
                self.assertIn("编辑状态：未知", social._edit_status({**permissions, **extra}))
        self.assertIn("编辑状态：未编辑　原始 edited_at=null", social._edit_status({"edited_at": None}))
        self.assertIn("原始 edited_at=字段缺失", social._edit_status({}))

    async def test_partial_nickname_does_not_pick_first_same_name_account(self):
        client = AsyncMock()
        client.get.return_value = {"list": [
            {"id": 1321612666, "screen_name": "老木匠"},
            {"id": 3058599833, "screen_name": "买股票的老木匠", "description": "职业木匠偶尔炒股"},
        ]}
        with patch.object(social, "get_client", return_value=client):
            result = await social.search_users("老木匠")
        for text in ("买股票的老木匠", "3058599833", "有多个候选账号", "不要直接选第一项"):
            self.assertIn(text, result)

    async def test_search_rejects_wrong_unknown_and_conflicting_authors(self):
        client = AsyncMock()
        client.get.return_value = {"list": [
            {"id": 1, "user_id": 123, "text": "本人观点"},
            {"id": 2, "user_id": 456, "text": "其他人提及目标用户"},
            {"id": 3, "text": "作者未知"},
            {"id": 4, "user_id": 123, "user": {"id": 456}, "text": "矛盾身份"},
            {"id": 5, "user": {"id": 123}, "text": "用户对象中的身份"},
        ]}
        with patch.object(social, "get_client", return_value=client):
            result = await social.search_posts("观点", user_id="123")
        for text in ("本人观点", "https://xueqiu.com/123/5", "已排除 3 条", "本页 2 条"):
            self.assertIn(text, result)
        for text in ("其他人提及", "作者未知", "矛盾身份"):
            self.assertNotIn(text, result)

    async def test_search_keyword_excerpt_and_quoted_context(self):
        client = AsyncMock()
        client.get.return_value = {"list": [{
            "id": 1, "user_id": 123, "text": "开头" * 1500 + "茅台本人观点//@另一个人:茅台引用观点",
            "retweet_status_id": 2, "retweeted_status": {
                "id": 2, "user_id": 456, "user": {"screen_name": "原帖作者"}, "text": "茅台原帖文字",
            },
        }]}
        with patch.object(social, "get_client", return_value=client):
            result = await social.search_posts("茅台", user_id="123")
        for text in ("茅台本人观点", "引用文字", "被转发帖（作者：原帖作者）", "https://xueqiu.com/456/2", "get_post"):
            self.assertIn(text, result)
        self.assertNotIn("开头" * 1500, result)

    async def test_homepage_and_post_urls_resolve_author_not_post(self):
        client = AsyncMock()
        client.get.return_value = {"list": [], "statuses": []}
        with patch.object(social, "get_client", return_value=client):
            for url in ("https://xueqiu.com/123/", "https://xueqiu.com/123/456?from=search"):
                await social.search_posts(" 茅台 ", user_id=url)
                self.assertEqual(client.get.call_args.args[1]["uid"], "123")
                self.assertEqual(client.get.call_args.args[1]["q"], "茅台")
                await social.get_user_posts(url)
                self.assertEqual(client.get.call_args.args[1]["user_id"], "123")
            client.get.reset_mock()
            await social.search_posts("茅台", user_id="https://example.com/123")
            await social.search_posts("  ", user_id="123")
            client.get.assert_not_awaited()

    async def test_only_wrong_authors_is_not_reported_as_no_history(self):
        client = AsyncMock()
        client.get.return_value = {"list": [{"id": 1, "user_id": 456, "text": "不相关"}]}
        with patch.object(social, "get_client", return_value=client):
            result = await social.search_posts("茅台", user_id="123")
        self.assertIn("无法通过作者身份校验", result)
        self.assertNotIn("不相关", result)

    async def test_full_post_separates_author_quote_and_retweet(self):
        client = AsyncMock()
        body = "这是本人文字" * 100
        client.get.return_value = {
            "id": 1, "user_id": 123, "text": body + "//@别人:他人的看法",
            "retweeted_status": {"id": 2, "user_id": 456, "text": "原帖文字", "edited_at": 1780363943000},
        }
        with patch.object(social, "get_client", return_value=client):
            result = await social.get_post("1", with_comments=False)
        self.assertIn(body, result)
        self.assertIn("引用文字（不代表本条作者的新观点）", result)
        self.assertIn("https://xueqiu.com/456/2", result)
        self.assertIn("作者 user_id: 123", result)
        outer, original = result.split("被转发帖（")
        self.assertIn("编辑状态：未知", outer)
        self.assertIn("编辑状态：已编辑（2026-06-02 09:32:23）", original)

    async def test_user_keyword_search_returns_ids_and_disambiguation(self):
        client = AsyncMock()
        client.get.return_value = {"count": 10, "list": [
            {"id": 123, "screen_name": "<em>财经</em>观察", "followers_count": 100,
             "verified_description": "认证作者", "description": "<p>红利投资</p>"},
            {"id": 456, "screen_name": "财经观察", "description": None},
        ]}
        with patch.object(social, "get_client", return_value=client):
            output = await social.search_users(" 财经 ", count=2, page=2)
        client.get.assert_awaited_once_with(
            "/query/v1/search/user.json", {"q": "财经", "count": 2, "page": 2},
            base=APP_API, referer=social.REFERER,
        )
        for expected in ("**3. 财经观察**", "**4. 财经观察**", "user_id: 123",
                         "user_id: 456", "https://xueqiu.com/123", "https://xueqiu.com/456",
                         "认证作者", "红利投资", "search_posts", "第 2 页"):
            self.assertIn(expected, output)
        self.assertNotIn("<em>", output)
        self.assertNotIn("<p>", output)

    async def test_user_search_blank_query_does_not_call_upstream(self):
        client = AsyncMock()
        with patch.object(social, "get_client", return_value=client):
            self.assertIn("query 不能为空", await social.search_users(" \t "))
        client.get.assert_not_awaited()

    async def test_user_search_empty_results_and_limits(self):
        client = AsyncMock()
        client.get.return_value = {"list": [], "count": 0}
        with patch.object(social, "get_client", return_value=client):
            output = await social.search_users("未匹配昵称", count=100, page=0)
        self.assertIn("未搜到与「未匹配昵称」相关的用户", output)
        self.assertEqual(client.get.call_args.args[1], {"q": "未匹配昵称", "count": 30, "page": 1})

    async def test_mcp_exposes_user_keyword_search(self):
        from xueqiu_mcp import server
        tool = next(t for t in await server.mcp.list_tools() if t.name == "search_users")
        schema = tool.model_dump(by_alias=True)["inputSchema"]
        self.assertIn("query", schema["required"])
        with patch.object(social, "search_users", new_callable=AsyncMock, return_value="ok") as search:
            self.assertEqual((await server.search_users("财经", count=2, page=3)).content[0].text, "ok")
            search.assert_awaited_once_with("财经", 2, 3, format="markdown")

    async def test_filtered_search_pagination_and_output(self):
        client = AsyncMock()
        client.get.return_value = {"list": [{"id": i, "user_id": 123, "text": "红利投资"} for i in range(1, 11)], "count": 10}
        with patch.object(social, "get_client", return_value=client):
            output = await social.search_posts("红利", count=5, page=2, user_id=" 123 ")
        client.get.assert_awaited_once_with(
            "/query/v1/user/status/search.json",
            {"q": "红利", "count": 10, "page": 1, "sort": "time", "uid": "123"},
            base=APP_API, referer=social.REFERER,
        )
        self.assertIn("用户 123", output)
        self.assertIn("第 2 页", output)
        self.assertIn("**6.", output)
        self.assertIn("帖子ID 6", output)
        self.assertNotIn("帖子ID 1　", output)

    async def test_small_pages_do_not_skip_remaining_matches(self):
        client = AsyncMock()
        client.get.return_value = {"list": [{"id": i, "user_id": 123, "text": f"茅台 {i}"} for i in range(1, 7)]}
        with patch.object(social, "get_client", return_value=client):
            result = await social.search_posts("茅台", user_id="123", count=3, page=2)
        self.assertEqual(client.get.call_args.args[1]["page"], 1)
        for pid in (4, 5, 6):
            self.assertIn(f"帖子ID {pid}", result)

    async def test_user_search_page_spans_upstream_boundaries(self):
        client = AsyncMock()
        client.get.side_effect = [
            {"list": [{"id": i, "user_id": 123, "text": f"茅台 {i}"} for i in range(1, 11)]},
            {"list": [{"id": i, "user_id": 123, "text": f"茅台 {i}"} for i in range(11, 21)]},
        ]
        with patch.object(social, "get_client", return_value=client):
            result = await social.search_posts("茅台", user_id="123", count=7, page=2)
        self.assertEqual([call.args[1]["page"] for call in client.get.call_args_list], [1, 2])
        for pid in range(8, 15):
            self.assertIn(f"帖子ID {pid}　", result)
        self.assertNotIn("帖子ID 15　", result)

    async def test_invalid_user_does_not_silently_search_everyone(self):
        client = AsyncMock()
        with patch.object(social, "get_client", return_value=client):
            for uid in ("name", "-1", "0", "１２３"):
                self.assertIn("user_id 需要", await social.search_posts("红利", user_id=uid))
        client.get.assert_not_awaited()

    async def test_empty_results_keep_scope_and_clamp_pagination(self):
        client = AsyncMock()
        client.get.return_value = {"list": []}
        with patch.object(social, "get_client", return_value=client):
            output = await social.search_posts("红利", count=100, page=0, user_id="123")
        self.assertIn("用户 123", output)
        self.assertEqual(client.get.call_args.args[1]["page"], 1)
        self.assertEqual(client.get.call_args.args[1]["count"], 10)

    async def test_mcp_exposes_optional_user_filter(self):
        from xueqiu_mcp import server
        tool = next(t for t in await server.mcp.list_tools() if t.name == "search_posts")
        schema = tool.model_dump(by_alias=True)["inputSchema"]
        self.assertIn("user_id", schema["properties"])
        self.assertNotIn("user_id", schema.get("required", []))
        with patch.object(social, "search_posts", new_callable=AsyncMock, return_value="ok") as search:
            self.assertEqual((await server.search_posts("红利", user_id="123")).content[0].text, "ok")
            search.assert_awaited_once_with("红利", 10, 1, "123", since_id=None, since=None, until=None,
                                           original_only=False, min_followers=None, format="markdown",
                                           exact_phrase=False, deduplicate=True, exclude_content_hashes=None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
