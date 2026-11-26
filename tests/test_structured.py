"""Contract checks for machine-readable output, cursors and session finality."""
import json
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

from xueqiu_mcp import server
from xueqiu_mcp.formatting import CN_TZ
from xueqiu_mcp.posts import filter_posts, post_record
from xueqiu_mcp.tools import social
from xueqiu_mcp.trading import partial_bar


class StructuredTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_read_tool_exposes_json(self):
        tools = await server.mcp.list_tools()
        mutating = {"login_with_cookie", "logout", "get_login_qrcode", "browser_login", "browser_close"}
        for tool in tools:
            schema = tool.model_dump(by_alias=True)["inputSchema"]["properties"]
            self.assertEqual("format" in schema, tool.name not in mutating, tool.name)

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
        self.assertEqual(result["symbols"], [{"name": "茅台", "code": "SH600519"}])
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
