"""联网回归：经真实 MCP 协议验证简称消歧、作者限定、分页与原帖全文。

默认运行当前 Python 的包；--server-command 可验证 AI 应用实际配置的启动程序。
只读公开数据，不修改登录态。不在离线 unittest discovery 中执行。
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main(command: str | None = None) -> None:
    params = StdioServerParameters(
        command=command or sys.executable, args=[] if command else ["-m", "xueqiu_mcp"],
    )
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            tools = {t.name: t for t in (await session.list_tools()).tools}
            assert "search_users" in tools, "实际启动的服务缺少 search_users，请更新安装"
            schema = tools["search_posts"].model_dump(by_alias=True)["inputSchema"]
            assert "user_id" in schema["properties"], "实际启动的服务不支持限定作者"

            async def call(name, **kwargs):
                result = await session.call_tool(name, kwargs)
                body = "\n".join(c.text for c in result.content if getattr(c, "text", None))
                assert not (getattr(result, "isError", False) or getattr(result, "is_error", False)), body
                return body

            users = await call("search_users", query="老木匠", count=10)
            assert "买股票的老木匠" in users and "3058599833" in users, users
            assert "有多个候选账号" in users, users
            print("PASS 简称搜索保留目标账号，并提示同名候选", flush=True)

            first_ids = set()
            for page in (1, 2):
                posts = await call("search_posts", query="茅台", user_id="3058599833", count=3, page=page)
                authors = re.findall(r"作者 user_id: (\d+)", posts)
                assert authors and set(authors) == {"3058599833"}, posts
                links = re.findall(r"帖子ID (\d+)\s+(https://xueqiu.com/3058599833/\d+)", posts)
                assert links and "茅台" in posts and "买股票的老木匠" in posts, posts
                ids = {pid for pid, _ in links}
                if page == 1:
                    first_ids = ids
                    source = links[0][1]
                    full = await call("get_post", post_id=source, with_comments=False)
                    assert "作者 user_id: 3058599833" in full and "本条作者文字：" in full, full
                    assert "茅台" in full and source in full, full
                    print(f"PASS 作者限定 + 原帖全文 {source}", flush=True)
                else:
                    assert ids - first_ids, "翻页没有返回新帖子"
                    print("PASS 第 2 页返回新的目标作者帖子", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-command")
    asyncio.run(main(parser.parse_args().server_command))
