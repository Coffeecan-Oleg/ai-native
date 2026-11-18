"""Saved review cards against disposable PostgreSQL and actual ASGI routes."""
import io,json,re,zipfile
from uuid import uuid4
from unittest.mock import patch
from starlette.testclient import TestClient
from test_skill_lifecycle import LifecycleFixture,CONTENT
from gateway_mcp.services import skill_lifecycle as service,skill_review,auth,corporate_skills,platforma_onboarding as onboarding


def archive(files):
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,'w') as z:
        for path,text in files.items():z.writestr(path,text)
    return buf.getvalue()


class SkillReviewWebTests(LifecycleFixture):
    def setUp(self):
        super().setUp()
        check=onboarding.check_web_path
        managed={'/platforma/skills/manage','/platforma/skills/manage/action','/platforma/skills/manage/edit'}
        self.enterContext(patch.object(onboarding,'check_web_path',side_effect=lambda actor,path:check(actor,'/platforma/skills' if path in managed else path)))
        self.http=self.enterContext(TestClient(self.mcp_test_server().streamable_http_app(),base_url='https://gateway.example.invalid'))

    def login(self,actor):
        self.http.cookies.set('gateway_token',auth.mint_gateway_token(actor),domain='gateway.example.invalid',path='/')

    def page(self,sid,revision=None):
        params={'id':sid}
        if revision is not None:params['version']=str(revision)
        return self.http.get('/platforma/skills/manage',params=params)

    def test_author_owner_private_report_and_denied_actor_does_not_run_scanner(self):
        sid=self.draft(content=CONTENT+b'\n[guide](private-only-guide.md)\n')['name']
        for actor in [self.reader,self.owner]:
            self.login(actor);page=self.page(sid)
            self.assertEqual(page.status_code,200,page.text)
            self.assertIn('Проверка файлов и ссылок',page.text);self.assertIn('Требуют внимания: 1',page.text)
            self.assertIn('Файлов: 1',page.text);self.assertIn('version=1',page.text)
        self.assertNotIn(sid,self.names())
        self.login(self.other)
        with patch.object(skill_review,'inspect_snapshot',side_effect=AssertionError('must authorize first')) as scanner:
            page=self.page(sid)
        self.assertEqual(page.status_code,403);self.assertNotIn('private-only-guide',page.text);scanner.assert_not_called()

    def test_new_saved_revision_fixes_report_and_old_snapshot_stays_unchanged(self):
        sid=self.draft(content=CONTENT+b'\n[guide](guide.md)\n')['name'];self.login(self.reader)
        self.assertIn('Требуют внимания: 1',self.page(sid).text)
        updated=CONTENT.replace(b'1.0.0',b'1.0.1')+b'\n[guide](guide.md)\n'
        service.update_draft(self.reader,skill_id=sid,expected_revision=1,title='Исправленный комплект',version='1.0.1',
            filename='package.zip',content=archive({'SKILL.md':updated,'guide.md':'Confirmed guide.\n'}),bundled=self.bundled)
        new=self.page(sid);old=self.page(sid,1)
        self.assertIn('Требуют внимания: 0',new.text);self.assertIn('редакция 2',new.text)
        self.assertIn('Требуют внимания: 1',old.text);self.assertIn('редакция 1',old.text)
        self.assertIn('Вы читаете сохранённую версию',old.text)
        self.assertIn('version=1&amp;path=SKILL.md',old.text);self.assertNotIn(sid,self.names())

    def test_report_is_advisory_and_existing_explicit_owner_publish_is_preserved(self):
        sid=service.create_draft(self.reader,title='Неполная декларация',filename='package.zip',
            content=archive({'SKILL.md':CONTENT,'requirements.json':'{invalid-synthetic-declaration'}),
            upload_key=str(uuid4()),bundled=self.bundled)['name']
        self.login(self.owner);page=self.page(sid)
        self.assertIn('Исправьте формат requirements.json',page.text)
        self.assertIn('Сервер не запускает файлы',page.text)
        self.action(sid,'submit',self.reader)
        with self.assertRaises(ValueError):self.action(sid,'publish',ack=False)
        self.action(sid,'publish',ack=True)
        self.assertIn(sid,self.names());self.assertEqual(self.row(sid)['stage'],'published')
        self.assertIn('requirements.json',corporate_skills.read_skill(sid,'requirements.json',actor=self.other)['path'])

    def test_card_output_is_escaped_including_operations_and_informational_old_address(self):
        operations={'schema':1,'operations':[{'id':'draft','title':'<script>bad()</script>','kind':'assistant'}]}
        sid=service.create_draft(self.reader,title='Пример <img src=x onerror=bad()>',filename='package.zip',
            content=archive({'SKILL.md':CONTENT+b'\nhttps://kromin.axion-test.akmetron.ru/mcp\n',
                'requirements.json':json.dumps(operations)}),upload_key=str(uuid4()),bundled=self.bundled)['name']
        self.login(self.reader);page=self.page(sid)
        self.assertEqual(page.status_code,200);self.assertNotIn('<script>bad()',page.text)
        self.assertNotIn('<img src=x',page.text);self.assertIn('&lt;script&gt;bad()',page.text)
        self.assertIn('Указан прежний адрес Платформы',page.text)
        self.assertIn('не подтверждает подключение сервиса или права',page.text)

    def test_version_mismatch_is_explained_after_metadata_only_edit(self):
        sid=self.draft()['name'];self.login(self.reader)
        service.update_draft(self.reader,skill_id=sid,expected_revision=1,title='Новая метка',version='1.0.1',bundled=self.bundled)
        page=self.page(sid)
        self.assertEqual(page.status_code,200);self.assertIn('Версия в заголовке SKILL.md отличается',page.text)
        self.assertIn('редакция 2, версия 1.0.1',page.text)
        self.assertEqual(service.detail(self.reader,sid,1)['payload']['version'],'1.0.0')
