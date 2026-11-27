"""Real audit/outbox/HTTP and producer paths; fake identities and delivery only."""
import asyncio
import os
import re
import unittest
from uuid import uuid4
from unittest.mock import patch
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient
import test_activity_notifications_http as previous
from gateway_mcp.routes import activity_notifications as ui
from gateway_mcp.services import telemetry, skill_activity, activity_delivery as worker
from gateway_mcp.services.storage_core import _connect

@unittest.skipUnless(os.getenv('ONBOARDING_TEST_DATABASE_URL'),'Disposable PostgreSQL required')
class JourneyTests(unittest.TestCase):
    setUp=previous.NotificationHttpTests.setUp
    form=previous.NotificationHttpTests.form

    def enable(self,categories):
        ui.store.save_settings(actor_subject=self.actor.subject,expected_revision=1,
                               enabled=True,categories=categories,interval_seconds=30)

    def deliver(self):
        with _connect() as db:db.execute('UPDATE platforma_activity_settings SET next_send_at=now()')
        messages=[]
        def sender(text):messages.append(text);return {'outcome':'sent','message_id':99001}
        with patch('gateway_mcp.services.storage_admin_console.admin_audit_user_names',return_value={self.actor.subject:'Иван Примеров'}):
            result=worker.run_once(sender=sender)
        return result,messages

    def test_preview_get_never_saves_enqueues_or_sends(self):
        before=ui.store.snapshot()
        with patch.object(worker,'send') as send:
            response=self.client.get(ui.PATH+'?preview_category=skill_runs')
            self.assertEqual(response.status_code,200)
            self.assertIn('ничего не отправлено',response.text)
            self.assertIn('Иван Примеров',response.text)
            send.assert_not_called()
        after=ui.store.snapshot()
        self.assertEqual(before,after)

    def test_unknown_preview_and_employee_do_not_expose_channel(self):
        self.assertEqual(self.client.get(ui.PATH+'?preview_category=secret').status_code,400)
        with patch.object(ui.corporate_access,'require_owner',side_effect=PermissionError()),patch.object(worker,'credential_url') as credential:
            self.assertEqual(self.client.get(ui.PATH+'?preview_category=logins').status_code,403)
            credential.assert_not_called()

    def test_missing_secret_preview_available_and_exception_not_rendered(self):
        with patch.object(worker,'credential_url',side_effect=RuntimeError('SECRET_VALUE')):
            response=self.client.get(ui.PATH+'?preview_category=agents')
        self.assertEqual(response.status_code,200)
        self.assertIn('Недоступен сервису',response.text)
        self.assertIn('ничего не отправлено',response.text)
        self.assertNotIn('SECRET_VALUE',response.text)

    def test_real_telemetry_producer_audit_delivery_and_no_replayed_notification(self):
        self.enable(['skill_runs'])
        args=dict(actor=self.actor,agent='codex',skill_id='shared-fixture',correlation_id=str(uuid4()))
        with patch.object(skill_activity,'enabled',return_value=False):
            telemetry.record_skill_event(event_type='started',**args)
            telemetry.record_skill_event(event_type='completed',**args)
            telemetry.record_skill_event(event_type='completed',**args)
        self.assertEqual(ui.store.snapshot()['counts'],{'pending':2})
        result,messages=self.deliver()
        self.assertEqual(result['count'],2);self.assertEqual(len(messages),1)
        self.assertIn('начал применение навыка',messages[0]);self.assertIn('завершил применение навыка',messages[0])
        page=self.client.get(ui.PATH)
        self.assertIn('99001',page.text);self.assertIn('Код отправки: act-',page.text)
        self.assertEqual(self.deliver()[1],[])

    def test_real_mcp_tool_finish_creates_read_notification_without_content(self):
        from gateway_mcp.tools import corporate_skills as tools, runtime
        from gateway_mcp import audit
        methods={}
        class Mcp:
            def tool(self,**kwargs):
                def wrap(fn):methods[fn.__name__]=fn;return fn
                return wrap
        tools.register_corporate_skill_tools(Mcp())
        self.enable(['skill_reads'])
        with patch.object(runtime,'require_scope',return_value=self.actor),patch.object(audit,'current_actor',return_value=self.actor),patch.object(ui.corporate_access,'enabled',return_value=False),patch.object(tools.corporate_skills,'read_skill',return_value={'skill':'shared-fixture','text':'PRIVATE_CONTENT'}):
            answer=asyncio.run(methods['gateway_corporate_skill_read']('shared-fixture'))
        self.assertIn('PRIVATE_CONTENT',answer)
        result,messages=self.deliver()
        self.assertEqual(result['status'],'sent')
        self.assertIn('прочитал файл навыка: shared-fixture',messages[0])
        self.assertNotIn('PRIVATE_CONTENT',messages[0])

    def test_agent_save_http_reaches_audit_queue_and_delivery(self):
        from gateway_mcp.routes import admin_showcase as agents
        self.enable(['agents']);routes=[]
        class Mcp:
            def custom_route(self,path,methods,**kwargs):
                def wrap(fn):routes.append(Route(path,fn,methods=methods));return fn
                return wrap
        agents.register_admin_showcase_routes(Mcp())
        key='fixture-'+uuid4().hex
        with patch.object(agents,'web_actor',return_value=self.actor),patch.object(agents,'require_current_admin'),TestClient(Starlette(routes=routes),base_url='https://platforma.test') as client:
            client.cookies.set(agents.CSRF_COOKIE,'fixture-csrf')
            r=client.post('/admin/agents/save',data={'csrf':'fixture-csrf','key':key,'name':'Пример агента','description':'PRIVATE_DESCRIPTION','binding':'[]','revision':'0'},follow_redirects=False)
        self.assertEqual(r.status_code,303)
        result,messages=self.deliver()
        self.assertEqual(result['status'],'sent');self.assertIn('добавил карточку агента: '+key,messages[0])
        self.assertNotIn('PRIVATE_DESCRIPTION',messages[0])

    def test_upload_review_return_and_publish_use_real_audit_events(self):
        from gateway_mcp.routes import platforma_skills as library, skill_lifecycle as lifecycle
        self.enable(['skill_changes']);routes=[]
        class Mcp:
            def custom_route(self,path,methods,**kwargs):
                def wrap(fn):routes.append(Route(path,fn,methods=methods));return fn
                return wrap
        library.register_platforma_skill_routes(Mcp());lifecycle.register_skill_lifecycle_routes(Mcp())
        self.enterContext(patch.dict(os.environ,{'GATEWAY_CORPORATE_SKILLS_ENABLED':'true','GATEWAY_PLATFORMA_SKILL_LIBRARY_ENABLED':'true'}))
        self.enterContext(patch.object(library,'web_actor',return_value=self.actor))
        self.enterContext(patch.object(library,'_bundled',return_value=[]))
        content=b'---\nname: fixture-skill\ndescription: Fictional example\nversion: "1.0.0"\n---\nPRIVATE_SKILL_BODY\n'
        with TestClient(Starlette(routes=routes),base_url='https://platforma.test') as client:
            page=client.get('/platforma/skills/upload');self.assertEqual(page.status_code,200)
            form={k:v for k,v in re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">',page.text)}
            form.update(title='Пример '+uuid4().hex,shared='yes')
            response=client.post('/platforma/skills/upload',data=form,files={'file':('SKILL.md',content,'text/markdown')},follow_redirects=False)
            self.assertEqual(response.status_code,303,response.text)
            sid=response.headers['location'].split('id=')[1]
            # The same idempotency key must not produce a second notification.
            self.assertEqual(client.post('/platforma/skills/upload',data=form,files={'file':('SKILL.md',content,'text/markdown')},follow_redirects=False).status_code,303)
            for revision,action in enumerate(['submit','request_changes','submit','publish'],1):
                response=client.post('/platforma/skills/manage/action',data={'csrf':form['csrf'],'skill_id':sid,'revision':str(revision),'action':action,'review_ack':'yes'},follow_redirects=False)
                self.assertEqual(response.status_code,303,response.text)
        self.assertEqual(ui.store.snapshot()['counts'],{'pending':5})
        result,first=self.deliver();self.assertEqual(result['count'],4)
        result,second=self.deliver();self.assertEqual(result['count'],1)
        messages='\n'.join(first+second)
        for text in ['загрузил навык','отправил навык на проверку','вернул навык на доработку','опубликовал навык',sid]:
            self.assertIn(text,messages)
        self.assertNotIn('PRIVATE_SKILL_BODY',messages)
        self.assertEqual(self.deliver()[1],[])

if __name__=='__main__':unittest.main()
