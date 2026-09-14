"""
Generic MCP connector layer. This module does not know anything specific
about MT5, GitHub, or Linear -- it just holds a named set of server
connection parameters and provides list_tools/call_tool against whichever
one is requested. Per-user credentials and per-connector connection
parameters (stdio command, or remote URL + auth header) are expected to
be loaded from Supabase's `mcp_connections` table at runtime, not
hardcoded here.
"""

from contextlib import asynccontextmanager
from dataclasses import dataclass

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@dataclass
class ConnectorConfig:
    name: str
    command: str
    args: list[str]


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
        server_params = StdioServerParameters(command=cfg.command, args=cfg.args)

        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session

    async def list_tools(self, connector_name: str) -> list[str]:
        async with self.session(connector_name) as session:
            result = await session.list_tools()
            return [tool.name for tool in result.tools]

    async def call_tool(self, connector_name: str, tool_name: str, arguments: dict):
        async with self.session(connector_name) as session:
            result = await session.call_tool(tool_name, arguments)
            return result
