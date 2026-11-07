"""Disposable PostgreSQL and real MCP only; provider HTTP is synthetic."""
import asyncio,json,os,subprocess,sys
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from starlette.testclient import TestClient
from test_external_limits_storage import LimitFixture,ROUTE,ACTOR
from gateway_mcp.services import external_limits as limits,external_safety as safety,auth,platforma_onboarding as onboarding
from gateway_mcp.services.storage_core import _connect
from gateway_mcp.services import unisender_readonly as boundary
from gateway_mcp.backends import unisender_readonly as backend

class SafetyFixture(LimitFixture):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(safety,'enabled',side_effect=lambda r:r.get('transport')=='unisender-readonly'))
        self.enterContext(patch.dict(os.environ,{limits.PREFIX+'USER_PER_MINUTE':'600',limits.PREFIX+'USER_BURST':'30',
            limits.PREFIX+'SHARED_PER_MINUTE':'600',limits.PREFIX+'SHARED_BURST':'30'}))
        with _connect() as conn:conn.execute('truncate gateway_external_calls,gateway_external_circuits')
    def reserve(self,subject=ACTOR.subject):return limits.require(ROUTE,SimpleNamespace(subject=subject))
    def finish(self,p,outcome='success'):safety.finish(p.lease,outcome)
    def counts(self):
        with _connect() as c:return c.execute('select count(*) n from gateway_external_calls').fetchone()['n']
    def state(self):
        with _connect() as c:return c.execute('select * from gateway_external_circuits').fetchone()
    def trip(self):
        for _ in range(3):self.finish(self.reserve(),'failure')
    def expire_open(self):
        with _connect() as c:c.execute("update gateway_external_circuits set opened_until=clock_timestamp()-interval '1 second'")

