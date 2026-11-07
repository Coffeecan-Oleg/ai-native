"""Real disposable PostgreSQL and browser forms; no corporate data or APIs."""
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import io
import re
from unittest.mock import patch
from uuid import uuid4
import zipfile

from starlette.testclient import TestClient
from test_skill_library import SkillLibraryFixture,CONTENT
from gateway_mcp.services import skill_lifecycle as service,skill_library,corporate_skills,auth
from gateway_mcp.services.storage_core import _connect


class LifecycleFixture(SkillLibraryFixture):
    def setUp(self):
        super().setUp()
        self.approve(self.other)
        self.enterContext(patch.object(service,'enabled',return_value=True))
        with _connect() as conn:
            conn.execute('truncate platforma_skill_workflow_events,platforma_skill_versions,platforma_skill_workflows restart identity cascade')

    def draft(self,actor=None,title='Полезный навык',key=None,content=CONTENT):
        return service.create_draft(actor or self.reader,title=title,filename='SKILL.md',content=content,
                                    upload_key=key or str(uuid4()),bundled=self.bundled)

    def row(self,sid,actor=None):return service.detail(actor or self.reader,sid)['workflow']

    def action(self,sid,action,actor=None,revision=None,ack=True):
        return service.transition(actor or self.owner,skill_id=sid,
            expected_revision=self.row(sid)['revision'] if revision is None else revision,
            action=action,bundled=self.bundled,review_ack=ack)

    def publish(self,sid):
        self.action(sid,'submit',self.reader);return self.action(sid,'publish')

    def names(self,actor=None):return [r['name'] for r in corporate_skills.list_skills(actor or self.other)['skills']]


