"""Portal-pinned Bitrix24 identity. Upstream tokens never reach MCP clients."""
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
from urllib.parse import urlencode

import httpx

PORTAL = 'akmetron.bitrix24.ru'
TOKEN_URL = 'https://oauth.bitrix24.tech/oauth/token/'
PROVIDER = 'bitrix24-login'
FLOW_COOKIE = 'gateway_bitrix_flow'
MAX_RESPONSE = 1024 * 1024
_LOCK = threading.RLock()
_CACHE = {}


def enabled():
    return os.getenv('GATEWAY_LOGIN_PROVIDER', 'yandex') == 'bitrix24'


def settings():
    if not enabled():
        raise PermissionError('bitrix_login_disabled')
    if os.getenv('GATEWAY_BITRIX_PORTAL', PORTAL) != PORTAL:
        raise PermissionError('bitrix_portal_configuration_invalid')
    client = os.getenv('GATEWAY_BITRIX_CLIENT_ID', '')
    secret = os.getenv('GATEWAY_BITRIX_CLIENT_SECRET', '')
    if not re.fullmatch(r'local\.[a-zA-Z0-9.]{8,120}', client) or not secret:
        raise PermissionError('bitrix_application_not_configured')
    pin = os.getenv('GATEWAY_BITRIX_MEMBER_ID', '')
    if pin and not re.fullmatch(r'[a-f0-9]{32}', pin):
        raise PermissionError('bitrix_member_configuration_invalid')
    return {'client_id': client, 'client_secret': secret, 'member_id': pin}


def parse_subject(value):
    match = re.fullmatch(r'bitrix24:([a-f0-9]{32}):([1-9][0-9]{0,18})', str(value))
    if not match:
        raise PermissionError('bitrix_identity_invalid')
    return match.group(1), match.group(2)


def is_subject(value):
    return str(value).startswith('bitrix24:')


def require_member(value):
    cfg = settings()
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{32}', value):
        raise PermissionError('bitrix_portal_identity_invalid')
    if cfg['member_id'] and not hmac.compare_digest(value, cfg['member_id']):
        raise PermissionError('bitrix_foreign_portal')
    return value


def callback_url():
    from gateway_mcp.config import public_url
    return public_url().rstrip('/') + '/auth/bitrix24/callback'


def authorize_url(state):
    cfg = settings()
    return 'https://' + PORTAL + '/oauth/authorize/?' + urlencode({'client_id':cfg['client_id'], 'state':state})


def safe_next(value):
    from urllib.parse import urlsplit
    value = str(value or '/admin')
    parts = urlsplit(value)
    if (parts.scheme or parts.netloc or not value.startswith('/') or value.startswith('//')
            or '\\' in value or any(ord(c) < 32 for c in value)):
        return '/admin'
    return value


def _request(url, data):
    # Only callers below select URLs. Never use callback-supplied domains/endpoints.
    try:
        with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
            with client.stream('POST', url, data=data) as response:
                if response.status_code != 200:
                    raise PermissionError('bitrix_upstream_rejected')
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_RESPONSE:
                        raise PermissionError('bitrix_response_too_large')
                    chunks.append(chunk)
                result = json.loads(b''.join(chunks))
        if not isinstance(result, dict) or result.get('error'):
            raise PermissionError('bitrix_upstream_rejected')
        return result
    except PermissionError:
        raise
    except Exception:
        # Do not expose request URLs, codes, bodies or upstream messages in errors.
        raise PermissionError('bitrix_identity_unavailable') from None


