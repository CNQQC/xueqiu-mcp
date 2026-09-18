"""扫码会话、临时网页与 MCP 图片契约。使用隔离凭证和假浏览器，不访问雪球。"""

from __future__ import annotations

import asyncio
import base64
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import httpx
from starlette.applications import Starlette

from xueqiu_mcp import auth, browser, login_flow
from xueqiu_mcp.client import XueqiuClient
from xueqiu_mcp.tools import account

PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aZ1sAAAAASUVORK5CYII="
QR = "data:image/png;base64," + PNG
COOKIES = {"xq_is_login": "1", "u": "123", "xq_a_token": "private-test-cookie"}


class LoginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "XUEQIU_COOKIE_FILE": str(Path(self.tmp.name) / "cookies.json"),
            "XUEQIU_BROWSER_PROFILE": str(Path(self.tmp.name) / "browser"),
            "XUEQIU_COOKIE": "", "XUEQIU_PUBLIC_BASE_URL": "https://mcp.example/xueqiu",
            "XUEQIU_TRANSPORT": "streamable-http",
        })
        self.env.start()
        self.client = XueqiuClient()
        self.session = AsyncMock(spec=browser.BrowserSession)
        self.session.open_login.return_value = {"logged_in": False, "qr_data": QR}
        self.session.login_snapshot.return_value = {"logged_in": False, "qr_data": QR}
        self.session.cookies.return_value = COOKIES
        # 真正执行接管/写文件代码，验证扫码后后续 API 能读到登录态。
        async def adopt(remember):
            return await browser.BrowserSession.adopt_cookies(self.session, remember)
        self.session.adopt_cookies.side_effect = adopt
        self.flow = login_flow.LoginFlow()
        self.patches = [patch.object(browser, "get_session", return_value=self.session),
                        patch.object(login_flow, "_flow", self.flow),
                        patch("xueqiu_mcp.client.get_client", return_value=self.client),
                        patch.object(account, "get_client", return_value=self.client),
                        patch.object(login_flow, "POLL_INTERVAL", 0.005)]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self):
        await self.flow.close()
        await self.client.aclose()
        for p in reversed(self.patches):
            p.stop()
        self.env.stop()
        self.tmp.cleanup()

    async def test_immediate_image_and_public_link_reuse(self):
        result = await asyncio.wait_for(account.get_login_qrcode(), 0.5)
        wire = result.model_dump(by_alias=True)
        self.assertEqual(wire["structuredContent"]["state"], "pending")
        img = wire["content"][1]
        self.assertEqual(img["mimeType"], "image/png")
        self.assertEqual(base64.b64decode(img["data"]), base64.b64decode(PNG))
        self.assertNotIn("data:", img["data"])
        url = wire["structuredContent"]["login_url"]
        self.assertTrue(url.startswith("https://mcp.example/xueqiu/login/"))
        self.assertNotIn("private-test-cookie", str(wire))
        await asyncio.gather(account.get_login_qrcode(), account.get_login_qrcode())
        self.session.open_login.assert_awaited_once()
        self.assertEqual(self.flow.attempt.login_url, url)
        self.assertIn("正在等待", await account.login_status())

    async def test_background_success_adopts_and_saves_without_followup(self):
        attempt = await self.flow.start()
        self.session.login_snapshot.return_value = {"logged_in": True}
        await asyncio.wait_for(attempt.done.wait(), 0.5)
        self.assertEqual(attempt.state, "success")
        self.assertEqual(self.client.user_cookies, COOKIES)
        self.assertEqual(auth.load_saved_cookies(), COOKIES)
        self.assertEqual(auth.store_path().stat().st_mode & 0o777, 0o600)
        state = self.flow.lookup(attempt.token)
        self.assertEqual(state["qr"], "")
        self.assertNotIn("private-test-cookie", str(state))

    async def test_remember_false_does_not_write_credentials(self):
        self.session.login_snapshot.return_value = {"logged_in": True}
        attempt = await self.flow.start(remember=False)
        await asyncio.wait_for(attempt.done.wait(), 0.5)
        self.assertTrue(self.client.has_user_cookie)
        self.assertFalse(auth.store_path().exists())

    async def test_existing_browser_login_returns_success_without_qr(self):
        self.session.open_login.return_value = {"logged_in": True}
        result = (await account.get_login_qrcode()).model_dump(by_alias=True)
        self.assertEqual(result["structuredContent"]["state"], "success")
        self.assertEqual([c["type"] for c in result["content"]], ["text"])
        self.assertTrue(self.client.has_user_cookie)

    async def test_refresh_revokes_old_token_and_closes_page(self):
        old = await self.flow.start()
        await asyncio.sleep(0)
        new = await self.flow.start(refresh=True)
        self.assertNotEqual(new.token, old.token)
        self.assertEqual(self.flow.lookup(old.token)["state"], "expired")
        self.assertEqual(old.qr_data, "")
        self.session.close_login_page.assert_awaited()

    async def test_qr_updates_in_same_session(self):
        attempt = await self.flow.start()
        self.session.login_snapshot.return_value = {"logged_in": False, "qr_data": QR + "new"}
        await asyncio.sleep(0.03)
        self.assertEqual(self.flow.lookup(attempt.token)["qr"], QR + "new")
        self.session.open_login.assert_awaited_once()

    async def test_timeout_closes_page_and_discards_qr(self):
        attempt = await self.flow.start()
        attempt.deadline = time.monotonic() - 1
        await asyncio.wait_for(attempt.done.wait(), 0.5)
        self.assertEqual(attempt.state, "expired")
        self.assertEqual(attempt.qr_data, "")
        self.session.close_login_page.assert_awaited()

    async def test_browser_failure_and_save_failure_are_terminal(self):
        self.session.login_snapshot.side_effect = RuntimeError("private-test-cookie")
        attempt = await self.flow.start()
        await asyncio.wait_for(attempt.done.wait(), 0.5)
        self.assertEqual(attempt.state, "error")
        self.assertNotIn("private-test-cookie", attempt.message)
        self.session.login_snapshot.side_effect = None
        self.session.login_snapshot.return_value = {"logged_in": True}
        self.session.adopt_cookies.side_effect = OSError("disk full")
        attempt = await self.flow.start(refresh=True)
        await asyncio.wait_for(attempt.done.wait(), 0.5)
        self.assertEqual(attempt.state, "error")
        self.assertFalse(self.client.has_user_cookie)

    async def test_logout_cancels_pending_login(self):
        attempt = await self.flow.start()
        await asyncio.sleep(0)
        await account.logout(forget=False)
        self.session.login_snapshot.return_value = {"logged_in": True}
        await asyncio.sleep(0.02)
        self.assertEqual(attempt.state, "cancelled")
        self.assertFalse(self.client.has_user_cookie)
        self.assertFalse(auth.store_path().exists())

    async def test_cancel_before_watcher_runs_still_closes_login_page(self):
        attempt = await self.flow.start()
        await self.flow.cancel()
        self.assertEqual(attempt.state, "cancelled")
        self.session.close_login_page.assert_awaited()

    async def test_cancel_during_link_startup_does_not_leave_reusable_session(self):
        entered = asyncio.Event()

        async def blocked_link(attempt):
            entered.set()
            await asyncio.Future()

        with patch.object(self.flow, "_set_link", side_effect=blocked_link):
            task = asyncio.create_task(self.flow.start())
            await asyncio.wait_for(entered.wait(), 0.5)
            old = self.flow.attempt
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(old.state, "cancelled")
        self.session.close_login_page.assert_awaited()
        new = await self.flow.start()
        self.assertNotEqual(old.token, new.token)

    async def test_cancel_when_snapshot_completes_stops_watcher(self):
        async def snapshot(mode):
            # 让响应完成和取消落在同一次调度，复现旧版 wait_for 吞掉取消。
            self.flow._task.cancel()
            return {"logged_in": False, "qr_data": QR}

        self.session.login_snapshot.side_effect = snapshot
        attempt = await self.flow.start()
        task = self.flow._task
        done, _ = await asyncio.wait({task}, timeout=0.5)
        # 失败时也让 teardown 收尾，避免回归测试本身挂住五分钟。
        if not done:
            attempt.deadline = 0
        self.assertIn(task, done, "响应完成时的取消被吞掉，轮询仍在继续")
        self.assertTrue(task.cancelled())
        self.assertEqual(attempt.state, "cancelled")
        self.session.close_login_page.assert_awaited()

    async def test_snapshot_timeout_cancels_inflight_read(self):
        cancelled = asyncio.Event()

        async def snapshot(mode):
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        self.session.login_snapshot.side_effect = snapshot
        attempt = await self.flow.start()
        attempt.deadline = time.monotonic() + 0.03
        await asyncio.wait_for(attempt.done.wait(), 0.5)
        self.assertEqual(attempt.state, "expired")
        self.assertTrue(cancelled.is_set())
        self.session.close_login_page.assert_awaited()

    async def test_manual_cookie_login_cancels_pending_scan(self):
        attempt = await self.flow.start()
        with patch.object(auth, "fetch_profile", return_value={"id": 123}):
            result = await account.login_with_cookie(auth.format_cookie_string(COOKIES))
        self.assertIn("登录成功", result)
        self.assertEqual(attempt.state, "cancelled")
        self.assertTrue(self.client.has_user_cookie)

    async def test_unavailable_browser_returns_tool_error(self):
        self.session.open_login.side_effect = browser.BrowserUnavailable("安装 playwright")
        wire = (await account.get_login_qrcode()).model_dump(by_alias=True)
        self.assertTrue(wire["isError"])
        self.assertIn("无法启动", wire["content"][0]["text"])

    async def test_missing_canvas_is_readable_error(self):
        self.session.open_login.return_value = {"logged_in": False}
        wire = (await account.get_login_qrcode()).model_dump(by_alias=True)
        self.assertTrue(wire["isError"])
        self.assertIn('mode="window"', wire["content"][0]["text"])
        self.session.close_login_page.assert_awaited()

    async def test_window_mode_has_no_broken_image(self):
        self.session.open_login.return_value = {"logged_in": False}
        wire = (await account.browser_login(mode="window")).model_dump(by_alias=True)
        self.assertEqual(wire["structuredContent"]["state"], "pending")
        self.assertEqual(len(wire["content"]), 1)

    async def test_public_state_headers_invalid_token_and_methods(self):
        attempt = await self.flow.start()
        app = Starlette(routes=login_flow.login_routes())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            page = await client.get(attempt.path)
            self.assertEqual(page.status_code, 200)
            self.assertNotIn(attempt.token, page.text)
            self.assertEqual(page.headers["cache-control"], "no-store")
            self.assertEqual(page.headers["referrer-policy"], "no-referrer")
            self.assertIn("frame-ancestors 'none'", page.headers["content-security-policy"])
            state = await client.get(attempt.path + "/state")
            self.assertEqual(state.json()["qr"], QR)
            unknown = await client.get("/login/unknown/state")
            unicode_token = await client.get("/login/无效/state")
            attempt.deadline = time.monotonic() - 1
            expired = await client.get(attempt.path + "/state")
            self.assertEqual(unknown.json(), expired.json())
            self.assertEqual(unknown.json(), unicode_token.json())
            self.assertNotIn("access-control-allow-origin", state.headers)
            self.assertEqual((await client.post(attempt.path + "/state")).status_code, 405)

    async def test_http_without_public_base_does_not_invent_remote_url(self):
        os.environ["XUEQIU_PUBLIC_BASE_URL"] = ""
        attempt = await self.flow.start()
        self.assertFalse(attempt.login_url)
        self.assertIn(attempt.path, attempt.link_note)
        self.assertIsNone(self.flow._web_task)

    async def test_invalid_public_base_is_not_rendered(self):
        os.environ["XUEQIU_PUBLIC_BASE_URL"] = "https://user:secret@example.com"
        attempt = await self.flow.start()
        self.assertFalse(attempt.login_url)
        self.assertNotIn("secret", str(account._login_result(attempt)))
        self.assertTrue(attempt.qr_data)

    async def test_stdio_local_link_serves_and_shuts_down(self):
        os.environ["XUEQIU_PUBLIC_BASE_URL"] = ""
        os.environ["XUEQIU_TRANSPORT"] = "stdio"
        attempt = await self.flow.start()
        self.assertTrue(attempt.login_url.startswith("http://127.0.0.1:"))
        async with httpx.AsyncClient(trust_env=False) as client:
            state = await client.get(attempt.login_url + "/state")
        self.assertEqual(state.json()["state"], "pending")
        task = self.flow._web_task
        await self.flow.close()
        self.assertTrue(task.done())
        self.assertIsNone(self.flow._web_socket)

    async def test_local_web_failure_still_delivers_image(self):
        os.environ["XUEQIU_PUBLIC_BASE_URL"] = ""
        os.environ["XUEQIU_TRANSPORT"] = "stdio"
        with patch.object(self.flow, "_ensure_local_web", side_effect=OSError("bind failed")):
            result = (await account.get_login_qrcode()).model_dump(by_alias=True)
        self.assertFalse(result["isError"])
        self.assertEqual(result["content"][1]["type"], "image")
        self.assertIn("网页启动失败", result["content"][0]["text"])

    async def test_mcp_registration_and_image_serialization(self):
        from xueqiu_mcp.server import mcp

        names = {tool.name for tool in await mcp.list_tools()}
        self.assertIn("get_login_qrcode", names)
        self.assertIn("browser_login", names)
        result = await mcp.call_tool("get_login_qrcode", {})
        # mcp 1.x returns (content, structured), mcp 2.x returns CallToolResult.
        content = result.content if hasattr(result, "content") else result[0]
        self.assertEqual([c.type for c in content], ["text", "image"])
        self.assertEqual(content[1].data, PNG)
        app = mcp.streamable_http_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            response = await client.get(self.flow.attempt.path + "/state")
        self.assertEqual(response.json()["qr"], QR)

    async def test_stdio_wire_delivers_native_image_and_live_link(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        child = '''
from unittest.mock import AsyncMock
from xueqiu_mcp import browser
from xueqiu_mcp.server import mcp
fake = AsyncMock(spec=browser.BrowserSession)
fake.open_login.return_value = {"logged_in": False, "qr_data": %r}
fake.login_snapshot.return_value = {"logged_in": False, "qr_data": %r}
browser.get_session = lambda: fake
mcp.run()
''' % (QR, QR)
        env = dict(os.environ, XUEQIU_PUBLIC_BASE_URL="", XUEQIU_TRANSPORT="stdio")
        params = StdioServerParameters(command=sys.executable, args=["-c", child], env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                response = await client.call_tool("get_login_qrcode", {})
                wire = response.model_dump(by_alias=True)
                self.assertFalse(wire["isError"])
                self.assertEqual([c["type"] for c in wire["content"]], ["text", "image"])
                self.assertEqual(wire["content"][1]["data"], PNG)
                url = wire["structuredContent"]["login_url"]
                async with httpx.AsyncClient(trust_env=False) as web:
                    state = await web.get(url + "/state")
                self.assertEqual(state.json()["state"], "pending")
                repeated = (await client.call_tool("get_login_qrcode", {})).model_dump(by_alias=True)
                self.assertEqual(repeated["structuredContent"]["login_url"], url)

    async def test_http_launch_options_on_both_sdk_versions(self):
        from xueqiu_mcp import server

        with patch.object(server.mcp, "run") as run:
            server._run_http("streamable-http", host="127.0.0.1", port=8123,
                             stateless_http=True, json_response=True)
        if hasattr(server.mcp.settings, "host"):
            self.assertEqual(server.mcp.settings.port, 8123)
            self.assertTrue(server.mcp.settings.stateless_http)
            run.assert_called_once_with("streamable-http")
        else:
            run.assert_called_once_with("streamable-http", host="127.0.0.1", port=8123,
                                        stateless_http=True, json_response=True)


class BrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_png_does_not_depend_on_text_recognition(self):
        session = browser.BrowserSession()
        page = AsyncMock()
        page.evaluate.side_effect = [QR, RuntimeError("matrix failed")]
        with patch.object(session, "_save_qr_png", return_value=None):
            qr = await session._read_qr(page, with_text=True)
        self.assertEqual(qr["qr_data"], QR)

    async def test_only_xueqiu_cookies_are_adopted(self):
        session = browser.BrowserSession()
        session._ctx = AsyncMock()
        session._ctx.cookies.return_value = [
            {"domain": ".xueqiu.com", "name": "xq_a_token", "value": "x"},
            {"domain": "api.xueqiu.com", "name": "u", "value": "123"},
            {"domain": "evilxueqiu.com", "name": "external", "value": "secret"},
        ]
        self.assertEqual(await session.cookies(), {"xq_a_token": "x", "u": "123"})

    async def test_zero_wait_never_sleeps(self):
        session = browser.BrowserSession()
        with patch.object(session, "cookies", return_value={}), patch.object(asyncio, "sleep") as sleep:
            self.assertEqual(await session.wait_login(timeout=0), {})
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
