"""Persistent admission and real MCP responses on disposable PostgreSQL only."""
import json,os,subprocess,sys,time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
import httpx
from starlette.testclient import TestClient
from test_reader_onboarding import ReaderFixture
from gateway_mcp.services import external_limits as limits,auth,platforma_onboarding as onboarding
from gateway_mcp.services.storage_core import _connect
from gateway_mcp.services import unisender_readonly as boundary
from gateway_mcp.backends import unisender_readonly as backend

ROUTE={'name':'unisender.getLists','transport':'unisender-readonly','backend':'unisender','scope':'unisender:read','http_method':'GET'}
ACTOR=SimpleNamespace(subject='synthetic:rate-employee')

class LimitFixture(ReaderFixture):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(limits,'enabled',side_effect=lambda route:route.get('transport') in limits.TRANSPORTS))
        self.enterContext(patch.dict(os.environ,{limits.PREFIX+'USER_PER_MINUTE':'1',limits.PREFIX+'USER_BURST':'1',
            limits.PREFIX+'SHARED_PER_MINUTE':'120',limits.PREFIX+'SHARED_BURST':'10'}))
        with _connect() as conn:conn.execute('truncate gateway_external_limits')
    def attempt(self,subject=ACTOR.subject,route=ROUTE):
        try:limits.require(route,SimpleNamespace(subject=subject));return 200
        except limits.LimitError as exc:return exc.response()['status']

class PersistentLimitsTests(LimitFixture):
    def test_burst_denials_do_not_spend_more_and_refill_works(self):
        with patch.dict(os.environ,{limits.PREFIX+'USER_PER_MINUTE':'5',limits.PREFIX+'USER_BURST':'5'}):
            for _ in range(5):self.assertEqual(self.attempt(),200)
            with _connect() as conn:before=conn.execute('select bucket,tat,updated_at from gateway_external_limits order by bucket').fetchall()
            self.assertEqual(self.attempt(),429)
            with _connect() as conn:
                self.assertEqual(conn.execute('select bucket,tat,updated_at from gateway_external_limits order by bucket').fetchall(),before)
                conn.execute("update gateway_external_limits set tat=clock_timestamp()-interval '2 minutes'")
            self.assertEqual(self.attempt(),200)
    def test_concurrent_calls_share_employee_budget(self):
        with ThreadPoolExecutor(max_workers=6) as pool:results=list(pool.map(lambda n:self.attempt(),range(6)))
        self.assertEqual(results.count(200),1,results);self.assertEqual(results.count(429),5,results)
    def test_different_employees_share_integration_budget(self):
        with patch.dict(os.environ,{limits.PREFIX+'SHARED_PER_MINUTE':'1',limits.PREFIX+'SHARED_BURST':'1'}):
            with ThreadPoolExecutor(max_workers=6) as pool:results=list(pool.map(lambda n:self.attempt('synthetic:'+str(n)),range(6)))
        self.assertEqual(results.count(200),1,results);self.assertEqual(results.count(429),5,results)
    def test_integrations_are_independent_and_no_raw_identity_is_stored(self):
        self.assertEqual(self.attempt(),200);self.assertEqual(self.attempt(),429)
        self.assertEqual(self.attempt(route=ROUTE|{'transport':'gitlab-rest'}),200)
        with _connect() as conn:rows=conn.execute('select integration,bucket from gateway_external_limits').fetchall()
        self.assertEqual(len(rows),4);self.assertNotIn(ACTOR.subject,json.dumps(rows))
    def test_new_process_cannot_reset_the_counter(self):
        self.assertEqual(self.attempt(),200)
        program="""import sys,json;sys.path.insert(0,ROOT)
from types import SimpleNamespace
from unittest.mock import patch
from gateway_mcp.services import external_limits as limits
with patch.object(limits,'enabled',return_value=True):
 try:limits.require({'transport':'unisender-readonly'},SimpleNamespace(subject='synthetic:rate-employee'));print('unexpected_allow')
 except limits.LimitError as error:print(json.dumps(error.response()))
""".replace('ROOT',repr(str(Path(__file__).resolve().parents[1])))
        child=subprocess.run([sys.executable,'-I','-c',program],capture_output=True,timeout=15,env=dict(os.environ))
        self.assertEqual(child.returncode,0,child.stderr.decode()[-500:]);self.assertEqual(json.loads(child.stdout)['status'],429)
    def test_lock_contention_is_bounded_and_fails_closed(self):
        with _connect() as conn:
            conn.execute('select pg_advisory_xact_lock(%s)',(limits.lock_key('unisender-readonly'),))
            started=time.monotonic();self.assertEqual(self.attempt(),503);self.assertLess(time.monotonic()-started,5)
        self.assertEqual(self.attempt(),200)
    def test_retention_only_prunes_expired_rows_of_the_new_table(self):
        with _connect() as conn:
            for subject,future in [('stale',False),('future-debt',True)]:
                conn.execute("""insert into gateway_external_limits(integration,bucket,tat,updated_at)
                    values ('unisender-readonly',%s,clock_timestamp()+(%s * interval '1 day'),clock_timestamp()-interval '8 days')""",
                    (limits.bucket(subject),20 if future else -8))
            audits=conn.execute('select count(*) n from audit_events').fetchone()['n']
        self.assertEqual(self.attempt(),200)
        with _connect() as conn:
            keys={r['bucket'] for r in conn.execute('select bucket from gateway_external_limits')}
            self.assertNotIn(limits.bucket('stale'),keys);self.assertIn(limits.bucket('future-debt'),keys)
            self.assertEqual(conn.execute('select count(*) n from audit_events').fetchone()['n'],audits)
    def test_missing_table_does_not_fall_back_to_unlimited_requests(self):
        import psycopg
        from psycopg.rows import dict_row
        def missing():return psycopg.connect(os.environ['GATEWAY_DATABASE_URL'],row_factory=dict_row,
            options='-c search_path=pg_temp -c statement_timeout=2000 -c lock_timeout=1000')
        with patch.object(limits,'_connect',side_effect=missing):self.assertEqual(self.attempt(),503)
        self.assertEqual(self.attempt(),200)
    def test_invalid_policy_does_not_create_counters(self):
        with patch.dict(os.environ,{limits.PREFIX+'USER_PER_MINUTE':'bad'}):self.assertEqual(self.attempt(),503)
        with _connect() as conn:self.assertEqual(conn.execute('select count(*) n from gateway_external_limits').fetchone()['n'],0)

