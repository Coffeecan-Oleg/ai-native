"""Real disposable PostgreSQL and MCP HTTP; upstream UniSender is always mocked."""
import copy
import json
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, AsyncMock

from starlette.testclient import TestClient
from test_reader_onboarding import ReaderFixture
from gateway_mcp.services import auth, policy, platforma_onboarding as onboarding, unisender_readonly as boundary
from gateway_mcp.services import storage_credentials, storage_crypto
from gateway_mcp.services.storage_core import _connect
from gateway_mcp.backends import unisender_readonly as backend


class UniSenderAccessTests(ReaderFixture):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ,{'GATEWAY_UNISENDER_READONLY_ENABLED':'true'}))

    def test_new_approval_gets_only_read_scope_and_resource(self):
        row=self.approve();actor=onboarding.oauth_reader_actor(self.fresh())
        self.assertIn(boundary.SCOPE,actor.scopes)
        self.assertTrue(boundary.ROUTES <= set(actor.tool_allowlist))
        with _connect() as conn:
            rows=conn.execute("select * from access_resource_grants where system='unisender' and subject_key=%s",(self.reader.subject,)).fetchall()
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['actions'],['read'])
        self.assertEqual(rows[0]['expires_at'],row['approved_until'])
        self.assertNotIn('unisender:write',actor.scopes)

    def test_owner_upgrade_is_idempotent_and_old_token_stays_limited(self):
        with patch.dict(os.environ,{'GATEWAY_UNISENDER_READONLY_ENABLED':'false'}):
            row=self.approve();old=onboarding.oauth_reader_actor(self.fresh())
        first=boundary.upgrade_approved_readers(self.owner);second=boundary.upgrade_approved_readers(self.owner)
        self.assertEqual(first['upgraded_readers'],1);self.assertEqual(second['upgraded_readers'],0)
        # Actual claims processing must respect the old credential ceiling.
        old_claims=auth.verify_gateway_token_claims(auth.mint_gateway_token(old))
        self.assertNotIn(boundary.SCOPE,old_claims['scope'].split())
        self.assertNotIn(boundary.SCOPE,policy.actor_from_claims(old_claims).scopes)
        with self.assertRaises(PermissionError):boundary.require_actor(old)
        fresh=onboarding.oauth_reader_actor(self.fresh())
        self.assertIn(boundary.SCOPE,fresh.scopes)
        self.assertTrue(boundary.ROUTES <= set(fresh.tool_allowlist))

    def test_revocation_closes_service_and_revokes_resource(self):
        row=self.approve();actor=onboarding.oauth_reader_actor(self.fresh())
        boundary.require_actor(actor)
        onboarding.decide(self.owner,row['id'],'revoked',row['revision'],'Revoke test')
        with self.assertRaises(PermissionError):boundary.require_actor(actor)
        with _connect() as conn:
            rows=conn.execute("select revoked_at from access_resource_grants where system='unisender' and subject_key=%s",(actor.subject,)).fetchall()
        self.assertTrue(rows and all(r['revoked_at'] for r in rows))

    def test_pending_and_disabled_are_denied(self):
        with self.assertRaises(PermissionError):boundary.require_actor(replace(self.reader,scopes=(boundary.SCOPE,)))
        self.approve();actor=onboarding.oauth_reader_actor(self.fresh())
        with patch.dict(os.environ,{'GATEWAY_UNISENDER_READONLY_ENABLED':'false'}):
            with self.assertRaises(PermissionError):boundary.require_actor(actor)

    def test_service_key_is_encrypted_and_not_in_metadata(self):
        fake='synthetic-service-key-for-encryption-check'
        with patch.object(storage_crypto,'_FERNET',None):
            self.assertTrue(storage_credentials.save_user_oauth_token(provider=boundary.PROVIDER,
                actor_subject=boundary.SERVICE_SUBJECT,yandex_id='',login='UniSender reader',email='',
                access_token=fake,token_type='api_key',scopes=list(boundary.METHODS),metadata={'read_only':True},expires_at_epoch=None))
            with _connect() as conn:
                row=conn.execute('select access_token_encrypted from user_oauth_tokens where provider=%s and actor_subject=%s',(boundary.PROVIDER,boundary.SERVICE_SUBJECT)).fetchone()
            self.assertNotEqual(row['access_token_encrypted'],fake)
            self.assertEqual(backend._credential(),fake)
            metadata=storage_credentials.list_service_oauth_tokens(boundary.PROVIDER)
            self.assertNotIn(fake,json.dumps(metadata));self.assertNotIn('access_token_encrypted',json.dumps(metadata))


