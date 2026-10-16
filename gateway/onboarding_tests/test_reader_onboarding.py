"""Real PostgreSQL fixture and real HTTP MCP/web; no live users or credentials."""
import copy
import base64
import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch, AsyncMock

from starlette.testclient import TestClient
from gateway_mcp.services import auth, policy, platforma_onboarding as service
from gateway_mcp.services.storage_core import _connect

ROOT = Path(__file__).resolve().parents[1]
TEST_URL = os.getenv('ONBOARDING_TEST_DATABASE_URL', '')


@unittest.skipUnless(TEST_URL, 'Disposable PostgreSQL required')
class ReaderFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.profiles = json.loads((ROOT/'addons/platforma-training/example-addon-profile.json').read_text())
        owner = self.profiles['profiles'].pop('test:owner')
        owner.update(subject='yandex:9001', expires_at='2099-01-01T00:00:00Z')
        self.profiles['profiles']['yandex:9001'] = owner
        self.profile_path = self.tmp/'profiles.json'
        self.profile_path.write_text(json.dumps(self.profiles))
        self.policy_path = self.tmp/'policy.json'
        self.policy_path.write_text(json.dumps({'schema':1,'default_groups':[], 'users':{'owner@example.invalid':{'groups':['admins']}}}))
        self.enterContext(patch.dict(os.environ, {
            'GATEWAY_DATABASE_URL':TEST_URL, 'GATEWAY_MIGRATIONS_DIR':str(ROOT/'migrations'),
            'GATEWAY_POLICY_FILE':str(self.policy_path),'GATEWAY_AUTH_ENABLED':'true',
            'GATEWAY_JWT_SECRET':'isolated-onboarding-signature-'*3,
            'GATEWAY_ISSUER_URL':'https://gateway.example.invalid',
            'GATEWAY_RESOURCE_URL':'https://gateway.example.invalid/mcp',
            'GATEWAY_PUBLIC_URL':'https://gateway.example.invalid',
            'GATEWAY_AUDIT_LOG':str(self.tmp/'audit.jsonl'),
            'GATEWAY_RESOURCE_POLICY_MODE':'strict','GATEWAY_TOOLS_FILE':str(ROOT/'gateway-platforma-tools.json'),
            'GATEWAY_KNOWLEDGE_VAULT_PATH':str(self.tmp/'vault'),
            'GATEWAY_ADDON_PROFILES_FILE':str(self.profile_path),
            'GATEWAY_MCP_OAUTH_SCOPES':' '.join(sorted(service.SCOPES|{'memory:write','telemetry:read','telemetry:write'})),
            'GATEWAY_MCP_OAUTH_TOOLS':' '.join(r['id'].removeprefix('mcp:') for r in owner['resources'] if r['kind'] in {'tool','backend_route'}),
            'GATEWAY_PLATFORMA_ONBOARDING_ENABLED':'true',
            'GATEWAY_PLATFORMA_OWNER_SUBJECT':'yandex:9001',
            'GATEWAY_PLATFORMA_READER_UNTIL':'2099-01-01T00:00:00Z'}))
        with _connect() as conn:
            self.assertEqual(conn.info.dbname, 'platforma_onboarding_test', 'Never run against the live database')
            conn.execute('truncate platforma_reader_events,platforma_reader_requests,access_scope_grants,access_resource_grants restart identity')
        self.owner = policy.GatewayActor(subject='yandex:9001', yandex_id='9001',email='owner@example.invalid',login='owner',groups=('admins',),scopes=('*',))
        self.reader = policy.GatewayActor(subject='yandex:9002',yandex_id='9002',email='reader@example.invalid',login='reader')
        self.other = policy.GatewayActor(subject='yandex:9003',yandex_id='9003',email='other@example.invalid',login='other')

    def approve(self, actor=None):
        actor = actor or self.reader
        row = service.own_request(actor, create=True)
        return service.decide(self.owner,row['id'],'approved',row['revision'],'Known test colleague')

    def fresh(self, actor=None):
        actor = actor or self.reader
        return policy.actor_from_user_info({'id':actor.yandex_id,'login':actor.login,'default_email':actor.email})

    def counts(self):
        with _connect() as conn:
            return tuple(conn.execute('select count(*) as n from '+table).fetchone()['n'] for table in ['platforma_reader_requests','access_scope_grants','access_resource_grants'])