def _validate_tokens(data, expected_member=''):
    cfg = settings()
    member = require_member(data.get('member_id'))
    if expected_member and member != expected_member:
        raise PermissionError('bitrix_foreign_portal')
    if data.get('client_endpoint') != 'https://' + PORTAL + '/rest/':
        raise PermissionError('bitrix_foreign_portal')
    # The live authorization server can report its base "app" scope, while the
    # portal reports the application's REST permissions. Check both independently.
    raw_scopes = data.get('scope')
    parts = raw_scopes.split(',') if isinstance(raw_scopes, str) else raw_scopes if isinstance(raw_scopes, list) else []
    scopes = {part.strip() for part in parts if isinstance(part, str) and part.strip()}
    if scopes not in ({'user_brief'}, {'app'}) or any(not isinstance(part, str) for part in parts):
        raise PermissionError('bitrix_unexpected_application_scopes')
    for field in ('access_token', 'refresh_token'):
        value = data.get(field)
        if not isinstance(value, str) or not 10 <= len(value) <= 4096 or any(ord(c)<33 for c in value):
            raise PermissionError('bitrix_credential_invalid')
    ttl = data.get('expires_in')
    if type(ttl) is not int or not 1 <= ttl <= 86400:
        raise PermissionError('bitrix_credential_invalid')
    # No `full` parameter: scope defaults to this application's granted scopes.
    granted = _request('https://' + PORTAL + '/rest/scope.json', {'auth':data['access_token']}).get('result')
    if not isinstance(granted, list) or any(not isinstance(item, str) for item in granted) or set(granted) != {'user_brief'}:
        raise PermissionError('bitrix_unexpected_application_scopes')
    return {'access_token':data['access_token'], 'refresh_token':data['refresh_token'],
            'expires_at':time.time()+ttl, 'member_id':member}


def exchange_code(code):
    cfg = settings()
    if not isinstance(code, str) or not 1 <= len(code) <= 4096 or any(ord(c)<33 for c in code):
        raise PermissionError('bitrix_code_invalid')
    result = _request(TOKEN_URL, {'grant_type':'authorization_code', 'client_id':cfg['client_id'],
                                 'client_secret':cfg['client_secret'], 'code':code})
    return _validate_tokens(result)


def _user_id(value):
    if type(value) is int:
        value = str(value)
    if not isinstance(value, str) or not re.fullmatch(r'[1-9][0-9]{0,18}', value):
        raise PermissionError('bitrix_employee_invalid')
    return value


def _active(value):
    return value is True or type(value) is str and value == 'Y'


def read_employee(tokens, expected_user=''):
    # Fetch only the token owner's identity; never accept an ID from the browser.
    current = _request('https://' + PORTAL + '/rest/user.current.json', {'auth':tokens['access_token']}).get('result')
    if not isinstance(current, dict) or not _active(current.get('ACTIVE')):
        raise PermissionError('bitrix_employee_inactive')
    user_id = _user_id(current.get('ID'))
    if expected_user and user_id != expected_user:
        raise PermissionError('bitrix_identity_changed')
    rows = _request('https://' + PORTAL + '/rest/user.get.json',
                    {'auth':tokens['access_token'], 'FILTER[ID]':user_id,
                     'FILTER[ACTIVE]':'Y', 'FILTER[USER_TYPE]':'employee'}).get('result')
    # Bitrix may ignore invalid filters. Verify returned facts, not the filter alone.
    if not isinstance(rows, list) or len(rows) != 1:
        raise PermissionError('bitrix_internal_employee_required')
    row = rows[0]
    if (not isinstance(row, dict) or _user_id(row.get('ID')) != user_id
            or not _active(row.get('ACTIVE')) or row.get('USER_TYPE') != 'employee'):
        raise PermissionError('bitrix_internal_employee_required')
    departments = row.get('UF_DEPARTMENT')
    if (not isinstance(departments, list) or not departments
            or not all(type(n) in (int,str) and re.fullmatch(r'[1-9][0-9]*', str(n)) for n in departments)):
        raise PermissionError('bitrix_employee_department_required')
    # No contacts, birthday, photo, phone, or whole profiles are retained.
    name = ' '.join(str(row.get(k) or '') for k in ('NAME','LAST_NAME')).strip()[:160]
    return {'subject':'bitrix24:'+tokens['member_id']+':'+user_id, 'user_id':user_id,
            'member_id':tokens['member_id'], 'name':name, 'verified_at':time.time()}


