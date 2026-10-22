"""HTTP view behavior with real fictional stores and explicit authorization doubles."""
from contextlib import ExitStack
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from starlette.exceptions import HTTPException
from starlette.requests import Request
from gateway_mcp.backends import platforma_sandbox_store as sandbox
from gateway_mcp.backends.platforma_demo_store import DemoStore
from gateway_mcp.routes import platforma_requests as ui
from gateway_mcp.services import addon_guard, platforma_onboarding as onboarding
from gateway_mcp.services.policy import GatewayActor
from test_platforma_demo import run_read

ROOT=Path(__file__).resolve().parents[1]
OWNER=GatewayActor(subject='yandex:9001',scopes=('*',))
READER=GatewayActor(subject='yandex:9002',scopes=())
BOUNDED=replace(READER,scopes=('tools:call','platforma:read'),tool_allowlist=tuple(onboarding.TOOLS | onboarding.ROUTES))


class Routes:
    def __init__(self):self.routes={};self.methods={}
    def custom_route(self,path,**kwargs):
        def register(fn):self.routes[path]=fn;self.methods[path]=kwargs['methods'];return fn
        return register


async def inline_thread(fn,*args):return fn(*args)


class RequestBrowserTests(unittest.TestCase):
    def setUp(self):
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ,{'GATEWAY_PLATFORMA_ONBOARDING_ENABLED':'true',
            'GATEWAY_PLATFORMA_OWNER_SUBJECT':OWNER.subject,'GATEWAY_PLATFORMA_SANDBOX_WRITE_ENABLED':'false',
            'GATEWAY_TOOLS_FILE':str(ROOT/'gateway-platforma-tools.json')}))
        self.owner_check=self.stack.enter_context(patch.object(onboarding,'require_owner'))
        self.reader_check=self.stack.enter_context(patch.object(onboarding,'oauth_reader_actor',return_value=BOUNDED))
        self.profile=self.stack.enter_context(patch.object(addon_guard,'check_call',return_value={'status':'active'}))
        self.resource=self.stack.enter_context(patch.object(ui,'require_resource_access'))
        self.audit=self.stack.enter_context(patch.object(ui,'audit_event'))
        self.stack.enter_context(patch.object(ui,'to_thread',side_effect=inline_thread))
        self.web=self.stack.enter_context(patch.object(ui,'web_actor',return_value=OWNER))
        self.routes=Routes();ui.register_platforma_request_routes(self.routes)

    def get(self,path=ui.LIST_PATH,params=None,raw=None):
        query=(raw if raw is not None else urlencode(params or {})).encode()
        request=Request({'type':'http','method':'GET','path':path,'query_string':query,'headers':[]})
        response=run_read(self.routes.routes[path](request))
        return response,response.body.decode() if hasattr(response,'body') else ''

    def test_list_shows_actual_records_filters_and_owner_navigation(self):
        response,html=self.get()
        self.assertEqual(response.status_code,200)
        for value in ['Учебные заявки','15.10.2026','Подразделение','Ответственный','Найдено:','/admin/telemetry/skills']:
            self.assertIn(value,html)
        self.assertIn('1–25',html)
        self.assertIn('REQ-',html)
        self.owner_check.assert_called_once()
        self.assertNotIn('*',self.resource.call_args.kwargs['actor'].scopes)

    def test_combined_filters_return_only_matching_actual_records(self):
        row=DemoStore().list_requests(period='2026-10',limit=1)['records'][0]
        params={'query':row['id'],'period':'all','unit_id':row['unit_id'],'service_id':row['service_id'],
                'status':row['status'],'priority':row['priority'],'due_from':row['due_at'],'due_to':row['due_at']}
        response,html=self.get(params=params)
        self.assertEqual(response.status_code,200);self.assertIn('Найдено: 1</h2>',html)
        self.assertIn(row['id'],html)

    def test_empty_results_are_explicit(self):
        response,html=self.get(params={'query':'NO-MATCH-9f26be7'})
        self.assertEqual(response.status_code,200);self.assertIn('Заявок по этим фильтрам нет',html)

    def test_pagination_and_card_links_keep_filters(self):
        response,html=self.get(params={'period':'all','limit':'25','offset':'25','sort':'id'})
        self.assertEqual(response.status_code,200)
        self.assertIn('offset=50',html);self.assertIn('offset=0',html)
        self.assertIn('period=all',html);self.assertIn('26–50',html)

    def test_detail_shows_dates_source_fields_and_return_filters(self):
        response,html=self.get(ui.DETAIL_PATH,{'id':'REQ-F-001','period':'all','query':'REQ-F','offset':'25'})
        self.assertEqual(response.status_code,200)
        for text in ['REQ-F-001','Источник:','Исходный учебный снимок','Трудозатраты','Причина / комментарий','Следующее действие','offset=25']:
            self.assertIn(text,html)
        self.assertNotIn('<button',html)

    def test_overlay_created_and_updated_through_mcp_store_is_visible(self):
        with tempfile.TemporaryDirectory(prefix='sandbox-test-',dir=os.getenv('SANDBOX_TEST_TMP',ROOT)) as directory:
            with patch.dict(os.environ,{'GATEWAY_PLATFORMA_SANDBOX_WRITE_ENABLED':'true','GATEWAY_PLATFORMA_SANDBOX_DATA_DIR':directory}):
                row=sandbox.create_request(template_request_id='REQ-F-001',title='UI overlay example',due_at='2026-10-20')['record']
                sandbox.update_request(request_id=row['id'],expected_revision=1,changes={'title':'<script>alert(1)</script> & example','priority':'Высокий'})
                response,html=self.get(ui.DETAIL_PATH,{'id':row['id']})
                self.assertEqual(response.status_code,200);self.assertIn('sandbox-overlay.json',html)
                self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt; &amp; example',html)
                self.assertNotIn('<script>alert(1)</script>',html);self.assertIn('<dd>2</dd>',html)
                response,html=self.get(params={'query':'example','period':'all'})
                self.assertEqual(response.status_code,200);self.assertIn(row['id'],html)
                self.assertNotIn('<script>alert(1)</script>',html)

    def test_missing_and_unknown_card_are_not_found_or_bad_request(self):
        self.assertEqual(self.get(ui.DETAIL_PATH)[0].status_code,400)
        self.assertEqual(self.get(ui.DETAIL_PATH,{'id':'not-a-record'})[0].status_code,404)

    def test_invalid_duplicate_long_or_conflicting_filters_fail(self):
        for query in ['offset=-1','limit=1000','status=unknown','priority=secret','query=a&query=b',
                      'unknown=value','due_from=2026-99-99','overdue_only=2','overdue_only=1&status=completed',
                      'query='+('x'*201),'period=1999-01']:
            with self.subTest(query=query):self.assertEqual(self.get(raw=query)[0].status_code,400)

    def test_anonymous_redirects_and_does_not_load_records(self):
        self.web.return_value=None
        response,html=self.get()
        self.assertEqual(response.status_code,303)
        self.assertIn('/auth/yandex/login',response.headers['location']);self.resource.assert_not_called()

    def test_pending_revoked_expired_or_db_failure_cannot_read(self):
        self.web.return_value=READER
        for error in [PermissionError('pending'),PermissionError('revoked'),PermissionError('expired'),RuntimeError('/private/database/password')]:
            self.reader_check.side_effect=error
            response,html=self.get()
            self.assertIn(response.status_code,(403,503));self.assertNotIn('REQ-F-001',html)
            self.assertNotIn('/private/database/password',html)

    def test_reader_sees_data_and_has_only_reader_navigation(self):
        self.web.return_value=READER
        response,html=self.get()
        self.assertEqual(response.status_code,200);self.assertIn('/join',html)
        for private in ['/admin/audit','/admin/telemetry','/credentials','/notifications']:
            self.assertNotIn('href="'+private,html)
        self.owner_check.assert_not_called();self.reader_check.assert_called_once()

    def test_resource_and_profile_denials_never_show_records(self):
        for target in [self.resource,self.profile]:
            target.side_effect=PermissionError('denied')
            response,html=self.get();self.assertEqual(response.status_code,403)
            self.assertNotIn('REQ-F-001',html);target.side_effect=None

    def test_constrained_mcp_identity_is_not_accepted_as_browser(self):
        self.web.return_value=replace(OWNER,tool_allowlist=('gateway_call_tool',))
        self.assertEqual(self.get()[0].status_code,403)

    def test_read_headers_and_registration_have_no_mutation_routes(self):
        self.assertEqual(self.routes.methods,{ui.LIST_PATH:['GET'],ui.DETAIL_PATH:['GET']})
        response,html=self.get()
        for key,value in [('cache-control','no-store'),('x-frame-options','DENY'),('x-content-type-options','nosniff')]:
            self.assertEqual(response.headers[key],value)
        self.assertIn("form-action 'self'",response.headers['content-security-policy'])
        self.assertNotIn('method="post"',html.lower())

    def test_audit_does_not_receive_raw_query_or_result(self):
        self.get(params={'query':'DO-NOT-LOG-CONTENT'})
        self.assertNotIn('DO-NOT-LOG-CONTENT',str(self.audit.call_args_list))

    def test_web_path_gate_opens_only_two_exact_paths_with_live_approval(self):
        with patch.object(onboarding,'active_reader',return_value={'status':'approved'}) as approval:
            for path in [ui.LIST_PATH,ui.DETAIL_PATH]:self.assertTrue(onboarding.check_web_path(READER,path))
            for path in ['/platforma/requests/export','/admin/telemetry/skills','/credentials','/my/skills']:
                self.assertFalse(onboarding.check_web_path(READER,path))
            self.assertEqual(approval.call_count,2)
        for result in [None,{}]:
            with patch.object(onboarding,'active_reader',return_value=result if result is None else None):
                self.assertFalse(onboarding.check_web_path(READER,ui.LIST_PATH))
        with patch.object(onboarding,'active_reader',side_effect=RuntimeError('db')):
            self.assertFalse(onboarding.check_web_path(READER,ui.LIST_PATH))

    def test_disabled_view_and_web_gate_denial_are_closed(self):
        with patch.dict(os.environ,{'GATEWAY_PLATFORMA_ONBOARDING_ENABLED':'false'}):
            self.assertEqual(self.get()[0].status_code,404)
        self.web.side_effect=HTTPException(403)
        self.assertEqual(self.get()[0].status_code,403)

    def test_reader_without_platforma_scope_is_denied(self):
        self.web.return_value=READER;self.reader_check.return_value=replace(BOUNDED,scopes=())
        self.assertEqual(self.get()[0].status_code,403)