class StorageTests(ReaderFixture):
    def test_request_identity_from_actor_and_no_automatic_grants(self):
        row = service.own_request(self.reader,create=True)
        self.assertEqual((row['subject'],row['email'],row['status']),('yandex:9002','reader@example.invalid','pending'))
        self.assertEqual(self.counts(),(1,0,0));self.assertIsNone(service.active_reader(self.reader.subject))
        with self.assertRaises(PermissionError):auth._issue_oauth_tokens(actor=self.reader,client_id='test',resource=auth.resource_url())

    def test_concurrent_visits_create_one_request(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            ids=list(pool.map(lambda _:str(service.own_request(self.reader,create=True)['id']),range(8)))
        self.assertEqual(len(set(ids)),1);self.assertEqual(self.counts(),(1,0,0))

    def test_approve_exact_scopes_resources_and_dynamic_membership(self):
        approved=self.approve();actor=self.fresh()
        self.assertEqual(set(actor.scopes),service.SCOPES);self.assertEqual(actor.groups,())
        self.assertEqual(self.counts(),(1,6,1));self.assertIsNotNone(service.active_reader(self.reader.subject))
        with _connect() as conn:
            row=conn.execute('select * from access_resource_grants').fetchone()
            self.assertEqual((row['subject_key'],row['system'],row['actions'],row['resource_pattern']),('yandex:9002','platforma',['read'],'*'))
        self.assertTrue(service.knowledge_reader(actor,self.tmp/'vault/spaces/organizational-awareness'))
        self.assertFalse(service.knowledge_reader(actor,self.tmp/'vault/spaces/another'))
        self.assertLessEqual(approved['approved_until'],service.settings()['until'])

    def test_concurrent_decisions_and_replay_do_not_duplicate_grants(self):
        row=service.own_request(self.reader,create=True)
        def decide(_):
            try:service.decide(self.owner,row['id'],'approved',1,'Test');return True
            except ValueError:return False
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(decide,range(2)))
        self.assertEqual(sum(results),1);self.assertEqual(self.counts(),(1,6,1))

    def test_failed_transaction_does_not_partially_open_access(self):
        row=service.own_request(self.reader,create=True)
        with patch.object(service,'_event',side_effect=RuntimeError('injected final transaction failure')):
            with self.assertRaises(RuntimeError):service.decide(self.owner,row['id'],'approved',1,'Test')
        self.assertEqual(self.counts(),(1,0,0));self.assertEqual(service.own_request(self.reader)['status'],'pending')

    def test_reject_resubmit_and_approve(self):
        row=service.own_request(self.reader,create=True)
        service.decide(self.owner,row['id'],'rejected',1,'Unknown')
        self.assertEqual(service.own_request(self.reader,create=True)['status'],'rejected')
        self.assertEqual(self.counts(),(1,0,0))
        row=service.own_request(self.reader,resubmit=True)
        self.assertEqual(row['status'],'pending')
        service.decide(self.owner,row['id'],'approved',row['revision'],'Now verified')
        self.assertIsNotNone(service.active_reader(self.reader.subject))

    def test_revoke_only_linked_grants_and_reopen_requires_new_approval(self):
        approved=self.approve();other=self.approve(self.other)
        service.decide(self.owner,approved['id'],'revoked',approved['revision'],'Training complete')
        self.assertIsNone(service.active_reader(self.reader.subject));self.assertIsNotNone(service.active_reader(self.other.subject))
        self.assertEqual(service.own_request(self.reader,create=True)['status'],'revoked')
        with self.assertRaises(PermissionError):service.oauth_reader_actor(self.fresh())
        row=service.own_request(self.reader,resubmit=True)
        self.assertEqual(row['status'],'pending');self.assertIsNone(service.active_reader(self.reader.subject))

    def test_expiry_or_linked_grant_revocation_closes_the_entire_reader_profile(self):
        row=self.approve()
        with _connect() as conn:
            conn.execute('update access_scope_grants set revoked_at=now() where id=%s',(row['scope_grant_ids'][0],))
        self.assertIsNone(service.active_reader(self.reader.subject))
        with self.assertRaises(PermissionError):service.reader_profile(self.reader)
        with _connect() as conn:
            conn.execute('update platforma_reader_requests set approved_until=now()-interval \'1 second\' where id=%s',(row['id'],))
        self.assertEqual(service.effective_status(service.own_request(self.reader)),'expired')

    def test_owner_cannot_apply_for_reader_and_unknown_user_cannot_approve(self):
        with self.assertRaises(PermissionError):service.own_request(self.owner,create=True)
        row=service.own_request(self.reader,create=True)
        for actor in [self.reader,self.other,policy.GatewayActor(subject='yandex:9003',scopes=('*',))]:
            with self.subTest(actor=actor.subject):
                with self.assertRaises(PermissionError):service.decide(actor,row['id'],'approved',1,'Test')
        self.assertEqual(self.counts(),(1,0,0))

    def test_oauth_clamps_a_broad_or_stale_actor_and_keeps_owner_unchanged(self):
        self.approve()
        broad=policy.GatewayActor(subject='yandex:9002',scopes=tuple(service.SCOPES|{'memory:write','telemetry:read','access:admin'}),groups=('admins',))
        from gateway_mcp.services.oauth_limits import limit_oauth_actor
        limited=limit_oauth_actor(broad)
        self.assertEqual(set(limited.scopes),service.SCOPES);self.assertEqual(limited.groups,())
        self.assertEqual(set(limited.tool_allowlist),service.TOOLS|service.ROUTES)
        limited=limit_oauth_actor(broad,['skills:read','memory:read'])
        self.assertEqual(set(limited.scopes),{'skills:read','memory:read'})
        self.assertEqual(service.oauth_reader_actor(self.owner),self.owner)

    def test_database_failure_is_closed_and_does_not_expose_exception_to_mcp(self):
        from gateway_mcp.services.addon_guard import profile_for
        with patch.object(service,'_db',side_effect=RuntimeError('private database detail')):
            with self.assertRaises(PermissionError) as denied:profile_for(self.reader)
            self.assertNotIn('private database detail',str(denied.exception))
            with self.assertRaisesRegex(PermissionError,'^Reader approval cannot be verified$'):service.oauth_reader_actor(self.reader)
            self.assertFalse(service.knowledge_reader(self.reader,self.tmp/'vault/spaces/organizational-awareness'))
            self.assertEqual(profile_for(self.owner)['subject'],self.owner.subject)

    def test_real_refresh_keeps_reader_limits_and_stops_after_revocation(self):
        row=self.approve()
        tokens=auth._issue_oauth_tokens(actor=self.fresh(),client_id='isolated-reader-client',resource=auth.resource_url())
        self.assertEqual(set(tokens['scope'].split()),service.SCOPES)
        rotated=auth.exchange_mcp_refresh_token(refresh_token=tokens['refresh_token'],client_id='isolated-reader-client',resource=auth.resource_url())
        claims=auth.verify_gateway_token_claims(rotated['access_token'])
        self.assertEqual(set(claims['scope'].split()),service.SCOPES)
        self.assertEqual(set(claims['tool_allowlist']),service.TOOLS|service.ROUTES)
        service.decide(self.owner,row['id'],'revoked',row['revision'],'Test revoke')
        with self.assertRaises(PermissionError):auth.exchange_mcp_refresh_token(refresh_token=rotated['refresh_token'],client_id='isolated-reader-client',resource=auth.resource_url())

    def test_real_code_exchange_from_scope_free_yandex_session_receives_only_approved_role(self):
        self.approve()
        waiting_actor=service.web_login_actor(self.fresh())
        self.assertEqual(waiting_actor.scopes,())
        verifier='isolated-pkce-verifier-for-reader-authorization-123456789'
        challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        state={'client_id':'isolated-reader-client','redirect_uri':'http://127.0.0.1:12345/callback',
               'scope':' '.join(sorted(service.SCOPES|{'memory:write','telemetry:read'})),
               'code_challenge':challenge,'resource':auth.resource_url()}
        code=auth.create_mcp_authorization_code(waiting_actor,state)
        tokens=auth.exchange_mcp_authorization_code(code=code,client_id=state['client_id'],redirect_uri=state['redirect_uri'],code_verifier=verifier,resource=state['resource'])
        self.assertEqual(set(tokens['scope'].split()),service.SCOPES)
        claims=auth.verify_gateway_token_claims(tokens['access_token'])
        self.assertEqual(set(claims['tool_allowlist']),service.TOOLS|service.ROUTES)


