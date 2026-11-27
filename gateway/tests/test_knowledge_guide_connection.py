"""Exercise the client entry point and transport failures without external access."""
import io
import json
import socket
import ssl
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent/'examples'/'knowledge-guide'))
import browser_login
import connection_check as checks
import guide

ORIGIN = 'https://platforma.akmetron.ru'
METADATA_PATH = '/.well-known/oauth-authorization-server'
SUBJECT = 'bitrix24:' + 'b' * 32 + ':42'
HEALTH = {'service': 'gateway-mcp', 'ok': True, 'auth_enabled': True}
METADATA = {
    'issuer': ORIGIN,
    'authorization_endpoint': ORIGIN + '/oauth/authorize',
    'token_endpoint': ORIGIN + '/oauth/token',
    'registration_endpoint': ORIGIN + '/oauth/register',
    'scopes_supported': ['skills:read', 'knowledge:read', 'tools:call'],
    'code_challenge_methods_supported': ['S256'],
    'response_types_supported': ['code'],
    'token_endpoint_auth_methods_supported': ['none'],
}


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.requests = []
        self.replies = {
            '/healthz': httpx.Response(200, json=HEALTH),
            METADATA_PATH: httpx.Response(200, json=METADATA),
            '/join': httpx.Response(200, text='<html>Вход</html>'),
            '/mcp': httpx.Response(401),
        }
        real_client = httpx.AsyncClient
        self.client = self.enterContext(patch.object(checks.httpx, 'AsyncClient',
            side_effect=lambda **kw: real_client(transport=httpx.MockTransport(self.handle), **kw)))

    def handle(self, request):
        self.requests.append(request)
        response = self.replies[request.url.path]
        if isinstance(response, Exception):
            raise response
        return response

    async def verify_failure(self, code):
        with self.assertRaises(checks.ConnectionCheckError) as failure:
            await checks.check_connection(guide.MCP_URL, guide.SCOPES)
        self.assertEqual(failure.exception.code, code)
        self.assertNotIn('PRIVATE-CONTROL', str(failure.exception))
        return failure.exception

    async def test_ready_probes_do_not_authorize_or_follow_redirects(self):
        result = await checks.check_connection(guide.MCP_URL, guide.SCOPES)
        self.assertTrue(result['connection_ready'])
        self.assertFalse(result['account_checked'])
        self.assertEqual(result['mcp_url'], guide.MCP_URL)
        self.assertEqual([r.url.path for r in self.requests], ['/healthz', METADATA_PATH, '/join', '/mcp'])
        for request in self.requests:
            self.assertEqual(request.method, 'GET')
            self.assertNotIn('authorization', request.headers)
        self.assertFalse(self.client.call_args.kwargs['follow_redirects'])

    async def test_redirect_and_status_failures_stop_at_failed_probe(self):
        for path, status, code in [
            ('/healthz', 503, 'service_unavailable'),
            ('/healthz', 302, 'unexpected_redirect'),
            ('/healthz', 404, 'unexpected_status'),
            (METADATA_PATH, 301, 'unexpected_redirect'),
            ('/join', 503, 'service_unavailable'),
            ('/mcp', 200, 'unexpected_status'),
        ]:
            with self.subTest(path=path, status=status):
                self.requests.clear()
                with patch.dict(self.replies, {path: httpx.Response(status,
                        headers={'Location': 'https://foreign.invalid/?secret=PRIVATE-CONTROL'})}):
                    error = await self.verify_failure(code)
                    self.assertEqual(self.requests[-1].url.path, path)
                    if path == '/join':
                        self.assertIn('Битрикс24', str(error))

    async def test_wrong_gateway_and_disabled_authentication(self):
        for change, code in [
            ({'service': 'another-service'}, 'gateway_unhealthy'),
            ({'ok': False}, 'gateway_unhealthy'),
            ({'auth_enabled': False}, 'authentication_disabled'),
            ({'auth_enabled': 'true'}, 'authentication_disabled'),
        ]:
            with self.subTest(change=change):
                with patch.dict(self.replies, {'/healthz': httpx.Response(200, json=HEALTH|change)}):
                    await self.verify_failure(code)

    async def test_wrong_issuer_and_foreign_endpoints(self):
        for field in ('issuer', 'authorization_endpoint', 'token_endpoint', 'registration_endpoint'):
            with self.subTest(field=field):
                bad = METADATA | {field: 'https://foreign.invalid/?secret=PRIVATE-CONTROL'}
                with patch.dict(self.replies, {METADATA_PATH: httpx.Response(200, json=bad)}):
                    await self.verify_failure('oauth_address_mismatch')

    async def test_missing_permissions_pkce_and_malformed_lists(self):
        for field, value, code in [
            ('scopes_supported', ['skills:read'], 'scopes_unavailable'),
            ('scopes_supported', 'skills:read knowledge:read', 'scopes_unavailable'),
            ('scopes_supported', [[]], 'scopes_unavailable'),
            ('code_challenge_methods_supported', None, 'oauth_flow_unavailable'),
            ('code_challenge_methods_supported', 'S256', 'oauth_flow_unavailable'),
            ('response_types_supported', [], 'oauth_flow_unavailable'),
            ('token_endpoint_auth_methods_supported', ['client_secret_basic'], 'oauth_flow_unavailable'),
        ]:
            with self.subTest(field=field, value=value):
                with patch.dict(self.replies, {METADATA_PATH: httpx.Response(200, json=METADATA|{field:value})}):
                    await self.verify_failure(code)

    async def test_invalid_or_large_json_is_not_returned(self):
        for content in (b'PRIVATE-CONTROL', b'[]', b'null',
                        b'{"error":"PRIVATE-CONTROL","padding":"' + b'x'*65536 + b'"}'):
            with self.subTest(length=len(content)):
                with patch.dict(self.replies, {METADATA_PATH: httpx.Response(200, content=content)}):
                    await self.verify_failure('invalid_response')

    async def test_network_errors_are_classified_without_echoing_details(self):
        for cause, code in [
            (socket.gaierror(-2, 'PRIVATE-CONTROL'), 'dns_unavailable'),
            (ssl.SSLCertVerificationError('PRIVATE-CONTROL'), 'tls_error'),
            (OSError('PRIVATE-CONTROL'), 'network_unavailable'),
        ]:
            with self.subTest(code=code):
                error = httpx.ConnectError('PRIVATE-CONTROL')
                error.__cause__ = cause
                with patch.dict(self.replies, {'/healthz': error}):
                    await self.verify_failure(code)
        with patch.dict(self.replies, {'/healthz': httpx.ReadTimeout('PRIVATE-CONTROL')}):
            await self.verify_failure('timeout')

    async def test_invalid_address_is_rejected_before_connecting(self):
        for url in ('http://platforma.akmetron.ru/mcp', ORIGIN+'/mcp?code=PRIVATE-CONTROL',
                    ORIGIN+'/mcp#PRIVATE-CONTROL', 'https://user:PRIVATE-CONTROL@platforma.akmetron.ru/mcp'):
            with self.subTest(url=url):
                with self.assertRaises(checks.ConnectionCheckError) as failed:
                    await checks.check_connection(url, guide.SCOPES)
                self.assertEqual(failed.exception.code, 'invalid_address')
        self.assertEqual(self.requests, [])

    async def test_failed_preflight_does_not_start_oauth(self):
        error = checks.ConnectionCheckError('dns_unavailable', 'Адрес не найден.')
        with patch.object(guide, 'check_connection', AsyncMock(side_effect=error)), \
                patch.object(browser_login, 'authorized_client') as login:
            with self.assertRaises(checks.ConnectionCheckError):
                await guide.run(SUBJECT, None)
            login.assert_not_called()


