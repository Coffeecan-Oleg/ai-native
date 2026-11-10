"""Real HTTP forms and disposable PostgreSQL, with identity and sender fixtures."""
import os
import re
import unittest
from dataclasses import replace
from unittest.mock import patch
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient
from gateway_mcp.routes import activity_notifications as ui
from gateway_mcp.services import corporate_access
from gateway_mcp.services.policy import GatewayActor
from gateway_mcp.services.storage_core import _connect

URL=os.getenv('ONBOARDING_TEST_DATABASE_URL','')
SUBJECT='bitrix24:'+'a'*32+':3318'

@unittest.skipUnless(URL,'Disposable PostgreSQL required')
class NotificationHttpTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ,{'GATEWAY_DATABASE_URL':URL,
            'GATEWAY_PUBLIC_URL':'https://platforma.test','GATEWAY_JWT_SECRET':'synthetic-notification-csrf',
            'GATEWAY_BITRIX_OWNER_USER_ID':'3318','GATEWAY_BITRIX_MEMBER_ID':'a'*32}))
        with _connect() as conn:
            self.assertEqual(conn.info.dbname,'platforma_onboarding_test')
            conn.execute('TRUNCATE platforma_activity_outbox RESTART IDENTITY')
            conn.execute("UPDATE platforma_activity_settings SET enabled=false,categories='[]',revision=1,interval_seconds=60")
        self.actor=GatewayActor(subject=SUBJECT,scopes=('access:admin',))
        self.enterContext(patch.object(ui,'web_actor',side_effect=lambda _:self.actor))
        self.enterContext(patch.object(corporate_access,'enabled',return_value=True))
        self.enterContext(patch.object(corporate_access,'fresh_actor',side_effect=lambda _:self.actor))
        self.enterContext(patch.object(ui,'admin_audit_user_names',return_value={}))
        self.enterContext(patch.object(ui.activity_delivery,'configured',return_value=True))
        self.enterContext(patch.object(ui.activity_delivery,'credential_url',return_value='synthetic-no-network'))
        routes=[]
        class Mcp:
            def custom_route(self,path,methods,**kwargs):
                def wrap(fn):routes.append(Route(path,fn,methods=methods));return fn
                return wrap
        ui.register_activity_notification_routes(Mcp())
        self.client=self.enterContext(TestClient(Starlette(routes=routes),base_url='https://platforma.test'))

    def form(self):
        page=self.client.get(ui.PATH)
        self.assertEqual(page.status_code,200)
        csrf=re.search(r'name="csrf" value="([^"]+)"',page.text).group(1)
        return {'csrf':csrf,'revision':'1','enabled':'yes','category':'logins','interval':'60'}

    def test_get_save_disable_and_stale_form(self):
        form=self.form()
        response=self.client.post(ui.PATH+'/settings',data=form,follow_redirects=False)
        self.assertEqual(response.status_code,303)
        self.assertTrue(ui.store.snapshot()['settings']['enabled'])
        self.assertEqual(self.client.post(ui.PATH+'/settings',data=form).status_code,409)
        form.update(revision='2');form.pop('enabled')
        self.assertEqual(self.client.post(ui.PATH+'/settings',data=form,follow_redirects=False).status_code,303)
        self.assertFalse(ui.store.snapshot()['settings']['enabled'])

    def test_csrf_origin_and_unknown_destination_rejected(self):
        form=self.form()
        self.assertEqual(self.client.post(ui.PATH+'/settings',data=form|{'csrf':'wrong'}).status_code,403)
        self.assertEqual(self.client.post(ui.PATH+'/settings',data=form,headers={'origin':'https://evil.test'}).status_code,403)
        self.assertEqual(self.client.post(ui.PATH+'/settings',data=form|{'chat':'chat1'}).status_code,409)
        self.assertFalse(ui.store.snapshot()['settings']['enabled'])

    def test_employee_and_delegated_token_cannot_read_or_save(self):
        form=self.form()
        for actor in [replace(self.actor,subject='bitrix24:'+'a'*32+':42'),
                      replace(self.actor,tool_allowlist=('gateway_access_profile',))]:
            self.actor=actor
            self.assertEqual(self.client.get(ui.PATH).status_code,403)
            self.assertEqual(self.client.post(ui.PATH+'/settings',data=form).status_code,403)

    def test_revoked_owner_cannot_read_or_save(self):
        form=self.form()
        with patch.object(corporate_access,'fresh_actor',side_effect=PermissionError('revoked')):
            self.assertEqual(self.client.get(ui.PATH).status_code,403)
            self.assertEqual(self.client.post(ui.PATH+'/settings',data=form).status_code,403)

    def test_oversize_and_duplicate_fields_rejected(self):
        form=self.form()
        self.assertEqual(self.client.post(ui.PATH+'/settings',content='x'*4097,headers={'content-type':'application/x-www-form-urlencoded'}).status_code,409)
        from urllib.parse import urlencode
        raw=urlencode(form)+'&revision=2'
        self.assertEqual(self.client.post(ui.PATH+'/settings',content=raw,headers={'content-type':'application/x-www-form-urlencoded'}).status_code,409)

    def test_channel_missing_cannot_enable_but_can_disable(self):
        form=self.form()
        with patch.object(ui.activity_delivery,'credential_url',side_effect=ValueError('SECRET')):
            response=self.client.post(ui.PATH+'/settings',data=form)
            self.assertEqual(response.status_code,409);self.assertNotIn('SECRET',response.text)
            form.pop('enabled')
            self.assertEqual(self.client.post(ui.PATH+'/settings',data=form,follow_redirects=False).status_code,303)

    def test_mcp_lifespan_delivers_a_committed_event_with_fake_transport(self):
        from threading import Event
        import time
        from gateway_mcp.mcp_runtime import BrowserMCPServer
        from gateway_mcp.services.storage_audit import append_audit_event
        delivered=Event()
        ui.store.save_settings(actor_subject=SUBJECT,expected_revision=1,enabled=True,categories=['logins'],interval_seconds=30)
        append_audit_event({'event':'corporate_login','status':'ok','actor':{'subject':SUBJECT},'ts_ms':1794314596000})
        with _connect() as conn:conn.execute('UPDATE platforma_activity_settings SET next_send_at=now()')
        def fake_send(message):
            self.assertIn('вошёл в Платформу',message)
            delivered.set();return {'outcome':'sent','message_id':987}
        with patch.object(ui.activity_delivery,'send',side_effect=fake_send) as sender:
            with TestClient(BrowserMCPServer('activity-fixture',stateless_http=True).streamable_http_app()):
                self.assertTrue(delivered.wait(6),'worker did not start through the actual MCP lifespan')
                for _ in range(40):
                    if ui.store.snapshot()['rows'][0]['state']=='sent':break
                    time.sleep(.05)
                self.assertEqual(ui.store.snapshot()['rows'][0]['message_id'],987)
            sender.assert_called_once()

if __name__=='__main__':unittest.main()