class LimitsProtocolTests(LimitFixture):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ,{'GATEWAY_UNISENDER_READONLY_ENABLED':'true',
            'GATEWAY_MCP_OAUTH_SCOPES':os.environ['GATEWAY_MCP_OAUTH_SCOPES']+' '+boundary.SCOPE,
            'GATEWAY_MCP_OAUTH_TOOLS':os.environ['GATEWAY_MCP_OAUTH_TOOLS']+' '+' '.join(boundary.ROUTES)}))
        self.approve();actor=onboarding.oauth_reader_actor(self.fresh())
        self.http=self.enterContext(TestClient(self.mcp_test_server().streamable_http_app(),base_url='https://gateway.example.invalid'))
        self.headers={'Authorization':'Bearer '+auth.mint_gateway_token(actor),'Accept':'application/json, text/event-stream'}
        init=self.rpc({'id':1,'method':'initialize','params':{'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'quota-synthetic','version':'1'}}})
        self.headers['MCP-Protocol-Version']=init['result']['protocolVersion'];self.rpc({'method':'notifications/initialized'});self.seq=10
    def rpc(self,body):
        r=self.http.post('/mcp',json={'jsonrpc':'2.0',**body},headers=self.headers)
        self.assertIn(r.status_code,(200,202),r.text[:300])
        if r.headers.get('mcp-session-id'):self.headers['Mcp-Session-Id']=r.headers['mcp-session-id']
        if not r.content:return {}
        if 'text/event-stream' in r.headers.get('content-type',''):
            return next(json.loads(s[5:].strip()) for s in r.text.splitlines() if s.startswith('data:') and any(k in s for k in ['"result"','"error"']))
        return r.json()
    def call(self,name='unisender.getLists'):
        self.seq+=1;r=self.rpc({'id':self.seq,'method':'tools/call','params':{'name':'gateway_call_tool','arguments':{'tool_name':name}}})['result']
        text=next(c['text'] for c in r['content'] if c['type']=='text')
        return r,json.loads(text) if not r.get('isError') else {'error':text}
    def test_real_mcp_denial_has_wait_and_audit_but_no_second_upstream(self):
        with patch.object(backend,'_post_api',AsyncMock(return_value=([{'id':123,'title':'Synthetic list'}],'synthetic-key'))) as upstream:
            _,first=self.call();raw,second=self.call()
            self.assertTrue(first['ok']);self.assertFalse(raw.get('isError',False));self.assertEqual(second['status'],429)
            self.assertFalse(second['upstream_contacted']);self.assertGreater(second['retry_after_seconds'],0)
            upstream.assert_awaited_once()
        with _connect() as conn:
            own=conn.execute("select tat-updated_at debt from gateway_external_limits where bucket=%s",(limits.bucket(self.reader.subject),)).fetchone()
            self.assertEqual(own['debt'].total_seconds(),60)
            events=conn.execute("select status,payload from audit_events where actor_subject=%s and status='rate_limited'",(self.reader.subject,)).fetchall()
        self.assertTrue(events);self.assertIn(second['gateway_request_id'],json.dumps(events,default=str))
    def test_provider_429_creates_persistent_pause_and_keeps_bodies_and_key_private(self):
        original=httpx.AsyncClient;requests=[]
        def reply(request):requests.append(request);return httpx.Response(429,headers={'Retry-After':'17'},text='SECRET_PROVIDER_BODY')
        transport=httpx.MockTransport(reply)
        with (patch.object(backend,'_credential',return_value='synthetic-private-key-not-real') as credential,
             patch.object(backend.httpx,'AsyncClient',side_effect=lambda **kwargs:original(transport=transport,**kwargs))):
            _,first=self.call();_,second=self.call()
        self.assertEqual(len(requests),1);credential.assert_called_once()
        self.assertTrue(first['upstream_contacted']);self.assertFalse(second['upstream_contacted'])
        self.assertEqual(first['retry_after_seconds'],17);self.assertEqual(second['error_code'],'EXTERNAL_UPSTREAM_RATE_LIMITED')
        for secret in ['SECRET_PROVIDER_BODY','synthetic-private-key-not-real']:self.assertNotIn(secret,json.dumps([first,second]))
        self.assertEqual(self.attempt('synthetic:another-employee'),429)
    def test_unapproved_method_is_denied_before_any_quota_or_upstream(self):
        with patch.object(backend,'_post_api',AsyncMock()) as upstream:
            raw,_=self.call('unisender.sendEmail');self.assertTrue(raw.get('isError'));upstream.assert_not_awaited()
        with _connect() as conn:self.assertEqual(conn.execute('select count(*) n from gateway_external_limits').fetchone()['n'],0)
