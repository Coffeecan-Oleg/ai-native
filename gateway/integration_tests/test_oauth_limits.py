"""Isolated integration suite: real JWT/MCP dependencies, never dependency stubs."""
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from gateway_mcp.services import auth, policy
from gateway_mcp.services.oauth_limits import limit_oauth_actor

SCOPES = ('access:request', 'platforma:read', 'skills:read', 'telemetry:write', 'tools:call', 'tools:read')
TOOLS = ('gateway_access_profile', 'gateway_search_tools', 'gateway_call_tool',
         'gateway_telemetry_skill_started', 'gateway_telemetry_skill_completed',
         'gateway_telemetry_skill_failed', 'platforma.context.get', 'platforma.services.list',
         'platforma.services.get', 'platforma.requests.list', 'platforma.requests.get')
ROOT = Path(__file__).resolve().parents[1]


class FakeMcp:
    def __init__(self):
        self.tools = {}

    def tool(self, **kwargs):
        def register(function):
            self.tools[function.__name__] = function
            return function
        return register


class OAuthLimitTests(unittest.TestCase):
    def setUp(self):
        environment = {
            'GATEWAY_AUTH_ENABLED': 'true',
            'GATEWAY_JWT_SECRET': 'isolated-test-signing-key-' * 3,
            'GATEWAY_ISSUER_URL': 'https://gateway.example.invalid',
            'GATEWAY_RESOURCE_URL': 'https://gateway.example.invalid/mcp',
            'GATEWAY_MCP_OAUTH_SCOPES': ' '.join(SCOPES),
            'GATEWAY_MCP_OAUTH_TOOLS': ' '.join(TOOLS),
            'GATEWAY_RESOURCE_POLICY_MODE': 'strict',
            'GATEWAY_TOOLS_FILE': str(ROOT / 'gateway-platforma-tools.json'),
        }
        self.enterContext(patch.dict(os.environ, environment, clear=True))
        self.enterContext(patch.object(policy, '_read_policy', return_value={
            'default_groups': [], 'allowed_email_domains': ['owner-only.invalid'],
            'users': {'owner@example.invalid': {'groups': ['admins']}},
            'groups': {'admins': {'scopes': ['*']}},
        }))
        self.enterContext(patch.object(policy, 'list_active_scope_grants', return_value=[]))
        for name, value in [('save_gateway_oauth_refresh_token', False),
                            ('consume_gateway_oauth_refresh_token', None),
                            ('save_postgres_oauth_state', False), ('pop_postgres_oauth_state', None),
                            ('gateway_oauth_access_token_revoked', False)]:
            self.enterContext(patch.object(auth, name, return_value=value))
        self.enterContext(patch('gateway_mcp.tools.runtime.finish_tool'))
        self.enterContext(patch('gateway_mcp.tools.runtime.deny'))
        self.owner = policy.actor_from_user_info({'id': 'test-owner', 'email': 'owner@example.invalid'})
        self.assertEqual(self.owner.scopes, ('*',))
        auth._OAUTH_STATES.clear()
        auth._OAUTH_REFRESH_TOKENS.clear()

    def issue(self, requested=None):
        return auth._issue_oauth_tokens(actor=self.owner, client_id='isolated-test-client',
                                        resource=auth.resource_url(), requested_scopes=requested)

    def actor(self, response):
        return policy.actor_from_claims(auth.verify_gateway_token_claims(response['access_token']))

    def test_authorization_code_and_pkce_issue_finite_signed_rights(self):
        verifier = 'only-an-isolated-pkce-test-verifier'
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        code = auth.create_mcp_authorization_code(self.owner, {
            'client_id': 'isolated-test-client', 'redirect_uri': 'http://127.0.0.1/callback',
            'code_challenge': challenge, 'scope': ' '.join(SCOPES), 'resource': auth.resource_url(),
        })
        response = auth.exchange_mcp_authorization_code(code=code, client_id='isolated-test-client',
            redirect_uri='http://127.0.0.1/callback', code_verifier=verifier, resource=auth.resource_url())
        actor = self.actor(response)
        self.assertEqual(set(actor.scopes), set(SCOPES))
        self.assertEqual(actor.groups, ())
        self.assertEqual(actor.subject, self.owner.subject)
        self.assertEqual(set(actor.tool_allowlist), set(TOOLS))
        self.assertFalse(policy.has_scope(actor, 'access:admin'))

    def test_omitted_request_scope_still_uses_server_ceiling(self):
        self.assertEqual(set(self.actor(self.issue()).scopes), set(SCOPES))

    def test_request_cannot_expand_server_ceiling(self):
        actor = self.actor(self.issue(['platforma:read', 'access:admin', 'mail:send']))
        self.assertEqual(actor.scopes, ('platforma:read',))
        with self.assertRaises(PermissionError):
            self.issue(['access:admin'])

    def test_group_and_database_grants_cannot_reexpand_signed_ceiling(self):
        response = self.issue()
        with patch.object(policy, '_apply_database_scope_grants', side_effect=lambda scopes, **kwargs: scopes.update({'*', 'access:admin'})):
            actor = self.actor(response)
        self.assertEqual(set(actor.scopes), set(SCOPES))
        self.assertEqual(actor.groups, ())

    def test_transport_metadata_scopes_never_become_tool_authority(self):
        response = self.issue()
        transport = asyncio.run(auth.GatewayJwtVerifier().verify_token(response['access_token']))
        self.assertIn('access:admin', transport.scopes)
        with patch.object(auth, 'get_access_token', return_value=transport):
            self.assertEqual(set(auth.current_actor().scopes), set(SCOPES))
            auth.require_scope('platforma:read', tool='platforma.services.get')
            with self.assertRaises(PermissionError):
                auth.require_scope('access:admin', tool='gateway_admin_grant_scope')

    def test_allowlist_rejects_even_a_tool_with_an_otherwise_granted_scope(self):
        with patch.object(auth, 'current_actor', return_value=self.actor(self.issue())):
            auth.require_scope('tools:call', tool='gateway_call_tool')
            with self.assertRaises(PermissionError):
                auth.require_scope('access:request', tool='gateway_access_request_create')

    def test_refresh_rotates_without_inheriting_owner_or_new_database_rights(self):
        issued = self.issue()
        with patch.object(auth, 'actor_from_user_info', return_value=self.owner):
            refreshed = auth.exchange_mcp_refresh_token(refresh_token=issued['refresh_token'],
                client_id='isolated-test-client', resource=auth.resource_url())
        self.assertEqual(self.actor(refreshed), self.actor(issued))
        with self.assertRaises(ValueError):
            auth.exchange_mcp_refresh_token(refresh_token=issued['refresh_token'],
                client_id='isolated-test-client', resource=auth.resource_url())

    def test_refresh_can_only_shrink_and_preserves_limits_if_profile_is_disabled(self):
        issued = self.issue(['platforma:read', 'tools:call'])
        with patch.dict(os.environ, {'GATEWAY_MCP_OAUTH_SCOPES': '', 'GATEWAY_MCP_OAUTH_TOOLS': ''}), \
             patch.object(auth, 'actor_from_user_info', return_value=self.owner):
            refreshed = auth.exchange_mcp_refresh_token(refresh_token=issued['refresh_token'],
                client_id='isolated-test-client', resource=auth.resource_url())
        self.assertEqual(self.actor(refreshed).scopes, ('platforma:read', 'tools:call'))
        with patch.dict(os.environ, {'GATEWAY_MCP_OAUTH_SCOPES': 'platforma:read',
                                   'GATEWAY_MCP_OAUTH_TOOLS': 'platforma.services.get'}), \
             patch.object(auth, 'actor_from_user_info', return_value=self.owner):
            narrowed = auth.exchange_mcp_refresh_token(refresh_token=refreshed['refresh_token'],
                client_id='isolated-test-client', resource=auth.resource_url())
        self.assertEqual(self.actor(narrowed).scopes, ('platforma:read',))
        self.assertEqual(self.actor(narrowed).tool_allowlist, ('platforma.services.get',))

    def test_refresh_respects_current_user_permissions_and_allowlist(self):
        issued = self.issue()
        reduced = policy.GatewayActor(subject=self.owner.subject, email=self.owner.email,
                                      scopes=('platforma:read',))
        with patch.object(auth, 'actor_from_user_info', return_value=reduced):
            refreshed = auth.exchange_mcp_refresh_token(refresh_token=issued['refresh_token'],
                client_id='isolated-test-client', resource=auth.resource_url())
        self.assertEqual(self.actor(refreshed).scopes, ('platforma:read',))
        with patch.object(auth, 'is_user_allowed', return_value=False), self.assertRaises(PermissionError):
            auth.exchange_mcp_refresh_token(refresh_token=refreshed['refresh_token'],
                client_id='isolated-test-client', resource=auth.resource_url())

    def test_invalid_profile_or_signed_limit_fails_closed(self):
        for values in ({'GATEWAY_MCP_OAUTH_SCOPES': '*'}, {'GATEWAY_MCP_OAUTH_TOOLS': ''}):
            with patch.dict(os.environ, values), self.assertRaises(ValueError):
                self.issue()
        with self.assertRaises(ValueError):
            policy.actor_from_claims({'sub': 'test', 'scope': '*', 'scope_ceiling': '*'} )

    def test_signed_token_tampering_is_rejected(self):
        response = self.issue()
        token = response['access_token']
        claims = auth.decode_gateway_token(token, verify=False)
        claims['scope'] = '*'
        segments = token.split('.')
        segments[1] = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
        self.assertIsNone(asyncio.run(auth.GatewayJwtVerifier().verify_token('.'.join(segments))))

    def test_web_owner_token_remains_separate_and_administrative(self):
        token = auth.mint_gateway_token(self.owner)
        actor = policy.actor_from_claims(auth.verify_gateway_token_claims(token))
        self.assertTrue(policy.has_scope(actor, 'access:admin'))
        self.assertIsNone(actor.scope_ceiling)
        self.assertNotIn('scope_ceiling', auth.actor_payload(self.owner))

    def test_actual_router_reads_with_explicit_resource_grant_and_rejects_write(self):
        from gateway_mcp.services import access_policy
        from gateway_mcp.tools import router
        mcp = FakeMcp()
        router.register_router_tools(mcp)
        call = mcp.tools['gateway_call_tool']
        actor = self.actor(self.issue())
        grant = {'id': 1, 'actions': ['read'], 'effect': 'allow', 'resource_pattern': '*'}
        with patch.object(auth, 'current_actor', return_value=actor), \
             patch.object(router, 'current_actor', return_value=actor), \
             patch.object(router, 'audit_event'), \
             patch.object(access_policy, 'list_active_resource_grants', return_value=[grant]):
            service = json.loads(asyncio.run(call('platforma.services.list', '{"limit":1}')))['data']['records'][0]
            result = json.loads(asyncio.run(call('platforma.requests.list', json.dumps({'service_id': service['id'], 'period': 'all', 'limit': 1}))))
            self.assertEqual(result['data']['records'][0]['service_id'], service['id'])
            self.assertTrue(result['gateway_request_id'])
            self.assertIn('#requests/', result['data']['records'][0]['source'])
            with self.assertRaisesRegex(ValueError, 'unknown gateway route'):
                asyncio.run(call('platforma.requests.delete', '{}'))
        with patch.object(auth, 'current_actor', return_value=actor), \
             patch.object(router, 'current_actor', return_value=actor), \
             patch.object(access_policy, 'list_active_resource_grants', return_value=[]), \
             self.assertRaises(PermissionError):
            asyncio.run(call('platforma.services.list', '{"limit":1}'))

    def test_actual_admin_tool_stops_before_reading_or_changing_grants(self):
        from gateway_mcp.tools import access
        mcp = FakeMcp()
        access.register_access_tools(mcp)
        with patch.object(auth, 'current_actor', return_value=self.actor(self.issue())), \
             patch.object(access, 'admin_list_access') as backend, self.assertRaises(PermissionError):
            asyncio.run(mcp.tools['gateway_admin_list_access']())
        backend.assert_not_called()

    def test_limited_bearer_cannot_open_admin_http_routes(self):
        from starlette.requests import Request
        from starlette.responses import PlainTextResponse
        from gateway_mcp.routes.admin_access import _guard
        calls = []

        @_guard
        async def protected(request, actor):
            calls.append(actor.subject)
            return PlainTextResponse('unexpected')

        request = Request({'type': 'http', 'method': 'GET', 'path': '/admin',
                           'headers': [(b'authorization', ('Bearer ' + self.issue()['access_token']).encode())]})
        response = asyncio.run(protected(request))
        self.assertEqual(response.status_code, 303)
        self.assertEqual(calls, [])

    def test_mcp_bearer_cannot_be_reused_as_credentials_browser_session(self):
        from starlette.requests import Request
        from gateway_mcp.web import web_actor
        token = self.issue()['access_token']
        for headers in ([(b'authorization', ('Bearer ' + token).encode())],
                        [(b'cookie', ('gateway_token=' + token).encode())]):
            request = Request({'type': 'http', 'method': 'GET', 'path': '/credentials', 'headers': headers})
            self.assertIsNone(web_actor(request))

    def test_added_write_route_still_stops_before_backend(self):
        from gateway_mcp.tools import router
        mcp = FakeMcp()
        router.register_router_tools(mcp)
        write_route = {'name': 'platforma.requests.update', 'scope': 'platforma:write',
                       'http_method': 'PATCH', 'transport': 'platforma-demo'}
        with patch.object(auth, 'current_actor', return_value=self.actor(self.issue())), \
             patch.object(router, 'read_json', return_value={'tools': [write_route]}), \
             patch.object(router, 'call_backend') as backend, self.assertRaises(PermissionError):
            asyncio.run(mcp.tools['gateway_call_tool']('platforma.requests.update', '{}'))
        backend.assert_not_called()


if __name__ == '__main__':
    unittest.main()
