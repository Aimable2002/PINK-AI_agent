import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.connectors.manager import ConnectorConfig, MCPConnectorManager
from app.core.agent_runtime import run_agent_loop
from app.core.llm_client import get_default_tools


class FakeResponse:
    def __init__(self, *, content=None, tool_calls=None):
        self.choices = [
            SimpleNamespace(
                message=SimpleNamespace(content=content, tool_calls=tool_calls or [])
            )
        ]


class TestBackendRuntime(unittest.IsolatedAsyncioTestCase):
    async def test_load_user_connectors_registers_rows(self):
        manager = MCPConnectorManager()

        class FakeTable:
            def __init__(self, rows):
                self.rows = rows

            def select(self, *_args, **_kwargs):
                return self

            def eq(self, key, value):
                self.key = key
                self.value = value
                return self

            def execute(self):
                return SimpleNamespace(data=self.rows)

        class FakeClient:
            def __init__(self, rows):
                self._rows = rows

            def table(self, *_args, **_kwargs):
                return FakeTable(self._rows)

        rows = [
            {
                "connector_id": "github",
                "transport": "http",
                "server_url": "https://api.githubcopilot.com/mcp/",
                "auth_token": "token",
                "auth_header_name": "Authorization",
                "scopes": [{"key": "repo.read", "granted": True}],
            },
            {
                "connector_id": "linear",
                "transport": "http",
                "server_url": "https://mcp.linear.app/mcp",
                "auth_token": "token",
                "auth_header_name": "Authorization",
                "scopes": [{"key": "issues.write", "granted": True}],
            },
        ]

        with patch("app.connectors.manager.get_client", return_value=FakeClient(rows)):
            registered = await manager.load_user_connectors("user-123")

        self.assertEqual(registered, ["github", "linear"])
        self.assertIn("github", manager._connectors)
        self.assertIn("linear", manager._connectors)
        self.assertEqual(manager._connectors["github"].url, "https://api.githubcopilot.com/mcp/")
        self.assertEqual(
            manager._connectors["github"].headers,
            {"Authorization": "Bearer token"},
        )
        self.assertEqual(manager._connectors["github"].scopes, {"repo.read": True})

    async def test_scope_enforcement_blocks_disallowed_tool(self):
        manager = MCPConnectorManager()
        manager.register(
            ConnectorConfig(
                name="github",
                transport="http",
                url="https://api.githubcopilot.com/mcp/",
                scopes={"repo.read": True, "repo.write": False},
            )
        )

        async def fake_list_tool_schemas(_connector_name):
            return [
                {
                    "type": "function",
                    "function": {
                        "name": "github__repo_write",
                        "description": "write repo",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ]

        async def fake_call_tier(_tier, _messages, **_kwargs):
            if not getattr(fake_call_tier, "called", False):
                fake_call_tier.called = True
                return FakeResponse(
                    tool_calls=[
                        SimpleNamespace(
                            id="call_1",
                            function=SimpleNamespace(
                                name="github__repo_write",
                                arguments=json.dumps({}),
                            ),
                        )
                    ]
                )
            return FakeResponse(content="done")

        async def fake_select_tier(_prompt):
            return "small"

        with patch.object(manager, "list_tool_schemas", side_effect=fake_list_tool_schemas):
            with patch.object(manager, "call_tool", side_effect=AssertionError("call_tool should not be invoked")):
                result = await run_agent_loop(
                    "test",
                    [],
                    ["github"],
                    connector_manager=manager,
                    call_tier_fn=fake_call_tier,
                    select_tier_fn=fake_select_tier,
                    mode="agent",
                )

        self.assertEqual(result["stopped_reason"], "model_completed")
        self.assertEqual(result["tier"], "small")
        self.assertTrue(
            any("scope" in str(step["detail"]).lower() for step in result["steps"]),
            result["steps"],
        )

    def test_default_tools_switch_by_mode(self):
        dev_tools = get_default_tools("dev")
        prod_tools = get_default_tools("prod")

        self.assertEqual(dev_tools[0]["type"], "function")
        self.assertEqual(dev_tools[0]["function"]["name"], "web_search")
        self.assertEqual(prod_tools[0]["type"], "function")
        self.assertEqual(prod_tools[0]["function"]["name"], "web_search")


if __name__ == "__main__":
    unittest.main()
