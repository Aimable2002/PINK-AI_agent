import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.api.routes_ea import router
from app.main import app


class TestEAContract(unittest.TestCase):
    def test_pending_orders_are_user_scoped(self):
        app.dependency_overrides.clear()
        app.include_router(router)
        app.dependency_overrides[
            __import__("app.api.dependencies", fromlist=["get_current_user"]).get_current_user
        ] = lambda: SimpleNamespace(user_id="user-1")
        try:
            with patch("app.api.routes_ea.list_pending_trade_orders", return_value=[{"id": "order-1"}]) as list_orders:
                response = TestClient(app).get("/v1/ea/orders")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"orders": [{"id": "order-1"}]})
            list_orders.assert_called_once_with("user-1", 50)
        finally:
            app.dependency_overrides.clear()

    def test_invalid_execution_status_is_rejected(self):
        app.dependency_overrides.clear()
        app.include_router(router)
        app.dependency_overrides[
            __import__("app.api.dependencies", fromlist=["get_current_user"]).get_current_user
        ] = lambda: SimpleNamespace(user_id="user-1")
        try:
            response = TestClient(app).post(
                "/v1/ea/orders/order-1/execution",
                json={"mt5_account_id": "123", "status": "unknown"},
            )
            self.assertEqual(response.status_code, 422)
        finally:
            app.dependency_overrides.clear()