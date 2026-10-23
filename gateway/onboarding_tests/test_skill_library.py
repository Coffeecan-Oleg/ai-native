"""Real disposable PostgreSQL, personal browser forms and HTTP MCP."""
import hashlib
import json
import os
from pathlib import Path
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4
import io
import zipfile

from starlette.testclient import TestClient
from test_reader_onboarding import ReaderFixture
from gateway_mcp.services import auth, corporate_skills, platforma_onboarding as onboarding, skill_library
from gateway_mcp.services.storage_core import _connect

CONTENT=b'---\nname: example-skill\ndescription: Fictional request assistance\nversion: "1.0.0"\n---\n\nUse only fictional data. Ask one question.\n'


class SkillLibraryFixture(ReaderFixture):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ,{'GATEWAY_CORPORATE_SKILLS_ENABLED':'true',
            'GATEWAY_PLATFORMA_SKILL_LIBRARY_ENABLED':'true','GATEWAY_BRAND':'akmetron'}))
        with _connect() as conn:
            conn.execute('truncate platforma_shared_skill_events,platforma_shared_skill_files,platforma_shared_skills restart identity')
        self.bundled=corporate_skills.list_skills()['skills']
        self.approved=self.approve()

    def upload(self,actor=None,title='Разбор заявки',key=None,content=CONTENT):
        return skill_library.upload(actor or self.reader,title=title,filename='SKILL.md',content=content,
                                    upload_key=key or str(uuid4()),bundled=self.bundled)

    def find(self,actor,identifier):
        return next(r for r in corporate_skills.list_skills(actor)['skills'] if r['name']==identifier)


