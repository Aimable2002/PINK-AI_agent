import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.agent_services import trading_agent
from app.agent_services import telegram_signal_monitor
from app.config import LITELLM_DEBUG, get_forecast_config
from app.core.billing import sum_usage, usage_event
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

    async def test_unavailable_connector_error_is_given_to_model(self):
        manager = MCPConnectorManager()
        manager.register(
            ConnectorConfig(
                name="mt5",
                transport="http",
                url="https://example.test/mcp",
            )
        )

        async def unavailable(_connector_name):
            raise RuntimeError("MT5 tunnel is offline")

        seen_messages = []

        async def fake_call_tier(_tier, messages, **_kwargs):
            seen_messages.extend(messages)
            return FakeResponse(content="MT5 is unavailable right now.")

        async def fake_select_tier(_prompt):
            return "small"

        with patch.object(manager, "list_tool_schemas", side_effect=unavailable):
            result = await run_agent_loop(
                "Get the latest candles",
                [],
                ["mt5"],
                connector_manager=manager,
                call_tier_fn=fake_call_tier,
                select_tier_fn=fake_select_tier,
                mode="agent",
            )

        self.assertEqual(result["stopped_reason"], "model_completed")
        self.assertTrue(any("MT5 tunnel is offline" in str(message) for message in seen_messages))

    def test_default_tools_switch_by_mode(self):
        dev_tools = get_default_tools("dev")
        prod_tools = get_default_tools("prod")

        self.assertEqual(dev_tools[0]["type"], "function")
        self.assertEqual(dev_tools[0]["function"]["name"], "web_search")
        self.assertIn("browser_use", {tool["function"]["name"] for tool in dev_tools})
        self.assertEqual(prod_tools[0]["type"], "function")
        self.assertEqual(prod_tools[0]["function"]["name"], "web_search")
        self.assertIn("browser_use", {tool["function"]["name"] for tool in prod_tools})

    def test_litellm_debug_is_enabled_by_default(self):
        self.assertTrue(LITELLM_DEBUG)

    async def test_trading_agent_requires_pair_and_timeframe(self):
        manager = SimpleNamespace(call_tool=AsyncMock())
        with self.assertRaisesRegex(ValueError, "pair.*timeframe"):
            await trading_agent.generate_signal("user-123", {"config": {"pair": None, "timeframe": None}}, manager)

    async def test_trading_agent_combines_forecast_responses_deterministically(self):
        combined = trading_agent._combine_forecast_results([
            {"model": "kronos", "direction": "long", "confidence": 0.8, "raw": {"forecast": 1.02}},
            {"model": "chronos2", "direction": "short", "confidence": 0.7, "raw": {"forecast": 0.99}},
        ], "kronos")
        self.assertEqual(combined["direction"], "long")
        self.assertEqual(combined["model"], "kronos")
        self.assertGreaterEqual(combined["confidence"], 0.70)

    def test_get_forecast_config_fails_closed(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "KRONOS_PROVIDER"):
                get_forecast_config("kronos")

    def test_telegram_signal_parser_normalizes_multiple_targets(self):
        signal = telegram_signal_monitor._parse_signal_response(
            '{"is_signal":true,"signal_type":"forex","symbol":"eurusd",'
            '"direction":"buy","entry":1.08,"take_profits":[1.09,1.10],'
            '"stop_loss":1.07,"expiry_minutes":null,"reasoning":"clear"}'
        )
        self.assertEqual(signal["symbol"], "EURUSD")
        self.assertEqual(signal["take_profits"], [1.09, 1.10])
        self.assertEqual(signal["parse_status"], "parsed")

    def test_telegram_signal_parser_accepts_entry_range(self):
        signal = telegram_signal_monitor._parse_signal_response(
            '{"is_signal":true,"signal_type":"forex","symbol":"XAUUSD",'
            '"direction":"sell","entry":"4346-48",'
            '"take_profits":[4341,4336,4331,4311],"stop_loss":4358}'
        )
        self.assertTrue(signal["is_signal"])
        self.assertEqual(signal["entry"], 4346.0)
        self.assertEqual(signal["take_profits"], [4341.0, 4336.0, 4331.0, 4311.0])
        self.assertEqual(signal["parse_status"], "parsed")

    def test_telegram_signal_filter_accepts_index_signal(self):
        self.assertTrue(telegram_signal_monitor.looks_like_trading_text(
            "NAS100 SELL\nENTRY @ 30251\nSL: 30433\nTP1: 30081\nTP2: 29894\nTP3: 29692"
        ))

    def test_telegram_signal_parser_preserves_explicit_order_type(self):
        signal = telegram_signal_monitor._parse_signal_response(
            '{"is_signal":true,"signal_type":"forex","symbol":"NAS100",'
            '"direction":"sell","order_type":"market","entry":30251,'
            '"take_profits":[30081],"stop_loss":30433}',
            "NAS100 SELL LIMIT\nENTRY 30251\nSL 30433\nTP1 30081",
        )
        self.assertTrue(signal["is_signal"])
        self.assertEqual(signal["order_type"], "limit")

    def test_telegram_signal_parser_defaults_to_market_order(self):
        signal = telegram_signal_monitor._parse_signal_response(
            '{"is_signal":true,"signal_type":"forex","symbol":"EURUSD",'
            '"direction":"buy","entry":1.08,"take_profits":[1.09],"stop_loss":1.07}'
        )
        self.assertEqual(signal["order_type"], "market")

    def test_telegram_signal_parser_recovers_null_entry_from_source_range(self):
        signal = telegram_signal_monitor._parse_signal_response(
            '{"entry":null,"symbol":"XAUUSD","direction":"sell",'
            '"is_signal":false,"stop_loss":4358,"signal_type":"forex",'
            '"take_profits":[4341,4336,4331,4311]}',
            "XAUUSD SELL\nENTRY 4346-48\nSL 4358\nTP 4341\nTP 4336\nTP 4331\nTP 4311",
        )
        self.assertTrue(signal["is_signal"])
        self.assertEqual(signal["entry"], 4346.0)
        self.assertEqual(signal["parse_status"], "parsed")

    def test_telegram_signal_parser_rejects_invalid_json(self):
        signal = telegram_signal_monitor._parse_signal_response("not json")
        self.assertFalse(signal["is_signal"])
        self.assertEqual(signal["parse_status"], "rejected")

    def test_trading_ensemble_builds_actionable_signal(self):
        signal = trading_agent._build_actionable_signal("EURUSD", "15m", [
            {"model": "chronos2", "direction": "long", "confidence": 0.8, "entry": 1.08, "take_profits": [1.09], "stop_loss": 1.07},
            {"model": "timesfm2_5", "direction": "long", "confidence": 0.7, "entry": 1.08, "take_profits": [1.10], "stop_loss": 1.07},
            {"model": "moirai_moe", "direction": "short", "confidence": 0.6, "entry": 1.08, "take_profits": [1.06], "stop_loss": 1.09},
        ])
        self.assertEqual(signal["symbol"], "EURUSD")
        self.assertEqual(signal["direction"], "long")
        self.assertEqual(signal["consensus"], 2)

    def test_usage_events_preserve_exact_decimal_costs(self):
        events = [
            usage_event("model", "litellm", "small", cost_usd=0.001234),
            usage_event("search", "serper", "web_search", credits=0.25),
        ]
        cost_usd, credits = sum_usage(events)
        self.assertEqual(cost_usd, 0.001234)
        self.assertEqual(credits, 0.3734)

    async def test_agent_result_includes_classifier_and_agent_model_events(self):
        classifier_response = SimpleNamespace(
            model="classifier-model",
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=2, total_tokens=12),
            choices=[SimpleNamespace(message=SimpleNamespace(content="0.1"))],
        )
        agent_response = SimpleNamespace(
            model="agent-model",
            usage=SimpleNamespace(prompt_tokens=20, completion_tokens=5, total_tokens=25),
            choices=[SimpleNamespace(message=SimpleNamespace(content="done", tool_calls=[]))],
        )

        async def fake_select(_prompt, usage_callback=None):
            if usage_callback:
                usage_callback(classifier_response, "classifier")
            return "small"

        async def fake_call(_tier, _messages, **_kwargs):
            return agent_response

        with patch("app.core.llm_client.litellm.completion_cost", return_value=0.01):
            result = await run_agent_loop(
                "test",
                [],
                [],
                call_tier_fn=fake_call,
                select_tier_fn=fake_select,
                mode="agent",
            )

        self.assertEqual([event["operation_type"] for event in result["usage_events"]], ["model", "model"])
        self.assertEqual(result["cost_usd"], 0.02)


if __name__ == "__main__":
    unittest.main()