class PersistentSafetyTests(SafetyFixture):
    def test_employee_concurrency_and_release_preserve_rate_budget_on_denial(self):
        first,second=self.reserve(),self.reserve()
        with _connect() as c:before=c.execute('select bucket,tat from gateway_external_limits order by bucket').fetchall()
        with self.assertRaises(safety.SafetyError) as caught:self.reserve()
        self.assertEqual(caught.exception.code,'EXTERNAL_CONCURRENCY_LIMIT');self.assertEqual(caught.exception.level,'employee')
        with _connect() as c:self.assertEqual(c.execute('select bucket,tat from gateway_external_limits order by bucket').fetchall(),before)
        self.assertEqual(self.counts(),2);self.finish(first);third=self.reserve();self.assertEqual(self.counts(),2)
        self.finish(second);self.finish(third);self.assertEqual(self.counts(),0)
    def test_shared_concurrency_serializes_parallel_employees(self):
        def attempt(n):
            try:return self.reserve('synthetic:'+str(n))
            except safety.SafetyError as e:return e.code
        with ThreadPoolExecutor(max_workers=10) as pool:results=list(pool.map(attempt,range(10)))
        leases=[p for p in results if not isinstance(p,str)]
        self.assertEqual(len(leases),4);self.assertEqual(results.count('EXTERNAL_CONCURRENCY_LIMIT'),6)
        for p in leases:self.finish(p)
        self.assertEqual(self.counts(),0)
    def test_failed_service_trips_and_next_call_is_local_denial(self):
        self.trip();state=self.state();self.assertEqual(state['failures'],3);self.assertIsNotNone(state['opened_until'])
        with self.assertRaises(safety.SafetyError) as caught:self.reserve('synthetic:another')
        self.assertEqual(caught.exception.code,'EXTERNAL_CIRCUIT_OPEN');self.assertFalse(caught.exception.contacted);self.assertEqual(self.counts(),0)
    def test_success_resets_consecutive_failures_and_old_failure_window_expires(self):
        self.finish(self.reserve(),'failure');self.finish(self.reserve(),'success');self.assertEqual(self.state()['failures'],0)
        self.finish(self.reserve(),'failure')
        with _connect() as c:c.execute("update gateway_external_circuits set last_failure_at=clock_timestamp()-interval '61 seconds'")
        self.finish(self.reserve(),'failure');self.assertEqual(self.state()['failures'],1);self.assertIsNone(self.state()['opened_until'])
    def test_half_open_has_one_probe_under_concurrent_calls_and_success_closes_it(self):
        self.trip();self.expire_open()
        def attempt(n):
            try:return self.reserve('synthetic:probe'+str(n))
            except safety.SafetyError as e:return e.code
        with ThreadPoolExecutor(max_workers=6) as pool:results=list(pool.map(attempt,range(6)))
        probes=[p for p in results if not isinstance(p,str)]
        self.assertEqual(len(probes),1);self.assertEqual(results.count('EXTERNAL_CIRCUIT_OPEN'),5)
        self.finish(probes[0]);self.assertIsNone(self.state()['opened_until']);self.assertEqual(self.state()['failures'],0)
        self.finish(self.reserve())
    def test_failed_probe_reopens_and_stale_success_cannot_clear_it(self):
        old=self.reserve('synthetic:old');self.trip();self.expire_open();probe=self.reserve('synthetic:probe')
        self.finish(probe,'failure');generation=self.state()['generation'];self.finish(old,'success')
        self.assertEqual(self.state()['generation'],generation);self.assertIsNotNone(self.state()['opened_until'])
    def test_reserved_call_is_rechecked_after_another_call_trips_circuit(self):
        old=self.reserve('synthetic:old');self.trip()
        with self.assertRaises(safety.SafetyError):safety.start(old.lease)
        self.finish(old,'neutral');self.assertEqual(self.counts(),0)
    def test_expired_crash_lease_is_reclaimed_and_old_result_ignored(self):
        first,second=self.reserve(),self.reserve()
        with _connect() as c:c.execute("update gateway_external_calls set expires_at=clock_timestamp()-interval '1 second'")
        replacement=self.reserve();self.finish(first,'failure');self.finish(second,'failure')
        self.assertEqual(self.state()['failures'],0);self.assertEqual(self.counts(),1);self.finish(replacement)
    def test_abandoned_probe_gets_new_generation_and_old_completion_is_ignored(self):
        self.trip();self.expire_open();old=self.reserve()
        with _connect() as c:c.execute("update gateway_external_calls set expires_at=clock_timestamp()-interval '1 second'")
        new=self.reserve();generation=self.state()['generation'];self.finish(old,'success')
        self.assertEqual(self.state()['generation'],generation);self.assertEqual(self.state()['probe_id'],new.lease.call_id)
        self.finish(new);self.assertIsNone(self.state()['opened_until'])
    def test_second_process_observes_open_state(self):
        self.trip()
        code="""import sys,json;sys.path.insert(0,ROOT)
from types import SimpleNamespace
from unittest.mock import patch
from gateway_mcp.services import external_limits as l,external_safety as s
with patch.object(l,'enabled',return_value=True),patch.object(s,'enabled',return_value=True):
 try:l.require({'transport':'unisender-readonly'},SimpleNamespace(subject='synthetic:new-process'));print('unexpected')
 except l.LimitError as e:print(json.dumps(e.response()))
""".replace('ROOT',repr(str(__import__('pathlib').Path(__file__).resolve().parents[1])))
        p=subprocess.run([sys.executable,'-I','-c',code],env=dict(os.environ),capture_output=True,timeout=15)
        self.assertEqual(p.returncode,0,p.stderr.decode()[-400:]);self.assertEqual(json.loads(p.stdout)['error_code'],'EXTERNAL_CIRCUIT_OPEN')
    def test_abandoned_probe_does_not_claim_recovery_without_success(self):
        self.trip();self.expire_open();p=self.reserve();self.finish(p,'neutral')
        self.assertIsNotNone(self.state()['opened_until']);self.assertEqual(self.state()['failures'],3)
        next_probe=self.reserve();self.finish(next_probe,'success');self.assertIsNone(self.state()['opened_until'])
    def test_invalid_policy_and_missing_table_fail_closed_without_rate_spend(self):
        with patch.dict(os.environ,{safety.PREFIX+'USER_CONCURRENCY':'0'}),self.assertRaises(limits.LimitError):self.reserve()
        with _connect() as c:self.assertEqual(c.execute('select count(*) n from gateway_external_limits').fetchone()['n'],0)
        with _connect() as c:c.execute('alter table gateway_external_calls rename to qa_missing_calls')
        try:
            with self.assertRaises(limits.LimitError) as caught:self.reserve()
            self.assertEqual(caught.exception.response()['status'],503)
        finally:
            with _connect() as c:c.execute('alter table qa_missing_calls rename to gateway_external_calls')
        self.assertEqual(self.counts(),0)
    def test_unused_approval_permit_releases_once_and_stores_no_identity_or_payload(self):
        p=self.reserve('synthetic:private-identity');limits.release_unused(p);limits.release_unused(p);self.assertEqual(self.counts(),0)
        with _connect() as c:rows=c.execute('select row_to_json(t) data from gateway_external_circuits t').fetchall()
        self.assertNotIn('private-identity',json.dumps(rows,default=str));self.assertEqual(self.state()['failures'],0)
    def test_real_cancellation_releases_database_slot_without_trip(self):
        async def scenario():
            p=self.reserve();entered=asyncio.Event()
            async def waiting():entered.set();await asyncio.sleep(60)
            task=asyncio.create_task(safety.invoke(p,waiting));await entered.wait();task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
        asyncio.run(scenario());self.assertEqual(self.counts(),0);self.assertEqual(self.state()['failures'],0)

