"""Personal training /admin views against disposable PostgreSQL, never live data."""
from dataclasses import replace
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

from starlette.testclient import TestClient
import test_reader_onboarding as fixtures
from gateway_mcp.routes import training_admin as ui
from gateway_mcp.services import auth, platforma_onboarding as service
from gateway_mcp.services.observability import audit_event
from gateway_mcp.services.storage_core import _connect
from gateway_mcp.services.telemetry import record_skill_event


PATHS=('/admin','/admin/audit','/admin/telemetry/skills')


class TrainingAdminTests(fixtures.ReaderFixture):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ, {'GATEWAY_BRAND':'akmetron',
            'GATEWAY_PLATFORMA_PARTICIPANT_PILOT_ENABLED':'true',
            'GATEWAY_PLATFORMA_SKILL_LIBRARY_ENABLED':'true'}))
        self.profiles['profiles']['yandex:9001']['skill_telemetry']={'project':'platforma-training'}
        self.profile_path.write_text(json.dumps(self.profiles))
        from gateway_mcp.server import create_mcp
        self.http=self.enterContext(TestClient(create_mcp().streamable_http_app(),base_url='https://gateway.example.invalid'))

    def login(self,actor):
        self.http.cookies.clear()
        self.http.cookies.set('gateway_token',auth.mint_gateway_token(actor),domain='gateway.example.invalid',path='/')

    def approved_login(self):
        self.approved=self.approve();self.login(service.web_login_actor(self.reader))
        return self.approved

    def test_approved_waiting_cookie_opens_three_personal_views(self):
        self.approved_login()
        for path in PATHS:
            response=self.http.get(path)
            self.assertEqual(response.status_code,200,(path,response.text))
            self.assertIn('Обзор MCP',response.text)
            self.assertIn('Мои обращения к MCP',response.text)
            self.assertIn('Мои запуски навыков',response.text)
        page=self.http.get('/admin').text
        self.assertIn('LAB-UNISENDER-001',page)
        self.assertIn('platforma.requests.update',page)
        self.assertIn('gateway_corporate_skill_read',page)
        self.assertIn('Реальный UniSender пока не подключён',page)
        self.assertNotIn('href="/admin/users"',page)

    def test_anonymous_and_delegated_credentials_require_browser_login(self):
        self.approve()
        for actor in (None,replace(self.owner,tool_allowlist=('gateway_call_tool',)),
                      replace(self.reader,tool_allowlist=('gateway_call_tool',))):
            self.http.cookies.clear()
            if actor:self.login(actor)
            for path in PATHS:
                response=self.http.get(path,follow_redirects=False)
                self.assertEqual(response.status_code,303,path)
        self.http.cookies.clear()
        token=auth.mint_gateway_token(replace(self.reader,tool_allowlist=('gateway_call_tool',)))
        self.assertEqual(self.http.get('/admin',headers={'Authorization':'Bearer '+token},follow_redirects=False).status_code,303)

    def test_pending_and_rejected_are_denied(self):
        row=service.own_request(self.reader,create=True);self.login(self.reader)
        for decision in ('pending','rejected'):
            if decision=='rejected':service.decide(self.owner,row['id'],'rejected',row['revision'],'Test rejection')
            for path in PATHS:self.assertEqual(self.http.get(path).status_code,403)

    def test_revoked_approval_closes_existing_cookie(self):
        row=self.approved_login()
        self.assertEqual(self.http.get('/admin').status_code,200)
        service.decide(self.owner,row['id'],'revoked',row['revision'],'Revoke observer')
        for path in PATHS:self.assertEqual(self.http.get(path).status_code,403)

    def test_expired_approval_closes_existing_cookie(self):
        row=self.approved_login()
        with _connect() as conn:
            conn.execute("update platforma_reader_requests set approved_until=now()-interval '1 second' where id=%s",(row['id'],))
        for path in PATHS:self.assertEqual(self.http.get(path).status_code,403)

    def test_revoked_linked_grant_closes_all_pages(self):
        row=self.approved_login()
        with _connect() as conn:
            conn.execute('update access_scope_grants set revoked_at=now() where id=%s',(row['scope_grant_ids'][0],))
        for path in PATHS:self.assertEqual(self.http.get(path).status_code,403)

    def test_cached_admin_group_never_turns_participant_into_owner(self):
        self.approve()
        self.login(replace(self.reader,scopes=('*',),groups=('admins',)))
        response=self.http.get('/admin')
        self.assertEqual(response.status_code,200)
        self.assertIn('Учебная песочница · режим наблюдения',response.text)
        self.assertNotIn('href="/admin/users"',response.text)
        for path in ('/admin/users','/admin/integrations','/admin/platforma-readers','/admin/metrics','/credentials','/my/skills'):
            self.assertEqual(self.http.get(path).status_code,403,path)

    def test_owner_dashboard_and_owner_global_views_keep_existing_handlers(self):
        self.login(self.owner)
        with patch('gateway_mcp.routes.admin_access.require_current_admin'),\
             patch('gateway_mcp.routes.admin_access.storage_showcase.showcase_snapshot',return_value={}),\
             patch('gateway_mcp.routes.admin_access.storage_showcase.showcase_people',return_value={}),\
             patch('gateway_mcp.routes.admin_access.storage_showcase.agent_profiles',return_value=[]),\
             patch('gateway_mcp.routes.admin_access.team_ai_ui.render',return_value='<h1>OWNER_DASHBOARD</h1>'):
            response=self.http.get('/admin')
        self.assertEqual(response.status_code,200,response.text)
        self.assertIn('OWNER_DASHBOARD',response.text)
        self.assertNotIn('режим наблюдения',response.text)
        for path in PATHS[1:]:self.assertEqual(self.http.get(path).status_code,200)

    def test_personal_audit_uses_real_db_and_ignores_forged_actor(self):
        self.approved_login();self.approve(self.other)
        for actor,label in ((self.reader,'OWN_EVENT'),(self.other,'FOREIGN_EVENT'),(self.owner,'OWNER_EVENT')):
            audit_event(event='tool_call',actor=actor,tool=label,status='ok',arguments={'text':'DO_NOT_RENDER_PAYLOAD'})
        response=self.http.get('/admin/audit?actor_subject='+self.owner.subject+'&actor_subject='+self.other.subject+'&include_payload=true')
        self.assertEqual(response.status_code,200,response.text)
        self.assertIn('OWN_EVENT',response.text)
        for hidden in ('FOREIGN_EVENT','OWNER_EVENT','DO_NOT_RENDER_PAYLOAD',self.owner.email,self.other.email):
            self.assertNotIn(hidden,response.text)

    def test_oauth_failure_is_explained_without_raw_error_or_payload(self):
        self.approved_login()
        audit_event(event='tool_call',actor=self.reader,tool='gateway_call_tool',status='denied',
            arguments={'tool_name':'platforma.unisender-demo.stats.get','gateway_request_id':'LAB-OWN-AUDIT','password':'PRIVATE_VALUE'},
            error='tool is outside delegated OAuth allowlist: SECRET_MUST_NOT_RENDER')
        response=self.http.get('/admin/audit')
        self.assertIn('Пройдите новый личный вход',response.text)
        self.assertIn('platforma.unisender-demo.stats.get',response.text)
        self.assertIn('LAB-OWN-AUDIT',response.text)
        for hidden in ('SECRET_MUST_NOT_RENDER','PRIVATE_VALUE'):self.assertNotIn(hidden,response.text)

    def test_skill_stats_are_bound_to_exact_subject_and_training_project(self):
        self.approved_login()
        for actor,project,label in ((self.reader,'platforma-training','OWN_SKILL'),(self.owner,'platforma-training','OWNER_SKILL'),
                                    (self.other,'platforma-training','FOREIGN_SKILL'),(self.reader,'private-project','PRIVATE_PROJECT_SKILL')):
            correlation=uuid4().hex
            for event in ('started','completed'):
                record_skill_event(actor=actor,event_type=event,agent='codex',skill_id=label,project=project,correlation_id=correlation,duration_ms=12)
        response=self.http.get('/admin/telemetry/skills?actor_subject='+self.owner.subject+'&project=private-project')
        self.assertEqual(response.status_code,200,response.text)
        self.assertIn('OWN_SKILL',response.text)
        self.assertIn('<td>0</td>',response.text)
        for hidden in ('OWNER_SKILL','FOREIGN_SKILL','PRIVATE_PROJECT_SKILL'):self.assertNotIn(hidden,response.text)

    def test_read_only_legacy_reader_has_overview_and_audit_without_writer_metrics(self):
        with patch.dict(os.environ,{'GATEWAY_PLATFORMA_PARTICIPANT_PILOT_ENABLED':'false'}):
            self.approve();self.login(service.web_login_actor(self.reader))
            for path in PATHS[:2]:self.assertEqual(self.http.get(path).status_code,200)
            response=self.http.get('/admin');self.assertNotIn('platforma.requests.update',response.text)
            self.assertEqual(self.http.get(PATHS[2]).status_code,403)

    def test_gets_and_rejected_posts_do_not_change_privileges_or_records(self):
        self.approved_login()
        tables=('access_scope_grants','access_resource_grants','platforma_reader_requests','platforma_reader_events',
                'platforma_shared_skills','assistant_skill_events','audit_events','schema_migrations')
        def counts():
            with _connect() as conn:return {t:conn.execute('select count(*) n from '+t).fetchone()['n'] for t in tables}
        before=counts()
        for path in PATHS:
            self.assertEqual(self.http.get(path).status_code,200)
            for method in ('POST','PUT','PATCH','DELETE'):
                self.assertEqual(self.http.request(method,path,content='NEVER_SAVE').status_code,405,(method,path))
        self.assertEqual(counts(),before)

    def test_exact_web_allowlist_does_not_open_neighboring_paths(self):
        self.approved_login()
        for path in ('/admin/users','/admin/integrations','/admin/access-requests','/admin/platforma-readers',
                     '/admin/metrics','/admin/people','/admin/agents','/admin/factory/connections',
                     '/admin/audit/export','/admin/telemetry/skills/export'):
            self.assertFalse(service.check_web_path(self.reader,path),path)
            self.assertIn(self.http.get(path).status_code,(403,404),path)

    def test_escaped_metadata_headers_and_no_executable_controls(self):
        self.approved_login()
        with patch.object(ui,'read_json',return_value={'tools':[{'name':'platforma.context.get','description':'<img src=x onerror=bad()>'},
                                                             {'name':'private.secret.route','description':'NEVER_DISCLOSE_FOREIGN_REGISTRY'}]}):
            response=self.http.get('/admin')
        self.assertIn('&lt;img',response.text)
        self.assertNotIn('<img src=x',response.text)
        self.assertNotIn('NEVER_DISCLOSE_FOREIGN_REGISTRY',response.text)
        for path in PATHS:
            response=self.http.get(path)
            self.assertEqual(response.headers['cache-control'],'no-store')
            self.assertEqual(response.headers['x-content-type-options'],'nosniff')
            self.assertIn("frame-ancestors 'none'",response.headers['content-security-policy'])
            self.assertNotIn('<script',response.text)
            self.assertNotIn('method="post"',response.text.lower())

    def test_storage_error_is_safe_and_has_no_partial_data(self):
        self.approved_login()
        for path,target in (('/admin','read_json'),('/admin/audit','list_audit_events'),('/admin/telemetry/skills','skill_stats')):
            with patch.object(ui,target,side_effect=RuntimeError('NEVER_EXPOSE_SECRET_PATH')):
                response=self.http.get(path)
            self.assertEqual(response.status_code,503,response.text)
            self.assertNotIn('NEVER_EXPOSE_SECRET_PATH',response.text)

    def test_disabled_training_does_not_bypass_original_admin_authorization(self):
        self.login(self.reader)
        with patch.dict(os.environ,{'GATEWAY_PLATFORMA_ONBOARDING_ENABLED':'false'}):
            for path in PATHS:self.assertEqual(self.http.get(path).status_code,403)

    def test_save_review_previews_from_real_authenticated_http(self):
        self.approved_login()
        audit_event(event='tool_call',actor=self.reader,tool='gateway_call_tool',status='denied',
            arguments={'tool_name':'platforma.unisender-demo.stats.get','gateway_request_id':'LAB-REVIEW-ONLY'},
            error='tool is outside delegated OAuth allowlist: platforma.unisender-demo.stats.get')
        for name,path in zip(('overview','audit','skills'),PATHS):
            response=self.http.get(path)
            self.assertEqual(response.status_code,200)
            html=response.text.replace('<button','<button disabled').replace('href="/','href="#preview/')
            print('TRAINING_ADMIN_PREVIEW:'+json.dumps({'name':name,'html':html},ensure_ascii=False))


if __name__=='__main__':unittest.main()
