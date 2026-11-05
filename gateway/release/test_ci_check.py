"""Validate failure boundaries with fake Docker responses, never start containers."""
import copy
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('ci_check', Path(__file__).with_name('ci-check.py'))
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)

BASELINE = {'test_postgres_image': 'postgres@sha256:' + 'a'*64, 'sdk_sha256': 'b'*64,
            'migration_count': 25, 'release_id': 'synthetic-release'}
HTTP = {path: {'status': status} for path, status in {
    '/healthz': 200, '/metrics': 404, '/mcp': 401, '/auth/bitrix24/frame-session': 401,
    '/.well-known/oauth-authorization-server': 200, '/auth/bitrix24/frame-sdk.js': 200}.items()}
HTTP['/healthz']['body'] = {'auth_enabled': True, 'postgres': {'enabled': True, 'ok': True}}
HTTP['/auth/bitrix24/frame-sdk.js']['sha256'] = BASELINE['sdk_sha256']
MIGRATIONS = {'database_enabled': True, 'pending': [], 'applied': [str(x) for x in range(25)]}


class CopyChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.reports = Path(self.temp.name)
        self.copy = ci.CopyRun('synthetic-image', BASELINE, self.reports)
        self.calls = []

    def fake_docker(self, args, timeout=90, required=True):
        self.calls.append(args)
        value = ''
        if args[1:3] == ['exec', self.copy.prefix+'-app']:
            value = json.dumps(MIGRATIONS if 'migrate' in args else HTTP)
        elif args[1]=='run' and args[-1].endswith('check-lifecycle-upgrade.py'):
            value=json.dumps({'successful':True})
        elif args[1] == 'inspect':
            value = json.dumps([{'Config': {'Labels': {'akmetron.ci.check': self.copy.nonce}},
                                 'State': {'Running': True}}])
        elif args[1:3] == ['network', 'inspect']:
            value = json.dumps([{'Labels': {'akmetron.ci.check': self.copy.nonce}}])
        return subprocess.CompletedProcess(args, 0, value, '')

    def test_production_host_refused_before_any_mutation(self):
        with patch.object(ci.sys, 'platform', 'linux'), patch.object(ci.platform, 'machine', return_value='x86_64'), \
             patch.object(ci, 'command', return_value=subprocess.CompletedProcess([], 0, 'platforma-sandbox-gateway-1\n', '')) as run:
            with self.assertRaisesRegex(ci.CheckFailure, 'refuse_production'):
                ci.preflight(self.reports)
        self.assertEqual(len(run.call_args_list), 1)
        self.assertEqual(run.call_args.args[0][1], 'ps')

    def test_low_headroom_refused_without_container_operations(self):
        def read(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, '' if args[1] == 'ps' else json.dumps(self.temp.name), '')
        with patch.object(ci.sys, 'platform', 'linux'), patch.object(ci.platform, 'machine', return_value='x86_64'), \
             patch.object(ci, 'available_memory', return_value=600*1024**2), patch.object(ci, 'command', side_effect=read), \
             patch.object(ci.shutil, 'disk_usage', return_value=type('Disk', (), {'free': 8*1024**3})()):
            with self.assertRaisesRegex(ci.CheckFailure, 'insufficient_memory'):
                ci.preflight(self.reports)

    def test_windows_preflight_fails_before_docker(self):
        with patch.object(ci.sys, 'platform', 'win32'), patch.object(ci, 'command') as run:
            with self.assertRaisesRegex(ci.CheckFailure, 'linux_amd64'):
                ci.preflight(self.reports)
        run.assert_not_called()

    def test_fresh_database_plan_internal_and_cleanup_owned_only(self):
        with patch.object(ci, 'command', side_effect=self.fake_docker):
            result = self.copy.startup()
        self.assertTrue(result['temporary_resources_removed'])
        network = next(c for c in self.calls if c[1:3] == ['network', 'create'])
        self.assertIn('--internal', network)
        starts = [c for c in self.calls if c[1] == 'run']
        self.assertEqual(len(starts), 3)
        app = next(c for c in starts if c[c.index('--name') + 1].endswith('-app'))
        env = dict(value.split('=', 1) for i, value in enumerate(app) if i and app[i - 1] == '-e')
        self.assertEqual(env.get('GATEWAY_BITRIX_EMBED_ENABLED'), 'true')
        for args in starts:
            self.assertNotIn('-p', args)
            self.assertNotIn('--publish', args)
            if args[-1].endswith('check-lifecycle-upgrade.py'):
                self.assertTrue(args[args.index('-v')+1].endswith(':/checks:ro'))
                self.assertIn('-e',args)
            else:self.assertNotIn('-v', args)
            self.assertNotIn('--volume', args)
            self.assertEqual(args[args.index('--network')+1], self.copy.network)
        deletes = [c for c in self.calls if c[1] == 'rm']
        self.assertEqual(len(deletes), 3)
        self.assertTrue(all(c[-1].startswith(self.copy.prefix) for c in deletes))
        text = (self.reports/'fresh-database-startup.json').read_text()
        for secret in ['synthetic-copy-password', 'synthetic-application-secret', 'GATEWAY_JWT_SECRET']:
            self.assertNotIn(secret, text)

    def test_rejected_metrics_contract_still_cleans_up(self):
        original = copy.deepcopy(HTTP)
        try:
            HTTP['/metrics']['status'] = 200
            with patch.object(ci, 'command', side_effect=self.fake_docker):
                with self.assertRaisesRegex(ci.CheckFailure, 'http_contract'):
                    self.copy.startup()
        finally:
            HTTP.clear(); HTTP.update(original)
        self.assertEqual(len([c for c in self.calls if c[1] == 'rm']), 3)
        self.assertFalse((self.reports/'fresh-database-startup.json').exists())
        probe = json.loads((self.reports/'startup-probe.json').read_text())
        self.assertEqual(probe['http']['/metrics']['status'], 200)
        self.assertEqual(probe['migrations'], {'database_enabled': True, 'applied_count': 25, 'pending_count': 0})
        self.assertNotIn('body', probe['http']['/healthz'])
        for secret in ['synthetic-copy-password', 'synthetic-application-secret', 'GATEWAY_JWT_SECRET']:
            self.assertNotIn(secret, json.dumps(probe))

    def test_foreign_label_not_deleted(self):
        self.copy.containers = [self.copy.prefix+'-db']
        wrong = subprocess.CompletedProcess([], 0, json.dumps([{'Config': {'Labels': {'akmetron.ci.check': 'foreign'}}}]), '')
        with patch.object(ci, 'command', return_value=wrong) as run:
            with self.assertRaisesRegex(ci.CheckFailure, 'foreign_resource'):
                self.copy.cleanup()
        self.assertEqual(len(run.call_args_list), 1)
        self.assertEqual(run.call_args.args[0][1], 'inspect')

    def test_pending_or_incomplete_schema_rejected(self):
        for value in [MIGRATIONS | {'pending': ['missing']}, MIGRATIONS | {'applied': []}, MIGRATIONS | {'database_enabled': False}]:
            with self.assertRaisesRegex(ci.CheckFailure, 'migrations'):
                ci.validate_startup(HTTP, value, BASELINE)

    def test_health_without_auth_or_database_rejected(self):
        for value in [{'auth_enabled': False, 'postgres': {'ok': True}}, {'auth_enabled': True, 'postgres': {'ok': False}}]:
            http = copy.deepcopy(HTTP); http['/healthz']['body'] = value
            with self.assertRaisesRegex(ci.CheckFailure, 'health_or_auth'):
                ci.validate_startup(http, MIGRATIONS, BASELINE)


if __name__ == '__main__':
    unittest.main()
