"""Real web authentication and disposable-DB access boundaries for the guide."""
from dataclasses import replace
import json
import os
import re
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient
from test_reader_onboarding import ReaderFixture
from gateway_mcp.routes import platforma_guide as guide
from gateway_mcp.services import auth, platforma_onboarding as service
from gateway_mcp.services.storage_core import _connect


class GuideWebTests(ReaderFixture):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ, {'GATEWAY_BRAND':'akmetron',
            'GATEWAY_PLATFORMA_SKILL_LIBRARY_ENABLED':'true'}))
        from gateway_mcp.server import create_mcp
        self.http = self.enterContext(TestClient(create_mcp().streamable_http_app(),base_url='https://gateway.example.invalid'))

    def login(self,actor):
        self.http.cookies.set('gateway_token',auth.mint_gateway_token(actor),domain='gateway.example.invalid',path='/')

    def test_owner_and_two_approved_colleagues_read_same_guide(self):
        self.approve(); self.approve(self.other)
        for actor in (self.owner,self.reader,self.other):
            self.login(actor)
            page=self.http.get(guide.PATH)
            self.assertEqual(page.status_code,200,page.text)
            self.assertIn('Карточка подключения сервиса',page.text)
            self.assertIn('UniSender пока не подключён.',page.text)
            self.assertIn('aria-current="page">Как это работает',page.text)
            self.assertEqual(len(re.findall('data-card-answer',page.text)),11)  # ten fields plus JS selector
            self.assertIn('API-ключ',page.text)

    def test_anonymous_and_delegated_owner_need_personal_browser_login(self):
        for actor in (None, replace(self.owner,tool_allowlist=('gateway_corporate_skill_list',))):
            self.http.cookies.clear()
            if actor: self.login(actor)
            for path in (guide.PATH,guide.PATH+'/download?document=memo'):
                response=self.http.get(path,follow_redirects=False)
                self.assertEqual(response.status_code,303)
                self.assertTrue(response.headers['location'].startswith('/auth/yandex/login?next='))
                self.assertEqual(response.headers['cache-control'],'no-store')

    def test_pending_and_rejected_cannot_read_any_document(self):
        row=service.own_request(self.other,create=True)
        self.login(self.other)
        for decision in ('pending','rejected'):
            if decision=='rejected':service.decide(self.owner,row['id'],'rejected',row['revision'],'Test rejection')
            for path in (guide.PATH,guide.PATH+'/download?document=card'):
                response=self.http.get(path)
                self.assertEqual(response.status_code,403)
                self.assertNotIn('Готовое сообщение своему ИИ',response.text)

    def test_revocation_closes_existing_cookie(self):
        row=self.approve(); self.login(self.reader)
        self.assertEqual(self.http.get(guide.PATH).status_code,200)
        service.decide(self.owner,row['id'],'revoked',row['revision'],'Test revoke')
        self.assertEqual(self.http.get(guide.PATH).status_code,403)
        self.assertEqual(self.http.get(guide.PATH+'/download?document=memo').status_code,403)

    def test_expiry_and_revoked_grant_close_existing_cookie(self):
        row=self.approve(); self.login(self.reader)
        with _connect() as conn:
            conn.execute('update access_scope_grants set revoked_at=now() where id=%s',(row['scope_grant_ids'][0],))
        self.assertEqual(self.http.get(guide.PATH).status_code,403)
        with _connect() as conn:
            conn.execute('update access_scope_grants set revoked_at=null where id=%s',(row['scope_grant_ids'][0],))
            conn.execute("update platforma_reader_requests set approved_until=now()-interval '1 second' where id=%s",(row['id'],))
        self.assertEqual(self.http.get(guide.PATH+'/download?document=message').status_code,403)

    def test_disabled_training_and_unknown_subpath_stay_closed(self):
        self.login(self.owner)
        with patch.dict(os.environ,{'GATEWAY_PLATFORMA_ONBOARDING_ENABLED':'false'}):
            self.assertEqual(self.http.get(guide.PATH).status_code,404)
            self.assertEqual(self.http.get(guide.PATH+'/download?document=card').status_code,404)
        self.assertEqual(self.http.get(guide.PATH+'/upload').status_code,404)

    def test_three_downloads_are_exact_utf8_fixed_attachments(self):
        self.approve(); self.login(self.reader)
        data=guide.content()
        for key in guide.DOCUMENTS:
            response=self.http.get(guide.PATH+'/download',params={'document':key})
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.content.decode('utf-8'),data['documents'][key])
            self.assertIn('attachment;',response.headers['content-disposition'])
            self.assertIn("filename*=UTF-8''",response.headers['content-disposition'])
            self.assertIn('text/plain',response.headers['content-type'])
            self.assertEqual(response.headers['cache-control'],'no-store')

    def test_invalid_or_duplicate_download_parameters_cannot_select_paths(self):
        self.login(self.owner)
        for query in ('','document=../../.env','document=card&path=/etc/passwd','document=card&document=memo','document=%0D%0Asecret'):
            response=self.http.get(guide.PATH+'/download?'+query)
            self.assertEqual(response.status_code,400,response.text)
            self.assertNotIn('Content-Disposition',response.headers)
        self.assertEqual(self.http.get(guide.PATH+'?answer=anything').status_code,400)

    def test_no_post_or_other_mutating_route_and_no_grant_side_effects(self):
        self.approve(); self.login(self.reader)
        before=self.counts()
        for method in ('post','put','patch','delete'):
            for path in (guide.PATH,guide.PATH+'/download?document=card'):
                response=self.http.request(method.upper(),path,content='NEVER_PERSIST_TEST_VALUE')
                self.assertEqual(response.status_code,405)
                self.assertNotIn('NEVER_PERSIST_TEST_VALUE',response.text)
        self.http.get(guide.PATH)
        self.http.get(guide.PATH+'/download?document=card')
        self.assertEqual(self.counts(),before)

    def test_nonce_csp_no_forms_no_inline_handlers_and_no_remote_assets(self):
        self.login(self.owner)
        responses=[self.http.get(guide.PATH) for _ in range(2)]
        nonces=[]
        for response in responses:
            nonce=re.search(r'<script nonce="([^"]+)"',response.text)[1]; nonces.append(nonce)
            csp=response.headers['content-security-policy']
            self.assertIn("script-src 'nonce-"+nonce+"'",csp)
            for restriction in ("connect-src 'none'","form-action 'none'","frame-ancestors 'none'","base-uri 'none'"):
                self.assertIn(restriction,csp)
            self.assertNotIn('<form',response.text)
            self.assertNotRegex(response.text,r'\son(?:click|input|submit)=')
            self.assertNotRegex(response.text,r'<(?:script|img|link)[^>]+(?:src|href)="https?://')
            self.assertEqual(response.headers['x-content-type-options'],'nosniff')
        self.assertNotEqual(nonces[0],nonces[1])

    def test_reader_private_admin_and_credentials_paths_remain_denied(self):
        self.approve(); self.login(self.reader)
        self.assertEqual(self.http.get(guide.PATH).status_code,200)
        for path in ('/admin/platforma-readers','/admin/telemetry/skills','/credentials'):
            self.assertEqual(self.http.get(path).status_code,403,path)

    def test_read_failure_has_safe_error_and_no_partial_guide(self):
        self.login(self.owner)
        with patch.object(guide,'content',side_effect=OSError('DO_NOT_EXPOSE_INTERNAL_PATH')):
            response=self.http.get(guide.PATH)
            self.assertEqual(response.status_code,503)
            self.assertNotIn('DO_NOT_EXPOSE',response.text)
            self.assertNotIn('Карточка подключения',response.text)