class LifecycleStorageTests(LifecycleFixture):
    def test_draft_is_private_and_absent_from_shared_tables_and_mcp(self):
        row=self.draft();sid=row['name']
        self.assertNotIn(sid,self.names())
        with self.assertRaises(PermissionError):service.detail(self.other,sid)
        self.assertEqual(service.dashboard(self.other),[])
        self.assertEqual(len(service.dashboard(self.owner)),1)
        self.assertEqual(service.detail(self.reader,sid)['payload']['files']['SKILL.md'],CONTENT.decode())
        with _connect() as conn:
            self.assertEqual(conn.execute('select count(*) n from platforma_shared_skills').fetchone()['n'],0)

    def test_full_cycle_keeps_old_publication_during_review_and_retains_history(self):
        sid=self.draft()['name'];self.publish(sid)
        self.assertTrue(next(r for r in corporate_skills.list_skills(self.other)['skills'] if r['name']==sid)['reviewed'])
        first=corporate_skills.read_skill(sid,actor=self.other)
        current=self.row(sid)
        service.update_draft(self.reader,skill_id=sid,expected_revision=current['revision'],title='Новая редакция',
            version='1.0.1',filename='SKILL.md',content=CONTENT+b'\nA second safe instruction.\n',bundled=self.bundled)
        self.assertEqual(corporate_skills.read_skill(sid,actor=self.other)['content'],first['content'])
        self.action(sid,'submit',self.reader)
        self.assertEqual(corporate_skills.read_skill(sid,actor=self.other)['sha256'],first['sha256'])
        self.action(sid,'publish')
        second=corporate_skills.read_skill(sid,actor=self.other)
        self.assertIn('A second safe instruction.',second['content'])
        self.assertEqual(second['skill'],sid)
        detail=service.detail(self.reader,sid)
        self.assertEqual(len(detail['versions']),2)
        self.assertEqual(service.detail(self.reader,sid,1)['payload']['files']['SKILL.md'],CONTENT.decode())
        cached=next(r for r in corporate_skills.list_skills(self.other)['skills'] if r['name']==sid)
        self.action(sid,'archive');self.assertNotIn(sid,self.names())
        with self.assertRaises(PermissionError):skill_library.read_resource(self.other,cached,'SKILL.md')
        self.action(sid,'restore');self.assertIn(sid,self.names())
        self.assertEqual(corporate_skills.read_skill(sid,actor=self.other)['content'],second['content'])
        self.assertEqual(len(service.detail(self.owner,sid)['versions']),2)

    def test_only_owner_can_review_publish_archive_and_restore(self):
        sid=self.draft()['name'];self.action(sid,'submit',self.reader)
        for action in ['publish','request_changes','archive','restore']:
            with self.subTest(action=action),self.assertRaises(PermissionError):self.action(sid,action,self.reader)
        with self.assertRaises(ValueError):self.action(sid,'publish',ack=False)
        self.action(sid,'request_changes');self.assertEqual(self.row(sid)['stage'],'draft')
        with self.assertRaises(ValueError):self.action(sid,'publish')

    def test_restore_keeps_newer_unreviewed_candidate_as_draft(self):
        sid=self.draft()['name'];self.publish(sid)
        previous=corporate_skills.read_skill(sid,actor=self.other)
        service.update_draft(self.reader,skill_id=sid,expected_revision=self.row(sid)['revision'],
            title='Следующая редакция',version='1.0.1',filename='SKILL.md',
            content=CONTENT+b'\nNew unreviewed instruction.\n',bundled=self.bundled)
        self.action(sid,'submit',self.reader);self.action(sid,'archive');self.action(sid,'restore')
        self.assertEqual(self.row(sid)['stage'],'draft')
        self.assertEqual(corporate_skills.read_skill(sid,actor=self.other)['sha256'],previous['sha256'])
        with self.assertRaises(ValueError):self.action(sid,'publish')
        self.action(sid,'submit',self.reader);self.action(sid,'publish')
        self.assertIn('New unreviewed instruction.',corporate_skills.read_skill(sid,actor=self.other)['content'])

    def test_mcp_credentials_cannot_enter_browser_workflow_even_for_owner(self):
        sid=self.draft()['name'];delegated=replace(self.owner,tool_allowlist=('gateway_corporate_skill_read',))
        for op in [lambda:self.draft(delegated),lambda:service.dashboard(delegated),lambda:service.detail(delegated,sid),lambda:self.action(sid,'archive',delegated)]:
            with self.assertRaises(PermissionError):op()

    def test_foreign_actor_cannot_guess_or_modify_draft(self):
        sid=self.draft()['name']
        for op in [lambda:service.detail(self.other,sid),lambda:self.action(sid,'submit',self.other),
                   lambda:service.update_draft(self.other,skill_id=sid,expected_revision=1,title='Подмена',version='2',bundled=self.bundled)]:
            with self.assertRaises(PermissionError):op()
        for identifier in ['../secret','market-foothold','shared-'+'x'*32]:
            with self.assertRaises(ValueError):service.detail(self.owner,identifier)

    def test_stale_and_concurrent_forms_have_one_winner(self):
        sid=self.draft()['name']
        def submit(_):
            try:self.action(sid,'submit',self.reader,revision=1);return True
            except ValueError:return False
        with ThreadPoolExecutor(max_workers=2) as pool:out=list(pool.map(submit,range(2)))
        self.assertEqual(sum(out),1)
        with self.assertRaises(ValueError):self.action(sid,'publish',revision=1)
        self.assertEqual(self.row(sid)['stage'],'review')

    def test_same_upload_key_replays_only_identical_author_and_payload(self):
        key=str(uuid4());one=self.draft(key=key);two=self.draft(key=key)
        self.assertEqual(one['name'],two['name']);self.assertTrue(two['replayed'])
        with self.assertRaises(ValueError):self.draft(key=key,title='Изменено')
        with self.assertRaises(ValueError):self.draft(actor=self.other,key=key)
        self.assertEqual(len(service.dashboard(self.owner)),1)

    def test_changed_material_requires_new_visible_version_before_publication(self):
        sid=self.draft()['name'];self.publish(sid)
        old=corporate_skills.read_skill(sid,actor=self.other)
        service.update_draft(self.reader,skill_id=sid,expected_revision=self.row(sid)['revision'],title='Новое имя',version='1.0.0',bundled=self.bundled)
        self.action(sid,'submit',self.reader)
        with self.assertRaisesRegex(ValueError,'новую версию'):self.action(sid,'publish')
        self.assertEqual(corporate_skills.read_skill(sid,actor=self.other)['content'],old['content'])
        self.assertEqual(self.row(sid)['stage'],'review')

    def test_legacy_import_does_not_change_files_or_visibility_and_builtin_denied(self):
        old=self.upload();before=corporate_skills.read_skill(old['name'],actor=self.other)
        result=service.adopt_published(self.reader,skill_id=old['name'],expected_revision=1)
        self.assertEqual(result['stage'],'published')
        after=corporate_skills.read_skill(old['name'],actor=self.other)
        self.assertFalse(next(r for r in corporate_skills.list_skills(self.other)['skills'] if r['name']==old['name'])['reviewed'])
        self.assertEqual((before['content'],before['sha256']),(after['content'],after['sha256']))
        self.assertIn(old['name'],self.names())
        with self.assertRaises(ValueError):service.adopt_published(self.owner,skill_id=self.bundled[0]['name'],expected_revision=0)
        with self.assertRaises(ValueError):skill_library.rename(self.reader,skill_id=old['name'],title='Обход проверки',expected_revision=1,bundled=self.bundled)

    def test_legacy_foreign_adoption_and_stale_revision_denied(self):
        row=self.upload()
        with self.assertRaises(PermissionError):service.adopt_published(self.other,skill_id=row['name'],expected_revision=1)
        with self.assertRaises(ValueError):service.adopt_published(self.reader,skill_id=row['name'],expected_revision=2)
        self.assertEqual(service.dashboard(self.owner),[])

    def test_transaction_failure_rolls_back_publication_and_all_files(self):
        sid=self.draft()['name'];self.action(sid,'submit',self.reader)
        with patch.object(service,'_event',side_effect=RuntimeError('injected persistence failure')):
            with self.assertRaises(RuntimeError):self.action(sid,'publish')
        self.assertEqual(self.row(sid)['stage'],'review');self.assertNotIn(sid,self.names())
        with _connect() as conn:
            for table in ['platforma_shared_skills','platforma_shared_skill_files']:
                self.assertEqual(conn.execute('select count(*) n from '+table).fetchone()['n'],0)

    def test_draft_title_collisions_and_quotas_include_private_uploads(self):
        self.draft(title='Общее имя')
        with self.assertRaises(ValueError):self.draft(actor=self.other,title='ОБЩЕЕ ИМЯ')
        with patch.object(skill_library,'MAX_PER_AUTHOR',1):
            with self.assertRaises(ValueError):self.draft(title='Второй материал')

    def test_version_integrity_tampering_is_detected(self):
        sid=self.draft()['name']
        with _connect() as conn:
            conn.execute("update platforma_skill_versions set files=jsonb_set(files,'{SKILL.md}','\"changed\"') where skill_id=%s",(sid,))
        with self.assertRaises(ValueError):service.detail(self.reader,sid)

    def test_invalid_package_never_creates_workflow(self):
        raw=io.BytesIO()
        with zipfile.ZipFile(raw,'w') as z:z.writestr('../SKILL.md',CONTENT)
        with self.assertRaises(ValueError):service.create_draft(self.reader,title='Bad paths',filename='bad.zip',content=raw.getvalue(),upload_key=str(uuid4()),bundled=self.bundled)
        self.assertEqual(service.dashboard(self.owner),[])

    def test_version_limit_preserves_immutable_history(self):
        sid=self.draft()['name']
        with patch.object(service,'MAX_VERSIONS',1):
            with self.assertRaises(ValueError):service.update_draft(self.reader,skill_id=sid,expected_revision=1,title='Новое имя',version='2',bundled=self.bundled)
        self.assertEqual(self.row(sid)['revision'],1)
        self.assertEqual(len(service.detail(self.reader,sid)['versions']),1)


