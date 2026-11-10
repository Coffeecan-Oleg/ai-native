"""Projection and actual ASGI view; no business probes or server secrets."""
import unittest
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
from unittest.mock import patch
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient
from gateway_mcp.routes import admin_integration_status as ui,admin_ui
from gateway_mcp.services import external_status as service
from gateway_mcp.services.policy import GatewayActor

NOW=datetime(2026,11,7,12,tzinfo=timezone.utc)
RATE={'USER_PER_MINUTE':30,'USER_BURST':5,'SHARED_PER_MINUTE':120,'SHARED_BURST':10}
SAFETY={'USER_CONCURRENCY':2,'SHARED_CONCURRENCY':4,'FAILURE_THRESHOLD':3,'OPEN_SECONDS':30,'TIMEOUT_SECONDS':30}
OWNER=GatewayActor(subject='synthetic:owner',scopes=('access:admin',))

def model(**kw):
    args=dict(transport='unisender-readonly',names=['unisender.getLists'],now=NOW,rate=None,circuit=None,calls=[],observations={},last={},rate_rules=RATE,safety_rules=SAFETY)
    args.update(kw)
    with patch.object(service.limits,'enabled',return_value=True),patch.object(service.safety,'enabled',return_value=True):return service._project(**args)

class ProjectionTests(unittest.TestCase):
    def test_missing_observation_is_not_provider_health(self):
        row=model();self.assertEqual(row['totals']['total'],0);self.assertIsNone(row['last_success_at'])
        self.assertEqual(row['phase'],'ready');self.assertEqual(row['shared_burst_available'],10)
        self.assertNotIn('user_bucket',row);self.assertNotIn('probe_id',row)
    def test_current_rate_debt_reduces_shared_budget_without_writing(self):
        rate={'tat':NOW+timedelta(seconds=5),'blocked_until':None};before=dict(rate);row=model(rate=rate)
        self.assertEqual(rate,before);self.assertEqual(row['shared_burst_available'],0);self.assertEqual(row['shared_next_seconds'],1)
    def test_upstream_and_failure_pauses_are_distinct_and_max_wait_is_used(self):
        row=model(rate={'tat':NOW,'blocked_until':NOW+timedelta(seconds=12)},circuit={'opened_until':NOW+timedelta(seconds=30),'probe_id':None})
        self.assertEqual(len(row['pauses']),2);self.assertEqual(row['shared_next_seconds'],30);self.assertEqual(row['shared_burst_available'],0)
        self.assertEqual(row['phase'],'paused')
    def test_expired_pause_waits_for_probe_and_active_probe_is_not_recovery(self):
        c={'opened_until':NOW-timedelta(seconds=1),'probe_id':'probe'};self.assertEqual(model(circuit=c)['phase'],'awaiting_probe')
        call={'integration':'unisender-readonly','call_id':'probe','expires_at':NOW+timedelta(seconds=15)}
        row=model(circuit=c,calls=[call]);self.assertEqual(row['phase'],'probing');self.assertEqual(row['shared_next_seconds'],15)
    def test_expired_slots_are_ignored_and_full_capacity_is_visible(self):
        calls=[{'integration':'unisender-readonly','call_id':str(i),'expires_at':NOW+timedelta(seconds=i+1)} for i in range(4)]
        calls.append({'integration':'unisender-readonly','call_id':'expired','expires_at':NOW})
        row=model(calls=calls);self.assertEqual(row['active_calls'],4);self.assertEqual(row['phase'],'busy');self.assertEqual(row['shared_next_seconds'],1)
    def test_allowlist_drops_unknown_error_and_invalid_request_id(self):
        row=model(last={'unisender.getLists':{'created_at':NOW,'code':'SECRET<script>','request_id':'secret?token=x'}})
        self.assertIsNone(row['latest_problem']['code']);self.assertIsNone(row['latest_problem']['request_id']);self.assertNotIn('SECRET',str(row))
    def test_registered_routes_first_match_and_internal_exclusion(self):
        rows=[{'name':'same','transport':'platforma-demo'},{'name':'same','transport':'unisender-readonly'},
              {'name':'unisender.getLists','transport':'unisender-readonly'},{'name':'unisender.getLists','transport':'infra'}]
        with patch.object(service,'read_json',return_value={'tools':rows}):self.assertEqual(service._registry(),{'unisender.getLists':'unisender-readonly'})
        with patch.object(service,'read_json',return_value={'tools':[{'name':'bad name','transport':'infra'}]}),self.assertRaises(ValueError):service._registry()
    def test_current_authority_precedes_catalogue_and_status_read(self):
        with (patch.object(service,'require_current_admin',side_effect=PermissionError('revoked')),patch.object(service,'_registry') as registry,
              patch.object(service.limits,'_connect') as db):
            with self.assertRaises(PermissionError):service.snapshot(OWNER)
            registry.assert_not_called();db.assert_not_called()
    def test_database_failure_is_not_zero_usage_and_never_echoed(self):
        with patch.object(service,'require_current_admin'),patch.object(service,'_registry',return_value={}),patch.object(service.limits,'_connect',side_effect=RuntimeError('SECRET')):
            with self.assertRaises(service.StatusUnavailable) as caught:service.snapshot(OWNER)
        self.assertNotIn('SECRET',str(caught.exception))

