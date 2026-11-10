"""Real read-only SQL and ASGI response on disposable synthetic data only."""
import hashlib,json,os
from pathlib import Path
from unittest.mock import patch
from starlette.applications import Starlette
from starlette.testclient import TestClient
from starlette.routing import Route
from test_reader_onboarding import ReaderFixture
from gateway_mcp.services import external_status as service,external_limits as limits,external_safety as safety
from gateway_mcp.services.storage_core import _connect
from gateway_mcp.services.policy import GatewayActor
from gateway_mcp.services import admin_roles
from gateway_mcp.routes import admin_integration_status as ui

class FakeMcp:
    def __init__(self):self.routes=[]
    def custom_route(self,path,methods,**kw):
        def register(fn):self.routes.append(Route(path,fn,methods=methods));return fn
        return register

ROUTE={'name':'unisender.getLists','transport':'unisender-readonly'}
OTHER={'name':'not.current','transport':'platforma-demo'}

class StatusStorageTests(ReaderFixture):
    def setUp(self):
        super().setUp();self.owner=GatewayActor(subject='synthetic:owner',scopes=('access:admin',))
        self.enterContext(patch.object(service,'require_current_admin'))
        self.enterContext(patch.object(service,'read_json',return_value={'tools':[ROUTE,OTHER]}))
        self.enterContext(patch.object(limits,'enabled',return_value=True));self.enterContext(patch.object(safety,'enabled',return_value=True))
        with _connect() as c:
            c.execute('truncate gateway_external_calls,gateway_external_circuits,gateway_external_limits,audit_events')
    def seed_event(self,status='ok',code='',route='unisender.getLists',hours=0,request_id='abcdef0123456789',event='tool_call',tool='gateway_call_tool'):
        payload={'arguments':{'tool_name':route,'gateway_request_id':request_id,'private':'SECRET_INPUT'},'error':code,'actor':{'subject':'PRIVATE_EMPLOYEE'}}
        with _connect() as c:c.execute("""insert into audit_events(ts_ms,event,tool,system,decision,status,scope,payload,created_at)
            values(1,%s,%s,'unisender','allow',%s,'unisender:read',%s::jsonb,clock_timestamp()-(%s*interval '1 hour'))""",
            (event,tool,status,json.dumps(payload),hours))
    def seed_pause(self):
        with _connect() as c:
            c.execute("""insert into gateway_external_limits values('unisender-readonly','global',clock_timestamp(),clock_timestamp()+interval '12 seconds',clock_timestamp())""")
            c.execute("""insert into gateway_external_circuits(integration,failures,opened_until,generation,updated_at)
                values('unisender-readonly',3,clock_timestamp()+interval '30 seconds',1,clock_timestamp())""")
            c.execute("""insert into gateway_external_calls values('synthetic-call','unisender-readonly','PRIVATE_HASH',0,false,clock_timestamp()+interval '20 seconds')""")
    def state_hashes(self):
        from psycopg import sql
        result={}
        with _connect() as c:
            tables=[r['tablename'] for r in c.execute("select tablename from pg_tables where schemaname='public' order by tablename")]
            for t in tables:
                rows=c.execute(sql.SQL('select row_to_json(t)::text as data from {} t').format(sql.Identifier(t))).fetchall()
                result[t]=hashlib.sha256('\n'.join(sorted(r['data'] for r in rows)).encode()).hexdigest()
        return result
    def test_reads_current_pauses_slots_and_observations_without_any_table_change(self):
        self.seed_pause();self.seed_event();self.seed_event('error','EXTERNAL_SERVICE_UNAVAILABLE')
        before=self.state_hashes();r=service.snapshot(self.owner);after=self.state_hashes()
        self.assertEqual(before,after);self.assertFalse(r['provider_probed']);self.assertFalse(r['state_written'])
        self.assertEqual(len(r['rows']),1);row=r['rows'][0];self.assertEqual(row['active_calls'],1);self.assertEqual(len(row['pauses']),2)
        self.assertEqual(row['totals']['total'],2);self.assertEqual(row['totals']['success'],1);self.assertEqual(row['totals']['errors'],1)
        for secret in ['SECRET_INPUT','PRIVATE_EMPLOYEE','PRIVATE_HASH','synthetic-call']:self.assertNotIn(secret,json.dumps(r))
    def test_no_events_is_known_zero_and_not_a_successful_health_probe(self):
        r=service.snapshot(self.owner)['rows'][0];self.assertEqual(r['totals']['total'],0);self.assertIsNone(r['last_success_at'])
    def test_period_route_event_and_tool_filters_do_not_mix_other_calls(self):
        self.seed_event();self.seed_event('error','EXTERNAL_CALL_TIMEOUT',hours=25)
        self.seed_event(route='not.current');self.seed_event(event='resource_access');self.seed_event(tool='other_tool')
        r=service.snapshot(self.owner)['rows'][0];self.assertEqual(r['totals']['total'],1);self.assertEqual(r['totals']['errors'],0)
    def test_known_safe_reason_and_request_id_but_no_raw_payload(self):
        self.seed_event('rate_limited','EXTERNAL_RATE_LIMITED');r=service.snapshot(self.owner)['rows'][0]
        self.assertEqual(r['latest_problem']['code'],'EXTERNAL_RATE_LIMITED');self.assertEqual(r['latest_problem']['request_id'],'abcdef0123456789')
        self.seed_event('error','SECRET<script>','unisender.getLists',request_id='SECRET_INVALID_ID')
        r=service.snapshot(self.owner);self.assertNotIn('SECRET',json.dumps(r));self.assertIsNone(r['rows'][0]['latest_problem']['code'])
    def test_snapshot_works_when_connection_enforces_read_only(self):
        original=limits._connect
        def readonly():
            c=original();c.execute('set session default_transaction_read_only=on');c.commit();return c
        with patch.object(limits,'_connect',side_effect=readonly):self.assertEqual(len(service.snapshot(self.owner)['rows']),1)
    def test_missing_status_table_is_unavailable_not_fake_success(self):
        with _connect() as c:c.execute('alter table gateway_external_calls rename to qa_hidden_calls')
        try:
            with self.assertRaises(service.StatusUnavailable):service.snapshot(self.owner)
        finally:
            with _connect() as c:c.execute('alter table qa_hidden_calls rename to gateway_external_calls')
    def test_live_authority_deny_blocks_claimed_admin_before_snapshot(self):
        with patch.object(service,'require_current_admin',side_effect=admin_roles.require_current_admin),patch.object(admin_roles,'admin_role_state',return_value={'is_admin':False}),patch.object(service,'_registry') as registry:
            with self.assertRaises(PermissionError):service.snapshot(self.owner)
            registry.assert_not_called()
    def test_snapshot_respects_postgres_statement_timeout(self):
        original=limits._connect
        def timed():
            c=original();c.execute('set statement_timeout=1');c.commit();return c
        # Deliberate lock blocks the SELECT without touching a live database.
        with _connect() as lock:
            lock.execute('lock table gateway_external_calls in access exclusive mode')
            with patch.object(limits,'_connect',side_effect=timed),self.assertRaises(service.StatusUnavailable):service.snapshot(self.owner)
    def test_actual_http_owner_view_exports_preview_and_does_not_modify_tables(self):
        self.seed_pause();self.seed_event();self.seed_event('error','EXTERNAL_SERVICE_UNAVAILABLE')
        m=FakeMcp();ui.register_integration_status_routes(m);before=self.state_hashes()
        with TestClient(Starlette(routes=m.routes)) as client,patch.object(ui,'web_actor',return_value=self.owner),patch('gateway_mcp.services.corporate_access.enabled',return_value=True),patch.dict(os.environ,{'GATEWAY_BRAND':'akmetron'}):
            r=client.get(ui.PATH);self.assertEqual(r.status_code,200);self.assertIn('Пауза после сбоев',r.text)
            self.assertIn('Запрос abcdef0123456789',r.text);self.assertNotIn('SECRET',r.text)
            self.assertEqual(client.post(ui.PATH).status_code,405)
        self.assertEqual(before,self.state_hashes())
        # Only the explicitly created disposable QA result mount receives this file.
        result=Path('/results')
        if result.is_dir() and os.getenv('GATEWAY_DATABASE_URL','').endswith('/platforma_onboarding_test'):
            (result/'status-preview.html').write_text(r.text,encoding='utf-8')
