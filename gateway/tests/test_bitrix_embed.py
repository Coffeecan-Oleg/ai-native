"""Synthetic current-user frame and cross-site browser-session boundaries."""
import hashlib
import json
import os
import unittest
from unittest.mock import MagicMock,patch
from urllib.parse import urlencode

import httpx
from starlette.applications import Starlette
from starlette.responses import HTMLResponse,PlainTextResponse,RedirectResponse
from starlette.routing import Route

from gateway_mcp.services import auth,bitrix_identity as identity,bitrix_embed as embed,corporate_access as access,storage
from gateway_mcp.routes.auth import register_auth_routes
from gateway_mcp.services.policy import GatewayActor
from gateway_mcp.web import web_actor,login_redirect,clear_gateway_cookie
from test_bitrix_identity import ENV,MEMBER,SUBJECT
from test_bitrix_auth_flow import FakeServer

BASE='https://gateway.example.invalid'
SOURCE='synthetic-frame-source-token'
DIGEST=hashlib.sha256(SOURCE.encode()).hexdigest()
FIELDS={'AUTH_ID':'synthetic-personal-access','REFRESH_ID':'synthetic-personal-refresh',
    'AUTH_EXPIRES':'3600','APPLICATION_SCOPE':'user_brief','member_id':MEMBER,
    'APPLICATION_TOKEN':SOURCE,'USER_ID':'3318'}
QUERY={'DOMAIN':identity.PORTAL,'APP_SID':'s'*32}


class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ,ENV|{'GATEWAY_BITRIX_EMBED_ENABLED':'true'}))
        identity._CACHE.clear()
        self.employee={'subject':SUBJECT,'user_id':'42','member_id':MEMBER,'name':'Synthetic Employee','verified_at':0}
        self.row={'ID':'42','ACTIVE':True,'USER_TYPE':'employee','UF_DEPARTMENT':[1],'NAME':'Synthetic'}
        self.info={'CODE':ENV['GATEWAY_BITRIX_CLIENT_ID'],'ID':7,'STATUS':'L','INSTALLED':True}
        self.calls=[]
        self.current_id='42'
        def api(url,data):
            self.calls.append((url,data))
            if url.endswith('/scope.json'):return {'result':['user_brief']}
            if url.endswith('/app.info.json'):return {'result':self.info}
            if url.endswith('/user.current.json'):return {'result':{'ID':self.current_id,'ACTIVE':True}}
            if url.endswith('/user.get.json'):return {'result':[self.row]}
            raise AssertionError('unlisted endpoint')
        self.api=self.enterContext(patch.object(identity,'_request',side_effect=api))
        self.record=self.enterContext(patch.object(embed,'_source_record',return_value={'scopes':['user_brief'],'access_token':DIGEST}))
        self.save=self.enterContext(patch.object(identity,'_save'))

    def test_launch_uses_personal_api_identity_not_claimed_administrator(self):
        employee=embed.complete_launch(FIELDS,QUERY)
        self.assertEqual(employee['subject'],SUBJECT)
        self.assertEqual(self.save.call_args.args[1]['user_id'],'42')
        self.assertNotIn('AUTH_ID',employee)
        self.assertTrue(all(url.startswith('https://'+identity.PORTAL+'/rest/') for url,_ in self.calls))

    def test_source_mismatch_is_rejected_before_api_or_storage(self):
        with self.assertRaises(PermissionError):embed.complete_launch(FIELDS|{'APPLICATION_TOKEN':'another-synthetic-token'},QUERY)
        self.api.assert_not_called();self.save.assert_not_called()

    def test_wrong_portal_member_and_missing_session_cannot_launch(self):
        for query in [QUERY|{'DOMAIN':'foreign.bitrix24.ru'},QUERY|{'APP_SID':''}]:
            with self.subTest(query=query),self.assertRaises(PermissionError):embed.complete_launch(FIELDS,query)
        with self.assertRaises(PermissionError):embed.complete_launch(FIELDS|{'member_id':'b'*32},QUERY)
        self.save.assert_not_called()

    def test_other_application_and_uninstalled_app_are_rejected(self):
        for field,value in [('CODE','local.other.12345678'),('STATUS','F'),('INSTALLED',False)]:
            with self.subTest(field=field),patch.dict(self.info,{field:value}),self.assertRaises(PermissionError):embed.complete_launch(FIELDS,QUERY)
        self.save.assert_not_called()

    def test_inactive_external_or_missing_department_never_gets_saved(self):
        for change in [{'ACTIVE':False},{'USER_TYPE':'extranet'},{'UF_DEPARTMENT':[]}]:
            with self.subTest(change=change),patch.dict(self.row,change),self.assertRaises(PermissionError):embed.complete_launch(FIELDS,QUERY)
        self.save.assert_not_called()

    def test_missing_pin_disabled_feature_and_bad_ttl_fail_closed(self):
        for change in [{'GATEWAY_BITRIX_MEMBER_ID':''},{'GATEWAY_BITRIX_EMBED_ENABLED':'false'}]:
            with patch.dict(os.environ,change),self.assertRaises(PermissionError):embed.complete_launch(FIELDS,QUERY)
        for ttl in ['0','3601','no','-1']:
            with self.subTest(ttl=ttl),self.assertRaises(PermissionError):embed.complete_launch(FIELDS|{'AUTH_EXPIRES':ttl},QUERY)

    def test_source_enrollment_requires_exact_verified_owner_and_never_overwrites(self):
        conn=MagicMock()
        with patch('gateway_mcp.services.storage_core._connect',return_value=conn),patch.object(embed,'_source_record',return_value=None),patch.object(storage,'save_user_oauth_token',return_value=True) as save:
            with self.assertRaises(PermissionError):embed._pin_source(self.employee,DIGEST,7)
            save.assert_not_called()
            embed._pin_source(self.employee|{'user_id':'3318'},DIGEST,7)
            self.assertEqual(save.call_args.kwargs['access_token'],DIGEST)
            self.assertNotIn(SOURCE,str(save.call_args))
        with patch('gateway_mcp.services.storage_core._connect',return_value=conn),patch.object(storage,'save_user_oauth_token') as save:
            embed._pin_source(self.employee,DIGEST,7)
            save.assert_not_called()

    def test_first_launch_cannot_enroll_a_colleague_or_use_claimed_owner_id(self):
        with patch.object(embed,'_source_record',return_value=None),patch('gateway_mcp.services.storage_core._connect',return_value=MagicMock()),patch.object(storage,'save_user_oauth_token') as save:
            with self.assertRaises(PermissionError):embed.complete_launch(FIELDS,QUERY)
            save.assert_not_called();self.save.assert_not_called()

    def test_first_verified_owner_launch_enrolls_only_hash_then_saves_personal_tokens(self):
        self.current_id='3318';self.row['ID']='3318'
        with patch.object(embed,'_source_record',return_value=None),patch('gateway_mcp.services.storage_core._connect',return_value=MagicMock()),patch.object(storage,'save_user_oauth_token',return_value=True) as save:
            employee=embed.complete_launch(FIELDS,QUERY)
            self.assertTrue(employee['subject'].endswith(':3318'))
            self.assertEqual(save.call_args.kwargs['access_token'],DIGEST)
            self.assertEqual(self.save.call_args.args[1]['subject'],employee['subject'])


class FrameHttpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ,ENV|{'GATEWAY_BITRIX_EMBED_ENABLED':'true','GATEWAY_PUBLIC_URL':BASE,
            'GATEWAY_ISSUER_URL':BASE,'GATEWAY_RESOURCE_URL':BASE+'/mcp','GATEWAY_JWT_SECRET':'synthetic-test-secret-not-production',
            'GATEWAY_AUTH_ENABLED':'true','GATEWAY_DATABASE_URL':'','GATEWAY_MCP_OAUTH_SCOPES':'','GATEWAY_MCP_OAUTH_TOOLS':''}))
        self.employee={'subject':SUBJECT,'user_id':'42','member_id':MEMBER,'name':'Synthetic','verified_at':0}
        self.enterContext(patch.object(identity,'current_employee',return_value=self.employee))
        self.enterContext(patch.object(storage,'list_active_scope_grants',return_value=[]))
        self.enterContext(patch('gateway_mcp.services.observability.audit_event'))
        actor=access.fresh_actor(SUBJECT)
        self.token=auth.mint_gateway_token(actor,ttl_seconds=3600,browser_channel=embed.CHANNEL)
        self.normal=auth.mint_gateway_token(actor)
        self.post_calls=0
        async def panel(request):
            actor=web_actor(request)
            if actor is None:return login_redirect('/admin')
            if request.method=='POST':
                self.post_calls+=1
                body=await request.body()
                if request.cookies.get('example_csrf')!='nonce' or body!=b'csrf=nonce':return PlainTextResponse('csrf failed',status_code=403)
            response=HTMLResponse('own panel',headers={'X-Frame-Options':'DENY','Content-Security-Policy':"default-src 'self'; frame-ancestors 'none'; form-action 'self'"})
            response.set_cookie('example_csrf','nonce',httponly=True,secure=True,samesite='strict',path='/admin')
            return response
        fake=FakeServer();register_auth_routes(fake)
        async def policies(request):
            response=HTMLResponse('policies')
            response.headers.append('Content-Security-Policy',"default-src 'none'; frame-ancestors 'none'")
            response.headers.append('Content-Security-Policy',"script-src 'none'; frame-ancestors 'none'")
            return response
        fake.routes += [Route('/admin',panel,methods=['GET','POST']),Route('/admin/policies',policies)]
        self.app=embed.BitrixFrameMiddleware(Starlette(routes=fake.routes))

    def client(self):return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url=BASE,follow_redirects=False)

    async def test_personal_launch_stays_in_frame_and_issues_partitioned_browser_cookie(self):
        complete=self.enterContext(patch.object(embed,'complete_launch',return_value=self.employee))
        async with self.client() as client:
            response=await client.post('/auth/bitrix24/callback',params=QUERY,data=FIELDS,headers={'Origin':embed.PORTAL_ORIGIN})
        self.assertEqual(response.status_code,200);self.assertNotIn('location',response.headers)
        self.assertIn('bitrix-frame-status',response.text)
        cookie=response.headers['set-cookie']
        for flag in ['Secure','HttpOnly','SameSite=None','Partitioned']:self.assertIn(flag,cookie)
        self.assertNotIn(FIELDS['AUTH_ID'],cookie+response.text)
        self.assertEqual(complete.call_count,1)

    async def test_installation_post_is_still_inert_with_frame_feature(self):
        complete=self.enterContext(patch.object(embed,'complete_launch'))
        async with self.client() as client:
            response=await client.post('/auth/bitrix24/install',data=FIELDS)
        self.assertEqual(response.status_code,200);complete.assert_not_called()
        self.assertNotIn('gateway_token',response.headers.get('set-cookie',''))

    async def test_untrusted_origin_duplicate_body_and_oversized_body_are_rejected(self):
        complete=self.enterContext(patch.object(embed,'complete_launch'))
        async with self.client() as client:
            for origin in ['https://foreign.example.invalid','null','']:
                response=await client.post('/auth/bitrix24/callback',params=QUERY,data=FIELDS,headers={'Origin':origin})
                self.assertEqual(response.status_code,403)
            for body in ['AUTH_ID=one&AUTH_ID=two','x='+'a'*embed.MAX_BODY]:
                response=await client.post('/auth/bitrix24/callback',params=QUERY,content=body,headers={'Origin':embed.PORTAL_ORIGIN,'Content-Type':'application/x-www-form-urlencoded'})
                self.assertEqual(response.status_code,403)
            complete.assert_not_called()

    async def test_frame_headers_allow_only_portal_and_keep_csrf_cookie(self):
        async with self.client() as client:
            client.cookies.set(embed.COOKIE,self.token)
            response=await client.get('/admin')
        self.assertEqual(response.status_code,200)
        self.assertNotIn('x-frame-options',response.headers)
        self.assertIn('frame-ancestors '+embed.PORTAL_ORIGIN,response.headers['content-security-policy'])
        self.assertIn("default-src 'self'",response.headers['content-security-policy'])
        self.assertIn('example_csrf=nonce',response.headers['set-cookie'])
        self.assertIn('SameSite=None',response.headers['set-cookie']);self.assertIn('Partitioned',response.headers['set-cookie'])

    async def test_ordinary_browser_keeps_original_headers_and_strict_csrf(self):
        async with self.client() as client:
            client.cookies.set('gateway_token',self.normal)
            response=await client.get('/admin')
        self.assertEqual(response.headers['x-frame-options'],'DENY')
        self.assertIn('SameSite=strict',response.headers['set-cookie'])
        self.assertNotIn('Partitioned',response.headers['set-cookie'])

    async def test_frame_cookie_cannot_hold_ordinary_browser_token_or_vice_versa(self):
        for name,token in [(embed.COOKIE,self.normal),('gateway_token',self.token)]:
            async with self.client() as client:
                client.cookies.set(name,token)
                response=await client.get('/admin',headers={'Sec-Fetch-Dest':'iframe'})
            self.assertEqual(response.status_code,401)

    async def test_frame_logout_keeps_cookie_deletion_without_external_navigation(self):
        revoke=self.enterContext(patch('gateway_mcp.routes.auth.revoke_mcp_oauth_token'))
        async with self.client() as client:
            client.cookies.set(embed.COOKIE,self.token)
            response=await client.get('/auth/logout')
        self.assertEqual(response.status_code,200)
        self.assertIn('Вы вышли',response.text)
        revoke.assert_called_once_with(self.token)
        self.assertNotIn('location',response.headers)
        deleted=next(value for value in response.headers.get_list('set-cookie') if value.startswith(embed.COOKIE+'='))
        for flag in ['Max-Age=0','SameSite=None','Partitioned','HttpOnly']:self.assertIn(flag,deleted)

    async def test_frame_logout_does_not_revoke_standalone_session(self):
        revoke=self.enterContext(patch('gateway_mcp.routes.auth.revoke_mcp_oauth_token'))
        async with self.client() as client:
            client.cookies.set(embed.COOKIE,self.token);client.cookies.set('gateway_token',self.normal)
            response=await client.post('/auth/logout',headers={'Origin':BASE})
        self.assertEqual(response.status_code,200);revoke.assert_called_once_with(self.token)

    async def test_standalone_logout_still_uses_original_redirect_and_token(self):
        revoke=self.enterContext(patch('gateway_mcp.routes.auth.revoke_mcp_oauth_token'))
        async with self.client() as client:
            client.cookies.set('gateway_token',self.normal)
            response=await client.get('/auth/logout')
        self.assertEqual(response.status_code,303)
        self.assertEqual(response.headers['location'],'/auth/logged-out')
        revoke.assert_called_once_with(self.normal)

    async def test_all_existing_csp_policies_remain_effective(self):
        async with self.client() as client:
            client.cookies.set(embed.COOKIE,self.token)
            response=await client.get('/admin/policies')
        values=response.headers.get_list('content-security-policy')
        self.assertEqual(len(values),2)
        self.assertTrue(any("default-src 'none'" in value for value in values))
        self.assertTrue(any("script-src 'none'" in value for value in values))
        self.assertTrue(all('frame-ancestors '+embed.PORTAL_ORIGIN in value for value in values))

    async def test_frame_unsafe_requests_require_origin_and_existing_csrf_check(self):
        async with self.client() as client:
            client.cookies.set(embed.COOKIE,self.token)
            for origin in ['',embed.PORTAL_ORIGIN,'https://foreign.example.invalid']:
                response=await client.post('/admin',content='csrf=nonce',headers={'Origin':origin})
                self.assertEqual(response.status_code,403)
            self.assertEqual(self.post_calls,0)
            response=await client.post('/admin',content='csrf=nonce',headers={'Origin':BASE})
            self.assertEqual(response.status_code,403)
            client.cookies.set('example_csrf','nonce')
            response=await client.post('/admin',content='csrf=nonce',headers={'Origin':BASE})
            self.assertEqual(response.status_code,200)

    async def test_blocked_frame_cookie_returns_inline_message_without_external_redirect(self):
        async with self.client() as client:
            response=await client.get('/admin',headers={'Sec-Fetch-Dest':'iframe'})
        self.assertEqual(response.status_code,401)
        self.assertNotIn('location',response.headers)
        self.assertNotIn('target="_blank"',response.text)

    async def test_frame_browser_token_cannot_be_used_as_mcp_bearer(self):
        self.assertIsNone(await auth.GatewayJwtVerifier().verify_token(self.token))
        mcp_token=auth.mint_gateway_token(access.delegated(access.fresh_actor(SUBJECT)))
        self.assertIsNotNone(await auth.GatewayJwtVerifier().verify_token(mcp_token))

    async def test_explicit_disable_restores_inert_callback(self):
        self.enterContext(patch.dict(os.environ,{'GATEWAY_BITRIX_EMBED_ENABLED':'false'}))
        complete=self.enterContext(patch.object(embed,'complete_launch'))
        async with self.client() as client:
            response=await client.post('/auth/bitrix24/callback',data=FIELDS)
        self.assertEqual(response.status_code,200);complete.assert_not_called()

    async def test_real_mcp_app_factory_is_wrapped_without_losing_lifespan(self):
        from gateway_mcp.mcp_runtime import create_server
        server=create_server(name='frame-test',auth=None,token_verifier=None)
        wrapped=server.streamable_http_app()
        self.assertIsInstance(wrapped,embed.BitrixFrameMiddleware)
        self.assertIsInstance(wrapped.app,Starlette)
        self.assertIsNotNone(wrapped.app.router.lifespan_context)
