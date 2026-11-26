"""针对空行情、估值窗口、资金分档、股息、搜索和凭证回退的离线回归。"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xueqiu_mcp import auth, server
from xueqiu_mcp.client import XueqiuClient
from xueqiu_mcp.tools import account, capital, finance, quote, social


class QualityTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_quotes_fail_while_mixed_results_keep_valid_symbols(self):
        client = AsyncMock()
        valid = {"symbol": "00700", "name": "腾讯控股", "current": None}
        with patch.object(quote, "get_client", return_value=client):
            client.get.return_value = {"data": {"items": [{"quote": {}}, None]}}
            for fmt in ("markdown", "json"):
                result = (await server.get_quote("SH999999", format=fmt)).model_dump(by_alias=True)
                self.assertTrue(result["isError"])
                self.assertIn("SH999999", result["content"][0]["text"])
                self.assertIn("search_stock", result["content"][0]["text"])
            client.get.return_value["data"]["items"].append({"quote": valid})
            data = await quote.get_quote("SH999999,00700", format="json")
        self.assertEqual([row["symbol"] for row in data["items"]], ["00700"])
        self.assertEqual(data["missing_symbols"], ["SH999999"])

    async def test_valuations_use_one_source_window_and_preserve_null(self):
        bars = [[1731974400000 + i * 86400000, 100, 22.2172] for i in range(30)]
        client = AsyncMock()
        async def upstream(path, params):
            if path != quote.KLINE_PATH:
                return {}
            rows = [r.copy() for r in bars[params["count"]:]]
            if params["count"] == -5:
                rows[0][-1] = None
            return {"data": {"column": ["timestamp", "close", "pe"], "item": rows}}
        client.get.side_effect = upstream
        with patch.object(quote, "get_client", return_value=client):
            short = await quote.get_kline("600519", count=5, with_valuation=True, format="json")
            long = await quote.get_kline("600519", count=30, with_valuation=True, format="json")
            await quote.get_kline("600519", count=5)
        self.assertEqual(len(short["items"]), 5)
        self.assertEqual(short["items"], long["items"][-5:])
        self.assertEqual(short["items"][0]["pe"], 22.2172)
        self.assertIsNone(short["items"][0]["pb"])
        sizes = [call.args[1]["count"] for call in client.get.call_args_list if call.args[0] == quote.KLINE_PATH]
        self.assertEqual(sizes, [-500, -500, -5])

    async def test_assort_keeps_missing_and_zero_separate(self):
        client = AsyncMock()
        async def upstream(path, params):
            if path.endswith("assort.json"):
                return {"data": {"buy_xlarge": 0, "sell_xlarge": None, "buy_large": 2, "sell_large": 2}}
            return {}
        client.get.side_effect = upstream
        with patch.object(capital, "get_client", return_value=client):
            result = await capital.get_capital_flow("600519", format="json")
            table = await capital.get_capital_flow("600519")
        flows = result["order_flow"]
        self.assertEqual(flows["buy_xlarge"], 0)
        self.assertIsNone(flows["sell_xlarge"])
        self.assertIsNone(flows["net_xlarge"])
        self.assertEqual(flows["net_large"], 0)
        self.assertIn("| 特大单 |", table)
        self.assertIn("capital/history", table)

    async def test_hk_dps_keeps_source_value_and_basis(self):
        client = AsyncMock()
        client.get.return_value = {"data": {"currency": "CNY", "list": [{"dps": [397.5, None]}]}}
        with patch.object(finance, "get_client", return_value=client):
            result = await finance.get_financial_statement("00700", format="json")
            table = await finance.get_financial_statement("00700")
        self.assertEqual(result["items"][0]["metrics"]["dps"], [397.5, None])
        self.assertEqual(result["units"]["dps"], "cent_per_share")
        self.assertIn("派息公告", result["metric_notes"]["dps"])
        self.assertIn("雪球 dps", table)

    def test_three_bands_require_both_totals_and_no_separate_xlarge(self):
        amounts = {f"{side}_{level}": value for side in ("buy", "sell")
                   for level, value in (("large", 1.1), ("medium", 2.2), ("small", 3.3), ("total", 6.6))}
        self.assertEqual(capital._order_layout(amounts), "three_reported_bands")
        self.assertEqual(capital._order_layout({**amounts, "sell_total": 7}), "undetermined")
        self.assertEqual(capital._order_layout({**amounts, "buy_xlarge": 0}), "xlarge_reported")

    async def test_profile_rejects_an_unrelated_returned_account(self):
        client = AsyncMock()
        with patch.object(social, "get_client", return_value=client):
            client.get.return_value = {"user": {"id": 99, "followers_count": 0}}
            profile = await social.get_user_profile("https://xueqiu.com/99", format="json")
            self.assertEqual((profile["id"], profile["followers_count"]), ("99", 0))
            self.assertEqual(client.get.call_args.args[1], {"id": "99"})
            failed = await server.get_user_profile("100", format="json")
            self.assertTrue(failed.model_dump(by_alias=True)["isError"])

    async def test_exact_phrase_and_dedup_across_authors(self):
        client = AsyncMock()
        client.get.return_value = {"list": [
            {"id": 1, "user_id": 10, "text": "<b>现金</b>分红  方案"},
            {"id": 2, "user_id": 20, "text": "现金分红 方案"},
            {"id": 3, "user_id": 30, "text": "现金及分红方案"},
            {"id": 4, "user_id": 40, "title": "现金分红", "text": "<img src='example'>"},
            {"id": 5, "user_id": 50, "title": "现金分红", "text": "<img src='other'>"},
        ]}
        with patch.object(social, "get_client", return_value=client):
            result = await social.search_posts("现金分红", exact_phrase=True, format="json")
            all_posts = await social.search_posts("现金分红", deduplicate=False, format="json")
            seen = [all_posts["items"][0]["content_hash"]]
            resumed = await social.search_posts("现金分红", exact_phrase=True, exclude_content_hashes=seen, format="json")
        self.assertEqual([r["id"] for r in resumed["items"]], ["4", "5"])
        self.assertEqual([r["id"] for r in result["items"]], ["1", "4", "5"])
        self.assertIn("合并 1 条", result["note"])
        self.assertEqual(result["pagination"]["returned"], 3)
        self.assertEqual(len(all_posts["items"]), 5)
        self.assertEqual(all_posts["items"][0]["content_hash"], all_posts["items"][1]["content_hash"])

    async def test_post_documents_and_preserves_comment_time_order(self):
        client = AsyncMock()
        async def upstream(path, *args, **kwargs):
            if path.endswith("/show.json"):
                return {"id": 9, "text": "正文"}
            return {"comments": [
                {"id": 2, "created_at": 1731974500000, "like_count": 0},
                {"id": 1, "created_at": 1731974400000, "like_count": 100},
            ]}
        client.get.side_effect = upstream
        with patch.object(social, "get_client", return_value=client):
            data = await social.get_post("9", format="json")
            table = await social.get_post("9")
        self.assertEqual([row["id"] for row in data["comments"]], ["2", "1"])
        self.assertEqual(data["comment_sort"], "time_desc")
        self.assertIn("按时间倒序", table)

    async def test_remember_falls_back_and_reports_actual_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blocked = root / "blocked"
            blocked.touch()
            jar = {"xq_is_login": "1", "u": "123"}
            with patch.dict(os.environ, {"XUEQIU_CREDENTIAL_DIR": str(blocked / "data"), "XUEQIU_COOKIE_FILE": "",
                                         "XUEQIU_BROWSER": "0", "XUEQIU_COOKIE": ""}), \
                    patch.object(Path, "home", return_value=root / "home"), \
                    patch.object(tempfile, "gettempdir", return_value=tmp):
                client = XueqiuClient()
                with patch.object(account, "get_client", return_value=client), \
                        patch.object(auth, "fetch_profile", AsyncMock(return_value={"id": 123})):
                    reply = await account.login_with_cookie(auth.format_cookie_string(jar), remember=True)
                    status = await account.login_status("json")
                path = auth.store_path()
                self.assertIn(str(path), reply)
                self.assertEqual(status["state"], "authenticated")
                self.assertEqual(XueqiuClient().user_cookies, jar)
                self.assertEqual(status["credential_storage"]["location"], "home_fallback")
                self.assertEqual(Path(status["credential_storage"]["directory"]), path.parent)
                self.assertTrue(status["credential_storage"]["writable"])
                self.assertFalse(status["browser"]["available"])
                self.assertEqual(auth.load_saved_cookies(), jar)
                self.assertTrue(auth.clear_cookies())
                with patch.object(Path, "home", return_value=blocked):
                    path = auth.save_cookies(jar)
                    self.assertEqual(auth.storage_status()["location"], "temporary")
                    self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                    self.assertEqual(auth.load_saved_cookies(), jar)
                    self.assertTrue(auth.clear_cookies())
                    self.assertFalse(auth.load_saved_cookies())


if __name__ == "__main__":
    unittest.main(verbosity=2)