def _save(tokens, employee):
    from gateway_mcp.services.storage import save_user_oauth_token
    envelope = {k:tokens[k] for k in ('access_token','refresh_token','expires_at','member_id')}
    if not save_user_oauth_token(provider=PROVIDER, actor_subject=employee['subject'], yandex_id='',
            login=employee['name'], email='', access_token=json.dumps(envelope), token_type='Bitrix24OAuth',
            scopes=['user_brief'], metadata={'portal':PORTAL,'user_id':employee['user_id'],
            'member_id':employee['member_id']}, expires_at_epoch=None):
        raise PermissionError('bitrix_secure_storage_required')


def complete_login(code, callback_member):
    tokens = exchange_code(code)
    if callback_member != tokens['member_id']:
        raise PermissionError('bitrix_foreign_portal')
    employee = read_employee(tokens)
    _save(tokens, employee)
    with _LOCK:
        _CACHE[employee['subject']] = employee
    return employee


def current_employee(subject):
    """Fail closed on revocation, unavailable API, missing fields or stale identity.

    A 60-second, process-local cache bounds dismissal/type-change propagation.
    Encrypted credential existence/revocation is checked on every request.
    """
    member, user_id = parse_subject(subject)
    require_member(member)
    from gateway_mcp.services.storage import get_user_oauth_token
    try:
        record = get_user_oauth_token(PROVIDER, subject)
        if not record or record.get('scopes') != ['user_brief']:
            raise PermissionError('bitrix_login_required')
        tokens = json.loads(record['access_token'])
        if tokens.get('member_id') != member:
            raise PermissionError('bitrix_foreign_portal')
        with _LOCK:
            cached = _CACHE.get(subject)
            if cached and 0 <= time.time()-cached['verified_at'] < 60 and tokens['expires_at'] > time.time()+30:
                return dict(cached)
            if tokens['expires_at'] <= time.time()+30:
                # Refresh is serialized across processes/workers in PostgreSQL.
                from gateway_mcp.services.storage_core import _connect
                with _connect() as conn, conn.cursor() as cur:
                    cur.execute("set local statement_timeout = '5s'")
                    cur.execute("select pg_advisory_xact_lock(hashtextextended(%s,0))", ('bitrix24-refresh:'+subject,))
                    latest = get_user_oauth_token(PROVIDER, subject)
                    if not latest:
                        raise PermissionError('bitrix_login_required')
                    tokens = json.loads(latest['access_token'])
                    if tokens['expires_at'] <= time.time()+30:
                        cfg = settings()
                        data = _request(TOKEN_URL, {'grant_type':'refresh_token', 'client_id':cfg['client_id'],
                                                   'client_secret':cfg['client_secret'], 'refresh_token':tokens['refresh_token']})
                        tokens = _validate_tokens(data, member)
                        employee = read_employee(tokens, user_id)
                        _save(tokens, employee)
                    else:
                        employee = read_employee(tokens, user_id)
            else:
                employee = read_employee(tokens, user_id)
            if len(_CACHE) >= 1000:
                _CACHE.clear()
            _CACHE[subject] = employee
            return dict(employee)
    except PermissionError:
        _CACHE.pop(subject, None)
        raise
    except Exception:
        _CACHE.pop(subject, None)
        raise PermissionError('bitrix_identity_unavailable') from None


def browser_state_matches(state, cookie):
    return (isinstance(state,str) and 16 <= len(state) <= 256 and isinstance(cookie,str)
            and hmac.compare_digest(state,cookie))


class SafeAccessLog(logging.Filter):
    """Remove OAuth query strings before Uvicorn formats an access event."""
    def filter(self,record):
        if isinstance(record.args,tuple) and len(record.args)==5:
            args = list(record.args)
            path = args[2]
            if isinstance(path,str) and path.startswith(('/auth/','/oauth/')):
                args[2] = path.split('?',1)[0]
                record.args = tuple(args)
        return True


def install_safe_access_logging():
    logger = logging.getLogger('uvicorn.access')
    if not any(isinstance(f,SafeAccessLog) for f in logger.filters):
        logger.addFilter(SafeAccessLog())
