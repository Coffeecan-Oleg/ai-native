"""Real temporary PostgreSQL and actual browser routes; no business APIs."""
from unittest.mock import patch
from starlette.testclient import TestClient
from test_skill_lifecycle import LifecycleFixture,CONTENT
from gateway_mcp.services import skill_lifecycle as service,skill_comparison,auth,corporate_skills,platforma_onboarding as onboarding
from gateway_mcp.services.storage_core import _connect


class SkillComparisonWebTests(LifecycleFixture):
    def setUp(self):
        super().setUp();check=onboarding.check_web_path
        managed={'/platforma/skills/manage','/platforma/skills/manage/action','/platforma/skills/manage/edit'}
        self.enterContext(patch.object(onboarding,'check_web_path',side_effect=lambda actor,path:check(actor,'/platforma/skills' if path in managed else path)))
        self.http=self.enterContext(TestClient(self.mcp_test_server().streamable_http_app(),base_url='https://gateway.example.invalid'))

    def login(self,actor):self.http.cookies.set('gateway_token',auth.mint_gateway_token(actor),domain='gateway.example.invalid',path='/')
    def page(self,sid,revision=None):
        return self.http.get('/platforma/skills/manage',params={'id':sid,**({'version':str(revision)} if revision is not None else {})})
    def update(self,sid,version,content):
        return service.update_draft(self.reader,skill_id=sid,expected_revision=self.row(sid)['revision'],title='Обновлённый навык',
                                    version=version,filename='SKILL.md',content=content,bundled=self.bundled)

    def test_candidate_compares_with_publication_not_intermediate_unpublished_draft(self):
        sid=self.draft()['name'];self.publish(sid)
        first=corporate_skills.read_skill(sid,actor=self.other)
        self.update(sid,'1.0.1',CONTENT+b'\nIntermediate private rule.\n')
        self.update(sid,'1.0.2',CONTENT+b'\nFinal private rule.\n')
        data=service.detail(self.reader,sid)
        self.assertEqual(data['comparison_kind'],'published');self.assertEqual(data['comparison_base']['revision'],1)
        r=skill_comparison.compare(data['payload'],data['comparison_base'],basis=data['comparison_kind'])
        lines='\n'.join(x['text'] for item in r['files'] for x in item['diff'])
        self.assertIn('Final private rule.',lines);self.assertNotIn('Intermediate private rule.',lines)
        self.assertEqual(corporate_skills.read_skill(sid,actor=self.other)['sha256'],first['sha256'])

    def test_unpublished_and_historical_selection_never_compare_to_future_snapshot(self):
        sid=self.draft()['name'];self.update(sid,'1.0.1',CONTENT+b'\nSecond private rule.\n')
        second=service.detail(self.reader,sid);self.assertEqual(second['comparison_kind'],'previous_saved')
        second_revision=second['payload']['revision'];self.assertEqual(second['comparison_base']['revision'],1)
        self.publish(sid)
        historical=service.detail(self.reader,sid,1)
        self.assertIsNone(historical['comparison_base']);self.assertIsNone(historical['comparison_kind'])
        selected=service.detail(self.reader,sid,second_revision)
        self.assertEqual(selected['comparison_base']['revision'],1);self.assertEqual(selected['comparison_kind'],'previous_saved')

    def test_private_comparison_is_author_owner_only_and_does_not_execute_for_outsider(self):
        sid=self.draft()['name'];self.update(sid,'1.0.1',CONTENT+b'\nPrivate comparison marker.\n')
        for actor in [self.reader,self.owner]:
            self.login(actor);page=self.page(sid)
            self.assertEqual(page.status_code,200,page.text);self.assertIn('Что изменилось в этой редакции',page.text)
            self.assertIn('+Private comparison marker.',page.text)
        self.login(self.other)
        with patch.object(skill_comparison,'compare',side_effect=AssertionError('authorization must run first')) as compare:
            page=self.page(sid)
        self.assertEqual(page.status_code,403);self.assertNotIn('Private comparison marker',page.text);compare.assert_not_called()
        self.assertNotIn(sid,self.names())

    def test_full_page_escapes_difference_and_links_to_both_saved_revisions(self):
        sid=self.draft()['name'];self.update(sid,'1.0.1',CONTENT+b'\n<script>must_not_execute()</script>\n')
        self.login(self.reader);page=self.page(sid)
        self.assertEqual(page.status_code,200,page.text)
        self.assertNotIn('<script>must_not_execute()',page.text);self.assertIn('+&lt;script&gt;must_not_execute()',page.text)
        self.assertIn('version=1&amp;path=SKILL.md',page.text);self.assertIn('version=2&amp;path=SKILL.md',page.text)

    def test_reading_comparison_changes_no_skill_state_or_workflow_history(self):
        sid=self.draft()['name'];self.update(sid,'1.0.1',CONTENT+b'\nUpdated rule.\n');self.login(self.owner)
        def counts():
            with _connect() as conn:return [conn.execute('select count(*) n from '+table).fetchone()['n'] for table in
                ['platforma_skill_workflows','platforma_skill_versions','platforma_skill_workflow_events','platforma_shared_skills','platforma_shared_skill_files']]
        before=counts();self.assertEqual(self.page(sid).status_code,200);self.assertEqual(counts(),before)
        self.assertEqual(self.row(sid)['stage'],'draft');self.assertNotIn(sid,self.names())

    def test_archived_history_is_still_private_and_comparable_without_restore(self):
        sid=self.draft()['name'];self.publish(sid);self.update(sid,'1.0.1',CONTENT+b'\nArchived private rule.\n')
        self.action(sid,'archive');self.login(self.reader);page=self.page(sid)
        self.assertEqual(page.status_code,200);self.assertIn('Что изменилось в этой редакции',page.text)
        self.assertNotIn(sid,self.names());self.assertEqual(self.row(sid)['stage'],'archived')
