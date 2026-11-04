"""Real, read-only UniSender adapter. No arbitrary method, URL, headers or body."""
import json
import re
from datetime import datetime, timezone

import httpx

from gateway_mcp.backends.common import BackendConfigError, BackendRouteError
from gateway_mcp.services import unisender_readonly as boundary

API_ROOT = 'https://api.unisender.com/ru/api/'
MAX_RESPONSE = 1024 * 1024


def _credential():
    from gateway_mcp.services.storage import get_user_oauth_token
    try:
        record = get_user_oauth_token(boundary.PROVIDER, boundary.SERVICE_SUBJECT)
        key = record.get('access_token', '') if record else ''
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,256}', key):
            raise ValueError
        if set(record.get('scopes', [])) != set(boundary.METHODS):
            raise ValueError
        return key
    except Exception:
        raise BackendConfigError('UNISENDER_CREDENTIAL_UNAVAILABLE') from None


async def _post_api(method, parameters):
    if method not in boundary.METHODS:
        raise PermissionError('UNISENDER_METHOD_DENIED')
    key = _credential()
    # Read operations use POST only to keep the credential out of URL/proxy logs.
    body = {'format': 'json', 'api_key': key, **parameters}
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client:
            async with client.stream('POST', API_ROOT + method, data=body) as response:
                if response.status_code != 200:
                    raise BackendRouteError('UNISENDER_UPSTREAM_HTTP_ERROR')
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_RESPONSE:
                        raise BackendRouteError('UNISENDER_RESPONSE_TOO_LARGE')
                    chunks.append(chunk)
                payload = json.loads(b''.join(chunks))
    except BackendRouteError:
        raise
    except Exception:
        # Never include request bodies, response bodies, credentials or raw errors.
        raise BackendRouteError('UNISENDER_UPSTREAM_UNAVAILABLE') from None
    if not isinstance(payload, dict) or 'error' in payload or 'result' not in payload:
        raise BackendRouteError('UNISENDER_UPSTREAM_REJECTED')
    return payload['result'], key


def _positive_id(value):
    if type(value) is not int or not 1 <= value <= 2**63 - 1:
        raise BackendRouteError('UNISENDER_INVALID_LIST_ID')
    return value


def _upstream_id(value):
    if type(value) is int:
        return _positive_id(value)
    if isinstance(value, str) and re.fullmatch(r'[0-9]{1,19}', value):
        return _positive_id(int(value))
    raise BackendRouteError('UNISENDER_INVALID_UPSTREAM_RESPONSE')


async def _call_unisender_readonly(route, arguments):
    from gateway_mcp.services.auth import current_actor
    name = route.get('name')
    if (name not in boundary.ROUTES or route.get('backend') != 'unisender'
            or route.get('scope') != boundary.SCOPE or route.get('http_method') != 'GET'):
        raise PermissionError('UNISENDER_ROUTE_DENIED')
    if not isinstance(arguments, dict):
        raise BackendRouteError('UNISENDER_INVALID_ARGUMENTS')
    if name == 'unisender.getLists':
        if arguments:
            raise BackendRouteError('UNISENDER_UNEXPECTED_ARGUMENTS')
        method, parameters = 'getLists', {}
    else:
        if set(arguments) - {'list_id', 'contact_type'} or 'list_id' not in arguments:
            raise BackendRouteError('UNISENDER_UNEXPECTED_ARGUMENTS')
        list_id = _positive_id(arguments['list_id'])
        contact_type = arguments.get('contact_type', 'address')
        if contact_type not in ('address', 'phone'):
            raise BackendRouteError('UNISENDER_INVALID_CONTACT_TYPE')
        method = 'getContactCount'
        parameters = {'list_id': str(list_id), 'params[type]': contact_type}
    boundary.require_actor(current_actor())
    result, key = await _post_api(method, parameters)
    data = {'synthetic': False, 'read_only': True, 'real_unisender_connected': True,
            'upstream_contacted': True, 'source': 'unisender-api:' + method,
            'fetched_at': datetime.now(timezone.utc).isoformat()}
    if method == 'getLists':
        if not isinstance(result, list) or len(result) > 5000:
            raise BackendRouteError('UNISENDER_INVALID_UPSTREAM_RESPONSE')
        lists = []
        for item in result:
            if not isinstance(item, dict):
                raise BackendRouteError('UNISENDER_INVALID_UPSTREAM_RESPONSE')
            identifier, title = _upstream_id(item.get('id')), item.get('title')
            if (not isinstance(title, str) or len(title) > 1000 or key in title
                    or any(ord(c) < 32 for c in title)):
                raise BackendRouteError('UNISENDER_INVALID_UPSTREAM_RESPONSE')
            lists.append({'id': identifier, 'title': title})
        data.update(lists=lists, count=len(lists))
    else:
        if not isinstance(result, dict) or _upstream_id(result.get('listId')) != list_id:
            raise BackendRouteError('UNISENDER_INVALID_UPSTREAM_RESPONSE')
        raw = result.get('count')
        if type(raw) is int and 0 <= raw <= 2**63 - 1:
            count = raw
        elif isinstance(raw, str) and re.fullmatch(r'[0-9]{1,19}', raw) and int(raw) <= 2**63 - 1:
            count = int(raw)
        else:
            raise BackendRouteError('UNISENDER_INVALID_UPSTREAM_RESPONSE')
        params = result.get('searchParams')
        if not isinstance(params, dict) or params.get('type') != contact_type:
            raise BackendRouteError('UNISENDER_INVALID_UPSTREAM_RESPONSE')
        data.update(list_id=list_id, contact_type=contact_type, count=count,
                    count_note='Contacts of the selected type; not an active-subscriber count.')
    return {'ok': True, 'status': 200, 'data': data}
