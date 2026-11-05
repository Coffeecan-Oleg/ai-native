"""Checks on a separate Docker builder; never a deploy command."""
import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
PRODUCTION_NAMES = {'platforma-sandbox-gateway-1', 'platforma-sandbox-postgres-1'}


class CheckFailure(RuntimeError):
    pass


def command(args, timeout=90, required=True):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if required and result.returncode:
        # No environment values or full command lines in failure reports.
        raise CheckFailure('command_failed:' + ':'.join(args[:2]))
    return result


def write_report(root, name, value):
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')


def available_memory():
    values = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
    available = int(values['MemAvailable'].split()[0]) * 1024
    group = Path('/sys/fs/cgroup')
    maximum = group / 'memory.max'
    if maximum.is_file() and maximum.read_text().strip() != 'max':
        available = min(available, int(maximum.read_text()) - int((group / 'memory.current').read_text()))
    return available


def preflight(reports):
    if sys.platform != 'linux' or platform.machine() != 'x86_64':
        raise CheckFailure('linux_amd64_builder_required')
    names = set(command(['docker', 'ps', '-a', '--format', '{{.Names}}']).stdout.splitlines())
    if names & PRODUCTION_NAMES:
        raise CheckFailure('refuse_production_service_host')
    memory = available_memory()
    docker_root = json.loads(command(['docker', 'info', '--format', '{{json .DockerRootDir}}']).stdout)
    free_disk = shutil.disk_usage(docker_root).free
    if memory < 1536 * 1024**2:
        raise CheckFailure('insufficient_memory_headroom')
    if free_disk < 3 * 1024**3:
        raise CheckFailure('insufficient_docker_disk')
    result = {'successful': True, 'available_memory_bytes': memory, 'docker_disk_free_bytes': free_disk,
              'production_container_names_present': False, 'deployment': False}
    write_report(reports, 'preflight.json', result)
    return result


def probe_program():
    return '''import hashlib,json,urllib.request,urllib.error
results={}
for path in ['/healthz','/metrics','/mcp','/auth/bitrix24/frame-session','/.well-known/oauth-authorization-server','/auth/bitrix24/frame-sdk.js']:
 request=urllib.request.Request('http://127.0.0.1:8000'+path,headers={'Host':'copy.example.invalid'})
 try:
  with urllib.request.urlopen(request,timeout=3) as response:status=response.status;data=response.read()
 except urllib.error.HTTPError as error:status=error.code;data=error.read()
 results[path]={'status':status}
 if path=='/healthz':results[path]['body']=json.loads(data)
 if path.endswith('.js'):results[path]['sha256']=hashlib.sha256(data).hexdigest()
print(json.dumps(results))
'''


def validate_startup(http, migrations, baseline):
    health = http['/healthz']
    if health['status'] != 200 or not health['body']['auth_enabled'] or not health['body']['postgres'].get('ok'):
        raise CheckFailure('startup_health_or_auth_failed')
    expected = {'/metrics': 404, '/mcp': 401, '/auth/bitrix24/frame-session': 401,
                '/.well-known/oauth-authorization-server': 200, '/auth/bitrix24/frame-sdk.js': 200}
    if any(http[path]['status'] != status for path, status in expected.items()):
        raise CheckFailure('startup_http_contract_failed')
    if http['/auth/bitrix24/frame-sdk.js']['sha256'] != baseline['sdk_sha256']:
        raise CheckFailure('sdk_integrity_failed')
    if (not migrations.get('database_enabled') or migrations.get('pending')
            or len(migrations.get('applied', [])) != baseline['migration_count']):
        raise CheckFailure('fresh_database_migrations_failed')


