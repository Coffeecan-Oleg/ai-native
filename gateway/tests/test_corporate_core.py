"""Corporate-only routes, identity directory and transport attribution."""
import json
import os
import time
import unittest
from unittest.mock import patch

import httpx
from starlette.applications import Starlette

from gateway_mcp.routes import corporate_ui, admin_ui
from gateway_mcp.routes.credentials import register_credentials_routes
from gateway_mcp.routes.auth import register_auth_routes
from gateway_mcp.services import corporate_access as access, bitrix_identity as identity, storage
from gateway_mcp.services import storage_admin_console as directory
from gateway_mcp.services.policy import GatewayActor
from gateway_mcp.tools.discovery import register_discovery_tools
from test_bitrix_identity import ENV,SUBJECT
from test_bitrix_auth_flow import FakeServer,BASE


class CorporateCoreTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ,ENV))

    def test_both_owner_and_employee_lose_demo_route_and_scope(self):
        for owner in (False,True):
            with self.subTest(owner=owner), patch.dict(os.environ,{'GATEWAY_BITRIX_OWNER_USER_ID':'42' if owner else '3318'}), patch.object(identity,'current_employee',return_value={'user_id':'42','name':'Synthetic'}), patch.object(storage,'list_active_scope_grants',return_value=[]):
                actor=access.delegated(access.fresh_actor(SUBJECT))
                self.assertNotIn('platforma:write',actor.scopes)
                self.assertNotIn('platforma:read',actor.scopes)
                for route in ('platforma.requests.list','platforma.requests.create','platforma.requests.update','platforma.unisender-demo.stats.get'):
                    self.assertNotIn(route,actor.tool_allowlist)
                    with self.assertRaises(PermissionError):
                        access.require_route_access(actor,{'name':route},{})

    def test_corporate_navigation_has_no_demo_or_legacy_identity(self):
        for admin in (True,False):
            nav=admin_ui._navigation(active='',is_admin=admin,training_enabled=True,training_reader=not admin)
            for forbidden in ('Учебные','/platforma/requests','/credentials','/admin/platforma-readers','Яндекс'):
                self.assertNotIn(forbidden,nav)
            self.assertIn('/platforma/skills',nav)
            self.assertEqual('/admin/users' in nav,admin)

    def test_credential_forms_are_not_registered_in_corporate_mode(self):
        server=FakeServer();register_credentials_routes(server)
        self.assertEqual(server.routes,[])

    def test_roster_is_parameterized_and_never_reads_secrets(self):
        with patch.object(directory,'_rows',return_value=[]) as read:
            directory.admin_users_page(query="' OR 1=1 --")
        sql,params=read.call_args.args
        self.assertNotIn(params[0],sql)
        for forbidden in ('access_token_encrypted','refresh_token','select *','from access_requests'):
            self.assertNotIn(forbidden,sql)
        self.assertIn("decision = 'allow' and status = 'ok'",sql)
        self.assertIn('last_mcp_authorization_at',sql)

    def test_legacy_directory_keeps_original_queries(self):
        with patch.dict(os.environ,{'GATEWAY_LOGIN_PROVIDER':'yandex'}):
            self.assertEqual(directory._users_cte(),directory._USERS)
            server=FakeServer();register_credentials_routes(server)
            self.assertGreater(len(server.routes),0)

    def test_corporate_grants_never_merge_name_email_or_yandex_aliases(self):
        with patch.object(directory,'_rows',return_value=[]) as read:
            directory.admin_user_grants({'subject':SUBJECT,'email':'old@example.test','login':'Old User','yandex_id':'42'})
        for call in read.call_args_list:
            self.assertEqual(call.args[1][0],[SUBJECT])

    def test_roster_times_are_moscow_not_refresh_or_online(self):
        self.assertEqual(corporate_ui.roster_time('2026-11-03T10:00:00+00:00'),'03.11.2026 13:00')
        self.assertEqual(corporate_ui.roster_time(None),'—')
        self.assertEqual(corporate_ui.roster_time('2026-11-03T10:00:00'),'—')
        body=corporate_ui.roster_body([{'subject':SUBJECT,'login':'<script>','bitrix_user_id':'42','updated_at':'2026-11-03T10:00:00+00:00'}],'')
        self.assertIn('&lt;script&gt;',body)
        self.assertNotIn('<script>',body)
        self.assertNotIn('03.11.2026 13:00',body)

    def test_tool_audit_marks_channel_without_changing_arguments(self):
        from gateway_mcp import audit
        with patch.object(audit,'current_actor',return_value=GatewayActor(subject=SUBJECT)),patch.object(audit,'observe_tool_call'),patch.object(audit,'audit_event') as event:
            audit.finish_tool(tool='gateway_access_profile',system='gateway',started_at=time.monotonic(),status='ok',decision='allow',scope='skills:read',arguments={})
        self.assertEqual(event.call_args.kwargs['channel'],'mcp')
        self.assertEqual(event.call_args.kwargs['arguments'],{})


class CorporateHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_yandex_bind_routes_cannot_initiate_oauth(self):
        with patch.dict(os.environ,ENV):
            server=FakeServer();register_auth_routes(server)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=Starlette(routes=server.routes)),base_url=BASE) as client:
                for path in ('/auth/yandex/service-login','/auth/yandex/disk-login'):
                    response=await client.get(path)
                    self.assertEqual(response.status_code,410)
                    self.assertNotIn('location',response.headers)

    async def test_discovery_only_returns_current_authorized_routes(self):
        from gateway_mcp.tools import discovery
        class Tools:
            def tool(self,**kw):
                def register(fn):self.fn=fn;return fn
                return register
        server=Tools();register_discovery_tools(server)
        actor=GatewayActor(subject=SUBJECT,scopes=('tools:read','unisender:read'),tool_allowlist=('gateway_search_tools','unisender.getLists'))
        rows=[{'name':n} for n in ('platforma.requests.list','platforma.requests.create','platforma.unisender-demo.stats.get','unisender.getLists','unisender.getContactCount','unisender.sendEmail')]
        with patch.dict(os.environ,ENV),patch.object(discovery.ToolRun,'require_scope',return_value=actor),patch.object(discovery.ToolRun,'finish'),patch.object(discovery,'read_json',return_value={'tools':rows}):
            result=json.loads(await server.fn())
        self.assertEqual([r['name'] for r in result['tools']],['unisender.getLists'])


if __name__=='__main__':unittest.main()
