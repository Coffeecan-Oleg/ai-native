"""Real PostgreSQL timing aggregation, audit origin and read-only acceptance."""
import hashlib,json,os
from pathlib import Path
from unittest.mock import patch
from starlette.applications import Starlette
from starlette.testclient import TestClient
from starlette.routing import Route
from test_reader_onboarding import ReaderFixture
from gateway_mcp.services import external_status as service,external_limits as limits,external_safety as safety
from gateway_mcp.services import observability
from gateway_mcp.services.storage_core import _connect
from gateway_mcp.services.policy import GatewayActor
from gateway_mcp.routes import admin_integration_status as ui
from gateway_mcp import audit

ROUTE={'name':'unisender.getLists','transport':'unisender-readonly'}
SECOND={'name':'unisender.getContactCount','transport':'unisender-readonly'}
UNSET=object()


class TimingStorageTests(ReaderFixture):
    def setUp(self):
        super().setUp()
        self.owner=GatewayActor(subject='synthetic:owner',scopes=('access:admin',))
        self.enterContext(patch.object(service,'require_current_admin'))
        self.enterContext(patch.object(service,'read_json',return_value={'tools':[ROUTE,SECOND]}))
        self.enterContext(patch.object(limits,'enabled',return_value=True))
        self.enterContext(patch.object(safety,'enabled',return_value=True))
        with _connect() as c:
            c.execute('truncate gateway_external_calls,gateway_external_circuits,gateway_external_limits,audit_events')

    def seed(self,ms=UNSET,*,kind='tool_run_v1',status='ok',decision='allow',hours=0,
             route='unisender.getLists',tool='gateway_call_tool',event='tool_call',nested=False):
        args={'tool_name':route,'private':'SECRET_INPUT','gateway_request_id':'abcdef0123456789'}
        if ms is not UNSET:
            if nested:args['arguments']={'gateway_duration_ms':ms,'gateway_timing_kind':kind}
            else:args.update(gateway_duration_ms=ms,gateway_timing_kind=kind)
        payload={'arguments':args,'actor':{'subject':'PRIVATE_EMPLOYEE'}}
        with _connect() as c:
            c.execute('''insert into audit_events(ts_ms,event,tool,system,decision,status,scope,payload,created_at)
                values(1,%s,%s,'unisender',%s,%s,'unisender:read',%s::jsonb,clock_timestamp()-(%s*interval '1 hour'))''',
                (event,tool,decision,status,json.dumps(payload),hours))

    def timing(self):return service.snapshot(self.owner)['rows'][0]['timing']

    def group(self,name='success'):
        return next(g for g in self.timing()['groups'] if g['outcome']==name)

    def hashes(self):
        from psycopg import sql
        result={}
        with _connect() as c:
            for t in c.execute("select tablename from pg_tables where schemaname='public' order by tablename").fetchall():
                name=t['tablename']
                rows=c.execute(sql.SQL('select row_to_json(t)::text as data from {} t').format(sql.Identifier(name))).fetchall()
                result[name]=hashlib.sha256('\n'.join(sorted(r['data'] for r in rows)).encode()).hexdigest()
        return result

    def test_discrete_percentiles_and_missing_coverage(self):
        for ms in range(1,21):self.seed(ms*100)
        self.seed()
        g=self.group();self.assertEqual((g['observed'],g['timed']),(21,20))
        self.assertEqual((g['p50_ms'],g['p95_ms'],g['max_ms']),(1000,1900,2000))

    def test_fast_denials_and_errors_cannot_reduce_success_percentile(self):
        self.seed(4000);self.seed(5000)
        self.seed(5,status='rate_limited');self.seed(10,status='error');self.seed(0,status='denied',decision='deny')
        self.assertEqual(self.group()['p95_ms'],5000)
        self.assertEqual(self.group('limited')['p95_ms'],5)
        self.assertEqual(self.group('errors')['p95_ms'],10)
        self.assertEqual(self.group('denied')['p95_ms'],0)

    def test_two_routes_are_aggregated_before_percentiles(self):
        self.seed(100);self.seed(200,route=SECOND['name']);self.seed(900,route=SECOND['name'])
        g=self.group();self.assertEqual((g['timed'],g['p50_ms'],g['p95_ms']),(3,200,900))

    def test_old_events_remain_untimed_and_zero_is_a_valid_value(self):
        self.seed();self.seed(0)
        g=self.group();self.assertEqual((g['observed'],g['timed'],g['p95_ms']),(2,1,0))

    def test_invalid_timing_values_are_ignored_without_cast_error(self):
        for value in [None,True,False,'123',-1,1.5,86400001,10**50,{},[], 'SECRET<script>']:
            self.seed(value)
        self.seed(86400000);self.seed(1,kind='client');self.seed(2,nested=True)
        g=self.group();self.assertEqual(g['timed'],1);self.assertEqual(g['p95_ms'],86400000)

    def test_period_future_tool_event_and_catalogue_boundaries(self):
        self.seed(100)
        self.seed(9999,hours=25);self.seed(9999,hours=-1);self.seed(9999,route='not.registered')
        self.seed(9999,tool='different');self.seed(9999,event='resource_access')
        self.assertEqual(self.group()['timed'],1);self.assertEqual(self.group()['p95_ms'],100)

    def test_recent_sample_limit_applies_before_validity_filter(self):
        self.seed(9999,hours=1)
        with _connect() as c:
            payload=json.dumps({'arguments':{'tool_name':ROUTE['name']}})
            c.execute('''insert into audit_events(ts_ms,event,tool,decision,status,payload)
                select 1,'tool_call','gateway_call_tool','allow','ok',%s::jsonb from generate_series(1,1000)''',(payload,))
        g=self.group();self.assertEqual((g['observed'],g['timed']),(1000,0));self.assertIsNone(g['p95_ms'])

    def test_sample_limit_is_per_adapter(self):
        with patch.object(service,'read_json',return_value={'tools':[ROUTE,{'name':'infra.get','transport':'infra'}]}):
            self.seed(300);self.seed(500,route='infra.get')
            rows=service.snapshot(self.owner)['rows'];self.assertEqual(len(rows),2)
            by={r['transport']:r['timing']['groups'][0] for r in rows}
            self.assertEqual(by['infra']['p95_ms'],500);self.assertEqual(by['unisender-readonly']['p95_ms'],300)

    def test_empty_registry_and_missing_rows_have_no_fabricated_timings(self):
        self.assertEqual(self.timing()['timed_events'],0)
        with patch.object(service,'read_json',return_value={'tools':[]}):
            self.assertEqual(service.snapshot(self.owner)['rows'],[])

    def test_server_finish_audit_roundtrip_produces_timing_and_redacts_secret(self):
        with patch.object(audit,'current_actor',return_value=self.owner),patch.object(audit.time,'perf_counter',return_value=10.5):
            audit.finish_tool(tool='gateway_call_tool',system='unisender',started_at=10,status='ok',
                decision='allow',scope='unisender:read',arguments={'tool_name':ROUTE['name'],'api_key':'DO_NOT_STORE',
                'gateway_duration_ms':1,'gateway_timing_kind':'client'})
        self.assertEqual(self.group()['p95_ms'],500)
        with _connect() as c:
            row=c.execute('select payload from audit_events').fetchone()['payload']
        self.assertEqual(row['arguments']['api_key'],'[redacted]')
        self.assertNotIn('DO_NOT_STORE',json.dumps(row))

    def test_snapshot_is_readonly_and_does_not_export_identity_or_input(self):
        self.seed(500);before=self.hashes();original=limits._connect
        def readonly():
            c=original();c.execute('set session default_transaction_read_only=on');c.commit();return c
        with patch.object(limits,'_connect',side_effect=readonly):value=service.snapshot(self.owner)
        self.assertEqual(before,self.hashes())
        for secret in ['SECRET_INPUT','PRIVATE_EMPLOYEE']:
            self.assertNotIn(secret,json.dumps(value))

    def test_real_http_response_renders_timings_without_table_changes(self):
        self.seed(2500);self.seed(4000);self.seed(15,status='rate_limited');self.seed(status='error')
        routes=[]
        class Mcp:
            def custom_route(self,path,methods,**kw):
                def register(fn):routes.append(Route(path,fn,methods=methods));return fn
                return register
        ui.register_integration_status_routes(Mcp());before=self.hashes()
        with TestClient(Starlette(routes=routes)) as client,patch.object(ui,'web_actor',return_value=self.owner), \
             patch('gateway_mcp.services.corporate_access.enabled',return_value=True),patch.dict(os.environ,{'GATEWAY_BRAND':'akmetron'}):
            response=client.get(ui.PATH)
            self.assertEqual(response.status_code,200);self.assertIn('4000 мс',response.text)
            self.assertIn('Нет замеров',response.text);self.assertNotIn('SECRET_INPUT',response.text)
            self.assertEqual(client.post(ui.PATH).status_code,405)
        self.assertEqual(before,self.hashes())
        result=Path('/results')
        if result.is_dir() and os.getenv('GATEWAY_DATABASE_URL','').endswith('/platforma_onboarding_test'):
            (result/'integration-timing-preview.html').write_text(response.text,encoding='utf-8')
