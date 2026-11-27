"""Synthetic display/ACL checks: no live DB, portal calls or credential reads."""
import copy
import os
import unittest
from unittest.mock import patch

import httpx
from starlette.applications import Starlette
from starlette.routing import Route
from gateway_mcp.routes import admin_audit as ui
from gateway_mcp.services import storage_admin_console as store
from gateway_mcp.services.policy import GatewayActor

MEMBER = 'a' * 32
SUBJECT = 'bitrix24:' + MEMBER + ':42'
ROW = {'created_at': '2026-11-04T14:40:00Z', 'actor_subject': SUBJECT,
       'event': 'tool_call', 'tool': 'gateway_access_profile', 'system': 'access',
       'decision': 'allow', 'status': 'ok', 'scope': 'skills:read',
       'payload': {'arguments': {'password': 'never-render-this'}}}


class FakeServer:
    def __init__(self): self.routes = []
    def custom_route(self, path, methods, **kwargs):
        def bind(func):
            self.routes.append(Route(path, func, methods=methods)); return func
        return bind


class LookupTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {'GATEWAY_BITRIX_MEMBER_ID': MEMBER}))
        self.enterContext(patch('gateway_mcp.services.corporate_access.enabled', return_value=True))

    def test_single_bounded_query_and_no_credential_fields(self):
        subjects = ['bitrix24:' + MEMBER + ':' + str(i) for i in range(1, 52)]
        with patch.object(store, '_rows', return_value=[{'subject': subjects[0], 'login': 'Сергей Смирнов'},
                                                     {'subject': 'foreign', 'login': 'not allowed'}]) as query:
            names = store.admin_audit_user_names(subjects)
        self.assertEqual(names, {subjects[0]: 'Сергей Смирнов'})
        sql, params = query.call_args.args
        self.assertEqual(len(params[0]), 50)
        self.assertNotIn(subjects[-1], params[0])
        self.assertIn('select subject, login from known_users where subject = any(%s)', sql)
        self.assertNotIn('access_token', sql)
        self.assertNotIn('refresh_token', sql)
        self.assertEqual(query.call_count, 1)

    def test_foreign_portal_and_legacy_keys_do_not_trigger_query(self):
        with patch.object(store, '_rows') as query:
            self.assertEqual(store.admin_audit_user_names(['bitrix24:'+'b'*32+':42', 'yandex:42', "' OR true --"]), {})
        query.assert_not_called()

    def test_missing_tenant_pin_and_disabled_corporate_mode_fail_closed(self):
        with patch.object(store, '_rows') as query:
            with patch.dict(os.environ, {'GATEWAY_BITRIX_MEMBER_ID': ''}):
                self.assertEqual(store.admin_audit_user_names([SUBJECT]), {})
            with patch('gateway_mcp.services.corporate_access.enabled', return_value=False):
                self.assertEqual(store.admin_audit_user_names([SUBJECT]), {})
        query.assert_not_called()


class AuditPageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        server = FakeServer(); ui.register_admin_audit_routes(server)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=Starlette(routes=server.routes)), base_url='https://copy.example.invalid')
        self.addAsyncCleanup(self.client.aclose)
        self.enterContext(patch('gateway_mcp.routes.training_admin.is_training_visitor', return_value=False))

    async def request(self, names=None, rows=None, scopes=('access:admin',), name_error=None):
        actor = GatewayActor(subject=SUBJECT, scopes=scopes)
        with patch.object(ui, 'web_actor', return_value=actor), \
             patch.object(ui, 'list_audit_events', return_value=[ROW] if rows is None else rows) as events, \
             patch.object(ui, 'admin_audit_user_names', return_value=names or {}, side_effect=name_error) as lookup:
            response = await self.client.get('/admin/audit')
        return response, events, lookup

    async def test_name_initial_and_collapsible_exact_id_without_row_mutation(self):
        original = copy.deepcopy(ROW)
        response, events, _ = await self.request({SUBJECT: 'Сергей Смирнов'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('Смирнов С.', response.text)
        self.assertIn('title="Сергей Смирнов"', response.text)
        self.assertIn('<summary>Технический ID</summary><code>'+SUBJECT+'</code>', response.text)
        self.assertNotIn('never-render-this', response.text)
        self.assertFalse(events.call_args.kwargs['include_payload'])
        self.assertEqual(ROW, original)

    async def test_names_are_resolved_only_for_visible_first_page(self):
        rows = [ROW | {'actor_subject': 'bitrix24:'+MEMBER+':'+str(i)} for i in range(1, 52)]
        _, _, lookup = await self.request(rows=rows)
        self.assertEqual(len(lookup.call_args.args[0]), 50)
        self.assertNotIn(rows[-1]['actor_subject'], lookup.call_args.args[0])

    async def test_unauthorized_user_never_reads_events_or_names(self):
        response, events, lookup = await self.request(scopes=('telemetry:read',))
        self.assertEqual(response.status_code, 403)
        events.assert_not_called(); lookup.assert_not_called()

    async def test_unknown_name_and_failed_lookup_preserve_events(self):
        for error in [None, RuntimeError('synthetic-unavailable')]:
            response, _, _ = await self.request(name_error=error)
            self.assertEqual(response.status_code, 200)
            self.assertIn(SUBJECT, response.text)
            self.assertIn('gateway_access_profile', response.text)
            self.assertNotIn('synthetic-unavailable', response.text)

    async def test_html_name_is_escaped_in_text_and_title(self):
        response, _, _ = await self.request({SUBJECT: '<img src=x onerror="alert(1)"> <script>bad</script>'})
        self.assertNotIn('<img src=x', response.text)
        self.assertNotIn('<script>bad', response.text)
        self.assertIn('&lt;img', response.text)
        self.assertIn('&quot;alert(1)&quot;', response.text)

    async def test_compound_name_is_not_guessed(self):
        response, _, _ = await self.request({SUBJECT: 'Анна Мария де Соуза'})
        self.assertIn('Анна Мария де Соуза', response.text)
        self.assertNotIn('Соуза А.', response.text)


if __name__ == '__main__':
    unittest.main()