class GuideContentTests(unittest.TestCase):
    def test_memo_and_all_dynamic_text_are_escaped(self):
        data=guide.content()
        data['documents']['memo']='</p><script>malicious()</script>'
        data['fields'][0]['hint']='"><img src=x onerror=bad()>'
        body=guide.render_body(data)
        self.assertNotIn('<script>malicious()',body)
        self.assertIn('&lt;script&gt;malicious()',body)
        self.assertNotIn('<img src=x',body)
        self.assertIn('&lt;img',body)

    def test_local_script_has_no_network_or_persistence_api(self):
        script=(guide.ASSETS/'platforma-guide.js').read_text()
        for forbidden in ('fetch(', 'XMLHttpRequest','WebSocket','sendBeacon','localStorage','sessionStorage','indexedDB','.innerHTML','.outerHTML','eval('):
            self.assertNotIn(forbidden,script)
        self.assertIn('new Blob',script)
        self.assertIn('navigator.clipboard.writeText(output.value)',script)
        self.assertEqual(len(guide.content()['fields']),10)

    def test_package_config_includes_both_runtime_assets(self):
        # JSON/JS must travel with the installed wheel, not only the checkout.
        import tomllib
        root=guide.ASSETS.parents[1]
        config=tomllib.loads((root/'pyproject.toml').read_text())
        patterns=config['tool']['setuptools']['package-data']['gateway_mcp']
        included={p.name for pattern in patterns for p in (root/'gateway_mcp').glob(pattern)}
        self.assertTrue({'platforma-guide.json','platforma-guide.js'} <= included)


if __name__=='__main__':unittest.main()