class CopyRun:
    def __init__(self, image, baseline, reports):
        self.image, self.baseline, self.reports = image, baseline, reports
        self.nonce = uuid.uuid4().hex
        self.prefix = 'akme-ci-' + self.nonce[:12]
        self.label = 'akmetron.ci.check=' + self.nonce
        self.network = self.prefix + '-net'
        self.containers = []
        self.network_created = False

    def owned(self, kind, name):
        args = ['docker', 'inspect', name] if kind == 'container' else ['docker', 'network', 'inspect', name]
        result = command(args, required=False)
        if result.returncode:
            return False
        info = json.loads(result.stdout)[0]
        labels = info['Config']['Labels'] if kind == 'container' else info['Labels']
        if not name.startswith(self.prefix) or labels.get('akmetron.ci.check') != self.nonce:
            raise CheckFailure('refuse_foreign_resource_cleanup')
        return True

    def cleanup(self):
        for name in reversed(self.containers):
            if self.owned('container', name):
                command(['docker', 'rm', '-f', '-v', name])
        if self.network_created and self.owned('network', self.network):
            command(['docker', 'network', 'rm', self.network])

    def startup(self):
        pg, app = self.prefix + '-db', self.prefix + '-app'
        # Pull before creating the internal-only test network.
        command(['docker', 'pull', self.baseline['test_postgres_image']], timeout=240)
        try:
            command(['docker', 'network', 'create', '--internal', '--label', self.label, self.network])
            self.network_created = True
            self.containers.append(pg)
            command(['docker', 'run', '-d', '--name', pg, '--label', self.label,
                     '--network', self.network, '--network-alias', 'db', '--memory', '256m',
                     '--cpus', '1', '--pids-limit', '96', '--tmpfs',
                     '/var/lib/postgresql/data:rw,nosuid,nodev,size=192m',
                     '-e', 'POSTGRES_PASSWORD=synthetic-copy-password', '-e', 'POSTGRES_USER=copy',
                     '-e', 'POSTGRES_DB=copy', self.baseline['test_postgres_image']])
            for _ in range(45):
                if command(['docker', 'exec', pg, 'pg_isready', '-U', 'copy', '-d', 'copy'], required=False).returncode == 0:
                    break
                time.sleep(1)
            else:
                raise CheckFailure('copy_database_not_ready')
            command(['docker','exec',pg,'psql','-U','copy','-d','copy','-c',
                     'create database platforma_upgrade_test'])
            upgrade=self.prefix+'-upgrade';self.containers.append(upgrade)
            checked=command(['docker','run','--name',upgrade,'--label',self.label,
                '--network',self.network,'--read-only','--memory','384m','--pids-limit','96',
                '--cap-drop','ALL','--security-opt','no-new-privileges:true',
                '--tmpfs','/tmp:rw,nosuid,nodev,size=64m','-v',str(SOURCE)+':/checks:ro',
                '-e','GATEWAY_TEST_ISOLATED=true',
                '-e','GATEWAY_DATABASE_URL=postgresql://copy:synthetic-copy-password@db:5432/platforma_upgrade_test',
                self.image,'python','/checks/release/check-lifecycle-upgrade.py'],timeout=90)
            upgrade_result=json.loads(checked.stdout.strip().splitlines()[-1])
            if upgrade_result.get('successful') is not True:
                raise CheckFailure('lifecycle_upgrade_failed')
            write_report(self.reports,'lifecycle-upgrade.json',upgrade_result)
            env = {
                'GATEWAY_DATABASE_URL': 'postgresql://copy:synthetic-copy-password@db:5432/copy',
                'GATEWAY_AUTH_ENABLED': 'true', 'GATEWAY_LOGIN_PROVIDER': 'bitrix24',
                'GATEWAY_BITRIX_EMBED_ENABLED': 'true',
                'GATEWAY_BITRIX_PORTAL': 'akmetron.bitrix24.ru',
                'GATEWAY_BITRIX_CLIENT_ID': 'local.synthetic.12345678',
                'GATEWAY_BITRIX_CLIENT_SECRET': 'synthetic-application-secret',
                'GATEWAY_BITRIX_MEMBER_ID': 'a' * 32, 'GATEWAY_BITRIX_OWNER_USER_ID': '3318',
                'GATEWAY_JWT_SECRET': 'synthetic-copy-key-never-use-in-production',
                'GATEWAY_PUBLIC_URL': 'https://copy.example.invalid',
                'GATEWAY_ISSUER_URL': 'https://copy.example.invalid',
                'GATEWAY_RESOURCE_URL': 'https://copy.example.invalid/mcp',
                'GATEWAY_AUDIT_LOG': '/tmp/audit.jsonl',
                'GATEWAY_RELEASE_SHA': self.baseline['release_id'],
                'GATEWAY_TOOLS_FILE': '/app/gateway-tools.json',
            }
            args = ['docker', 'run', '-d', '--name', app, '--label', self.label,
                    '--network', self.network, '--read-only', '--memory', '384m', '--cpus', '1',
                    '--pids-limit', '96', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
                    '--tmpfs', '/tmp:rw,nosuid,nodev,size=64m']
            for name, value in env.items():
                args += ['-e', name + '=' + value]
            args += [self.image, 'sh', '-c', 'gateway-mcp migrate up && exec gateway-mcp --transport streamable-http']
            self.containers.append(app)
            command(args)
            deadline = time.monotonic() + 75
            while time.monotonic() < deadline:
                result = command(['docker', 'exec', app, 'python', '-c', probe_program()], required=False, timeout=25)
                if not result.returncode:
                    http = json.loads(result.stdout)
                    break
                info = json.loads(command(['docker', 'inspect', app]).stdout)[0]
                if not info['State']['Running']:
                    raise CheckFailure('copy_gateway_exited')
                time.sleep(1)
            else:
                raise CheckFailure('copy_gateway_startup_timeout')
            migrations = json.loads(command(['docker', 'exec', app, 'gateway-mcp', 'migrate', 'status']).stdout)
            # Preserve a bounded, credential-free explanation if acceptance fails.
            write_report(self.reports, 'startup-probe.json', {
                'http': {path: {key: value for key, value in row.items() if key in {'status', 'sha256'}}
                         for path, row in http.items()},
                'health': {'auth_enabled': http['/healthz']['body'].get('auth_enabled'),
                           'postgres_ok': http['/healthz']['body'].get('postgres', {}).get('ok')},
                'migrations': {'database_enabled': migrations.get('database_enabled'),
                               'applied_count': len(migrations.get('applied', [])),
                               'pending_count': len(migrations.get('pending', []))},
            })
            validate_startup(http, migrations, self.baseline)
            report = {'successful': True, 'http': http, 'migrations': migrations,
                      'network_internal': True, 'host_ports_published': False,
                      'production_mounts': False, 'real_business_api_calls': False}
        finally:
            self.cleanup()
        report['temporary_resources_removed'] = True
        write_report(self.reports, 'fresh-database-startup.json', report)
        return report


