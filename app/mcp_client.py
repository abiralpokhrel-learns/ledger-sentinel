"""Razorpay MCP integration.

Day 1 of the build plan is about confirming WHICH MCP deployment we can use
(Remote vs Local Docker). This module abstracts that decision:

  - mode "remote": `npx mcp-remote <RAZORPAY_MCP_URL>` over stdio, with the
    merchant bearer token supplied as a header.
  - mode "local":  `docker run razorpay/mcp` with key id/secret as env vars.

It is intentionally defensive: if the server is unreachable (e.g. no test-mode
account yet) the call raises a clear error and the pipeline falls back to the
synthetic settlement file. Nothing here is on the deterministic critical path
for the demo — it is the live-data ingestion option.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Optional

from app.config import mcp_mode


class RazorpayMCP:
    def __init__(self, mode: Optional[str] = None):
        self.mode = mode or mcp_mode()

    async def _connect(self):
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "The `mcp` Python SDK is not installed. Add it to requirements "
                "or run the pipeline with the synthetic settlement file."
            ) from exc

        if self.mode == "local":
            params = StdioServerParameters(
                command="docker",
                args=[
                    "run", "-i", "--rm",
                    "-e", f"RAZORPAY_KEY_ID={os.getenv('RAZORPAY_KEY_ID','')}",
                    "-e", f"RAZORPAY_KEY_SECRET={os.getenv('RAZORPAY_KEY_SECRET','')}",
                    "razorpay/mcp",
                ],
            )
        else:
            params = StdioServerParameters(
                command="npx",
                args=["mcp-remote", os.getenv("RAZORPAY_MCP_URL", "https://mcp.razorpay.com/mcp")],
                env={"Authorization": f"Bearer {os.getenv('RAZORPAY_MERCHANT_TOKEN','')}"},
            )

        self._stdio = stdio_client(params)
        read, write = await self._stdio.__aenter__()
        self._session = ClientSession(read, write)
        await self._session.__aenter__()
        await self._session.initialize()
        return self._session

    async def call_tool(self, tool: str, arguments: dict) -> dict:
        session = await self._connect()
        try:
            result = await session.call_tool(tool, arguments)
            return json.loads(result.content[0].text)
        finally:
            await self._session.__aexit__(None, None, None)
            await self._stdio.__aexit__(None, None, None)

    async def fetch_all_settlements(self) -> dict:
        return await self.call_tool("fetch_all_settlements", {})


def fetch_settlements_sync() -> dict:
    """Convenience wrapper; returns {} if the server is unavailable."""
    try:
        return asyncio.run(RazorpayMCP().fetch_all_settlements())
    except Exception as exc:  # pragma: no cover - depends on external creds
        print(f"[mcp] settlement fetch unavailable ({exc}); using synthetic file.")
        return {}
