"""Corporate directory journey using the real HTML routes and disposable PostgreSQL."""
import json
import os
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from gateway_mcp.routes import admin_showcase
from gateway_mcp.services import corporate_access, storage_showcase
from gateway_mcp.services.corporate_access import fresh_actor as resolve_actor
from gateway_mcp.services.policy import GatewayActor
from gateway_mcp.services.storage_core import _connect
from gateway_mcp.services.telemetry import record_skill_event

URL = os.getenv('ONBOARDING_TEST_DATABASE_URL', '')
MEMBER = 'c' * 32
ADMIN = 'bitrix24:' + MEMBER + ':731981'
SERVICE = 'bitrix24:' + MEMBER + ':731982'
FOREIGN = 'bitrix24:' + 'd' * 32 + ':731983'
PATH = '/admin/agents'


@unittest.skipUnless(URL, 'Disposable PostgreSQL required')
class AgentDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {
            'GATEWAY_DATABASE_URL': URL, 'GATEWAY_BITRIX_MEMBER_ID': MEMBER,
            'GATEWAY_LOGIN_PROVIDER': 'bitrix24',
            'GATEWAY_BITRIX_OWNER_USER_ID': ADMIN.rsplit(':', 1)[1],
        }))
        self.actor = GatewayActor(subject=ADMIN, scopes=('access:admin',))
        self.enterContext(patch.object(admin_showcase, 'web_actor', side_effect=lambda _: self.actor))
        self.enterContext(patch.object(corporate_access, 'fresh_actor', side_effect=lambda _: self.actor))
        self.key = 'dev-agent-' + uuid4().hex[:12]
        self.name = 'Проверка ' + self.key
        with _connect() as conn:
            self.assertEqual(conn.info.dbname, 'platforma_onboarding_test')
            for subject in (ADMIN, SERVICE, FOREIGN):
                _, member, number = subject.split(':')
                meta = {'portal': 'akmetron.bitrix24.ru', 'member_id': member, 'user_id': number}
                conn.execute("""insert into user_oauth_tokens
                    (provider,actor_subject,login,access_token_encrypted,metadata)
                    values ('bitrix24-login',%s,'fixture','unused',%s::jsonb)""", (subject, json.dumps(meta)))
                conn.execute("""insert into audit_events
                    (ts_ms,event,actor_subject,system,decision,status,payload)
                    values (1,'corporate_login',%s,'identity','allow','ok','{}'::jsonb)""", (subject,))
        self.addCleanup(self.cleanup_rows)
        routes = []

        class Router:
            def custom_route(self, path, methods, **kwargs):
                def register(handler):
                    routes.append(Route(path, handler, methods=methods))
                    return handler
                return register

        admin_showcase.register_admin_showcase_routes(Router())
        self.client = self.enterContext(TestClient(Starlette(routes=routes), base_url='https://platforma.test'))
        page = self.client.get(PATH)
        self.assertEqual(page.status_code, 200)
        self.csrf = page.cookies[admin_showcase.CSRF_COOKIE]

    def cleanup_rows(self):
        subjects = [ADMIN, SERVICE, FOREIGN]
        with _connect() as conn:
            conn.execute('delete from assistant_agent_profiles where agent_key like %s', (self.key + '%',))
            conn.execute('delete from assistant_skill_events where actor_subject = any(%s)', (subjects,))
            conn.execute('delete from audit_events where actor_subject = any(%s)', (subjects,))
            conn.execute('delete from user_oauth_tokens where actor_subject = any(%s)', (subjects,))

    def save(self, revision=0, binding=None, key=None):
        return self.client.post(PATH + '/save', data={
            'csrf': self.csrf, 'key': key or self.key, 'name': self.name, 'description': 'Тестовая карточка',
            'revision': str(revision), 'binding': json.dumps(binding or []),
        }, follow_redirects=False)

    def event(self, subject, kind):
        result = record_skill_event(actor=GatewayActor(subject=subject), event_type=kind,
                                    correlation_id=self.key, agent='codex', skill_pack='fixture',
                                    skill_id='example')
        self.assertTrue(result['stored'])

    def test_create_reload_then_bind_first_run(self):
        created = self.save()
        self.assertEqual(created.status_code, 303)
        detail = created.headers['location']
        page = self.client.get(detail)
        self.assertEqual(page.status_code, 200, 'a newly saved unbound card must open')
        self.assertIn('Источник телеметрии ещё не привязан', page.text)
        self.assertNotIn(SERVICE, page.text)
        self.assertIn(self.name, self.client.get(PATH + '?days=7').text)
        self.assertIsNone(self.profile()['starts'])
        self.assertEqual(self.save(1, [SERVICE, 'codex']).status_code, 409)
        self.event(SERVICE, 'started')
        self.assertIn(SERVICE, self.client.get(detail).text)
        self.assertEqual(self.save(1, [SERVICE, 'codex']).status_code, 303)
        self.assertEqual(self.profile()['pending'], 1)
        self.event(SERVICE, 'completed')
        self.assertEqual(self.client.get(detail).status_code, 200)
        profile = self.profile()
        self.assertEqual((profile['revision'], profile['starts'], profile['completed']), (2, 1, 1))
        self.assertEqual(self.save(1).status_code, 409)  # stale editor
        self.assertEqual(self.save(2).status_code, 303)
        self.assertEqual(self.client.get(detail).status_code, 200)
        self.assertIsNone(self.profile()['starts'])
        self.actor = GatewayActor(subject=SERVICE)
        self.assertEqual(self.client.get(detail).status_code, 403)
        self.assertEqual(self.save(3).status_code, 403)

    def profile(self):
        return next(p for p in storage_showcase.agent_profiles() if p['agent_key'] == self.key)

    def test_read_only_guide_from_mcp_to_audit_and_card(self):
        from mcp.server.fastmcp import FastMCP
        from mcp.shared.memory import create_connected_server_and_client_session
        from gateway_mcp.services import addon_guard, auth, bitrix_identity, storage_access, storage_audit
        from gateway_mcp.services.platforma_onboarding import DOCUMENTS
        from gateway_mcp.tools.access_requests import register_access_request_tools
        from gateway_mcp.tools.knowledge import register_knowledge_tools
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'examples' / 'knowledge-guide'))
        self.addCleanup(sys.path.pop, 0)
        import guide

        vault = Path(self.enterContext(tempfile.TemporaryDirectory()))
        space = vault / 'spaces' / 'organizational-awareness'
        for path, body in [
            (guide.PASSPORT, '---\ntitle: Паспорт\n---\nhttps://platforma.akmetron.ru/mcp · akmetron.bitrix24.ru'),
            (guide.CONTROL_PATH, guide.CONTROL_MARKER),
        ]:
            target = space / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding='utf-8')
        catalogue = vault / 'catalogue.json'
        catalogue.write_text(json.dumps({'profiles': {'fixture': {'documents': {guide.SPACE: {
            name: {} for name in DOCUMENTS}}}}}))
        self.enterContext(patch.dict(os.environ, {'GATEWAY_KNOWLEDGE_GUIDE_SUBJECT': SERVICE,
            'GATEWAY_KNOWLEDGE_VAULT_PATH': str(vault), 'GATEWAY_ADDON_PROFILES_FILE': str(catalogue),
            'GATEWAY_PLATFORMA_OWNER_SUBJECT': 'fixture'}))
        server = FastMCP('isolated-acceptance')
        register_access_request_tools(server)
        register_knowledge_tools(server)
        created = self.save()
        self.assertEqual(created.status_code, 303)
        detail = created.headers['location']
        pair = [SERVICE, corporate_access.GUIDE_SOURCE]
        self.assertEqual(self.save(1, pair).status_code, 409)

        async def revoke():
            grant = storage_access.insert_scope_grant(subject_type='user', subject_key=SERVICE,
                scope='knowledge:read', effect='deny', reason='acceptance', created_by=ADMIN, ttl_days=1)
            self.addCleanup(storage_access.revoke_scope_grants, grant_id=grant['id'], actor_subject=ADMIN)

        async def journey():
            async with create_connected_server_and_client_session(server) as session:
                return await guide.check_journey(session, SERVICE, revoke)

        with patch.object(corporate_access, 'fresh_actor', resolve_actor), \
                patch.object(bitrix_identity, 'current_employee', return_value={
                    'name': 'Fixture', 'member_id': MEMBER, 'user_id': SERVICE.rsplit(':', 1)[1]}), \
                patch.object(auth, 'current_actor', return_value=resolve_actor(SERVICE)):
            addon_guard.register(server)
            receipts = asyncio.run(journey())

        self.assertEqual(self.save(1, pair).status_code, 303)
        self.assertEqual(self.profile()['reads'], 3)
        self.assertEqual(self.profile()['delivered'], 1)
        self.assertEqual(self.profile()['unsuccessful'], 2)
        page = self.client.get(detail)
        self.assertEqual(page.status_code, 200)
        self.assertIn('Документ выдан', page.text)
        self.assertIn('Доступ отклонён', page.text)
        self.assertNotIn(guide.CONTROL_MARKER, page.text)
        ids = [receipts['passport']['gateway_request_id'], receipts['closed_read']['request_id'],
               receipts['after_revocation']['request_id']]
        self.assertEqual(len(set(ids)), 3)
        for rid, status in zip(ids, ('ok', 'denied', 'denied'), strict=True):
            self.assertIn(rid, page.text)
            events = storage_audit.list_audit_events(gateway_request_id=rid, actor_subject=SERVICE)
            self.assertEqual([(e['event'], e['status']) for e in events], [('knowledge_guide_read', status)])
            self.assertEqual(events[0]['payload']['arguments'], {'gateway_request_id': rid})
        self.actor = GatewayActor(subject=SERVICE)
        self.assertEqual(self.client.get(detail).status_code, 403)

    def test_legacy_and_foreign_cards_stay_out_of_directory(self):
        self.event(FOREIGN, 'started')
        with _connect() as conn:
            for suffix, source, agent, editor in [
                ('-old', '', '', 'migration:0021'), ('-other', '', '', FOREIGN),
                ('-bound', FOREIGN, 'codex', ADMIN),
            ]:
                conn.execute("""insert into assistant_agent_profiles
                    (agent_key,display_name,actor_subject,agent,updated_by) values (%s,%s,%s,%s,%s)""",
                    (self.key + suffix, self.key + suffix, source, agent, editor))
        self.assertNotIn(self.key, self.client.get(PATH).text)
        for suffix in ('-old', '-other', '-bound'):
            self.assertEqual(self.client.get(PATH + '/detail', params={'key': self.key + suffix}).status_code, 404)
            self.assertEqual(self.save(1, key=self.key + suffix).status_code, 409)
        self.assertFalse(any(o['actor_subject'] == FOREIGN for o in storage_showcase.agent_binding_options()))
        self.assertEqual(self.save(binding=[FOREIGN, 'codex']).status_code, 409)
