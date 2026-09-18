"""偶发未登录重试与用户关键词搜索回归，使用假 HTTP 响应，不访问雪球。"""
from __future__ import annotations

import asyncio
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


class SearchTests(unittest.IsolatedAsyncioTestCase):
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
            self.assertEqual(await server.search_users("财经", count=2, page=3), "ok")
            search.assert_awaited_once_with("财经", 2, 3)

    async def test_filtered_search_pagination_and_output(self):
        client = AsyncMock()
        client.get.return_value = {"list": [{"id": 1, "user_id": 123, "text": "红利投资"}], "count": 20}
        with patch.object(social, "get_client", return_value=client):
            output = await social.search_posts("红利", count=5, page=2, user_id=" 123 ")
        client.get.assert_awaited_once_with(
            "/query/v1/user/status/search.json",
            {"q": "红利", "count": 5, "page": 2, "sort": "time", "uid": "123"},
            base=APP_API, referer=social.REFERER,
        )
        self.assertIn("用户 123", output)
        self.assertIn("第 2 页", output)
        self.assertIn("**6.", output)

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
        self.assertEqual(client.get.call_args.args[1]["count"], 30)

    async def test_mcp_exposes_optional_user_filter(self):
        from xueqiu_mcp import server
        tool = next(t for t in await server.mcp.list_tools() if t.name == "search_posts")
        schema = tool.model_dump(by_alias=True)["inputSchema"]
        self.assertIn("user_id", schema["properties"])
        self.assertNotIn("user_id", schema.get("required", []))
        with patch.object(social, "search_posts", new_callable=AsyncMock, return_value="ok") as search:
            self.assertEqual(await server.search_posts("红利", user_id="123"), "ok")
            search.assert_awaited_once_with("红利", 10, 1, "123")


if __name__ == "__main__":
    unittest.main(verbosity=2)
