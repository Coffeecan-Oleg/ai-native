from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gateway_mcp.backends import call_backend
from gateway_mcp.backends.common import BackendRouteError
from gateway_mcp.backends.platforma_demo_store import DATA_FILE
from gateway_mcp.backends import platforma_demo, platforma_sandbox_store as sandbox
from gateway_mcp.services.policy import GatewayActor
from gateway_mcp.services.access_inference import infer_route_access
from test_platforma_demo import run_read

ROOT = Path(__file__).resolve().parents[1]
ROUTES = {r['name']:r for r in json.loads((ROOT/'gateway-platforma-tools.json').read_text(encoding='utf-8'))['tools']}
OWNER = GatewayActor(subject='yandex:9001',scopes=('platforma:read','platforma:write'),scope_ceiling=('platforma:read','platforma:write'))


def call(name, **arguments):
    return run_read(call_backend(ROUTES[name],arguments))['data']


class SandboxWriteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sandbox-test-', dir=os.getenv('SANDBOX_TEST_TMP', ROOT))
        self.env = patch.dict(os.environ,{'GATEWAY_PLATFORMA_SANDBOX_WRITE_ENABLED':'true',
             'GATEWAY_PLATFORMA_SANDBOX_DATA_DIR':self.temp.name,
             'GATEWAY_PLATFORMA_OWNER_SUBJECT':OWNER.subject})
        self.env.start()
        self.actor = patch('gateway_mcp.backends.platforma_demo.current_actor',return_value=OWNER)
        self.actor.start()
        self.fixture_sha = hashlib.sha256(DATA_FILE.read_bytes()).hexdigest()

    def tearDown(self):
        self.assertEqual(hashlib.sha256(DATA_FILE.read_bytes()).hexdigest(),self.fixture_sha)
        self.actor.stop(); self.env.stop(); self.temp.cleanup()

    def create(self, **overrides):
        args = dict(template_request_id='REQ-F-001',title='Учебная проверка',due_at='2026-10-20')
        args.update(overrides)
        return call('platforma.requests.create',**args)['record']

    def test_create_persists_and_reads_through_existing_routes(self):
        before=call('platforma.context.get')['visible_requests']
        row=self.create()
        self.assertTrue(row['id'].startswith('2026-10:REQ-SBX-'))
        self.assertEqual(row['revision'],1)
        self.assertEqual(row['status'],'active')
        self.assertIn('sandbox-overlay.json',row['source'])
        self.assertEqual(call('platforma.requests.get',request_id=row['id'])['record'],row)
        self.assertEqual(call('platforma.context.get')['visible_requests'],before+1)
        self.assertEqual(call('platforma.requests.list',query='Учебная проверка',period='all')['records'][0]['id'],row['id'])

    def test_restart_of_store_preserves_data(self):
        row=self.create()
        platforma_demo._store.cache_clear()
        self.assertEqual(sandbox.SandboxStore().get_request(row['id'])['record'],row)

    def test_base_request_is_changed_only_in_overlay(self):
        before=call('platforma.requests.get',request_id='REQ-F-001')['record']
        after=call('platforma.requests.update',request_id=before['id'],expected_revision=0,
                   changes={'title':'Учебная правка существующей заявки'})['record']
        self.assertEqual(after['revision'],1)
        with patch.dict(os.environ,{'GATEWAY_PLATFORMA_SANDBOX_WRITE_ENABLED':'false'}):
            self.assertEqual(call('platforma.requests.get',request_id=before['id'])['record']['title'],before['title'])

    def test_update_fields_and_status_transitions(self):
        row=self.create(due_at='2026-10-15',created_at='2026-10-10')
        row=call('platforma.requests.update',request_id=row['id'],expected_revision=1,
                 changes={'title':'Новый заголовок','priority':'Критичный','reason':'Учебная причина',
                          'next_action':'Учебный шаг','effort_hours':2.5,'accepted_at':'2026-10-15'})['record']
        self.assertEqual((row['revision'],row['status'],row['effort_hours']),(2,'exact',2.5))
        row=call('platforma.requests.update',request_id=row['id'],expected_revision=2,
                 changes={'accepted_at':None,'due_at':'2026-10-14'})['record']
        self.assertTrue(row['is_overdue'])
        row=call('platforma.requests.update',request_id=row['id'],expected_revision=3,
                 changes={'accepted_at':'2026-10-15'})['record']
        self.assertEqual(row['status'],'late')

    def test_stale_revision_cannot_overwrite(self):
        row=self.create()
        call('platforma.requests.update',request_id=row['id'],expected_revision=1,changes={'title':'Первая'})
        with self.assertRaisesRegex(BackendRouteError,'REVISION_CONFLICT'):
            call('platforma.requests.update',request_id=row['id'],expected_revision=1,changes={'title':'Устаревшая'})
        self.assertEqual(call('platforma.requests.get',request_id=row['id'])['record']['title'],'Первая')

    def test_concurrent_updates_have_exactly_one_winner(self):
        row=self.create()
        def worker(title):
            try:
                sandbox.update_request(request_id=row['id'],expected_revision=1,changes={'title':title})
                return True
            except sandbox.QueryError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes=list(pool.map(worker,['Параллельная А','Параллельная Б']))
        self.assertEqual(outcomes.count(True),1)
        self.assertEqual(sandbox.SandboxStore().get_request(row['id'])['record']['revision'],2)

    def test_reader_cannot_write_even_with_feature_enabled(self):
        reader=GatewayActor(subject='yandex:9002',scopes=('platforma:read',),scope_ceiling=('platforma:read',))
        with patch('gateway_mcp.backends.platforma_demo.current_actor',return_value=reader):
            with self.assertRaises(PermissionError): self.create()
        self.assertFalse(sandbox.overlay_path().exists())

    def test_different_owner_with_forged_write_scope_is_denied(self):
        stranger=GatewayActor(subject='yandex:9002',scopes=('platforma:write',))
        with patch('gateway_mcp.backends.platforma_demo.current_actor',return_value=stranger):
            with self.assertRaises(PermissionError): self.create()

    def test_feature_switch_is_fail_closed(self):
        with patch.dict(os.environ,{'GATEWAY_PLATFORMA_SANDBOX_WRITE_ENABLED':'false'}):
            with self.assertRaises(PermissionError): self.create()

    def test_unknown_template_and_illegal_dates_are_rejected(self):
        for change in [{'template_request_id':'../../.env'},{'due_at':'2026-02-30'},
                       {'title':''},{'created_at':'2026-10-22'},{'due_at':'2026-10-01'},
                       {'priority':'invented'},{'due_at':None},{'title':{'text':'bad'}}]:
            with self.subTest(change=change),self.assertRaises(BackendRouteError): self.create(**change)

    def test_update_rejects_identity_overrides_and_invalid_values(self):
        row=self.create()
        for change in [{'service_id':'u1:m01'},{'source':'external'}, {'revision':100},
                       {'accepted_at':'2026-10-22'},{'due_at':None},{'assignee_id':'unknown'},
                       {'effort_hours':True},{'effort_hours':float('nan')},{}]:
            with self.subTest(change=change),self.assertRaises(BackendRouteError):
                call('platforma.requests.update',request_id=row['id'],expected_revision=1,changes=change)

    def test_quota_fails_before_file_replacement(self):
        row=self.create()
        before=sandbox.overlay_path().read_bytes()
        with patch.object(sandbox,'MAX_RECORDS',1):
            with self.assertRaisesRegex(BackendRouteError,'SANDBOX_QUOTA'): self.create()
        self.assertEqual(sandbox.overlay_path().read_bytes(),before)

    def test_corrupt_overlay_is_not_silently_ignored(self):
        self.create()
        sandbox.overlay_path().write_text('{invalid',encoding='utf-8')
        with self.assertRaisesRegex(BackendRouteError,'SANDBOX_STORE_INVALID'):
            call('platforma.requests.list')

    def test_router_definitions_require_idempotency_and_exact_resource(self):
        for name in ['platforma.requests.create','platforma.requests.update']:
            self.assertTrue(ROUTES[name]['requires_idempotency_key'])
            access=infer_route_access(ROUTES[name],{})
            self.assertEqual((access['system'],access['resource_type'],access['resource']),('platforma','sandbox_requests','sandbox'))


if __name__=='__main__': unittest.main()
