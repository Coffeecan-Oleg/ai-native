"""Synthetic HTTP/MCP regressions; no live identities, API calls or credentials."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mcp.types import CallToolResult, ServerResult, TextContent
from starlette.testclient import TestClient

from gateway_mcp.services import auth, bitrix_identity as identity, corporate_access as access, storage
from gateway_mcp.services import addon_guard
from gateway_mcp.services.policy import GatewayActor
from test_bitrix_identity import ENV, MEMBER, SUBJECT
import test_bitrix_embed as frame_tests

ROOT = Path(__file__).resolve().parents[1]
SPACE = 'space:organizational-awareness'


class CorporateKnowledgeProtocolTests(unittest.TestCase):
    def setUp(self):
        temp = self.enterContext(tempfile.TemporaryDirectory())
        self.temp = Path(temp)
        self.vault = self.temp / 'vault'
        root = self.vault / 'spaces' / 'organizational-awareness'
        data = json.loads((ROOT/'addons/platforma-training/example-addon-profile.json').read_text(encoding='utf-8'))
        documents = data['profiles']['test:owner']['documents'][SPACE]
        for name in documents:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('---\nid: fixture-'+str(len(name))+'\nspace: '+SPACE+'\ntitle: Shared fixture\n---\n# Shared fixture\nCORPORATE_ALLOWED\n', encoding='utf-8')
        (root/'unapproved.md').write_text('CORPORATE_ALLOWED '*1000+'UNAPPROVED_SECRET', encoding='utf-8')
        (root/'members.yaml').write_text('[{"subject":"OLD_PRIVATE_MEMBER","role":"admin"}]', encoding='utf-8')
        other = self.vault/'spaces'/'other'
        other.mkdir(); (other/'secret.md').write_text('FOREIGN_SECRET', encoding='utf-8')
        private = self.vault/'personal'/'other'
        private.mkdir(parents=True); (private/'secret.md').write_text('PERSONAL_SECRET', encoding='utf-8')
        self.profiles = self.temp/'profiles.json'
        self.profiles.write_text(json.dumps(data), encoding='utf-8')
        self.enterContext(patch.dict(os.environ, ENV | {
            'GATEWAY_BITRIX_EMBED_ENABLED':'true', 'GATEWAY_AUTH_ENABLED':'true',
            'GATEWAY_DATABASE_URL':'', 'GATEWAY_JWT_SECRET':'synthetic-regression-key-not-production',
            'GATEWAY_PUBLIC_URL':'https://gateway.example.invalid',
            'GATEWAY_ISSUER_URL':'https://gateway.example.invalid',
            'GATEWAY_RESOURCE_URL':'https://gateway.example.invalid/mcp',
            'GATEWAY_ADDON_PROFILES_FILE':str(self.profiles),
            'GATEWAY_PLATFORMA_OWNER_SUBJECT':'test:owner',
            'GATEWAY_PLATFORMA_ONBOARDING_ENABLED':'true',
            'GATEWAY_KNOWLEDGE_VAULT_PATH':str(self.vault),
            'GATEWAY_TOOLS_FILE':str(ROOT/'gateway-platforma-tools.json'),
            'GATEWAY_MCP_OAUTH_SCOPES':'', 'GATEWAY_MCP_OAUTH_TOOLS':'',
            'GATEWAY_AUDIT_LOG':str(self.temp/'audit.jsonl')}))
        self.enterContext(patch.object(identity, 'current_employee', return_value={
            'user_id':'42','subject':SUBJECT,'member_id':MEMBER,'name':'Synthetic Employee','verified_at':0}))
        self.grants = self.enterContext(patch.object(storage, 'list_active_scope_grants', return_value=[]))
        self.enterContext(patch.object(auth, 'gateway_oauth_access_token_revoked', return_value=False))
        self.enterContext(patch.object(identity, '_request', side_effect=AssertionError('NO_EXTERNAL_API')))
        self.actor = access.delegated(access.fresh_actor(SUBJECT))
        self.token = auth.mint_gateway_token(self.actor)
        from gateway_mcp.server import create_mcp
        self.http = self.enterContext(TestClient(create_mcp().streamable_http_app(), base_url='http://127.0.0.1:8000'))
        self.headers = {'Authorization':'Bearer '+self.token,'Accept':'application/json, text/event-stream'}
        init = self.rpc({'id':1,'method':'initialize','params':{
            'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'isolated-corporate-regression','version':'1'}}})
        self.headers['MCP-Protocol-Version'] = init['result']['protocolVersion']
        self.rpc({'method':'notifications/initialized'})
        self.seq = 10
        self.disk_before = self.snapshot()

    def snapshot(self):
        return {str(p.relative_to(self.vault)):hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.vault.rglob('*') if p.is_file() and '.usage-meter' not in p.parts}

    def rpc(self, body):
        response = self.http.post('/mcp', json={'jsonrpc':'2.0',**body}, headers=self.headers)
        self.assertIn(response.status_code,(200,202),response.text[:300])
        if response.headers.get('mcp-session-id'):
            self.headers['Mcp-Session-Id'] = response.headers['mcp-session-id']
        if not response.content:return {}
        if 'text/event-stream' in response.headers.get('content-type',''):
            return next(json.loads(line[5:].strip()) for line in response.text.splitlines()
                        if line.startswith('data:') and ('"result"' in line or '"error"' in line))
        return response.json()

    def tool(self, name, arguments=None):
        self.seq += 1
        envelope = self.rpc({'id':self.seq,'method':'tools/call','params':{'name':name,'arguments':arguments or {}}})
        self.assertNotIn('error',envelope)
        result = envelope['result']
        text = next(item['text'] for item in result['content'] if item['type']=='text')
        return result,json.loads(text)

    def test_shared_read_works_without_personal_memory_scope(self):
        self.assertIn('knowledge:read',self.actor.scopes)
        self.assertNotIn('memory:read',self.actor.scopes)
        result,payload = self.tool('gateway_knowledge_space_list')
        self.assertFalse(result.get('isError'),payload)
        self.assertEqual([s['space'] for s in payload['spaces']],[SPACE])
        self.assertNotIn('OLD_PRIVATE_MEMBER',json.dumps(result))

    def test_document_search_and_metadata_are_bounded_and_read_only(self):
        result,payload = self.tool('gateway_knowledge_document_get',{'space':SPACE,'document':'awareness/start.md'})
        self.assertFalse(result.get('isError'),payload)
        self.assertIn('CORPORATE_ALLOWED',payload['document']['content'])
        result,payload = self.tool('gateway_knowledge_search',{'space':SPACE,'query':'CORPORATE_ALLOWED','limit':1})
        self.assertFalse(result.get('isError'),payload)
        self.assertEqual(len(payload['documents']),1)
        self.assertNotIn('content',payload['documents'][0])
        self.assertNotIn('UNAPPROVED_SECRET',json.dumps(result))
        self.assertEqual(json.loads(result['structuredContent']['result']),payload)
        result,payload = self.tool('gateway_knowledge_space_get',{'space':SPACE})
        self.assertFalse(result.get('isError'),payload)
        self.assertNotIn('members',payload['space'])
        self.assertEqual(self.snapshot(),self.disk_before)

    def test_private_foreign_unapproved_and_write_calls_stay_denied(self):
        cases=[('gateway_knowledge_document_get',{'space':'personal:other','document':'secret.md'}),
               ('gateway_knowledge_space_get',{'space':'space:other'}),
               ('gateway_knowledge_search',{'space':'','query':''}),
               ('gateway_knowledge_document_get',{'space':SPACE,'document':'unapproved.md'}),
               ('gateway_knowledge_document_put',{'space':SPACE,'title':'x','content':'x'}),
               ('gateway_memory_search',{'scope':'user','subject':'other'}),
               ('gateway_knowledge_spaces',{}),
               ('gateway_knowledge_space_delete',{'space':SPACE})]
        for name,args in cases:
            with self.subTest(name=name,args=args):
                result,payload=self.tool(name,args)
                self.assertTrue(result.get('isError'),payload)
                for secret in ['UNAPPROVED_SECRET','PERSONAL_SECRET','FOREIGN_SECRET','OLD_PRIVATE_MEMBER']:
                    self.assertNotIn(secret,json.dumps(result))
        self.assertEqual(self.snapshot(),self.disk_before)

    def test_old_oauth_ceiling_requires_reauthentication_not_memory_grant(self):
        scopes=tuple(s for s in self.actor.scopes if s!='knowledge:read')
        old=GatewayActor(subject=SUBJECT,scopes=scopes,scope_ceiling=scopes,tool_allowlist=self.actor.tool_allowlist)
        self.headers['Authorization']='Bearer '+auth.mint_gateway_token(old)
        self.headers.pop('Mcp-Session-Id', None)
        self.rpc({'id':2,'method':'initialize','params':{'protocolVersion':'2025-03-26',
                  'capabilities':{},'clientInfo':{'name':'old-ceiling-fixture','version':'1'}}})
        self.rpc({'method':'notifications/initialized'})
        for name,args in [('gateway_knowledge_space_list',{}),('gateway_knowledge_document_get',{'space':SPACE,'document':'awareness/start.md'})]:
            result,payload=self.tool(name,args)
            self.assertTrue(result.get('isError'))
            self.assertEqual(payload['error'],'knowledge_scope_required')
            self.assertEqual(payload['required_scope'],'knowledge:read')
            self.assertTrue(payload['reauthentication_required'])

    def test_revoked_knowledge_scope_fails_closed(self):
        self.grants.return_value=[{'effect':'deny','scope':'knowledge:read'}]
        result,payload=self.tool('gateway_knowledge_space_list')
        self.assertTrue(result.get('isError'))
        self.assertEqual(payload['required_scope'],'knowledge:read')

    def test_new_oauth_scope_is_supported_without_admin_or_memory(self):
        from gateway_mcp.services.oauth_limits import limit_oauth_actor
        configured='skills:read tools:read tools:call telemetry:read telemetry:write knowledge:read'
        with patch.dict(os.environ,{'GATEWAY_SUPPORTED_SCOPES':configured,
                'GATEWAY_MCP_OAUTH_SCOPES':configured,
                'GATEWAY_MCP_OAUTH_TOOLS':' '.join(self.actor.tool_allowlist)}):
            self.assertIn('knowledge:read',auth.oauth_authorization_server_metadata()['scopes_supported'])
            limited=limit_oauth_actor(access.fresh_actor(SUBJECT),requested_scopes=['knowledge:read'])
            self.assertEqual(limited.scopes,('knowledge:read',))
            self.assertNotIn('memory:read',limited.scopes)
            self.assertNotIn('access:admin',limited.scopes)
            self.assertNotIn('gateway_knowledge_document_put',limited.tool_allowlist)

    def test_approved_path_cannot_follow_a_symlink_to_other_knowledge(self):
        path=self.vault/'spaces'/'organizational-awareness'/'awareness'/'start.md'
        path.unlink()
        try:
            path.symlink_to(self.vault/'personal'/'other'/'secret.md')
        except OSError as exc:
            self.skipTest('Symlinks unavailable on this test host: '+str(exc.winerror))
        result,payload=self.tool('gateway_knowledge_document_get',{'space':SPACE,'document':'awareness/start.md'})
        self.assertTrue(result.get('isError'))
        self.assertNotIn('PERSONAL_SECRET',json.dumps(result))


class FrameLaunchRegressionTests(frame_tests.FrameHttpTests):
    def setUp(self):
        super().setUp()
        self.fixture=self

    async def test_verified_launch_returns_initial_html_without_redirect(self):
        with patch.object(frame_tests.embed,'complete_launch',return_value=self.fixture.employee):
            async with self.fixture.client() as client:
                response=await client.post('/auth/bitrix24/callback',params=frame_tests.QUERY,
                    data=frame_tests.FIELDS,headers={'Origin':frame_tests.embed.PORTAL_ORIGIN})
        self.assertEqual(response.status_code,200)
        self.assertNotIn('location',response.headers)
        self.assertIn('bitrix-frame-status',response.text)
        self.assertIn('/auth/bitrix24/frame-client.js',response.text)
        self.assertIn('frame-ancestors '+frame_tests.embed.PORTAL_ORIGIN,response.headers['content-security-policy'])
        cookie=response.headers['set-cookie']
        for flag in ['HttpOnly','Secure','SameSite=None','Partitioned']:self.assertIn(flag,cookie)
        for private in [frame_tests.FIELDS['AUTH_ID'],frame_tests.FIELDS['REFRESH_ID'],cookie.split(';')[0].split('=',1)[1]]:
            self.assertNotIn(private,response.text)

    async def test_probe_accepts_only_personal_frame_session(self):
        async with self.fixture.client() as client:
            self.assertEqual((await client.get('/auth/bitrix24/frame-session')).status_code,401)
            client.cookies.set('gateway_token',self.fixture.normal)
            self.assertEqual((await client.get('/auth/bitrix24/frame-session')).status_code,401)
            client.cookies.set(frame_tests.embed.COOKIE,self.fixture.token)
            response=await client.get('/auth/bitrix24/frame-session')
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json(),{'ok':True})

    async def test_frame_client_is_bounded_public_asset_and_disabled_with_feature(self):
        async with self.fixture.client() as client:
            response=await client.get('/auth/bitrix24/frame-client.js')
            self.assertEqual(response.status_code,200)
            self.assertIn('application/javascript',response.headers['content-type'])
            self.assertNotIn('getAuth',response.text)
            self.assertNotIn('callMethod',response.text)
            with patch.dict(os.environ,{'GATEWAY_BITRIX_EMBED_ENABLED':'false'}):
                self.assertEqual((await client.get('/auth/bitrix24/frame-client.js')).status_code,404)


class KnowledgeErrorRegressionTests(unittest.TestCase):
    def test_plain_sdk_error_does_not_become_json_parse_error(self):
        result=ServerResult(CallToolResult(content=[TextContent(type='text',text=
            'Error executing tool gateway_knowledge_space_list: missing required scope for gateway_knowledge_space_list: memory:read')],
            isError=True,structuredContent={'result':'PRIVATE_STALE_PAYLOAD'}))
        actor=GatewayActor(subject='fixture:reader')
        profile={'reader_only':True,'spaces':[SPACE],'documents':{SPACE:{}}}
        result=addon_guard.filter_result(actor,'gateway_knowledge_space_list',result,profile)
        payload=json.loads(result.root.content[0].text)
        self.assertEqual(payload['error'],'knowledge_scope_required')
        self.assertEqual(payload['required_scope'],'memory:read')
        self.assertTrue(result.root.isError)
        self.assertIsNone(result.root.structuredContent)
        self.assertNotIn('PRIVATE_STALE_PAYLOAD',json.dumps(result.model_dump()))

    def test_malformed_success_and_unknown_errors_fail_closed(self):
        actor=GatewayActor(subject='fixture:reader')
        profile={'reader_only':True,'spaces':[SPACE],'documents':{SPACE:{}}}
        for value,error in [('PRIVATE_UNEXPECTED_TEXT',False),('PRIVATE_EXCEPTION_DETAIL',True),('[]',False),('{"ok":true,"spaces":"PRIVATE_BAD_SHAPE"}',False)]:
            result=ServerResult(CallToolResult(content=[TextContent(type='text',text=value)],isError=error))
            result=addon_guard.filter_result(actor,'gateway_knowledge_space_list',result,profile)
            self.assertTrue(result.root.isError)
            self.assertNotIn('PRIVATE_',json.dumps(result.model_dump()))


def load_tests(loader, tests, pattern):
    # The base frame suite is run separately, with its own asyncio lifecycle.
    return unittest.TestSuite(cls(name) for cls in (
        CorporateKnowledgeProtocolTests, FrameLaunchRegressionTests, KnowledgeErrorRegressionTests)
        for name in sorted(cls.__dict__) if name.startswith('test_'))


if __name__=='__main__':unittest.main()