class WebTests(ReaderFixture):
    def setUp(self):
        super().setUp()
        from gateway_mcp.server import create_mcp
        server=create_mcp();self.http=self.enterContext(TestClient(server.streamable_http_app(),base_url='https://gateway.example.invalid'))

    def login(self,actor):
        self.http.cookies.set('gateway_token',auth.mint_gateway_token(actor),domain='gateway.example.invalid',path='/')

    def fields(self,response):
        return {k:v for k,v in re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">',response.text)}

    def pending(self):
        self.login(self.reader);r=self.http.get('/join');self.assertEqual(r.status_code,200)
        self.assertIn('Заявка отправлена',r.text);return service.own_request(self.reader)

    def preview(self,row,decision='approved'):
        self.login(self.owner);r=self.http.get('/admin/platforma-readers');self.assertEqual(r.status_code,200)
        csrf=self.http.cookies.get('platforma_reader_csrf')
        form={'csrf':csrf,'revision':str(row['revision']),'decision':decision,'reason':'Test colleague'}
        r=self.http.post('/admin/platforma-readers/'+str(row['id'])+'/preview',data=form)
        self.assertEqual(r.status_code,200,r.text);return self.fields(r)

    def test_anonymous_entry_does_not_create_request_and_authenticated_entry_waits(self):
        r=self.http.get('/join');self.assertEqual(r.status_code,200);self.assertIn('Войти через Яндекс',r.text)
        self.assertEqual(self.counts(),(0,0,0));self.pending();self.pending();self.assertEqual(self.counts(),(1,0,0))

    def test_yandex_callback_and_waiting_request_use_authenticated_identity_without_email_form(self):
        # Real callback/state/cookie handling; only the external Yandex service is stubbed.
        state=auth.create_oauth_state('/join')
        with patch.object(auth,'exchange_yandex_code',AsyncMock(return_value={'access_token':'synthetic-yandex-only'})), \
             patch.object(auth,'fetch_yandex_user_info',AsyncMock(return_value={'id':'9002','login':'reader','default_email':'reader@example.invalid'})), \
             patch.object(auth,'_save_yandex_actor_token'):
            r=self.http.get('/auth/yandex/callback',params={'state':state,'code':'synthetic-code'},follow_redirects=False)
        self.assertEqual(r.status_code,303,r.text);self.assertEqual(r.headers['location'],'/join')
        claims=auth.verify_gateway_token_claims(self.http.cookies.get('gateway_token'))
        self.assertEqual((claims['sub'],claims['email'],claims['scope'],claims['groups']),('yandex:9002','reader@example.invalid','',[]))
        r=self.http.get('/join');self.assertIn('Заявка отправлена',r.text)
        self.assertNotIn('name="email"',r.text);self.assertEqual(self.counts(),(1,0,0))

    def test_waiting_cookie_cannot_inherit_policy_admin_scopes(self):
        broad=policy.GatewayActor(subject='yandex:9002',yandex_id='9002',email='reader@example.invalid',groups=('admins',),scopes=('*',))
        limited=service.web_login_actor(broad)
        self.assertEqual(limited.groups,());self.assertEqual(limited.scopes,())
        self.assertEqual(service.web_login_actor(self.owner),self.owner)

    def test_pending_and_approved_browser_sessions_cannot_open_other_web_surfaces(self):
        self.pending()
        for path in ['/credentials','/admin/platforma-readers','/admin/users','/admin/telemetry/skills']:
            r=self.http.get(path);self.assertEqual(r.status_code,403,(path,r.text))
        self.approve();self.login(self.fresh())
        self.assertEqual(self.http.get('/credentials').status_code,403)
        r=self.http.get('/join');self.assertIn('Доступ открыт',r.text);self.assertIn('https://gateway.example.invalid/mcp',r.text)
        self.assertNotIn('Подать заявку повторно',r.text)

    def test_owner_preview_confirm_approve_and_revoke(self):
        row=self.pending();form=self.preview(row);form['confirmed']='yes'
        path='/admin/platforma-readers/'+str(row['id'])+'/confirm'
        r=self.http.post(path,data=form,follow_redirects=False);self.assertEqual(r.status_code,303)
        self.assertIsNotNone(service.active_reader(self.reader.subject))
        self.assertEqual(self.http.post(path,data=form).status_code,409);self.assertEqual(self.counts(),(1,6,1))
        row=service.own_request(self.reader);form=self.preview(row,'revoked');form['confirmed']='yes'
        self.assertEqual(self.http.post(path,data=form,follow_redirects=False).status_code,303)
        self.assertIsNone(service.active_reader(self.reader.subject))

    def test_csrf_foreign_origin_and_unsigned_confirmation_are_denied(self):
        row=self.pending();self.login(self.owner);self.http.get('/admin/platforma-readers')
        path='/admin/platforma-readers/'+str(row['id'])
        form={'revision':'1','decision':'approved','reason':'Test'}
        self.assertEqual(self.http.post(path+'/preview',data=form).status_code,403)
        form['csrf']=self.http.cookies.get('platforma_reader_csrf')
        self.assertEqual(self.http.post(path+'/preview',data=form,headers={'Origin':'https://evil.invalid'}).status_code,403)
        form['confirmed']='yes';form['confirmation']='0.invalid'
        self.assertEqual(self.http.post(path+'/confirm',data=form).status_code,403)
        self.assertEqual(self.counts(),(1,0,0))

    def test_confirmation_binds_subject_expiry_reason_revision_and_decision(self):
        row=self.pending();form=self.preview(row);form['confirmed']='yes'
        path='/admin/platforma-readers/'+str(row['id'])+'/confirm'
        changed=dict(form,reason='Different reason')
        self.assertEqual(self.http.post(path,data=changed).status_code,403)
        changed=dict(form,decision='rejected')
        self.assertEqual(self.http.post(path,data=changed).status_code,403)
        with patch.dict(os.environ,{'GATEWAY_PLATFORMA_READER_UNTIL':'2098-01-01T00:00:00Z'}):
            self.assertEqual(self.http.post(path,data=form).status_code,403)
        # A changed verified account display invalidates the preview as well.
        newer=policy.GatewayActor(subject='yandex:9002',yandex_id='9002',email='updated@example.invalid',login='reader')
        service.own_request(newer,create=True)
        self.assertEqual(self.http.post(path,data=form).status_code,409)
        self.assertEqual(self.counts(),(1,0,0))

    def test_http_mcp_credential_is_not_an_owner_browser_session(self):
        from gateway_mcp.services.oauth_limits import limit_oauth_actor
        delegated=limit_oauth_actor(self.owner)
        self.login(delegated)
        r=self.http.get('/admin/platforma-readers',follow_redirects=False)
        self.assertEqual(r.status_code,303);self.assertIn('/auth/yandex/login',r.headers['location'])

    def test_html_escaping_and_private_response_headers(self):
        malicious=policy.GatewayActor(subject='yandex:9004',yandex_id='9004',email='<script>bad</script>@example.invalid',login='<img src=x>')
        self.login(malicious);r=self.http.get('/join')
        self.assertNotIn('<script>bad',r.text);self.assertIn('&lt;script&gt;',r.text)
        self.assertEqual(r.headers['cache-control'],'no-store');self.assertIn("frame-ancestors 'none'",r.headers['content-security-policy'])

    def test_render_review_pages_from_real_http_responses(self):
        pages={'entry':self.http.get('/join').text}
        row=self.pending();pages['waiting']=self.http.get('/join').text
        self.login(self.owner);r=self.http.get('/admin/platforma-readers');pages['owner-requests']=r.text
        form={'csrf':self.http.cookies.get('platforma_reader_csrf'),'revision':str(row['revision']),'decision':'approved','reason':'Учебный коллега'}
        r=self.http.post('/admin/platforma-readers/'+str(row['id'])+'/preview',data=form)
        self.assertEqual(r.status_code,200);pages['owner-confirmation']=r.text
        self.approve();self.login(self.fresh());pages['approved']=self.http.get('/join').text
        # Only dead synthetic fixtures; remove usable controls in saved review pages.
        for name,html in pages.items():
            html=re.sub(r'(name="(?:csrf|confirmation)" value=")[^"]*',r'\1preview-only',html)
            html=html.replace('<button','<button disabled').replace('http-equiv="refresh"','data-disabled-refresh="refresh"')
            print('ONBOARDING_PREVIEW:'+json.dumps({'name':name,'html':html},ensure_ascii=False))


class ProtocolTests(ReaderFixture):
    def setUp(self):
        super().setUp()
        from gateway_mcp.server import create_mcp
        self.http=self.enterContext(TestClient(create_mcp().streamable_http_app(),base_url='https://gateway.example.invalid'))
        from gateway_mcp.services.knowledge import create_space,put_document
        create_space(actor=self.owner,slug='organizational-awareness',title='Training map',members_json='[]')
        doc=json.loads((ROOT/'addons/platforma-training/awareness-bundle.json').read_text())['documents'][0]
        put_document(actor=self.owner,space=service.SPACE,**{k:doc[k] for k in ['path','title','content','kind','document_id','status']})
        self.row=self.approve();actor=service.oauth_reader_actor(self.fresh())
        self.token=auth.mint_gateway_token(actor)
        self.headers={'Authorization':'Bearer '+self.token,'Accept':'application/json, text/event-stream'}
        init=self.rpc({'id':1,'method':'initialize','params':{'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'isolated-reader-test','version':'1'}}})
        self.headers['MCP-Protocol-Version']=init['result']['protocolVersion'];self.rpc({'method':'notifications/initialized'});self.seq=10

    def rpc(self,body):
        r=self.http.post('/mcp',json={'jsonrpc':'2.0',**body},headers=self.headers)
        self.assertIn(r.status_code,(200,202),r.text[:300])
        if r.headers.get('mcp-session-id'):self.headers['Mcp-Session-Id']=r.headers['mcp-session-id']
        if not r.content:return {}
        if 'text/event-stream' in r.headers.get('content-type',''):
            return next(json.loads(s[5:].strip()) for s in r.text.splitlines() if s.startswith('data:') and any(k in s for k in ['"result"','"error"']))
        return r.json()

    def tool(self,name,args=None):
        self.seq+=1;r=self.rpc({'id':self.seq,'method':'tools/call','params':{'name':name,'arguments':args or {}}})['result']
        text=next(x['text'] for x in r['content'] if x['type']=='text')
        try:d=json.loads(text)
        except ValueError:d={'error':text}
        return r,d

    def test_real_mcp_reads_map_and_fictional_requests_and_only_common_space(self):
        r,d=self.tool('gateway_knowledge_document_get',{'space':service.SPACE,'document':'awareness/start.md'})
        self.assertFalse(r.get('isError'),d);self.assertEqual(d['document']['id'],'app11-2d97b0364fa97b9d')
        r,d=self.tool('gateway_knowledge_space_list',{})
        self.assertFalse(r.get('isError'),d);self.assertEqual([x['space'] for x in d['spaces']],[service.SPACE])
        self.assertFalse((self.tmp/'vault/personal/reader-example-invalid').exists())
        r,d=self.tool('gateway_call_tool',{'tool_name':'platforma.requests.list','arguments_json':'{"period":"2026-10","overdue_only":true,"limit":100}'})
        self.assertFalse(r.get('isError'),d);self.assertEqual(d['data']['total'],39)
        listed=self.rpc({'id':100,'method':'tools/list'})['result']['tools']
        self.assertEqual({r['name'] for r in listed},service.TOOLS)

    def test_owner_private_memory_usage_and_all_writes_are_denied(self):
        cases=[('gateway_memory_search',{}),('gateway_memory_write',{'content':'no'}),
               ('gateway_usage_dashboard',{}),('gateway_usage_import',{}),
               ('gateway_knowledge_document_get',{'space':'personal:owner-example-invalid','document':'usage/client-setup.md'}),
               ('gateway_knowledge_document_put',{'space':service.SPACE,'path':'awareness/start.md','title':'overwrite','content':'no'}),
               ('gateway_admin_list_access',{}),('gateway_knowledge_space_create',{}),
               ('gateway_call_tool',{'tool_name':'platforma.requests.delete','arguments_json':'{}'})]
        for name,args in cases:
            with self.subTest(tool=name):self.assertTrue(self.tool(name,args)[0].get('isError'))

    def test_old_token_is_denied_immediately_after_revoke_or_expiry(self):
        self.assertFalse(self.tool('gateway_access_profile')[0].get('isError'))
        service.decide(self.owner,self.row['id'],'revoked',self.row['revision'],'Revoke test')
        self.assertEqual(self.rpc({'id':101,'method':'tools/list'})['result']['tools'],[])
        self.assertTrue(self.tool('gateway_access_profile')[0].get('isError'))
        self.assertTrue(self.tool('gateway_knowledge_document_get',{'space':service.SPACE,'document':'awareness/start.md'})[0].get('isError'))

    def test_old_token_cannot_read_after_approval_expires(self):
        with _connect() as conn:
            conn.execute('update platforma_reader_requests set approved_until=now()-interval \'1 second\' where id=%s',(self.row['id'],))
        self.assertTrue(self.tool('gateway_access_profile')[0].get('isError'))


if __name__=='__main__':unittest.main()
