"""Corporate note reads through HTTP/MCP, with a synthetic portal and per-user grants."""
import json
import os
import unittest
from unittest.mock import patch

import httpx
import test_corporate_stability as protocol
from test_bitrix_identity import MEMBER, SUBJECT, TOKENS, RESPONSE
from gateway_mcp.services import bitrix_identity, bitrix_notes, external_limits, storage


class BitrixNoteTests(unittest.TestCase):
    def setUp(self):
        self.fixture = protocol.CorporateKnowledgeProtocolTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.enterContext(patch.dict(os.environ, {'GATEWAY_BITRIX_KB_READ':'true'}))
        self.record = {'scopes':['note','user_brief'], 'access_token':json.dumps(TOKENS),
                       'metadata':{'user_id':'42', 'portal':bitrix_identity.PORTAL}}
        self.stored = self.enterContext(patch.object(storage, 'get_user_oauth_token', return_value=self.record))
        self.enterContext(patch.object(external_limits, 'require',
            side_effect=lambda route, actor:external_limits._Permit(route, actor.subject)))
        self.docs = {
            489: ('Первый вход', 'Шаг 2. Подключите Платформу в своём ИИ-приложении',
                  'Добавьте удалённый MCP. Войдите через Битрикс24.'),
            531: ('Работа с навыком', '2. Скопируйте запрос из карточки',
                  'Скопируйте запрос. Добавьте свои исходные данные.')}
        self.status = {}
        self.calls = []
        self.collection = 29
        self.state = 'live'
        self.ctype = httpx.Client
        self.enterContext(patch.object(httpx, 'Client', side_effect=self.client))

    def client(self, **kwargs):
        self.assertFalse(kwargs['follow_redirects'])
        self.assertFalse(kwargs['trust_env'])
        return self.ctype(transport=httpx.MockTransport(self.respond), **kwargs)

    def respond(self, req):
        body = json.loads(req.content)
        self.assertEqual(str(req.url), 'https://akmetron.bitrix24.ru/rest/api/note.document.get')
        self.assertEqual(req.method, 'POST')
        self.assertEqual(set(body), {'id','auth'})
        self.assertEqual(body['auth'], TOKENS['access_token'])
        key = body['id']
        self.calls.append(key)
        if key in self.status:
            code, error = self.status[key]
            return httpx.Response(code, json={'error':{'code':error}})
        title, heading, content = self.docs[key]
        return httpx.Response(200, json={'result':{'item':{'id':key,'collectionId':self.collection,
            'state':self.state,'title':title,'markdown':'## '+heading+'\n\n'+content}}})

    def search(self, query, limit=1):
        result, data = self.fixture.tool('gateway_knowledge_search',
                                         {'query':query, 'space':protocol.SPACE, 'limit':limit})
        if not result.get('isError'):
            self.assertEqual(data, json.loads(result['structuredContent']['result']))
        return data

    def read(self, key):
        return self.fixture.tool('gateway_knowledge_document_get',
            {'document':f'bitrix24/notes/{key}.md','space':protocol.SPACE})

    def test_employee_gets_the_cited_section_then_current_text(self):
        for key, question in [(489,'Как подключить помощника к Платформе?'), (531,'Как применить найденный навык?')]:
            hit, = self.search(question)['documents']
            self.assertEqual(hit['path'], f'bitrix24/notes/{key}.md')
            self.assertEqual(hit['source_url'], f'https://akmetron.bitrix24.ru/note/document/{key}/#'
                             +bitrix_notes.SECTIONS[key,self.docs[key][1]][0])
            self.assertIn(self.docs[key][2], hit['snippet'])
            self.docs[key] = (*self.docs[key][:2], 'Новый текст после поиска.')
            self.assertIn(self.docs[key][2], self.read(key)[1]['document']['content'])
        self.stored.assert_called_with(bitrix_identity.PROVIDER, SUBJECT)

    def test_portal_revocation_and_restoration_in_one_mcp_session(self):
        query = 'Как применить найденный навык?'
        self.assertEqual(self.search(query)['count'], 1)
        self.status[531] = (403, 'BITRIX_REST_V3_EXCEPTION_ACCESSDENIEDEXCEPTION')
        denied = self.search(query)
        self.assertEqual(denied['documents'], [])
        self.assertNotIn('531', json.dumps(denied))
        self.assertTrue(self.read(531)[0]['isError'])
        self.assertEqual(self.search('Как подключить помощника к Платформе?')['count'], 1)
        del self.status[531]
        self.assertIn('document', self.read(531)[1])

    def test_limits_of_guide_and_revoked_gateway_scope(self):
        with patch.dict(os.environ, {'GATEWAY_KNOWLEDGE_GUIDE_SUBJECT':SUBJECT}):
            self.assertEqual(self.search('Как применить найденный навык?')['count'], 1)
            self.assertIn('document',self.read(489)[1])
            self.fixture.grants.return_value = [{'effect':'deny','scope':'knowledge:read'}]
            before = self.calls.copy()
            self.assertFalse(self.search('Как применить найденный навык?')['ok'])
            self.assertTrue(self.read(489)[0]['isError'])
            self.assertEqual(self.calls, before)

    def test_disabled_and_other_ids_make_no_portal_request(self):
        self.assertTrue(self.read(535)[0]['isError'])
        with patch.dict(os.environ, {'GATEWAY_BITRIX_KB_READ':'false'}):
            self.assertTrue(self.read(531)[0]['isError'])
            self.assertEqual(self.search('Как применить найденный навык?')['count'], 0)
        self.assertEqual(self.calls, [])

    def test_source_unavailable_keeps_existing_knowledge(self):
        self.status[489] = (500, 'INTERNAL_SERVER_ERROR')
        info = self.search('CORPORATE_ALLOWED')
        self.assertTrue(info['partial'])
        self.assertEqual(info['source_warning'], 'bitrix_notes_unavailable')
        self.assertTrue(info['documents'])
        self.assertNotIn('INTERNAL_SERVER_ERROR', json.dumps(info))

    def test_archived_and_moved_articles_are_excluded(self):
        for state, collection in [('archived',29),('trashed',29),('live',27)]:
            self.state, self.collection = state, collection
            self.assertEqual(self.search('Как применить найденный навык?')['count'], 0)
            self.assertTrue(self.read(531)[0]['isError'])

    def test_old_login_can_use_shared_files_and_requires_reconnect_for_notes(self):
        self.record['scopes'] = ['user_brief']
        data = self.search('CORPORATE_ALLOWED')
        self.assertTrue(data['count'])
        self.assertEqual(data['source_warning'], 'bitrix_notes_reconnect_required')
        self.assertTrue(self.read(489)[0]['isError'])
        self.assertEqual(self.calls, [])

    def test_passport_precedes_an_article_quoting_the_question(self):
        path = 'awareness/project-profile.md'
        root = self.fixture.vault/'spaces'/'organizational-awareness'
        (root/path).write_text('# Паспорт\nИНН 0000000000\n', encoding='utf-8')
        self.docs[531] = ('Навыки', 'Пример', 'Спросите «Какие у нас реквизиты?» и прочтите паспорт.')
        self.assertEqual(self.search('Какие у нас реквизиты?')['documents'][0]['path'], path)

    def test_wrong_subject_metadata_is_not_used_for_another_employee(self):
        self.record['metadata']['user_id'] = '3318'
        self.assertTrue(self.read(489)[0]['isError'])
        self.assertEqual(self.calls, [])

    def test_rate_limit_stops_a_search_before_contacting_the_portal(self):
        with patch.object(external_limits, 'require', side_effect=external_limits.LimitError()):
            self.assertEqual(self.search('Как применить найденный навык?')['source_warning'], 'bitrix_notes_unavailable')
        self.assertEqual(self.calls, [])

    def test_note_scope_is_opt_in_and_stored_only_after_portal_verification(self):
        with patch.object(bitrix_identity,'_request',return_value={'result':['user_brief','note']}):
            tokens = bitrix_identity._validate_tokens(RESPONSE | {'scope':'app'})
            with patch.object(storage,'save_user_oauth_token',return_value=True) as save:
                bitrix_identity._save(tokens, {'subject':SUBJECT,'user_id':'42','member_id':MEMBER,'name':'Test'})
            self.assertEqual(save.call_args.kwargs['scopes'], ['note','user_brief'])
            with patch.dict(os.environ, {'GATEWAY_BITRIX_KB_READ':'false'}), self.assertRaises(PermissionError):
                bitrix_identity._validate_tokens(RESPONSE | {'scope':'app'})
        with patch.object(bitrix_identity,'_request',return_value={'result':['user_brief','note','crm']}), self.assertRaises(PermissionError):
            bitrix_identity._validate_tokens(RESPONSE | {'scope':'app'})