def image_checks(image, baseline, reports):
    hardening = ['--network', 'none', '--read-only', '--memory', '384m', '--pids-limit', '96',
                 '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true']
    result = command(['docker', 'run', '--rm', *hardening, image, 'python',
                      '/usr/local/share/gateway-release/verify-image.py'])
    verified = json.loads(result.stdout.strip().splitlines()[-1])
    write_report(reports, 'installed-image.json', verified)
    result = command(['docker', 'run', '--rm', *hardening, '--tmpfs',
                      '/tmp:rw,nosuid,nodev,size=96m', '-v', str(SOURCE) + ':/checks:ro',
                      image, 'python', '/checks/release/run-tests.py'], timeout=240)
    regression = json.loads(result.stdout.strip().splitlines()[-1])
    if not regression.get('successful') or regression['tests'] < 143 or any(x['skipped'] for x in regression['cohorts']):
        raise CheckFailure('regression_incomplete_or_skipped')
    write_report(reports, 'regression.json', regression)
    CopyRun(image, baseline, reports).startup()
    image_id = json.loads(command(['docker', 'image', 'inspect', '--format', '{{json .Id}}', image]).stdout)
    result = {'successful': True, 'image': image_id, 'source_sha': os.getenv('SOURCE_GIT_SHA', 'local-copy'),
              'deployment': False, 'business_calls': False, 'image_uploaded': False}
    write_report(reports, 'acceptance.json', result)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=['preflight', 'checks'])
    parser.add_argument('--image', default='gateway-corporate-ci')
    parser.add_argument('--reports', type=Path, required=True)
    args = parser.parse_args()
    try:
        preflight(args.reports)
        if args.phase == 'checks':
            baseline = json.loads((SOURCE / 'release/baseline.json').read_text())
            image_checks(args.image, baseline, args.reports)
    except (CheckFailure, subprocess.TimeoutExpired) as error:
        message = str(error) if isinstance(error, CheckFailure) else 'command_timeout'
        write_report(args.reports, 'failure.json', {'successful': False, 'phase': args.phase, 'error': message})
        print(message, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