class SkillLibraryTests(SkillLibraryFixture):
    def test_two_readers_share_material_but_only_author_can_rename(self):
        self.approve(self.other)
        row=self.upload();found=self.find(self.other,row['name'])
        self.assertFalse(found['can_rename']);self.assertFalse(found['is_author'])
        before=corporate_skills.read_skill(row['name'],actor=self.other)
        with self.assertRaises(PermissionError):
            skill_library.rename(self.other,skill_id=row['name'],title='Чужое изменение',expected_revision=1,bundled=self.bundled)
        result=skill_library.rename(self.reader,skill_id=row['name'],title='Помощник инженера',expected_revision=1,bundled=self.bundled)
        after=corporate_skills.read_skill('Помощник инженера',actor=self.other)
        self.assertEqual((result['name'],after['skill']),(row['name'],row['name']))
        self.assertEqual(after['sha256'],before['sha256']);self.assertEqual(after['content'],before['content'])
        self.assertEqual(self.find(self.reader,row['name'])['revision'],2)

    def test_owner_renames_any_upload_and_builtin_without_file_changes(self):
        row=self.upload()
        skill_library.rename(self.owner,skill_id=row['name'],title='Общий помощник',expected_revision=1,bundled=self.bundled)
        builtin=self.bundled[0];before=corporate_skills.read_skill(builtin['name'])
        with self.assertRaises(PermissionError):
            skill_library.rename(self.reader,skill_id=builtin['name'],title='Моя подпись',expected_revision=0,bundled=self.bundled)
        skill_library.rename(self.owner,skill_id=builtin['name'],title='Фирменное оформление',expected_revision=0,bundled=self.bundled)
        after=corporate_skills.read_skill('Фирменное оформление',actor=self.reader)
        self.assertEqual((after['skill'],after['sha256']),(builtin['name'],before['sha256']))

    def test_name_collisions_case_unicode_and_builtin_ids(self):
        self.upload(title='Общий навык')
        for name in ['ОБЩИЙ НАВЫК',self.bundled[0]['title'],self.bundled[0]['name']]:
            with self.assertRaises(ValueError):self.upload(title=name)
        row=self.upload(title='Другой навык')
        with self.assertRaises(ValueError):
            skill_library.rename(self.reader,skill_id=row['name'],title='Общий навык',expected_revision=1,bundled=self.bundled)

    def test_repeat_upload_is_idempotent_and_changes_require_fresh_form(self):
        key=str(uuid4());one=self.upload(key=key);two=self.upload(key=key)
        self.assertEqual(one['name'],two['name']);self.assertTrue(two['replayed'])
        with self.assertRaises(ValueError):self.upload(key=key,title='Другой текст')
        with _connect() as conn:
            self.assertEqual(conn.execute('select count(*) n from platforma_shared_skills').fetchone()['n'],1)
            self.assertEqual(conn.execute('select count(*) n from platforma_shared_skill_events').fetchone()['n'],1)

    def test_stale_rename_conflicts_and_concurrent_name_claim_has_one_winner(self):
        row=self.upload()
        skill_library.rename(self.reader,skill_id=row['name'],title='Версия названия',expected_revision=1,bundled=self.bundled)
        with self.assertRaises(ValueError):
            skill_library.rename(self.reader,skill_id=row['name'],title='Старая форма',expected_revision=1,bundled=self.bundled)
        def claim(_):
            try:return self.upload(title='Одно общее имя')['name']
            except ValueError:return None
        with ThreadPoolExecutor(max_workers=2) as pool:result=list(pool.map(claim,range(2)))
        self.assertEqual(sum(r is not None for r in result),1)

    def test_pending_revoked_expired_and_mcp_credentials_cannot_upload(self):
        with self.assertRaises(PermissionError):self.upload(actor=self.other)
        delegated=onboarding.oauth_reader_actor(self.fresh())
        with self.assertRaises(PermissionError):self.upload(actor=delegated)
        row=self.upload()
        onboarding.decide(self.owner,self.approved['id'],'revoked',self.approved['revision'],'test')
        for operation in [lambda:self.upload(),lambda:corporate_skills.list_skills(delegated),lambda:corporate_skills.read_skill(row['name'],actor=delegated)]:
            with self.assertRaises(PermissionError):operation()
        with patch.dict(os.environ,{'GATEWAY_PLATFORMA_READER_UNTIL':'2000-01-01T00:00:00Z'}):
            with self.assertRaises(PermissionError):self.upload(actor=self.owner)

    def test_failed_transaction_never_publishes_partial_files(self):
        with _connect() as conn:
            conn.execute("""create function library_test_failure() returns trigger language plpgsql as $$
                begin raise exception 'injected file persistence failure'; end $$""")
            conn.execute('create trigger library_test_failure before insert on platforma_shared_skill_files for each row execute function library_test_failure()')
        try:
            import psycopg
            with self.assertRaises(psycopg.errors.RaiseException):self.upload()
        finally:
            with _connect() as conn:
                conn.execute('drop trigger library_test_failure on platforma_shared_skill_files')
                conn.execute('drop function library_test_failure()')
        with _connect() as conn:
            for table in ['platforma_shared_skill_files','platforma_shared_skills','platforma_shared_skill_events']:
                self.assertEqual(conn.execute('select count(*) n from '+table).fetchone()['n'],0)

    def test_author_quota_is_checked_before_publishing(self):
        with patch.object(skill_library,'MAX_PER_AUTHOR',2):
            self.upload(title='Первая инструкция');self.upload(title='Вторая инструкция')
            with self.assertRaises(ValueError):self.upload(title='Третья инструкция')
        with _connect() as conn:
            self.assertEqual(conn.execute('select count(*) n from platforma_shared_skills').fetchone()['n'],2)

    def test_database_content_tampering_is_detected_on_read(self):
        row=self.upload()
        with _connect() as conn:
            conn.execute('update platforma_shared_skill_files set content=%s where skill_id=%s',(b'tampered',row['name']))
        with self.assertRaises(ValueError):corporate_skills.read_skill(row['name'],actor=self.reader)

    def test_feature_disabled_keeps_original_catalogue(self):
        self.upload()
        with patch.dict(os.environ,{'GATEWAY_PLATFORMA_SKILL_LIBRARY_ENABLED':'false'}):
            self.assertEqual(len(corporate_skills.list_skills(self.reader)['skills']),len(self.bundled))
            with self.assertRaises(PermissionError):self.upload()