class SafetyProtocolTests(SafetyFixture):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ,{'GATEWAY_UNISENDER_READONLY_ENABLED':'true',
            'GATEWAY_MCP_OAUTH_SCOPES':os.environ['GATEWAY_MCP_OAUTH_SCOPES']+' '+boundary.SCOPE,
            'GATEWAY_MCP_OAUTH_TOOLS':os.environ['GATEWAY_MCP_OAUTH_TOOLS']+' '+' '.join(boundary.ROUTES)}))
        self.approve();actor=onboarding.oauth_reader_actor(self.fresh())
        self.http=self.enterContext(TestClient(self.mcp_test_server().streamable_http_app(),base_url='https://gateway.example.invalid'))
        self.headers={'Authorization':'Bearer '+auth.mint_gateway_token(actor),'Accept':'application/json, text/event-stream'}
        init=self.rpc({'id':1,'method':'initialize','params':{'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'safety-synthetic','version':'1'}}})
        self.headers['MCP-Protocol-Version']=init['result']['protocolVersion'];self.rpc({'method':'notifications/initialized'});self.seq=10
    def rpc(self,body):
        r=self.http.post('/mcp',json={'jsonrpc':'2.0',**body},headers=self.headers);self.assertIn(r.status_code,(200,202),r.text[:200])
        if r.headers.get('mcp-session-id'):self.headers['Mcp-Session-Id']=r.headers['mcp-session-id']
        if not r.content:return {}
        if 'text/event-stream' in r.headers.get('content-type',''):
            return next(json.loads(s[5:].strip()) for s in r.text.splitlines() if s.startswith('data:') and any(k in s for k in ['"result"','"error"']))
        return r.json()
    def call(self,name='unisender.getLists'):
        self.seq+=1;r=self.rpc({'id':self.seq,'method':'tools/call','params':{'name':'gateway_call_tool','arguments':{'tool_name':name}}})['result']
        text=next(c['text'] for c in r['content'] if c['type']=='text');return r,json.loads(text) if not r.get('isError') else {'error':text}
    def test_mcp_trip_local_denial_recovery_and_audit(self):
        with patch.object(backend,'_post_api',AsyncMock(side_effect=safety.TransientFailure('sanitized'))) as upstream:
            for _ in range(3):
                raw,r=self.call();self.assertFalse(raw.get('isError',False));self.assertEqual(r['status'],502);self.assertTrue(r['upstream_contacted'])
            _,r=self.call();self.assertEqual(r['status'],503);self.assertEqual(r['error_code'],'EXTERNAL_CIRCUIT_OPEN');self.assertFalse(r['upstream_contacted'])
            self.assertEqual(upstream.await_count,3);self.assertEqual(self.counts(),0)
        with _connect() as c:
            rows=c.execute("select payload from audit_events where status='error' and actor_subject=%s",(self.reader.subject,)).fetchall()
        self.assertIn(r['gateway_request_id'],json.dumps(rows,default=str));self.expire_open()
        with patch.object(backend,'_post_api',AsyncMock(return_value=([{'id':123,'title':'Synthetic'}],'synthetic-key'))) as upstream:
            _,r=self.call();self.assertTrue(r['ok']);upstream.assert_awaited_once()
        self.assertIsNone(self.state()['opened_until'])
    def test_denied_method_does_not_allocate_slot(self):
        with patch.object(backend,'_post_api',AsyncMock()) as upstream:
            raw,_=self.call('unisender.sendEmail');self.assertTrue(raw.get('isError'));upstream.assert_not_awaited()
        self.assertEqual(self.counts(),0);self.assertIsNone(self.state())
