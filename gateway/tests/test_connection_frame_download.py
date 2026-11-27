"""Real signed browser channels and HTTP routes, synthetic identity/storage only."""
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from starlette.applications import Starlette
from gateway_mcp.routes import connection_diagnostics as routes
from gateway_mcp.services import connection_diagnostics as service
from test_bitrix_auth_flow import FakeServer
import test_bitrix_embed as frame


class ConnectionFrameDownloadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        frame.FrameHttpTests.setUp(self)
        fake = FakeServer()
        routes.register_connection_diagnostics_routes(fake)
        self.app = frame.embed.BitrixFrameMiddleware(Starlette(routes=fake.routes))
        self.read = self.enterContext(patch.object(service, 'read_probe', return_value=None))

    def client(self):
        return frame.FrameHttpTests.client(self)

    async def test_frame_fetch_uses_own_signed_identity_and_no_external_redirect(self):
        async with self.client() as client:
            client.cookies.set(frame.embed.COOKIE, self.token)
            response = await client.get(routes.REPORT_PATH, params={'check': service.new_mark()},
                headers={'Sec-Fetch-Dest':'empty', 'Sec-Fetch-Mode':'same-origin'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.read.call_args.args[0].subject, frame.SUBJECT)
        self.assertNotIn('location', response.headers)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertIn('attachment;', response.headers['content-disposition'])

    async def test_standalone_session_still_downloads_own_report(self):
        async with self.client() as client:
            client.cookies.set('gateway_token', self.normal)
            response = await client.get(routes.REPORT_PATH, params={'check': service.new_mark()})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.read.call_args.args[0].subject, frame.SUBJECT)

    async def test_missing_or_wrong_channel_cookies_never_read_report(self):
        for cookie in ['', frame.embed.COOKIE+'='+self.normal, 'gateway_token='+self.token]:
            with self.subTest(cookie_kind=cookie.split('=')[0]):
                async with self.client() as client:
                    response = await client.get(routes.REPORT_PATH, params={'check': service.new_mark()},
                        headers={'Cookie':cookie, 'Sec-Fetch-Dest':'empty', 'Sec-Fetch-Mode':'same-origin'})
                self.assertIn(response.status_code, (303,401))
        self.read.assert_not_called()

    async def test_withdrawn_employee_cannot_download_report(self):
        with patch.object(frame.identity,'current_employee',side_effect=PermissionError('revoked')):
            async with self.client() as client:
                client.cookies.set(frame.embed.COOKIE,self.token)
                response = await client.get(routes.REPORT_PATH, params={'check': service.new_mark()})
        self.assertIn(response.status_code,(303,401))
        self.read.assert_not_called()

    async def test_page_loads_same_origin_script_and_has_no_download_navigation(self):
        async with self.client() as client:
            client.cookies.set(frame.embed.COOKIE,self.token)
            response = await client.get(routes.PATH)
        self.assertEqual(response.status_code,200)
        self.assertIn('src="'+routes.CLIENT_PATH+'"',response.text)
        self.assertIn('data-report-url="'+routes.REPORT_PATH,response.text)
        self.assertNotIn('href="'+routes.REPORT_PATH,response.text)
        self.assertIn('frame-ancestors '+frame.embed.PORTAL_ORIGIN,response.headers['content-security-policy'])
        self.read.assert_not_called()

    async def test_public_script_is_exact_static_asset_not_a_proxy_or_session(self):
        async with self.client() as client:
            response = await client.get(routes.CLIENT_PATH+'?url=https://foreign.invalid')
        expected=Path(routes.__file__).resolve().parents[1]/'help_assets'/'connection-download.js'
        self.assertEqual(response.content,expected.read_bytes())
        self.assertIn('application/javascript',response.headers['content-type'])
        self.assertEqual(response.headers['x-content-type-options'],'nosniff')
        self.assertEqual(response.headers['cache-control'],'no-store')
        self.assertNotIn('set-cookie',response.headers)
        self.read.assert_not_called()

    async def test_disabled_corporate_mode_hides_script(self):
        with patch.dict(os.environ,{'GATEWAY_LOGIN_PROVIDER':'yandex'}):
            async with self.client() as client:
                response=await client.get(routes.CLIENT_PATH)
        self.assertEqual(response.status_code,404)


if __name__=='__main__':unittest.main()