class UniSenderProtocolTests(ReaderFixture):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ,{
            'GATEWAY_UNISENDER_READONLY_ENABLED':'true',
            'GATEWAY_MCP_OAUTH_SCOPES':os.environ['GATEWAY_MCP_OAUTH_SCOPES']+' '+boundary.SCOPE,
            'GATEWAY_MCP_OAUTH_TOOLS':os.environ['GATEWAY_MCP_OAUTH_TOOLS']+' '+' '.join(boundary.ROUTES)}))
        self.row=self.approve();actor=onboarding.oauth_reader_actor(self.fresh())
        from gateway_mcp.server import create_mcp
        self.http=self.enterContext(TestClient(self.mcp_test_server().streamable_http_app(),base_url='https://gateway.example.invalid'))
        self.headers={'Authorization':'Bearer '+auth.mint_gateway_token(actor),'Accept':'application/json, text/event-stream'}
        init=self.rpc({'id':1,'method':'initialize','params':{'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'unisender-access-test','version':'1'}}})
        self.headers['MCP-Protocol-Version']=init['result']['protocolVersion'];self.rpc({'method':'notifications/initialized'});self.seq=10

    def rpc(self,body):
        response=self.http.post('/mcp',json={'jsonrpc':'2.0',**body},headers=self.headers)
        self.assertIn(response.status_code,(200,202),response.text[:300])
        if response.headers.get('mcp-session-id'):self.headers['Mcp-Session-Id']=response.headers['mcp-session-id']
        if not response.content:return {}
        if 'text/event-stream' in response.headers.get('content-type',''):
            return next(json.loads(s[5:].strip()) for s in response.text.splitlines() if s.startswith('data:') and any(k in s for k in ['"result"','"error"']))
        return response.json()

    def call(self,route,args=None):
        self.seq+=1
        raw=self.rpc({'id':self.seq,'method':'tools/call','params':{'name':'gateway_call_tool','arguments':{'tool_name':route,'arguments_json':json.dumps(args or {})}}})['result']
        text=next(c['text'] for c in raw['content'] if c['type']=='text')
        try:data=json.loads(text)
        except ValueError:data={'error':text}
        return raw,data

    def test_real_mcp_routes_work_and_other_methods_are_denied_without_upstream(self):
        payload=[{'id':19445573,'title':'Synthetic training list'}]
        with patch.object(backend,'_post_api',AsyncMock(return_value=(payload,'synthetic-not-a-real-key'))) as upstream:
            raw,data=self.call('unisender.getLists')
            self.assertFalse(raw.get('isError'));self.assertEqual(data['data']['lists'],payload)
            self.assertFalse(data['data']['synthetic']);self.assertEqual(upstream.await_count,1)
            raw,data=self.call('unisender.sendEmail',{'email':'must-not-send@example.invalid'})
            self.assertTrue(raw.get('isError'));self.assertEqual(upstream.await_count,1)

    def test_reader_count_and_audit_never_contain_contacts_or_secret(self):
        payload={'listId':19445573,'searchParams':{'type':'address'},'count':'42','contacts':['private@example.invalid']}
        with patch.object(backend,'_post_api',AsyncMock(return_value=(payload,'synthetic-not-a-real-key'))):
            raw,data=self.call('unisender.getContactCount',{'list_id':19445573})
        self.assertFalse(raw.get('isError'));self.assertEqual(data['data']['count'],42)
        with _connect() as conn:
            rows=conn.execute("select payload from audit_events where actor_subject=%s and tool in ('gateway_call_tool','unisender.getContactCount')",(self.reader.subject,)).fetchall()
        serialized=json.dumps(rows,default=str)
        self.assertNotIn('private@example.invalid',serialized)
        self.assertNotIn('synthetic-not-a-real-key',serialized)

    def test_revoked_token_denied_at_mcp_before_backend(self):
        onboarding.decide(self.owner,self.row['id'],'revoked',self.row['revision'],'Revoke test')
        with patch.object(backend,'_post_api',AsyncMock()) as upstream:
            raw,data=self.call('unisender.getLists')
        self.assertTrue(raw.get('isError'));upstream.assert_not_called()
