"""Actual authorized skill cards and MCP reads against disposable PostgreSQL."""
import hashlib,io,json,zipfile
from uuid import uuid4
from unittest.mock import patch
from starlette.testclient import TestClient
from test_skill_lifecycle import LifecycleFixture,CONTENT
from gateway_mcp.services import skill_lifecycle as lifecycle,skill_acceptance as service,auth,corporate_skills
from gateway_mcp.services import platforma_onboarding as onboarding
from gateway_mcp.services.storage_core import _connect

def package(content=CONTENT,marker='PUBLIC-SYNTHETIC',stale=False):
    data={'schema':1,'skill_sha256':'f'*64 if stale else hashlib.sha256(content).hexdigest(),
      'cases':[{'id':'case-'+str(i),'title':'Проверочный пример '+str(i),'input':marker,
       'expected_facts':['Сохранить вымышленное название'],'forbidden_actions':['Обращение к рабочим сервисам']} for i in range(3)]}
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w') as z:
        z.writestr('SKILL.md',content);z.writestr(service.FILE,json.dumps(data,ensure_ascii=False))
    return output.getvalue()

class AcceptanceWebTests(LifecycleFixture):
    def setUp(self):
        super().setUp();check=onboarding.check_web_path
        managed={'/platforma/skills/manage','/platforma/skills/manage/action','/platforma/skills/manage/edit'}
        self.enterContext(patch.object(onboarding,'check_web_path',side_effect=lambda actor,path:check(actor,'/platforma/skills' if path in managed else path)))
        self.http=self.enterContext(TestClient(self.mcp_test_server().streamable_http_app(),base_url='https://gateway.example.invalid'))

    def login(self,actor):self.http.cookies.set('gateway_token',auth.mint_gateway_token(actor),domain='gateway.example.invalid',path='/')
    def create(self,**kwargs):
        return lifecycle.create_draft(self.reader,title='Контрольные примеры',filename='package.zip',content=package(**kwargs),upload_key=str(uuid4()),bundled=self.bundled)['name']
    def page(self,sid,managed=False,version=None):
        params={'id':sid}
        if version is not None:params['version']=version
        return self.http.get('/platforma/skills/manage' if managed else '/platforma/skills/view',params=params)

    def test_private_authorization_precedes_inspection_then_published_examples_are_readable(self):
        sid=self.create(marker='PRIVATE-SYNTHETIC')
        for actor in [self.reader,self.owner]:
            self.login(actor);p=self.page(sid,True);self.assertEqual(p.status_code,200,p.text)
            self.assertIn('PRIVATE-SYNTHETIC',p.text);self.assertNotIn('Готовый запрос для ассистента',p.text)
        self.login(self.other)
        with patch.object(service,'inspect',side_effect=AssertionError('authorize first')) as scanner:
            self.assertEqual(self.page(sid,True).status_code,403);scanner.assert_not_called()
        with self.assertRaises(PermissionError):corporate_skills.read_skill(sid,service.FILE,actor=self.other)
        self.publish(sid);p=self.page(sid);self.assertEqual(p.status_code,200,p.text)
        self.assertIn('Готовый запрос для ассистента',p.text)
        reply=corporate_skills.read_skill(sid,service.FILE,actor=self.other)
        self.assertEqual(reply['sha256'],hashlib.sha256(reply['content'].encode()).hexdigest())
        self.login(self.owner)
        self.assertIn('Готовый запрос для ассистента',self.page(sid,True).text)
        self.assertNotIn('Это закрытая сохранённая редакция',self.page(sid,True).text)

    def test_new_draft_does_not_replace_published_cases_and_archive_denies_cached_read(self):
        sid=self.create();self.publish(sid);self.login(self.other)
        cached=corporate_skills.read_skill(sid,service.FILE,actor=self.other)
        lifecycle.update_draft(self.reader,skill_id=sid,expected_revision=self.row(sid)['revision'],title='Следующая редакция',version='1.0.1',filename='package.zip',
            content=package(CONTENT+b'\nUpdated instruction.\n','PRIVATE-NEW-CASE'),bundled=self.bundled)
        public=self.page(sid);self.assertIn('PUBLIC-SYNTHETIC',public.text);self.assertNotIn('PRIVATE-NEW-CASE',public.text)
        self.login(self.reader);self.assertIn('PRIVATE-NEW-CASE',self.page(sid,True).text)
        self.assertNotIn('Готовый запрос для ассистента',self.page(sid,True).text)
        self.assertIn('Готовый запрос для ассистента',self.page(sid,True,1).text)
        self.action(sid,'submit',self.reader);self.action(sid,'publish');self.login(self.other)
        current=self.page(sid);self.assertIn('PRIVATE-NEW-CASE',current.text);self.assertIn('1.0.1',current.text)
        self.login(self.owner);self.assertIn('PUBLIC-SYNTHETIC',self.page(sid,True,1).text)
        self.assertNotIn('Готовый запрос для ассистента',self.page(sid,True,1).text)
        self.action(sid,'archive')
        self.assertNotIn('Готовый запрос для ассистента',self.page(sid,True).text)
        self.login(self.other)
        with patch.object(service,'inspect',side_effect=AssertionError('archived')) as scanner:
            self.assertIn(self.page(sid).status_code,[403,409]);scanner.assert_not_called()
        with self.assertRaises(PermissionError):corporate_skills.read_skill(sid,cached['path'],actor=self.other)

    def test_stale_examples_are_advisory_without_changing_owner_publish_policy(self):
        sid=self.create(stale=True);self.login(self.owner)
        private=self.page(sid,True);self.assertIn('другому SHA256',private.text)
        self.publish(sid);self.login(self.other);public=self.page(sid)
        self.assertEqual(public.status_code,200,public.text);self.assertIn('другому SHA256',public.text)
        self.assertNotIn('Готовый запрос для ассистента',public.text)
        self.assertEqual(self.row(sid)['stage'],'published')

    def test_reads_are_escaped_and_do_not_create_acceptance_or_workflow_events(self):
        sid=self.create(marker='</textarea><script>fake()</script>');self.publish(sid);self.login(self.other)
        before=self.row(sid)['revision']
        with _connect() as conn:events=conn.execute('select count(*) n from platforma_skill_workflow_events').fetchone()['n']
        page=self.page(sid);self.assertEqual(page.status_code,200,page.text)
        self.assertIn('&lt;/textarea&gt;',page.text);self.assertNotIn('<script>fake()',page.text)
        self.assertIn('Качество ответа ещё не оценено',page.text)
        self.assertEqual(self.row(sid)['revision'],before)
        with _connect() as conn:self.assertEqual(conn.execute('select count(*) n from platforma_skill_workflow_events').fetchone()['n'],events)
