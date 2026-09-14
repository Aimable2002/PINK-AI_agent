"""
Generic MCP connector layer. This module does not know anything specific
about MT5, GitHub, or Linear -- it just holds a named set of server
connection parameters and provides list_tools/call_tool against whichever
one is requested. Per-user credentials and per-connector connection
parameters (stdio command, or remote URL + auth header) are expected to
be loaded from Supabase's `mcp_connections` table at runtime, not
hardcoded here.

Supports three transports so any MCP connector can be registered,
local or remote:
  - "stdio": spawns a local subprocess (command + args) and speaks MCP
    over its stdin/stdout. Used for connectors that only ship as a local
    binary/script (e.g. a local MT5 bridge).
  - "sse": connects to a remote server exposing the legacy MCP
    Server-Sent-Events transport at a URL, with optional auth headers.
  - "http": connects to a remote server exposing the modern MCP
    Streamable HTTP transport at a URL, with optional auth headers.
    This is what most hosted/remote MCP connectors use today (e.g.
    Linear's hosted MCP, most GitHub-hosted MCP servers).
"""

from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

_VALID_TRANSPORTS = ("stdio", "sse", "http")


@dataclass
class ConnectorConfig:
    name: str
    transport: str = "stdio"          # "stdio" | "sse" | "http"

    # stdio-only
    command: str | None = None
    args: list[str] = field(default_factory=list)

    # sse/http-only
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if self.transport not in _VALID_TRANSPORTS:
            raise ValueError(
                f"Unknown transport '{self.transport}' for connector "
                f"'{self.name}'. Must be one of {_VALID_TRANSPORTS}."
            )
        if self.transport == "stdio" and not self.command:
            raise ValueError(f"Connector '{self.name}' uses stdio but has no command set.")
        if self.transport in ("sse", "http") and not self.url:
            raise ValueError(f"Connector '{self.name}' uses {self.transport} but has no url set.")


class MCPConnectorManager:
    def __init__(self):
        self._connectors: dict[str, ConnectorConfig] = {}

    def register(self, config: ConnectorConfig) -> None:
        self._connectors[config.name] = config

    def is_registered(self, name: str) -> bool:
        return name in self._connectors

    @asynccontextmanager
    async def session(self, connector_name: str):
        if connector_name not in self._connectors:
            raise KeyError(f"No connector registered under '{connector_name}'")

        cfg = self._connectors[connector_name]

        if cfg.transport == "stdio":
            server_params = StdioServerParameters(command=cfg.command, args=cfg.args)
            async with stdio_client(server_params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session

        elif cfg.transport == "sse":
            async with sse_client(cfg.url, headers=cfg.headers or None) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session

        elif cfg.transport == "http":
            http_client = httpx.AsyncClient(headers=cfg.headers or None)
            try:
                async with streamable_http_client(cfg.url, http_client=http_client) as (
                    read,
                    write,
                    *_rest,
                ):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        yield session
            finally:
                await http_client.aclose()

        else: 
            raise ValueError(f"Unhandled transport '{cfg.transport}'")

    async def list_tools(self, connector_name: str) -> list[str]:
        async with self.session(connector_name) as session:
            result = await session.list_tools()
            return [tool.name for tool in result.tools]

    async def call_tool(self, connector_name: str, tool_name: str, arguments: dict):
        async with self.session(connector_name) as session:
            result = await session.call_tool(tool_name, arguments)
            return result