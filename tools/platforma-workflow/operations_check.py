#!/usr/bin/env python3
"""One-shot, read-only production check. No credentials, mutations or notifications."""
import argparse
import datetime as dt
import json
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

ORIGIN = 'https://platforma.akmetron.ru'
COMMANDS = ('status', 'version', 'counts')
PATHS = ('/healthz', '/mcp', '/metrics', '/.well-known/oauth-protected-resource/mcp')
LIMIT = 65536


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def http_read(path):
    if path not in PATHS:
        raise ValueError('path_not_allowed')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request(ORIGIN + path, headers={'Accept': 'application/json'})
    try:
        response = opener.open(request, timeout=10)
    except urllib.error.HTTPError as exc:
        response = exc
    except (OSError, urllib.error.URLError):
        return {'error': 'unavailable'}
    with response:
        raw = response.read(LIMIT + 1)
        if len(raw) > LIMIT:
            return {'status': response.status, 'error': 'oversized'}
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeError):
            body = None
        # Never record cookies, response headers or arbitrary server text.
        return {'status': response.status, 'body': body}


def ssh_read(config, command):
    if command not in COMMANDS:
        raise ValueError('command_not_allowed')
    try:
        result = subprocess.run(['ssh', '-T', '-o', 'BatchMode=yes', '-o',
                                 'StrictHostKeyChecking=yes', '-F', str(config),
                                 'platforma-production', command],
                                capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return {'error': 'unavailable'}
    if result.returncode or len(result.stdout) > LIMIT:
        return {'error': 'command_failed_or_oversized'}
    return {'text': result.stdout.decode('utf-8', errors='replace')}


def assess(observed, expected):
    if not re.fullmatch('[a-f0-9]{40}', expected):
        raise ValueError('full_expected_revision_required')
    checks = []

    def add(name, ok, expected_result, observed_result):
        checks.append({'name': name, 'status': 'pass' if ok else 'fail',
                       'expected': expected_result, 'observed': observed_result})

    for command in COMMANDS:
        text = observed.get('ssh', {}).get(command, {}).get('text')
        if not isinstance(text, str):
            checks.append({'name': 'ssh_' + command, 'status': 'unknown',
                           'expected': 'read-only response', 'observed': 'unavailable'})
            continue
        if command == 'status':
            lines = text.splitlines()
            health_lines = [line[8:] for line in lines if line.startswith('health: ')]
            try:
                health = json.loads(health_lines[0]) if len(health_lines) == 1 else {}
            except (ValueError, UnicodeError):
                health = {}
            if not isinstance(health, dict):
                health = {}
            for name in ('gateway', 'postgres'):
                rows = [line for line in lines if line.startswith('platforma-' + name + '-1  Up ')]
                add(name + '_healthy', len(rows) == 1 and '(healthy)' in rows[0],
                    'running and healthy', 'healthy' if len(rows) == 1 and '(healthy)' in rows[0] else 'not confirmed')
            add('ssh_auth_enabled', health.get('auth_enabled') is True, True, health.get('auth_enabled') is True)
            add('ssh_revision', health.get('release_sha') == expected, expected,
                health.get('release_sha') if re.fullmatch('[a-f0-9]{40}', str(health.get('release_sha', ''))) else 'unknown')
        elif command == 'version':
            tags = re.findall(r'platforma-gateway:([A-Za-z0-9_.-]+)', text)
            add('image_tag', 'evening-' + expected[:12] in tags, 'evening-' + expected[:12],
                sorted(set(tags))[:4])
        else:
            values = re.findall(r'^public\.schema_migrations=(\d+)$', text, re.M)
            add('schema', values == ['28'], 28, int(values[0]) if len(values) == 1 else 'unknown')

    for path in PATHS:
        reply = observed.get('http', {}).get(path, {})
        if reply.get('error') or type(reply.get('status')) is not int:
            checks.append({'name': path, 'status': 'unknown', 'expected': 'bounded HTTP response', 'observed': 'unavailable'})
            continue
        status, body = reply['status'], reply.get('body')
        if path == '/healthz':
            body = body if isinstance(body, dict) else {}
            pg = body.get('postgres') if isinstance(body.get('postgres'), dict) else {}
            add('https_health', status == 200 and body.get('ok') is True and pg.get('ok') is True,
                'HTTP 200; gateway and database ok', status)
            add('https_auth_enabled', body.get('auth_enabled') is True, True, body.get('auth_enabled') is True)
            add('https_revision', body.get('release_sha') == expected, expected,
                body.get('release_sha') if re.fullmatch('[a-f0-9]{40}', str(body.get('release_sha', ''))) else 'unknown')
        elif path == '/mcp':
            add('anonymous_mcp_denied', status == 401, 401, status)
        elif path == '/metrics':
            add('anonymous_metrics_closed', status in (401, 403, 404), '401, 403 or 404', status)
        else:
            body = body if isinstance(body, dict) else {}
            add('oauth_resource', status == 200 and body.get('resource') == ORIGIN + '/mcp'
                and body.get('authorization_servers') == [ORIGIN + '/'],
                'current resource and issuer', 'current' if body.get('resource') == ORIGIN + '/mcp' else 'mismatch')
    all_pass = bool(checks) and all(c['status'] == 'pass' for c in checks)
    return {'schema': 1, 'checked_at': dt.datetime.now(dt.timezone.utc).isoformat(),
            'expected_revision': expected, 'technical_checks_passed': all_pass, 'checks': checks,
            'writes_performed': False, 'notifications_sent': False,
            'not_verified': ['ordinary_employee_acceptance', 'off_server_backup',
                             'production_restore', 'disk_and_quota', 'notification_delivery',
                             'production_release_controller'],
            'overall_project_complete': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ssh-config', type=Path, required=True)
    parser.add_argument('--expected-revision', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch('[a-f0-9]{40}', args.expected_revision):
        parser.error('expected-revision must be the full reviewed commit SHA')
    if not args.ssh_config.is_file():
        parser.error('SSH config file does not exist')
    observed = {'ssh': {c: ssh_read(args.ssh_config, c) for c in COMMANDS},
                'http': {p: http_read(p) for p in PATHS}}
    report = assess(observed, args.expected_revision)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report['technical_checks_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
