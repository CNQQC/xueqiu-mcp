"""Contract checks for machine-readable output, cursors and session finality."""
import json
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

import httpx

from xueqiu_mcp import resolve, server
from xueqiu_mcp.client import XueqiuClient
from xueqiu_mcp.formatting import CN_TZ
from xueqiu_mcp.posts import filter_posts, post_record
from xueqiu_mcp.tools import f10, quote, social
from xueqiu_mcp.trading import partial_bar


class StructuredTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_read_tool_exposes_json(self):
        tools = await server.mcp.list_tools()
        mutating = {"login_with_cookie", "logout", "get_login_qrcode", "browser_login", "browser_close"}
        for tool in tools:
            schema = tool.model_dump(by_alias=True)["inputSchema"]["properties"]
            self.assertEqual("format" in schema, tool.name not in mutating, tool.name)
            if tool.name in ("search_posts", "get_user_posts"):
                self.assertEqual(schema["cursor"]["type"], "string")
                self.assertEqual({v["type"] for v in schema["page"]["anyOf"]}, {"integer", "string"})

    async def test_thirty_three_comments_are_reachable(self):
        async def fetch(path, params, **kwargs):
            if path.endswith("show.json"):
                return {"id": 99, "text": "原帖", "reply_count": 33}
            start = (params["page"] - 1) * params["count"]
            return {"comments": [{"id": i} for i in range(start, min(start + params["count"], 33))],
                    "count": 33, "maxPage": 2}
        client = AsyncMock()
        client.get.side_effect = fetch
        with patch.object(social, "get_client", return_value=client):
            ids = []
            page = 1
            while page is not None:
                result = await server.get_post("99", comment_count=30, page=page, format="json")
                data = json.loads(result.content[0].text)
                self.assertEqual(data, result.model_dump(by_alias=True)["structuredContent"])
                ids.extend(c["id"] for c in data["comments"])
                self.assertEqual(data["pagination"]["returned"], 30 if page == 1 else 3)
                page = data["pagination"]["next_page"]
            self.assertEqual(len(ids), 33)
            self.assertEqual(len(set(ids)), 33)

    async def test_filter_cursor_resumes_inside_upstream_page(self):
        async def fetch(path, params, **kwargs):
            start = (params["page"] - 1) * 10
            return {"maxPage": 3, "page": params["page"], "list": [
                {"id": i, "text": "@张三" if i % 3 == 0 else f"原文 {i}", "user": {"followers_count": 12}}
                for i in range(start + 1, start + 11)]}
        client = AsyncMock()
        client.get.side_effect = fetch
        ids, page = [], 1
        with patch.object(social, "get_client", return_value=client):
            while page is not None:
                result = await social.search_posts("市场", count=5, page=page, original_only=True,
                                                   since_id="4", min_followers=12, format="json")
                ids.extend(int(p["id"]) for p in result["items"])
                page = result["pagination"]["next_page"]
        self.assertEqual(ids, [i for i in range(5, 31) if i % 3])

    async def test_cursor_alias_and_conflict_on_both_tools(self):
        client = AsyncMock()
        rows = [{"id": i, "user_id": 123, "text": f"原文 {i}"} for i in range(10)]
        client.get.return_value = {"list": rows, "statuses": rows, "maxPage": 1, "page": 1}
        with patch.object(social, "get_client", return_value=client):
            for tool, arg in ((server.search_posts, "市场"), (server.get_user_posts, "123")):
                kw = dict(count=3, original_only=True, format="json")
                first = await tool(arg, **kw)
                token = json.loads(first.content[0].text)["pagination"]["next_page"]
                second = await tool(arg, cursor=token, **kw)
                self.assertEqual([p["id"] for p in json.loads(second.content[0].text)["items"]], ["3", "4", "5"])
                self.assertEqual(second.content, (await tool(arg, page=token, **kw)).content)
                self.assertTrue((await tool(arg, page=2, cursor=token, **kw)).model_dump(by_alias=True)["isError"])

    async def test_search_symbol_enrichment_only_reads_missing_records(self):
        client = AsyncMock()
        client.get.side_effect = [{"list": [{"id": 1, "text": "$茅台(SH600519)$"}, {"id": 2, "text": "药明"}]},
                                  {"id": 2, "text": "药明", "stockCorrelation": ["SH603259"]}]
        with patch.object(social, "get_client", return_value=client):
            rows = (await social.search_posts("市场", count=2, enrich_symbols=True, format="json"))["items"]
        self.assertNotIn("symbols_lookup", rows[0])
        self.assertEqual(rows[1]["symbols"], [{"name": None, "code": "SH603259", "source": "stockCorrelation"}])
        self.assertEqual(rows[1]["symbols_lookup"], "detail")
        self.assertEqual(client.get.await_count, 2)

    def test_symbol_sources_and_quoted_text(self):
        row = post_record({"text": "药明//@原作者: 看好", "description": '<a href="/S/00700">腾讯</a>',
                           "stock_list": [{"symbol": "00700"}, {"symbol": "MU"}],
                           "stockCorrelation": ["MU", "SH603259", ".IXIC"]})
        self.assertEqual(row["author_text"], "药明")
        self.assertEqual(row["quoted_text"], "//@原作者: 看好")
        self.assertEqual(row["text"], "药明//@原作者: 看好")
        self.assertEqual([i["code"] for i in row["symbols"]], ["00700", "MU", "SH603259", ".IXIC"])
        self.assertEqual(row["symbols"][0], {"name": "腾讯", "code": "00700", "source": "link"})
        self.assertEqual(post_record({"text": "宁德 药明 中际"})["symbols"], [])

    async def test_post_unavailability_is_only_confirmed_by_business_response(self):
        cases = [
            (httpx.Response(400, json={"error_code": "20210"}), "post_not_found"),
            (httpx.Response(404, json={"error_code": "404", "error_description": "帖子已删除"}), "post_not_found"),
            (httpx.Response(404, json={"error": "Not Found"}), "upstream_error"),
            (httpx.Response(403, text="<html>challenge</html>"), "waf_blocked"),
            (httpx.Response(503, json={"error_code": "20210"}), "upstream_error"),
            (httpx.Response(401, json={"error_code": "10022"}), "login_required"),
            (httpx.Response(429), "rate_limited"), (httpx.ReadTimeout("read"), "timeout"),
            (httpx.ConnectError("connect"), "network_error"),
            (httpx.Response(200, json={"id": "101"}), "invalid_response"),
        ]
        for response, code in cases:
            with self.subTest(code=code):
                def respond(request):
                    if isinstance(response, Exception):
                        raise response
                    return response
                client = XueqiuClient(user_cookie="xq_a_token=test", cache_enabled=False)
                async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client._client:
                    with patch.object(social, "get_client", return_value=client):
                        result = await server.get_post("99", with_comments=False, format="json")
                wire = result.model_dump(by_alias=True)
                self.assertTrue(wire["isError"])
                data = wire["structuredContent"]
                self.assertEqual(data, json.loads(result.content[0].text))
                self.assertEqual(data["error"]["code"], code)
                self.assertEqual(data["availability"], "not_found" if code == "post_not_found" else "unknown")

    async def test_historical_board_windows_use_exchange_zone_and_positive_count(self):
        client = AsyncMock()
        async def fetch(path, params):
            sym = params["symbol"]
            if path == quote.KLINE_PATH:
                return {"data": {"column": ["timestamp", "close"], "item": [[params["begin"], 1]]}}
            return {"data": {"items": [{"quote": {"symbol": sym}, "market": {
                "region": "CN" if sym == "BK0088" else "US"}}]}}
        client.get.side_effect = fetch
        with patch.object(quote, "get_client", return_value=client):
            for sym, offset in (("BK0088", "+08:00"), ("BK1499", "-05:00")):
                data = await quote.get_kline(sym, begin="2024-11-25", count=65, with_valuation=True,
                                             exclude_today=True, format="json")
                stamp = data["items"][0]["timestamp"]
                self.assertEqual(stamp, "2024-11-25T00:00:00" + offset)
                self.assertFalse(data["items"][0]["is_partial"])
                self.assertEqual(client.get.call_args.args[1]["count"], 65)
                ms = int(datetime.fromisoformat(stamp).timestamp() * 1000)
                repeated = await quote.get_kline(sym, begin=ms, count=65, with_valuation=True, format="json")
                self.assertEqual(repeated["items"], data["items"])
                later = await quote.get_kline(sym, begin=ms + 1, count=3, format="json")
                self.assertEqual(later["items"][0]["timestamp"], "2024-11-26T00:00:00" + offset)

    async def test_extended_quote_fields_follow_each_session(self):
        client = AsyncMock()
        items = [{"market": {"status": status}, "quote": {
            "symbol": sym, "current": 123, "current_ext": 123, "percent_ext": 0}}
                 for sym, status in (("SH600519", "交易中"), ("AAPL", "盘后交易"))]
        client.get.return_value = {"data": {"items": items}}
        with patch.object(quote, "get_client", return_value=client):
            rows = (await quote.get_quote("SH600519,AAPL", detail=True, format="json"))["items"]
            blocks = (await quote.get_quote("SH600519,AAPL", detail=True)).split("### ")
        self.assertNotIn("current_ext", rows[0])
        self.assertNotIn("percent_ext", rows[0])
        self.assertEqual(rows[1]["percent_ext"], 0)
        self.assertNotIn("盘后价", blocks[1])
        self.assertIn("盘后价", blocks[2])
        self.assertIn("current_ext", items[0]["quote"])

    async def test_fund_search_and_industry_benchmark_keep_asset_identity(self):
        client = AsyncMock()
        client.get.return_value = {"data": [{"code": "F161725", "stock_type": 23},
                                            {"code": "BK0088", "stock_type": 81},
                                            {"code": "AAPL", "stock_type": 0}]}
        with patch.object(resolve, "get_client", return_value=client):
            rows = (await quote.search_stock("白酒", format="json"))["items"]
        self.assertEqual([r["type"] for r in rows], ["场外基金", "板块指数", "美股"])
        industry = {"industry": [{"ind_name": "白酒", "ind_code": "BK0088"},
                                 {"ind_name": "其他", "ind_code": "S3405"}]}
        client.get.side_effect = [{"data": {"company": {"org_name_cn": "测试"}}}, {"data": industry}] * 2
        with patch.object(f10, "get_client", return_value=client):
            rows = (await f10.get_company_profile("SH600519", format="json"))["industries"]
            self.assertIn("BK0088", await f10.get_company_profile("SH600519"))
        self.assertEqual([r["kline_symbol"] for r in rows], ["BK0088", None])

    async def test_hot_cards_cursor_and_non_post_cards(self):
        client = AsyncMock()
        def feed(ids):
            return {"items": [{"id": i, "original_status": {"id": i} if i % 2 else None} for i in ids],
                    "next_max_id": ids[-1]}
        client.get.side_effect = [feed([9, 8, 7, 6, 5]), feed([7, 6, 5, 4, 3])]
        with patch.object(social, "get_client", return_value=client):
            first = await social.get_hot_posts(2, format="json")
            self.assertEqual(first["pagination"]["next_cursor"], first["pagination"]["next_page"])
            second = await social.get_hot_posts(2, cursor=first["pagination"]["next_cursor"], format="json")
        self.assertEqual([p["id"] for p in first["items"] + second["items"]], ["9", "7", "5", "3"])

    def test_record_and_bounds(self):
        raw = {"id": 9007199254740993, "user_id": 123, "created_at": 1780363943000,
               "text": "$茅台(SH600519)$ $茅台(SH600519)$", "like_count": 14375, "view_count": 0}
        result = post_record(raw)
        self.assertEqual(result["symbols"], [{"name": "茅台", "code": "SH600519", "source": "text"}])
        self.assertNotIn("text", result)
        self.assertEqual(result["author_text"], raw["text"])
        self.assertEqual(result["id"], "9007199254740993")
        self.assertEqual(result["like_count"], 14375)
        self.assertEqual(result["view_count"], 0)
        self.assertIsNone(result["reply_count"])
        self.assertIsNone(result["edited_at"])
        self.assertEqual(result["edit_status"], "unknown")
        self.assertFalse(result["edited_at_present"])
        edited = post_record({**raw, "edited_at": None, "retweeted_status": {**raw, "edited_at": raw["created_at"] + 1}})
        self.assertEqual(edited["edit_status"], "unedited")
        self.assertTrue(edited["edited_at_present"])
        self.assertIsNone(edited["edited_at_raw"])
        self.assertTrue(edited["retweeted_status"]["edited_after_creation"])
        self.assertEqual(edited["retweeted_status"]["edited_at_raw"], raw["created_at"] + 1)
        self.assertTrue(result["created_at"].endswith("+08:00"))
        self.assertEqual(filter_posts([raw], None, "2026-06-02", "2026-06-02", False, None), [raw])
        self.assertFalse(filter_posts([raw], None, None, "2026-06-02T09:32:23+08:00", False, None))

    def test_daily_finality(self):
        now = datetime(2026, 11, 26, 10, tzinfo=CN_TZ)
        stamp = "2026-11-26T00:00:00+08:00"
        snap = {"quote_as_of": now.isoformat(), "market_status": "交易中"}
        self.assertTrue(partial_bar(stamp, "day", now, snap))
        self.assertFalse(partial_bar("2026-11-25T00:00:00+08:00", "day", now, snap))
        self.assertIsNone(partial_bar(stamp, "day", now, {**snap, "quote_as_of": None}))
        self.assertFalse(partial_bar(stamp, "day", now, {**snap, "market_status": "已收盘"}))


if __name__ == "__main__":
    unittest.main()