class CommandTests(unittest.TestCase):
    def test_check_without_account_outputs_transport_only_status(self):
        result = {'connection_ready': True, 'account_checked': False}
        text = io.StringIO()
        with patch.object(guide, 'check_connection', AsyncMock(return_value=result)) as check, \
                patch.object(guide, 'run', AsyncMock()) as run, redirect_stdout(text):
            guide.main(['--check'])
        self.assertEqual(json.loads(text.getvalue()), result)
        check.assert_awaited_once_with(guide.MCP_URL, guide.SCOPES)
        run.assert_not_awaited()

    def test_existing_subject_and_search_command_still_runs(self):
        with patch.object(guide, 'run', AsyncMock(return_value={'answer':'fixture'})) as run, \
                redirect_stdout(io.StringIO()):
            guide.main(['--subject', SUBJECT, '--find', 'паспорт'])
        run.assert_awaited_once_with(SUBJECT, 'паспорт')

    def test_wrong_cli_combinations_are_rejected_before_requests(self):
        for args in (['--check','--find','паспорт'], ['--check','--subject',SUBJECT], ['--subject','']):
            with self.subTest(args=args):
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stopped:
                    guide.main(args)
                self.assertEqual(stopped.exception.code, 2)

    def test_error_exit_contains_only_diagnostic_message(self):
        diagnostic = checks.ConnectionCheckError('dns_unavailable', 'Адрес не найден.')
        diagnostic.__cause__ = ValueError('PRIVATE-CONTROL')
        text = io.StringIO()
        with patch.object(guide, 'check_connection', AsyncMock(side_effect=diagnostic)), \
                redirect_stderr(text), self.assertRaises(SystemExit) as stopped:
            guide.main(['--check'])
        self.assertEqual(stopped.exception.code, 1)
        self.assertEqual(text.getvalue(), 'Адрес не найден.\n')