class FakeMcp:
    def __init__(self):self.routes=[]
    def custom_route(self,path,methods,**kw):
        def register(fn):self.routes.append(Route(path,fn,methods=methods));return fn
        return register

class StatusViewTests(unittest.TestCase):
    def setUp(self):
        m=FakeMcp();ui.register_integration_status_routes(m);self.client=self.enterContext(TestClient(Starlette(routes=m.routes)))
        self.enterContext(patch.object(ui,'web_actor',return_value=OWNER))
    def test_anonymous_and_denied_users_never_read_global_status(self):
        with patch.object(ui,'web_actor',return_value=None),patch.object(service,'snapshot') as snap:
            self.assertIn(self.client.get(ui.PATH,follow_redirects=False).status_code,(302,303,307));snap.assert_not_called()
        with patch.object(service,'snapshot',side_effect=PermissionError('SECRET')):
            r=self.client.get(ui.PATH);self.assertEqual(r.status_code,403);self.assertNotIn('SECRET',r.text)
    def test_unavailable_is_503_no_store_without_fake_zeros_or_secret(self):
        with patch.object(service,'snapshot',side_effect=RuntimeError('SECRET_DATABASE_URL')):
            r=self.client.get(ui.PATH);self.assertEqual(r.status_code,503);self.assertIn('неизвестна',r.text)
            self.assertEqual(r.headers['cache-control'],'no-store');self.assertNotIn('SECRET',r.text);self.assertNotIn('Успешно: 0',r.text)
    def test_readonly_page_has_reason_id_navigation_and_no_controls(self):
        row=model(last={'unisender.getLists':{'created_at':NOW,'code':'EXTERNAL_CIRCUIT_OPEN','request_id':'abcdef0123456789'}})
        data={'observed_at':NOW.isoformat(),'rows':[row]}
        with patch.object(service,'snapshot',return_value=data),patch('gateway_mcp.services.corporate_access.enabled',return_value=True):r=self.client.get(ui.PATH)
        self.assertEqual(r.status_code,200);self.assertIn('Состояние интеграций',r.text);self.assertIn('Запрос abcdef0123456789',r.text)
        self.assertIn('Нет за последние 24 часа',r.text);self.assertNotIn('<form',r.text);self.assertNotIn('method="post"',r.text)
        self.assertIn('не подтверждены',r.text);self.assertEqual(r.headers['cache-control'],'no-store')
    def test_post_not_supported(self):
        with patch.object(service,'snapshot') as snapshot:self.assertEqual(self.client.post(ui.PATH).status_code,405);snapshot.assert_not_called()
    def test_renderer_escapes_values_and_unknown_phase_cannot_inject_html(self):
        row=model();row['label']='<script>bad</script>';row['latest_problem']={'label':'<img onerror=bad>','at':None,'code':None,'request_id':None}
        r=ui.render(OWNER,{'observed_at':NOW.isoformat(),'rows':[row]});self.assertIn('&lt;script&gt;',r);self.assertNotIn('<script>bad',r)
    def test_corporate_menu_only_admin_sees_new_link(self):
        with patch('gateway_mcp.services.corporate_access.enabled',return_value=True):
            self.assertIn(ui.PATH,admin_ui._navigation(active='',is_admin=True));self.assertNotIn(ui.PATH,admin_ui._navigation(active='',is_admin=False))