class LifecycleWebTests(LifecycleFixture):
    def setUp(self):
        super().setUp()
        # This inherited fixture deliberately uses legacy synthetic identities.
        # The feature is corporate-only; retain legacy auth checks while mapping
        # the three new paths to the existing library boundary for this fixture.
        from gateway_mcp.services import platforma_onboarding as onboarding
        check=onboarding.check_web_path
        managed={'/platforma/skills/manage','/platforma/skills/manage/action','/platforma/skills/manage/edit'}
        self.enterContext(patch.object(onboarding,'check_web_path',side_effect=lambda actor,path:check(actor,'/platforma/skills' if path in managed else path)))
        self.http=self.enterContext(TestClient(self.mcp_test_server().streamable_http_app(),base_url='https://gateway.example.invalid'))

    def login(self,actor):
        self.http.cookies.set('gateway_token',auth.mint_gateway_token(actor),domain='gateway.example.invalid',path='/')

    def fields(self,response):
        return {k:v for k,v in re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">',response.text)}

    def csrf(self):return self.fields(self.http.get('/platforma/skills/upload'))['csrf']

    def test_browser_upload_creates_private_draft_and_real_http_review_publishes(self):
        self.login(self.reader)
        form=self.fields(self.http.get('/platforma/skills/upload'))|{'title':'Безопасный <script>пример</script>','shared':'yes'}
        response=self.http.post('/platforma/skills/upload',data=form,files={'file':('SKILL.md',CONTENT+b'<script>never_execute()</script>','text/markdown')},follow_redirects=False)
        self.assertEqual(response.status_code,303,response.text)
        location=response.headers['location'];sid=location.split('id=')[1]
        self.assertIn('/manage?',location)
        page=self.http.get(location);self.assertEqual(page.status_code,200,page.text)
        self.assertIn('&lt;script&gt;never_execute()',page.text)
        self.assertNotIn('<script>never_execute()',page.text)
        submit={'csrf':self.csrf(),'skill_id':sid,'revision':'1','action':'submit'}
        self.assertEqual(self.http.post('/platforma/skills/manage/action',data=submit,follow_redirects=False).status_code,303)
        self.login(self.other)
        self.assertEqual(self.http.get(location).status_code,403)
        self.assertNotIn(sid,self.names())
        self.login(self.owner)
        publish={'csrf':self.csrf(),'skill_id':sid,'revision':'2','action':'publish','review_ack':'yes'}
        response=self.http.post('/platforma/skills/manage/action',data=publish,follow_redirects=False)
        self.assertEqual(response.status_code,303,response.text)
        self.assertIn(sid,self.names())

    def test_csrf_origin_duplicate_fields_and_owner_boundary(self):
        sid=self.draft()['name'];self.login(self.reader)
        body={'csrf':self.csrf(),'skill_id':sid,'revision':'1','action':'submit'}
        self.assertEqual(self.http.post('/platforma/skills/manage/action',data=body|{'csrf':'wrong'}).status_code,403)
        self.assertEqual(self.http.post('/platforma/skills/manage/action',data=body,headers={'origin':'https://evil.invalid'}).status_code,403)
        duplicate='csrf='+body['csrf']+'&skill_id='+sid+'&revision=1&action=submit&action=publish'
        self.assertEqual(self.http.post('/platforma/skills/manage/action',content=duplicate,headers={'content-type':'application/x-www-form-urlencoded'}).status_code,409)
        self.assertEqual(self.http.post('/platforma/skills/manage/action',data=body|{'action':'archive'}).status_code,403)
        self.assertEqual(self.row(sid)['stage'],'draft')

    def test_browser_version_edit_preserves_id_and_validates_multipart(self):
        sid=self.draft()['name'];self.login(self.reader)
        form={'csrf':self.csrf(),'skill_id':sid,'revision':'1','title':'Вторая редакция','version':'1.0.1'}
        response=self.http.post('/platforma/skills/manage/edit',data=form,files={'file':('SKILL.md',CONTENT+b'\nUpdated.\n','text/markdown')},follow_redirects=False)
        self.assertEqual(response.status_code,303,response.text)
        page=self.http.get(response.headers['location']);self.assertIn(sid,page.text)
        self.assertIn('1.0.1',page.text);self.assertIn('История материалов',page.text)
        self.assertEqual(len(service.detail(self.reader,sid)['versions']),2)
        self.assertEqual(self.http.get('/platforma/skills/manage',params={'id':sid,'path':'../secrets'}).status_code,409)

    def test_history_and_dashboard_do_not_disclose_other_authors_drafts(self):
        sid=self.draft()['name'];self.login(self.other)
        self.assertNotIn(sid,self.http.get('/platforma/skills/manage').text)
        self.assertEqual(self.http.get('/platforma/skills/manage',params={'id':sid,'version':'1'}).status_code,403)
        self.login(self.owner)
        self.assertIn(sid,self.http.get('/platforma/skills/manage').text)

    def _first_publish_form(self,skill_id):
        page=self.http.get('/platforma/skills/manage',params={'id':skill_id})
        self.assertEqual(page.status_code,200)
        forms=re.findall(r'<form\b.*?</form>',page.text,re.S)
        form=next(v for v in forms if 'name="action" value="publish"' in v)
        fields=dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">',form))
        return fields|{'review_ack':'yes'}

    def test_first_owner_review_page_can_publish_without_csrf_warmup(self):
        from gateway_mcp.routes.platforma_onboarding import COOKIE
        for previous in ['missing','another_actor']:
            with self.subTest(previous_cookie=previous):
                if COOKIE in self.http.cookies:self.http.cookies.delete(COOKIE)
                if previous=='another_actor':
                    self.login(self.reader);self.http.get('/platforma/skills/upload')
                sid=self.draft(title='Первая проверка '+previous)['name']
                self.action(sid,'submit',self.reader)
                self.login(self.owner)
                # Use the real first rendered publish form. No extra GET or
                # csrf() call may warm up or replace its cookie before POST.
                form=self._first_publish_form(sid)
                response=self.http.post('/platforma/skills/manage/action',data=form,follow_redirects=False)
                self.assertEqual(response.status_code,303)
                self.assertEqual(self.row(sid)['stage'],'published')

    def test_owner_first_page_still_rejects_wrong_csrf_and_foreign_origin(self):
        sid=self.draft()['name'];self.action(sid,'submit',self.reader);self.login(self.owner)
        form=self._first_publish_form(sid)
        for data,headers in [(form|{'csrf':'invalid'},{}),(form,{'origin':'https://foreign.example.invalid'})]:
            response=self.http.post('/platforma/skills/manage/action',data=data,headers=headers,follow_redirects=False)
            self.assertEqual(response.status_code,403)
            self.assertEqual(self.row(sid)['stage'],'review')