class SkillLibraryWebTests(SkillLibraryFixture):
    def setUp(self):
        super().setUp()
        from gateway_mcp.server import create_mcp
        self.http=self.enterContext(TestClient(create_mcp().streamable_http_app(),base_url='https://gateway.example.invalid'))

    def login(self,actor):
        self.http.cookies.set('gateway_token',auth.mint_gateway_token(actor),domain='gateway.example.invalid',path='/')

    def fields(self,response):
        return {k:v for k,v in re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">',response.text)}

    def test_browser_upload_view_search_rename_and_escaped_material(self):
        self.login(self.reader)
        page=self.http.get('/platforma/skills/upload');self.assertEqual(page.status_code,200)
        fields=self.fields(page)|{'title':'Проверка <script>bad</script>','shared':'yes'}
        uploaded=self.http.post('/platforma/skills/upload',data=fields,files={'file':('SKILL.md',CONTENT+b'<script>never_execute()</script>','text/markdown')},follow_redirects=False)
        self.assertEqual(uploaded.status_code,303,uploaded.text)
        page=self.http.get(uploaded.headers['location']);self.assertEqual(page.status_code,200,page.text)
        self.assertNotIn('<script>never_execute()',page.text);self.assertIn('&lt;script&gt;never_execute()',page.text)
        self.assertIn('shared-',page.text);self.assertIn('Понятное название',page.text)
        rename=self.fields(page)|{'title':'Понятное имя помощника'}
        response=self.http.post('/platforma/skills/rename',data=rename,follow_redirects=False)
        self.assertEqual(response.status_code,303,response.text)
        self.assertIn('Понятное имя помощника',self.http.get(response.headers['location']).text)
        self.assertIn('Понятное имя помощника',self.http.get('/platforma/skills',params={'q':'Понятное','source':'mine'}).text)
        self.assertIn("default-src 'none'",page.headers['content-security-policy'])

    def test_csrf_origin_and_other_author_rename_are_denied(self):
        row=self.upload();self.login(self.reader)
        self.assertEqual(self.http.post('/platforma/skills/rename',data={'skill_id':row['name'],'title':'Попытка','revision':'1','csrf':'no'}).status_code,403)
        page=self.http.get('/platforma/skills/view',params={'id':row['name']})
        form=self.fields(page)|{'title':'Неверный источник'}
        self.assertEqual(self.http.post('/platforma/skills/rename',data=form,headers={'Origin':'https://evil.invalid'}).status_code,403)
        self.approve(self.other);self.login(self.other)
        other=self.http.get('/platforma/skills/upload')
        form.update(csrf=self.fields(other)['csrf'])
        self.assertEqual(self.http.post('/platforma/skills/rename',data=form).status_code,403)
        self.assertNotIn('action="/platforma/skills/rename"',self.http.get('/platforma/skills/view',params={'id':row['name']}).text)

    def test_browser_zip_upload_replay_preserves_one_shared_skill(self):
        archive=io.BytesIO()
        with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
            z.writestr('demo/SKILL.md',CONTENT);z.writestr('demo/references/check.md',b'Fictional check')
        self.login(self.reader)
        form=self.fields(self.http.get('/platforma/skills/upload'))|{'title':'Навык из ZIP','shared':'yes'}
        one=self.http.post('/platforma/skills/upload',data=form,files={'file':('skill.zip',archive.getvalue(),'application/zip')},follow_redirects=False)
        two=self.http.post('/platforma/skills/upload',data=form,files={'file':('skill.zip',archive.getvalue(),'application/zip')},follow_redirects=False)
        self.assertEqual((one.status_code,two.status_code),(303,303));self.assertEqual(one.headers['location'],two.headers['location'])
        self.assertIn('references/check.md',self.http.get(one.headers['location']).text)
        with _connect() as conn:
            self.assertEqual(conn.execute('select count(*) n from platforma_shared_skills').fetchone()['n'],1)

    def test_browser_body_limit_rejects_before_any_file_persistence(self):
        from gateway_mcp.routes.platforma_skills import MAX_BODY
        self.login(self.reader)
        response=self.http.post('/platforma/skills/upload',content=b'x'*(MAX_BODY+1),headers={'content-type':'multipart/form-data; boundary=test'})
        self.assertEqual(response.status_code,409)
        with _connect() as conn:
            self.assertEqual(conn.execute('select count(*) n from platforma_shared_skill_files').fetchone()['n'],0)

    def test_pending_private_routes_and_feature_disable_remain_closed(self):
        self.login(self.other)
        self.assertEqual(self.http.get('/platforma/skills').status_code,403)
        self.login(self.reader)
        for path in ['/admin/platforma-readers','/admin/telemetry/skills','/credentials']:
            self.assertEqual(self.http.get(path).status_code,403)
        with patch.dict(os.environ,{'GATEWAY_PLATFORMA_SKILL_LIBRARY_ENABLED':'false'}):
            self.assertEqual(self.http.get('/platforma/skills').status_code,404)

    def test_delegated_owner_mcp_token_is_not_a_browser_write_session(self):
        self.login(replace(self.owner,tool_allowlist=('gateway_corporate_skill_list',)))
        self.assertEqual(self.http.get('/platforma/skills/upload',follow_redirects=False).status_code,303)

    def test_real_http_mcp_shared_read_by_title_and_telemetry_after_rename(self):
        from gateway_mcp.services.oauth_limits import limit_oauth_actor
        from gateway_mcp.services import addon_guard
        self.enterContext(patch.dict(os.environ,{'GATEWAY_PLATFORMA_PARTICIPANT_PILOT_ENABLED':'true',
            'GATEWAY_MCP_OAUTH_SCOPES':' '.join(sorted(onboarding.SCOPES|onboarding.PARTICIPANT_EXTRA_SCOPES)),
            'GATEWAY_MCP_OAUTH_TOOLS':' '.join(sorted(onboarding.TOOLS|onboarding.ROUTES|onboarding.PARTICIPANT_TOOLS|onboarding.PARTICIPANT_ROUTES))}))
        self.profiles['profiles']['yandex:9001']['skill_telemetry']={'project':'platforma-training','skills':{},'agents':['codex'],'clients':['codex-desktop'],'scopes':sorted(onboarding.SCOPES|onboarding.PARTICIPANT_EXTRA_SCOPES)}
        self.profile_path.write_text(json.dumps(self.profiles))
        onboarding.upgrade_approved_participants(self.owner)
        actor=limit_oauth_actor(self.fresh())
        row=self.upload();skill_library.rename(self.reader,skill_id=row['name'],title='Общий разбор',expected_revision=1,bundled=self.bundled)
        headers={'Authorization':'Bearer '+auth.mint_gateway_token(actor),'Accept':'application/json, text/event-stream'}
        def rpc(body):
            response=self.http.post('/mcp',json={'jsonrpc':'2.0',**body},headers=headers)
            self.assertIn(response.status_code,(200,202),response.text[:500])
            if response.headers.get('mcp-session-id'):headers['Mcp-Session-Id']=response.headers['mcp-session-id']
            if not response.content:return {}
            if 'text/event-stream' in response.headers.get('content-type',''):
                return next(json.loads(s[5:]) for s in response.text.splitlines() if s.startswith('data:') and ('"result"' in s or '"error"' in s))
            return response.json()
        init=rpc({'id':1,'method':'initialize','params':{'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'isolated-skill-library','version':'1'}}})
        headers['MCP-Protocol-Version']=init['result']['protocolVersion'];rpc({'method':'notifications/initialized'})
        counter=10
        def tool(name,args=None):
            nonlocal counter
            counter+=1;value=rpc({'id':counter,'method':'tools/call','params':{'name':name,'arguments':args or {}}})['result']
            self.assertFalse(value.get('isError'),value)
            return json.loads(next(c['text'] for c in value['content'] if c['type']=='text'))
        catalogue=tool('gateway_corporate_skill_list')
        self.assertIn(row['name'],[s['name'] for s in catalogue['skills']])
        read=tool('gateway_corporate_skill_read',{'skill':'Общий разбор'})
        self.assertEqual(read['skill'],row['name']);self.assertEqual(read['sha256'],hashlib.sha256(CONTENT).hexdigest())
        event={'agent':'codex','client':'codex-desktop','skill_id':row['name'],'skill_pack':'platforma-shared','skill_version':'1.0.0','project':'platforma-training','session_id':'isolated-skill-check','metadata_json':'{}'}
        start=tool('gateway_telemetry_skill_started',event)
        tool('gateway_telemetry_skill_completed',event|{'correlation_id':start['event']['correlation_id'],'duration_ms':10})
        stats=tool('gateway_telemetry_skill_stats',{'project':'platforma-training'})
        self.assertEqual(stats['skills'][0]['skill_id'],row['name']);self.assertEqual(stats['skills'][0]['completed_count'],1)
        with self.assertRaises(PermissionError):addon_guard.check_call(actor,'gateway_telemetry_skill_started',event|{'skill_version':'999'})
        with self.assertRaises(PermissionError):addon_guard.check_call(actor,'gateway_telemetry_skill_stats',{'actor_subject':self.owner.subject})
