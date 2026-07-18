# probe.py — proves the server + your SDK version in ~15 lines
import asyncio, httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

URL = "http://127.0.0.1:8000/mcp"

def factory(headers=None, timeout=None, auth=None):
    return httpx.AsyncClient(headers=headers, timeout=timeout, auth=auth,
                             verify=True, follow_redirects=True)

async def main():
    async with streamablehttp_client(URL, httpx_client_factory=factory) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = (await s.list_tools()).tools
            print("tools:", [t.name for t in tools])
            print("ping :", (await s.call_tool("ping", {})).content[0].text)

asyncio.run(main())