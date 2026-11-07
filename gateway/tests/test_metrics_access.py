"""Exercise the real HTTP route with synthetic credentials and no external APIs."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from starlette.applications import Starlette
from starlette.routing import Route
from gateway_mcp.routes.health import register_health_routes


class Routes:
    def __init__(self):
        self.routes = []

    def custom_route(self, path, methods, include_in_schema=False):
        def bind(func):
            self.routes.append(Route(path, func, methods=methods))
            return func
        return bind


class MetricsAccessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.token = "synthetic-metrics-key-" + "x" * 40
        self.path = Path(self.temp.name) / "metrics-token"
        self.path.write_text(self.token, encoding="ascii")
        self.path.chmod(0o600)
        self.env = patch.dict(os.environ, {"GATEWAY_METRICS_ENABLED": "true", "GATEWAY_METRICS_TOKEN_FILE": str(self.path)})
        self.env.start()
        self.addCleanup(self.env.stop)
        fake = Routes()
        register_health_routes(fake)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=Starlette(routes=fake.routes)), base_url="http://copy.invalid")
        self.addAsyncCleanup(self.client.aclose)
        self.render = patch("gateway_mcp.routes.health.metrics_response", return_value=b"gateway_example 1\n").start()
        self.addCleanup(patch.stopall)

    async def test_default_closed_even_with_gateway_auth_disabled(self):
        os.environ.pop("GATEWAY_METRICS_ENABLED", None)
        os.environ["GATEWAY_AUTH_ENABLED"] = "false"
        response = await self.client.get("/metrics", headers={"Authorization": "Bearer " + self.token})
        self.assertEqual(response.status_code, 404)
        self.render.assert_not_called()

    async def test_spoofed_forwarding_cookie_query_and_user_token_do_not_grant_access(self):
        for headers in ({}, {"X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1", "Cookie": "gateway_session=pretend"}, {"Authorization": "Bearer pretend-employee-oauth-token"}, {"Authorization": "Bearer \u0422\u043e\u043a\u0435\u043d".encode("utf-8")}):
            response = await self.client.get("/metrics?token=" + self.token, headers=headers)
            self.assertEqual(response.status_code, 401)
            self.assertNotIn(self.token, response.text)
        self.render.assert_not_called()

    async def test_service_key_returns_prometheus_without_caching(self):
        response = await self.client.get("/metrics", headers={"Authorization": "Bearer " + self.token})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "gateway_example 1\n")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn("text/plain", response.headers["content-type"])
        self.assertNotIn(self.token, response.text)

    async def test_invalid_configuration_fails_closed(self):
        for value in ("", "relative-token", str(self.path) + "-missing", self.temp.name):
            os.environ["GATEWAY_METRICS_TOKEN_FILE"] = value
            response = await self.client.get("/metrics", headers={"Authorization": "Bearer " + self.token})
            self.assertEqual(response.status_code, 503)
            self.assertNotIn(value or "metrics-token", response.text)
        self.render.assert_not_called()

    async def test_invalid_file_content_fails_closed(self):
        for content in ("short", "x" * 300, "wrong\nformat", "\u0441\u0435\u043a\u0440\u0435\u0442"):
            self.path.write_text(content, encoding="utf-8")
            self.assertEqual((await self.client.get("/metrics")).status_code, 503)
        self.render.assert_not_called()

    @unittest.skipIf(os.name != "posix", "Linux permissions checked in isolated Linux copy")
    async def test_world_readable_or_symlink_secret_rejected(self):
        self.path.chmod(0o644)
        self.assertEqual((await self.client.get("/metrics")).status_code, 503)
        self.path.chmod(0o600)
        link = self.path.with_name("link")
        link.symlink_to(self.path)
        os.environ["GATEWAY_METRICS_TOKEN_FILE"] = str(link)
        self.assertEqual((await self.client.get("/metrics")).status_code, 503)
        self.render.assert_not_called()

    async def test_health_contract_unchanged(self):
        with patch("gateway_mcp.routes.health.storage_health", return_value={"enabled": True, "ok": True}), patch("gateway_mcp.routes.health.auth_enabled", return_value=True):
            response = await self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertTrue(response.json()["auth_enabled"])

    async def test_unhealthy_database_still_returns_503(self):
        with patch("gateway_mcp.routes.health.storage_health", return_value={"enabled": True, "ok": False}), patch("gateway_mcp.routes.health.auth_enabled", return_value=True):
            response = await self.client.get("/healthz")
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json()["ok"])
