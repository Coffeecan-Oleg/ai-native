"""Read and summarize an operator receipt; never execute instructions from it."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime, timezone, timedelta
import hashlib
import hmac
import json
from pathlib import Path
import re
import subprocess

MAX_BYTES = 16384
REMOTE_PATH = 'runtime/workflow-handoff.json'
SOURCE_PATHS = frozenset({
    'runtime/workflow-dispatcher-source.py',
    'runtime/workflow-config-validator-source.py',
})
PRODUCTION_FLAGS = ('release_commands_installed', 'legacy_mutators_guarded',
                    'pinned_image_validator_checked', 'caddy_probes_verified')


class ReceiptError(ValueError):
    """Only fixed, nonsensitive error codes reach the CLI."""


def sha(value):
    return isinstance(value, str) and re.fullmatch('[a-f0-9]{64}', value) is not None


def fields(value, allowed, required):
    if not isinstance(value, dict) or set(value) - set(allowed) or not set(required) <= set(value):
        raise ReceiptError('handoff_invalid_fields')


def unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ReceiptError('handoff_duplicate_json_key')
        obj[key] = value
    return obj


def parse_receipt(raw, expected_package_sha256, *, now=None):
    if not sha(expected_package_sha256):
        raise ReceiptError('handoff_expected_digest_invalid')
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_BYTES:
        raise ReceiptError('handoff_size_invalid')
    try:
        data = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=unique_object,
                          parse_constant=lambda _: (_ for _ in ()).throw(ReceiptError('handoff_invalid_json')))
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise ReceiptError('handoff_invalid_json') from None
    fields(data, {'schema','status','package_sha256','recorded_at','development','production','source_copies','notes'},
           {'schema','status','package_sha256','development','production','source_copies','notes'})
    if type(data['schema']) is not int or data['schema'] != 1:
        raise ReceiptError('handoff_schema_unsupported')
    if data['status'] not in ('prepared','partial','ready','blocked') or not sha(data['package_sha256']):
        raise ReceiptError('handoff_identity_invalid')
    dev, prod = data['development'], data['production']
    fields(dev, {'public_url','https_route_verified'}, {'public_url','https_route_verified'})
    fields(prod, PRODUCTION_FLAGS, PRODUCTION_FLAGS)
    if dev['public_url'] != 'https://platforma-dev.akmetron.ru':
        raise ReceiptError('handoff_development_url_invalid')
    if type(dev['https_route_verified']) is not bool or any(type(prod[k]) is not bool for k in PRODUCTION_FLAGS):
        raise ReceiptError('handoff_boolean_required')
    copies = data['source_copies']
    if not isinstance(copies, list) or len(copies) > len(SOURCE_PATHS):
        raise ReceiptError('handoff_source_copies_invalid')
    seen = set()
    for entry in copies:
        fields(entry, {'path','sha256','bytes'}, {'path','sha256','bytes'})
        path = entry['path']
        if not isinstance(path, str) or path not in SOURCE_PATHS or path in seen:
            raise ReceiptError('handoff_source_path_invalid')
        if not sha(entry['sha256']) or type(entry['bytes']) is not int or not 0 < entry['bytes'] <= 262144:
            raise ReceiptError('handoff_source_identity_invalid')
        seen.add(path)
    notes = data['notes']
    if not isinstance(notes, list) or len(notes) > 12 or any(not isinstance(s,str) or len(s)>240 for s in notes):
        raise ReceiptError('handoff_notes_invalid')
    # Notes are never copied to output: accidental credentials in free text stay out of logs.
    timestamp = data.get('recorded_at')
    fresh = False
    if timestamp is not None:
        if not isinstance(timestamp,str) or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ',timestamp):
            raise ReceiptError('handoff_timestamp_invalid')
        try:
            moment = datetime.strptime(timestamp,'%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
        except ValueError:
            raise ReceiptError('handoff_timestamp_invalid') from None
        now = now or datetime.now(timezone.utc)
        age = now - moment
        fresh = -timedelta(minutes=5) <= age <= timedelta(hours=24)
    matches = hmac.compare_digest(data['package_sha256'],expected_package_sha256)
    remaining = []
    if not matches: remaining.append('package_identity_mismatch')
    if not fresh: remaining.append('timestamp_missing_or_stale')
    if data['status'] != 'ready': remaining.append('operator_status_not_ready')
    if not dev['https_route_verified']: remaining.append('development_https_pending')
    remaining.extend(k for k in PRODUCTION_FLAGS if not prod[k])
    remaining.extend('missing_source:'+path for path in sorted(SOURCE_PATHS-seen))
    return {
        'ok':True,'evidence_level':'operator_report_not_independently_verified',
        'receipt_sha256':hashlib.sha256(raw).hexdigest(),'package_matches':matches,
        'fresh':fresh,'recorded_at':timestamp,'operator_status':data['status'],
        'development_https_reported':dev['https_route_verified'],
        'production_reported':{k:prod[k] for k in PRODUCTION_FLAGS},
        'source_copies':copies,'notes_count':len(notes),'remaining':remaining,
        'operator_report_complete':not remaining,'deployment_authorized':False,
        'server_changes_performed':False,
    }


def read_file(path):
    with Path(path).open('rb') as stream:
        return stream.read(MAX_BYTES+1)


def ssh_argv(config):
    return ['ssh','-T','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',
            '-o','ConnectTimeout=6','-o','ConnectionAttempts=1','-F',str(config),
            'platforma-production','files get '+REMOTE_PATH]


def read_remote(config, *, timeout=20):
    # Read at most16KiB; a stalled or oversized response cannot hold the client forever.
    try:
        with subprocess.Popen(ssh_argv(config),stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL) as proc:
            try:
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(proc.stdout.read,MAX_BYTES+1)
                    try:
                        raw = future.result(timeout=timeout)
                    except FutureTimeout:
                        proc.kill(); proc.wait()
                        raise ReceiptError('handoff_ssh_timeout') from None
                    if len(raw) > MAX_BYTES:
                        proc.kill(); proc.wait()
                        raise ReceiptError('handoff_size_invalid')
                code = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.wait()
                raise ReceiptError('handoff_ssh_timeout') from None
    except OSError:
        raise ReceiptError('handoff_ssh_unavailable') from None
    if code:
        raise ReceiptError('handoff_receipt_unavailable')
    return raw
