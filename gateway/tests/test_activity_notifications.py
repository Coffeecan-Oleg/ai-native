import asyncio
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock
from support import install_dependency_stubs
install_dependency_stubs()
from gateway_mcp.services import activity_events as events, activity_delivery as delivery
from gateway_mcp.routes import activity_notifications as ui
from gateway_mcp.services.policy import GatewayActor

SUBJECT='bitrix24:'+'a'*32+':3318'


def event(name='corporate_login', **fields):
    return dict({'event':name,'actor':{'subject':SUBJECT},'status':'ok','ts_ms':1794314596000,'arguments':{}},**fields)


class ProjectionTests(unittest.TestCase):
    def test_categories_for_requested_actions(self):
        cases=[(event(),'logins'),(event('skill_library_uploaded',arguments={'skill_id':'shared-abc'}),'skill_changes'),
               (event('skill_lifecycle_changed',arguments={'skill_id':'shared-abc','action':'publish'}),'skill_changes'),
               (event('assistant_skill_event',arguments={'skill_id':'shared-abc','event_type':'completed'}),'skill_runs'),
               (event('assistant_agent_profile_updated',arguments={'agent_key':'test-agent','operation':'create'}),'agents'),
               (event('activity_notifications_changed'),'access'),(event('managed_integration_change'),'integrations'),
               (event('tool_call',tool='gateway_access_profile'),'mcp_calls'),
               (event('tool_call',tool='gateway_corporate_skill_read',arguments={'skill':'shared-abc'}),'skill_reads'),
               (event('corporate_login',status='denied'),'errors')]
        for payload,category in cases:
            with self.subTest(category=category):self.assertEqual(events.project(payload)['category'],category)

    def test_read_check_is_not_application(self):
        value=events.project(event('tool_call',tool='gateway_corporate_skill_read',arguments={'skill':'shared-abc','check_only':True}))
        self.assertNotEqual(value['category'],'skill_runs');self.assertNotEqual(value['category'],'skill_reads')

    def test_replay_and_unknown_events_are_not_announced(self):
        self.assertIsNone(events.project(event('skill_library_uploaded',arguments={'replayed':True})))
        self.assertIsNone(events.project(event('random_secret_operation')))

    def test_only_allowlisted_metadata(self):
        value=events.project(event('skill_library_uploaded',arguments={'skill_id':'shared-abc','file':'SECRET','token':'SECRET','title':'SECRET'},error='SECRET'))
        self.assertNotIn('SECRET',json.dumps(value));self.assertEqual(value['object_id'],'shared-abc')

    def test_private_memory_and_usage_errors_not_shared(self):
        for tool in ['gateway_memory_search','gateway_telemetry_usage_report','gateway_llm_proxy']:
            self.assertIsNone(events.project(event('tool_call',status='error',tool=tool)))

    def test_message_is_bounded_and_cannot_inject_chat_markup(self):
        payload=events.project(event('assistant_skill_event',arguments={'skill_id':'s'*96,'event_type':'completed'}))
        text=events.format_batch([{'details':payload}]*4,{SUBJECT:'[USER=all] Имя\nhttps://evil.invalid'},'act-123')
        self.assertLessEqual(len(text),1800);self.assertNotIn('[USER=',text);self.assertNotIn('https://evil.invalid',text)
        self.assertIn('по событию ИИ-приложения',text)

    def test_actor_not_taken_from_untrusted_arguments(self):
        value=events.project(event(arguments={'actor_subject':'someone','display_name':'Boss'}))
        self.assertEqual(value['actor_subject'],SUBJECT);self.assertNotIn('Boss',json.dumps(value))


class TransportTests(unittest.TestCase):
    def test_credential_validation_and_no_secret_in_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'secret';path.write_text('https://akmetron.bitrix24.ru/rest/1/abcdefghijkl/');path.chmod(0o600)
            with patch.dict(os.environ,{'GATEWAY_ACTIVITY_WEBHOOK_FILE':str(path)}):
                self.assertTrue(delivery.credential_url().endswith('im.message.add.json'))
                for bad in ['https://evil.invalid/rest/1/abcdefghijkl/','http://akmetron.bitrix24.ru/rest/1/abcdefghijkl/']:
                    path.write_text(bad)
                    self.assertEqual(delivery.send('test'),{'outcome':'rejected'})

    def test_timeout_is_unknown_not_automatic_retry(self):
        opener=Mock();opener.open.side_effect=TimeoutError('credential URL SECRET')
        with patch.object(delivery,'credential_url',return_value='https://example.invalid/SECRET'),patch.object(delivery.urllib.request,'build_opener',return_value=opener):
            self.assertEqual(delivery.send('test'),{'outcome':'unknown'})
        self.assertEqual(opener.open.call_count,1)

    def test_fixed_destination_and_positive_receipt(self):
        class Response(io.BytesIO):status=200
        opener=Mock();opener.open.return_value=Response(b'{"result":123}')
        with patch.object(delivery,'credential_url',return_value='https://example.invalid/test'),patch.object(delivery.urllib.request,'build_opener',return_value=opener):
            self.assertEqual(delivery.send('test'),{'outcome':'sent','message_id':123})
        request=opener.open.call_args.args[0]
        self.assertEqual(json.loads(request.data)['DIALOG_ID'],'chat68282')

    def test_lifespan_starts_only_after_success_and_stops(self):
        calls=[]; stop=Mock()
        async def app(scope,receive,send):
            await send({'type':'lifespan.startup.complete'})
            await send({'type':'lifespan.shutdown.complete'})
        async def send(value):calls.append(value)
        with patch.object(delivery,'configured',return_value=True),patch.object(delivery,'start_worker',return_value=(stop,None)) as start:
            asyncio.run(delivery.ActivityLifespan(app)({'type':'lifespan'},None,send))
        start.assert_called_once();self.assertTrue(stop.set.called)


class RouteTests(unittest.TestCase):
    def routes(self):
        found={}
        class Mcp:
            def custom_route(self,path,methods,**kw):
                def decorate(fn):found[path]=fn;return fn
                return decorate
        ui.register_activity_notification_routes(Mcp());return found

    def test_employee_denied_before_data_access(self):
        actor=GatewayActor(subject=SUBJECT,scopes=('skills:read',))
        with patch.object(ui.corporate_access,'enabled',return_value=True),patch.object(ui,'web_actor',return_value=actor),patch.object(ui.corporate_access,'require_owner',side_effect=PermissionError()),patch.object(ui.store,'snapshot') as read:
            response=asyncio.run(self.routes()[ui.PATH](Mock()))
        self.assertEqual(response.status_code,403);read.assert_not_called()

    def test_page_escapes_names_and_explains_pause(self):
        actor=GatewayActor(subject=SUBJECT,scopes=('access:admin',))
        settings={'enabled':False,'categories':list(events.CATEGORIES),'revision':1,'interval_seconds':60}
        data={'settings':settings,'counts':{},'rows':[]}
        with patch.object(ui,'_cookie',side_effect=lambda r,t:r):
            response=ui.render(actor,data,'<token>',transport=False)
        text=response.body.decode() if isinstance(response.body,bytes) else response.body
        for expected in ['Выключены','Сохранить настройки','Применение навыков','Чтение навыков','Уже начатая отправка может завершиться']:
            self.assertIn(expected,text)
        self.assertNotIn('<token>',text)


if __name__=='__main__':unittest.main()
