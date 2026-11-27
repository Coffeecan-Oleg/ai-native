"""Wire the real MCP handlers to synthetic identity, grants and documents."""
import asyncio
import base64
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import ProxyHandler, build_opener

from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session
from gateway_mcp.services import access_requests, addon_guard, auth, bitrix_identity, corporate_access, storage
from gateway_mcp.services.platforma_onboarding import DOCUMENTS
from gateway_mcp.tools.access_requests import register_access_request_tools
from gateway_mcp.tools.knowledge import register_knowledge_tools
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'examples' / 'knowledge-guide'))
from guide import GuideError, PASSPORT, SPACE, answer, call
import browser_login
import guide

MEMBER = 'b' * 32
AGENT = f'bitrix24:{MEMBER}:42'
OTHER = f'bitrix24:{MEMBER}:43'


class KnowledgeGuideTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.grants = []
        catalogue = tmp / 'catalogue.json'
        catalogue.write_text(json.dumps({'schema': 1, 'profiles': {'template': {
            'documents': {SPACE: {path: {} for path in DOCUMENTS}}}}}), encoding='utf-8')
        data = tmp / 'spaces' / 'organizational-awareness'
        self.passport = data / PASSPORT
        self.passport.parent.mkdir(parents=True)
        self.passport.write_text('---\ntitle: Паспорт\n---\nАдрес MCP https://kb.example.invalid/mcp. '
                                 'Вход через lab.bitrix24.ru.', encoding='utf-8')
        (data / 'closed.md').write_text('CLOSED-CONTROL-1947', encoding='utf-8')
        self.enterContext(patch.dict(os.environ, {
            'GATEWAY_LOGIN_PROVIDER': 'bitrix24', 'GATEWAY_BITRIX_MEMBER_ID': MEMBER,
            'GATEWAY_BITRIX_OWNER_USER_ID': '41', 'GATEWAY_BITRIX_DENIED_USER_IDS': '',
            'GATEWAY_KNOWLEDGE_GUIDE_SUBJECT': AGENT, 'GATEWAY_KNOWLEDGE_VAULT_PATH': str(tmp),
            'GATEWAY_PLATFORMA_OWNER_SUBJECT': 'template', 'GATEWAY_ADDON_PROFILES_FILE': str(catalogue),
            'GATEWAY_BITRIX_UNISENDER_USER_IDS': '42,43'}))
        self.enterContext(patch.object(bitrix_identity, 'current_employee', side_effect=lambda sub: {
            'user_id': sub.rsplit(':', 1)[1], 'name': 'Fixture', 'member_id': MEMBER}))
        self.enterContext(patch.object(storage, 'list_active_scope_grants', return_value=self.grants))
        self.enterContext(patch.object(access_requests, 'admin_list_access_packages', return_value={'packages': []}))
        self.enterContext(patch.object(access_requests, 'list_access_requests', return_value=[]))
        self.enterContext(patch.object(auth, 'current_actor', side_effect=lambda: corporate_access.fresh_actor(AGENT)))
        self.server = FastMCP('isolated-dev')
        register_access_request_tools(self.server)
        register_knowledge_tools(self.server)
        addon_guard.register(self.server)

    async def test_cited_answer_and_search(self):
        async with create_connected_server_and_client_session(self.server) as session:
            result = await answer(session, AGENT)
            self.assertEqual(result['document'], PASSPORT)
            self.assertEqual(result['answer'], 'Адрес подключения — https://kb.example.invalid/mcp. Вход — через lab.bitrix24.ru.')
            self.assertIn('kb.example.invalid', (await answer(session, AGENT, 'Адрес MCP'))['answer'])
            with self.assertRaises(GuideError):
                await answer(session, OTHER)

    async def test_article_answer_requires_fresh_authorised_text(self):
        from gateway_mcp.services import bitrix_notes
        path = 'bitrix24/notes/531.md'
        url = 'https://akmetron.bitrix24.ru/note/document/531/'
        first = {'path':path, 'space':SPACE, 'title':'Навыки', 'source_url':url,
                 'content':'## 2. Скопируйте запрос из карточки\nСкопируйте запрос и добавьте условия.'}
        async with create_connected_server_and_client_session(self.server) as session:
            with patch.dict(os.environ, {'GATEWAY_BITRIX_KB_READ':'true'}):
                for changed in (False, True):
                    reads = iter((first, first | {'content':'Текст обновлён.'} if changed else first))
                    def read_article(actor, space, key):
                        if key != path:
                            raise PermissionError('corporate_knowledge_document_denied')
                        return next(reads)
                    with patch.object(bitrix_notes,'read',side_effect=read_article):
                        if changed:
                            with self.assertRaises(GuideError):
                                await answer(session, AGENT, 'Как применить найденный навык?')
                        else:
                            result = await answer(session, AGENT, 'Как применить найденный навык?')
                            self.assertEqual(result['source_url'], url+'#2-skopiruyte-zapros-iz-kartochki')
                            self.assertIn(result['answer'], first['content'])

    async def test_denied_document_write_and_no_hits(self):
        async with create_connected_server_and_client_session(self.server) as session:
            with self.assertRaisesRegex(GuideError, 'не найден'):
                await answer(session, AGENT, 'CLOSED-CONTROL-1947')
            for name, args in [('gateway_knowledge_document_get', {'space': SPACE, 'document': 'closed.md'}),
                               ('gateway_knowledge_document_put', {'space': SPACE, 'document': PASSPORT})]:
                with self.assertRaises(GuideError):
                    await call(session, name, **args)
            self.assertTrue(self.passport.read_text(encoding='utf-8').startswith('---'))

    async def test_same_session_after_revocation(self):
        async with create_connected_server_and_client_session(self.server) as session:
            await answer(session, AGENT)
            self.grants.append({'effect': 'deny', 'scope': 'knowledge:read'})
            with self.assertRaises(GuideError):
                await call(session, 'gateway_knowledge_document_get', space=SPACE, document=PASSPORT)

    async def test_receipt_is_limited_to_designated_document_reads(self):
        async with create_connected_server_and_client_session(self.server) as session:
            search = await call(session, 'gateway_knowledge_search', space=SPACE, query='паспорт')
            self.assertNotIn('gateway_request_id', search)
            read = await call(session, 'gateway_knowledge_document_get', space=SPACE, document=PASSPORT)
            self.assertIn('gateway_request_id', read)
            with patch.dict(os.environ, {'GATEWAY_KNOWLEDGE_GUIDE_SUBJECT': OTHER}):
                ordinary = await call(session, 'gateway_knowledge_document_get', space=SPACE, document=PASSPORT)
                self.assertNotIn('gateway_request_id', ordinary)

    async def test_only_the_shipped_dev_passport_is_installed(self):
        examples = Path(guide.__file__).parent / 'dev-data'
        self.passport.write_bytes((examples / 'project-profile.md').read_bytes())
        with patch.dict(os.environ, {'GATEWAY_PLATFORMA_OWNER_SUBJECT': 'dev-guide-catalog-template',
                                     'GATEWAY_ADDON_PROFILES_FILE': str(examples / 'profiles.example.json')}):
            async with create_connected_server_and_client_session(self.server) as session:
                result = await answer(session, AGENT)
                self.assertEqual(result['title'], 'Паспорт dev-стенда Платформы')
                self.assertEqual(result['answer'], 'Адрес подключения — https://platforma-dev.akmetron.ru/mcp. '
                                 'Вход — через akmetron.bitrix24.ru.')

    def test_scope_and_tool_ceiling_apply_to_delegation_and_browser(self):
        actor = corporate_access.fresh_actor(AGENT)
        self.assertEqual(set(actor.scopes), {'knowledge:read', 'skills:read'})
        delegated = corporate_access.delegated(actor)
        self.assertEqual(set(delegated.tool_allowlist), corporate_access.GUIDE_TOOLS)
        narrowed = corporate_access.fresh_actor(AGENT, ceiling=['knowledge:read'], tools=['gateway_knowledge_search'])
        self.assertEqual(corporate_access.delegated(narrowed).tool_allowlist, ('gateway_knowledge_search',))
        self.assertFalse(corporate_access.web_path_allowed(actor, '/platforma/skills/upload'))
        other = corporate_access.fresh_actor(OTHER)
        self.assertIn('telemetry:write', other.scopes)
        self.assertIn('unisender:read', other.scopes)
        self.assertIsNone(other.tool_allowlist)
        with patch.dict(os.environ, {'GATEWAY_BITRIX_OWNER_USER_ID': '42'}), self.assertRaises(PermissionError):
            corporate_access.fresh_actor(AGENT)


class BrowserLoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_authorization_code_exchange(self):
        import httpx
        origin = 'https://platforma-dev.akmetron.ru'
        resource = origin + '/mcp'
        state = {}
        scopes = {'skills:read', 'knowledge:read'}

        def handle(request):
            path = request.url.path
            if path == '/mcp':
                if request.headers.get('Authorization') == 'Bearer fixture-token':
                    return httpx.Response(200, json={'ok': True})
                return httpx.Response(401, headers={'WWW-Authenticate':
                    f'Bearer resource_metadata="{origin}/.well-known/oauth-protected-resource/mcp"'})
            if path.startswith('/.well-known/oauth-protected-resource'):
                return httpx.Response(200, json={'resource': resource, 'authorization_servers': [origin],
                    'scopes_supported': sorted(scopes | {'telemetry:write'})})
            if path.startswith('/.well-known/oauth-authorization-server'):
                return httpx.Response(200, json={'issuer': origin, 'authorization_endpoint': origin + '/authorize',
                    'token_endpoint': origin + '/token', 'registration_endpoint': origin + '/register',
                    'response_types_supported': ['code'], 'code_challenge_methods_supported': ['S256']})
            if path == '/register':
                return httpx.Response(201, json={**json.loads(request.content), 'client_id': 'fixture-client'})
            if path == '/token':
                form = parse_qs(request.content.decode())
                self.assertEqual(form['code'], ['fixture-code'])
                proof = hashlib.sha256(form['code_verifier'][0].encode()).digest()
                self.assertEqual(base64.urlsafe_b64encode(proof).decode().rstrip('='), state['challenge'])
                self.assertEqual(form.get('resource'), [resource])
                return httpx.Response(200, json={'token_type': 'Bearer', 'access_token': 'fixture-token',
                    'expires_in': 3600, 'scope': 'knowledge:read skills:read'})
            return httpx.Response(404)

        def browser(url):
            params = parse_qs(urlsplit(url).query)
            self.assertEqual(set(params['scope'][0].split()), scopes)
            state['challenge'] = params['code_challenge'][0]
            qs = urlencode({'code': 'fixture-code', 'state': params['state'][0]})
            opener = build_opener(ProxyHandler({}))
            with opener.open(params['redirect_uri'][0] + '?' + qs, timeout=3) as response:
                return response.status == 200

        with patch.object(browser_login.webbrowser, 'open', side_effect=browser):
            async with browser_login.authorized_client(resource, scopes) as client:
                async with httpx.AsyncClient(transport=httpx.MockTransport(handle), auth=client.auth) as http:
                    self.assertEqual((await http.get(resource)).status_code, 200)

    async def test_local_callback_uses_state_and_keeps_consent_read_only(self):
        dev = 'https://platforma-dev.akmetron.ru'
        scopes = {'knowledge:read', 'skills:read'}
        with patch.object(browser_login, 'OAuthClientProvider', wraps=browser_login.OAuthClientProvider) as provider, \
                patch.object(browser_login.webbrowser, 'open', return_value=True) as browser:
            async with browser_login.authorized_client(dev + '/mcp', scopes):
                _, metadata, _, open_login, receive = provider.call_args.args
                port = urlsplit(str(metadata.redirect_uris[0])).port
                with self.assertRaises(ValueError):
                    await open_login('https://foreign.invalid/authorize?state=test')
                await open_login(dev + '/authorize?state=test&scope=irrelevant&code_challenge=example')
                consent = parse_qs(urlsplit(browser.call_args.args[0]).query)
                self.assertEqual(consent['scope'], ['knowledge:read skills:read'])
                self.assertEqual(consent['code_challenge'], ['example'])

                async def callback(state):
                    reader, writer = await asyncio.open_connection('127.0.0.1', port)
                    writer.write(f'GET /callback?state={state}&code=fixture HTTP/1.1\r\nHost: localhost\r\n\r\n'.encode())
                    await writer.drain()
                    reply = await reader.read()
                    writer.close()
                    await writer.wait_closed()
                    return reply

                self.assertIn(b'400 Bad Request', await callback('wrong-state'))
                self.assertIn(b'200 OK', await callback('test'))
                self.assertEqual(await receive(), ('fixture', 'test'))
