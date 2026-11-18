import base64
import hashlib
import json
import os
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit,parse_qs

import httpx
from starlette.applications import Starlette
from starlette.routing import Route
from gateway_mcp.services import auth, corporate_access as access, bitrix_identity as identity, storage
from gateway_mcp.routes.auth import register_auth_routes
from test_bitrix_identity import ENV,MEMBER,SUBJECT

BASE='https://gateway.example.invalid'


class FakeServer:
    def __init__(self):self.routes=[]
    def custom_route(self,path,methods,**kwargs):
        def register(handler):
            self.routes.append(Route(path,handler,methods=methods))
            return handler
        return register


class BrowserOAuthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ,ENV|{'GATEWAY_PUBLIC_URL':BASE,'GATEWAY_ISSUER_URL':BASE,
            'GATEWAY_RESOURCE_URL':BASE+'/mcp','GATEWAY_AUTH_ENABLED':'true','GATEWAY_JWT_SECRET':'synthetic-test-secret-not-production',
            'GATEWAY_DATABASE_URL':'','GATEWAY_MCP_OAUTH_SCOPES':'','GATEWAY_MCP_OAUTH_TOOLS':''}))
        self.enterContext(patch.object(auth,'save_postgres_oauth_state',return_value=False))
        self.enterContext(patch.object(auth,'pop_postgres_oauth_state',return_value=None))
        self.enterContext(patch.object(auth,'save_gateway_oauth_refresh_token',return_value=True))
        self.enterContext(patch.object(storage,'list_active_scope_grants',return_value=[]))
        self.enterContext(patch('gateway_mcp.services.observability.audit_event'))
        self.employee={'subject':SUBJECT,'user_id':'42','member_id':MEMBER,'name':'Synthetic','verified_at':time.time()}
        self.enterContext(patch.object(identity,'current_employee',return_value=self.employee))
        self.complete=self.enterContext(patch.object(identity,'complete_login',return_value=self.employee))
        auth._OAUTH_STATES.clear()
        server=FakeServer();register_auth_routes(server)
        self.app=Starlette(routes=server.routes)

    async def client(self):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url=BASE,follow_redirects=False)

    async def start(self,client,next_url='/admin'):
        response=await client.get('/auth/bitrix24/login',params={'next':next_url})
        self.assertEqual(response.status_code,303)
        self.assertEqual(urlsplit(response.headers['location']).hostname,'akmetron.bitrix24.ru')
        cookie=response.headers['set-cookie'].lower()
        self.assertIn('httponly',cookie);self.assertIn('secure',cookie);self.assertIn('samesite=lax',cookie)
        return parse_qs(urlsplit(response.headers['location']).query)['state'][0]

    async def test_browser_login_gets_own_gateway_cookie_not_upstream_token(self):
        async with await self.client() as client:
            state=await self.start(client)
            response=await client.get('/auth/bitrix24/callback',params={'state':state,'code':'fixture','domain':identity.PORTAL,'member_id':MEMBER})
            self.assertEqual(response.status_code,303)
            self.assertEqual(response.headers['location'],'/admin')
            self.assertIn('gateway_token=',response.headers['set-cookie'])
            self.assertEqual(response.headers['referrer-policy'],'no-referrer')
            token=client.cookies.get('gateway_token')
            claims=auth.decode_gateway_token(token,verify=False)
            self.assertEqual(claims['sub'],SUBJECT)
            self.assertNotIn('refresh_token',claims)

    async def test_callback_without_browser_binding_cannot_exchange_code(self):
        async with await self.client() as client:
            state=await self.start(client)
            client.cookies.clear()
            response=await client.get('/auth/bitrix24/callback',params={'state':state,'code':'fixture','domain':identity.PORTAL,'member_id':MEMBER})
            self.assertEqual(response.status_code,403);self.complete.assert_not_called()

    async def test_foreign_portal_callback_is_rejected_before_exchange(self):
        async with await self.client() as client:
            state=await self.start(client)
            response=await client.get('/auth/bitrix24/callback',params={'state':state,'code':'fixture','domain':'foreign.bitrix24.ru','member_id':MEMBER})
            self.assertEqual(response.status_code,403);self.complete.assert_not_called()

    async def test_replayed_state_cannot_create_second_login(self):
        async with await self.client() as client:
            state=await self.start(client)
            params={'state':state,'code':'fixture','domain':identity.PORTAL,'member_id':MEMBER}
            self.assertEqual((await client.get('/auth/bitrix24/callback',params=params)).status_code,303)
            client.cookies.set(identity.FLOW_COOKIE,state,domain='gateway.example.invalid',path='/')
            self.assertEqual((await client.get('/auth/bitrix24/callback',params=params)).status_code,403)
            self.assertEqual(self.complete.call_count,1)

    async def test_external_next_url_is_not_used(self):
        async with await self.client() as client:
            state=await self.start(client,'https://attacker.invalid')
            response=await client.get('/auth/bitrix24/callback',params={'state':state,'code':'fixture','domain':identity.PORTAL,'member_id':MEMBER})
            self.assertEqual(response.headers['location'],'/admin')

    async def test_login_failure_audits_only_known_reason(self):
        async with await self.client() as client:
            state=await self.start(client)
            with patch.object(identity,'complete_login',side_effect=PermissionError('bitrix_internal_employee_required')), patch('gateway_mcp.services.observability.audit_event') as audit:
                response=await client.get('/auth/bitrix24/callback',params={'state':state,'code':'synthetic-private-code','domain':identity.PORTAL,'member_id':MEMBER})
            self.assertEqual(response.status_code,403)
            self.assertEqual(audit.call_args.kwargs['error'],'bitrix_internal_employee_required')
            self.assertEqual(audit.call_args.kwargs['actor'].subject,'anonymous')
            self.assertNotIn('synthetic-private-code',str(audit.call_args))

    async def test_unknown_login_exception_never_enters_audit(self):
        async with await self.client() as client:
            state=await self.start(client)
            with patch.object(identity,'complete_login',side_effect=RuntimeError('synthetic-private-upstream-secret')), patch('gateway_mcp.services.observability.audit_event') as audit:
                response=await client.get('/auth/bitrix24/callback',params={'state':state,'code':'fixture','domain':identity.PORTAL,'member_id':MEMBER})
            self.assertEqual(response.status_code,403)
            self.assertEqual(audit.call_args.kwargs['error'],'bitrix_callback_failed')
            self.assertNotIn('synthetic-private-upstream-secret',str(audit.call_args))

    async def test_existing_mcp_pkce_flow_uses_bitrix_and_keeps_scope_ceiling(self):
        client_id=auth.register_oauth_client({'redirect_uris':['http://127.0.0.1:5555/callback']})['client_id']
        verifier='v'*43
        challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        async with await self.client() as client:
            response=await client.get('/oauth/authorize',params={'response_type':'code','client_id':client_id,
                'redirect_uri':'http://127.0.0.1:5555/callback','code_challenge':challenge,'code_challenge_method':'S256',
                'scope':'skills:read tools:read','resource':BASE+'/mcp','state':'client-fixture'})
            self.assertEqual(response.status_code,307)
            state=parse_qs(urlsplit(response.headers['location']).query)['state'][0]
            self.assertEqual(urlsplit(response.headers['location']).hostname,identity.PORTAL)
            callback=await client.get('/auth/bitrix24/callback',params={'state':state,'code':'fixture','domain':identity.PORTAL,'member_id':MEMBER})
            self.assertEqual(callback.status_code,303)
            target=urlsplit(callback.headers['location']);query=parse_qs(target.query)
            self.assertEqual(target.netloc,'127.0.0.1:5555');self.assertEqual(query['state'],['client-fixture'])
            result=auth.exchange_mcp_authorization_code(code=query['code'][0],client_id=client_id,
                redirect_uri='http://127.0.0.1:5555/callback',code_verifier=verifier,resource=BASE+'/mcp')
            self.assertEqual(set(result['scope'].split()),{'skills:read','tools:read'})
            claims=auth.decode_gateway_token(result['access_token'],verify=False)
            self.assertEqual(claims['sub'],SUBJECT);self.assertEqual(claims['groups'],[])
            self.assertNotIn('access:admin',claims['scope'])
            self.assertIsInstance(claims['tool_allowlist'],list)

    async def test_legacy_yandex_entry_switches_and_callback_is_closed(self):
        async with await self.client() as client:
            response=await client.get('/auth/yandex/login')
            self.assertTrue(response.headers['location'].startswith('/auth/bitrix24/login'))
            response=await client.get('/auth/yandex/callback',params={'code':'old-fixture'})
            self.assertEqual(response.status_code,403)

    async def test_portal_install_payload_cannot_log_in_as_saved_administrator(self):
        async with await self.client() as client:
            for path in ['/auth/bitrix24/install','/auth/bitrix24/callback']:
                response=await client.post(path,data={'AUTH_ID':'synthetic-admin-token','REFRESH_ID':'synthetic-admin-refresh','USER_ID':'3318'})
                self.assertEqual(response.status_code,200)
                self.assertNotIn('gateway_token',response.headers.get('set-cookie',''))
                self.assertNotIn('synthetic-admin',response.text)
                self.assertIn('frame-ancestors https://akmetron.bitrix24.ru',response.headers['content-security-policy'])
            self.complete.assert_not_called()

    async def test_gateway_refresh_rechecks_employee_and_never_uses_yandex_alias(self):
        actor=access.delegated(access.fresh_actor(SUBJECT))
        payload={'actor_payload':auth.actor_payload(actor),'client_id':'fixture','resource':BASE+'/mcp'}
        with patch.object(auth,'_consume_refresh_token',return_value=payload),patch.object(auth,'actor_from_user_info') as old:
            result=auth.exchange_mcp_refresh_token(refresh_token=auth.OAUTH_REFRESH_TOKEN_PREFIX+'fixture',client_id='fixture',resource=BASE+'/mcp')
            self.assertEqual(set(result['scope'].split()),access.BASE_SCOPES);old.assert_not_called()
        with patch.object(auth,'_consume_refresh_token',return_value=payload),patch.object(identity,'current_employee',side_effect=PermissionError('dismissed')):
            with self.assertRaises(PermissionError):auth.exchange_mcp_refresh_token(refresh_token=auth.OAUTH_REFRESH_TOKEN_PREFIX+'fixture',client_id='fixture',resource=BASE+'/mcp')

    async def test_baseline_mcp_profile_reads_only_exact_subject_packages(self):
        from gateway_mcp.tools import access_requests as tools, runtime
        from gateway_mcp.services import access_requests as service
        functions={}
        class Mcp:
            def tool(self,**kwargs):
                def register(function):functions[function.__name__]=function;return function
                return register
        tools.register_access_request_tools(Mcp())
        actor=access.delegated(access.fresh_actor(SUBJECT))
        self.assertNotIn('access:request',actor.scopes)
        with patch.object(auth,'current_actor',return_value=actor), patch.object(runtime,'finish_tool'), patch.object(service,'admin_list_access_packages',return_value={'packages':[]}) as packages, patch.object(service,'list_access_requests',return_value=[]):
            result=json.loads(await functions['gateway_access_profile']())
        self.assertTrue(result['ok']);self.assertEqual(result['actor']['subject'],SUBJECT)
        self.assertEqual(packages.call_count,1)
        self.assertEqual(packages.call_args.kwargs['subject_key'],SUBJECT)

    async def test_legacy_profile_still_requires_access_request_scope(self):
        from gateway_mcp.tools import access_requests as tools, runtime
        from gateway_mcp.services.policy import GatewayActor
        functions={}
        class Mcp:
            def tool(self,**kwargs):
                def register(function):functions[function.__name__]=function;return function
                return register
        tools.register_access_request_tools(Mcp())
        actor=GatewayActor(subject='yandex:42',scopes=('skills:read',))
        with patch.dict(os.environ,{'GATEWAY_LOGIN_PROVIDER':'yandex'}),patch.object(auth,'current_actor',return_value=actor),patch.object(runtime,'finish_tool'),patch.object(runtime,'deny'):
            with self.assertRaises(PermissionError):await functions['gateway_access_profile']()


if __name__=='__main__':unittest.main()
