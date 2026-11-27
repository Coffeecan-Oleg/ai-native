import copy
import json
import unittest
from unittest.mock import patch
import operations_check as ops

SHA = 'f95be3a63c65417ef821f278dba7095d7098cf68'


def fixture():
    health = {'ok': True, 'auth_enabled': True, 'release_sha': SHA, 'postgres': {'ok': True}}
    return {'ssh': {
        'status': {'text': 'platforma-gateway-1  Up 1 hour (healthy)\nplatforma-postgres-1  Up 1 hour (healthy)\nhealth: ' + json.dumps(health)},
        'version': {'text': 'image: platforma-gateway:evening-' + SHA[:12]},
        'counts': {'text': 'public.schema_migrations=28\n'}},
        'http': {'/healthz': {'status': 200, 'body': health}, '/mcp': {'status': 401},
                 '/metrics': {'status': 404}, '/.well-known/oauth-protected-resource/mcp': {
                     'status': 200, 'body': {'resource': ops.ORIGIN + '/mcp', 'authorization_servers': [ops.ORIGIN + '/']}}}}


class OperationsTests(unittest.TestCase):
    def test_good_health_does_not_claim_full_acceptance(self):
        report = ops.assess(fixture(), SHA)
        self.assertTrue(report['technical_checks_passed'])
        self.assertFalse(report['overall_project_complete'])
        self.assertFalse(report['notifications_sent'])
        self.assertIn('off_server_backup', report['not_verified'])

    def test_missing_observations_are_not_success(self):
        report = ops.assess({}, SHA)
        self.assertFalse(report['technical_checks_passed'])
        self.assertEqual({'unknown'}, {c['status'] for c in report['checks']})

    def test_each_failed_channel_refuses_success(self):
        for group in ('ssh', 'http'):
            for key in fixture()[group]:
                with self.subTest(group=group, key=key):
                    data = fixture()
                    data[group][key] = {'error': 'unavailable'}
                    self.assertFalse(ops.assess(data, SHA)['technical_checks_passed'])

    def test_wrong_revision_schema_unhealthy_and_anonymous_access(self):
        alterations = [
            ('ssh', 'status', {'text': fixture()['ssh']['status']['text'].replace('(healthy)', '(unhealthy)')}),
            ('ssh', 'counts', {'text': 'public.schema_migrations=27\n'}),
            ('ssh', 'version', {'text': 'platforma-gateway:old'}),
            ('http', '/mcp', {'status': 200}),
            ('http', '/metrics', {'status': 200}),
            ('http', '/healthz', {'status': 200, 'body': {'ok': True, 'auth_enabled': False}}),
            ('http', '/healthz', {'status': 200, 'body': ['malformed']}),
            ('http', '/healthz', {'status': 200, 'body': {**fixture()['http']['/healthz']['body'], 'release_sha': 'a'*40}}),
            ('http', '/.well-known/oauth-protected-resource/mcp', {'status': 200, 'body': {'resource': 'https://old.invalid/mcp'}}),
        ]
        for group, key, value in alterations:
            with self.subTest(group=group, key=key, value=value):
                data = fixture()
                data[group][key] = value
                self.assertFalse(ops.assess(data, SHA)['technical_checks_passed'])

    def test_raw_response_and_secrets_do_not_enter_report(self):
        data = fixture()
        data['ssh']['version']['text'] += '\nsecret=synthetic-must-not-leak'
        data['http']['/healthz']['body']['token'] = 'synthetic-must-not-leak'
        self.assertNotIn('synthetic-must-not-leak', json.dumps(ops.assess(data, SHA)))

    def test_only_fixed_read_commands_and_paths(self):
        for command in ('deploy abc', 'logs', 'env-check', 'db dump', 'restart', 'status; id'):
            with self.subTest(command=command), patch('operations_check.subprocess.run') as run:
                with self.assertRaises(ValueError):
                    ops.ssh_read('unused', command)
                run.assert_not_called()
        with self.assertRaises(ValueError):
            ops.http_read('https://elsewhere.invalid')

    def test_ssh_has_no_shell_and_host_verification(self):
        with patch('operations_check.subprocess.run') as run:
            run.return_value.returncode = 0
            run.return_value.stdout = b'healthy'
            ops.ssh_read('config', 'status')
            argv = run.call_args.args[0]
            self.assertIn('StrictHostKeyChecking=yes', argv)
            self.assertEqual(argv[-2:], ['platforma-production', 'status'])
            self.assertNotIn('shell', run.call_args.kwargs)

    def test_redirect_is_not_followed(self):
        self.assertIsNone(ops.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://outside.invalid'))

    def test_exact_published_issuer_required(self):
        for issuer in (ops.ORIGIN, 'https://old.invalid/', ops.ORIGIN+'/other'):
            data = fixture()
            data['http']['/.well-known/oauth-protected-resource/mcp']['body']['authorization_servers'] = [issuer]
            self.assertFalse(ops.assess(data, SHA)['technical_checks_passed'])

    def test_revision_requires_full_sha(self):
        for invalid in ('f95be3a', ';whoami', 'g'*40):
            with self.assertRaises(ValueError):
                ops.assess(fixture(), invalid)
