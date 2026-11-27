import os
import unittest
from unittest.mock import patch

from mcp.shared.memory import create_connected_server_and_client_session

from gateway_mcp.mcp_runtime import create_server
from gateway_mcp.tools.access_requests import register_access_request_tools
from gateway_mcp.tools.knowledge import register_knowledge_tools


class CompanyContextHintTests(unittest.IsolatedAsyncioTestCase):
    async def inspect_session(self, provider):
        with patch.dict(os.environ, {"GATEWAY_LOGIN_PROVIDER": provider, "GATEWAY_BITRIX_EMBED_ENABLED": "false"}):
            server = create_server(name="company-context-test", token_verifier=None, auth=None)
            register_access_request_tools(server)
            register_knowledge_tools(server)
            async with create_connected_server_and_client_session(server) as session:
                greeting = await session.initialize()
                tools = await session.list_tools()
                return greeting.instructions or "", {tool.name: tool for tool in tools.tools}

    async def test_corporate_hint_points_to_registered_tools_and_space_guidance(self):
        hint, tools = await self.inspect_session("bitrix24")
        for name in ("gateway_access_profile", "gateway_knowledge_search", "gateway_knowledge_document_get"):
            self.assertIn(name, hint)
            self.assertIn(name, tools)
        search = tools["gateway_knowledge_search"]
        self.assertIn("space", search.inputSchema["properties"])
        self.assertIn("space:organizational-awareness", search.description)
        self.assertNotIn("space:organizational-awareness", hint)
        self.assertNotIn("awareness/", hint)
        self.assertNotIn("7723827170", hint)

    async def test_legacy_greeting_and_tool_list(self):
        hint, legacy_tools = await self.inspect_session("yandex")
        self.assertNotIn("gateway_knowledge_search", hint)
        _, corporate_tools = await self.inspect_session("bitrix24")
        self.assertEqual(set(legacy_tools), set(corporate_tools))
