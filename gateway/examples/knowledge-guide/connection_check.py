"""Check the dev entry points before opening a browser or registering an OAuth client."""
import asyncio
import json
import socket
import ssl
from urllib.parse import urlsplit

import httpx

MAX_RESPONSE_BYTES = 65536


class ConnectionCheckError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def network_failure(error):
    seen = set()
    current = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, socket.gaierror):
            return ConnectionCheckError('dns_unavailable',
                'Адрес dev не найден в DNS. Администратору нужно проверить запись platforma-dev.akmetron.ru.')
        if isinstance(current, ssl.SSLError):
            return ConnectionCheckError('tls_error',
                'Не удалось подтвердить HTTPS-соединение. Администратору нужно проверить сертификат dev.')
        current = current.__cause__ or current.__context__
    return ConnectionCheckError('network_unavailable',
        'Не удалось соединиться с dev. Проверьте доступ к сети и доступность стенда.')


async def probe(client, url, expected_status, *, parse_json=False, unavailable_message=None):
    async with client.stream('GET', url) as response:
        if response.status_code == 503:
            raise ConnectionCheckError('service_unavailable', unavailable_message or
                'Dev временно недоступен. Повторите проверку после завершения обслуживания.')
        if response.is_redirect:
            raise ConnectionCheckError('unexpected_redirect',
                'Dev перенаправляет запрос на другой адрес. Администратору нужно проверить маршрут стенда.')
        if response.status_code != expected_status:
            raise ConnectionCheckError('unexpected_status',
                'Dev вернул неожиданный ответ. Администратору нужно проверить шлюз и маршрут подключения.')
        if not parse_json:
            return
        body = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=8192):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ConnectionCheckError('invalid_response', 'Ответ dev превышает допустимый размер.')
        try:
            data = json.loads(body)
        except (ValueError, UnicodeError):
            raise ConnectionCheckError('invalid_response', 'Dev вернул ответ в неподходящем формате.') from None
        if not isinstance(data, dict):
            raise ConnectionCheckError('invalid_response', 'Dev вернул ответ в неподходящем формате.')
        return data


def supports(metadata, field, required):
    values = metadata.get(field)
    return (isinstance(values, list) and all(isinstance(value, str) for value in values)
            and required.issubset(values))


async def check_connection(url, scopes):
    target = urlsplit(url)
    if (target.scheme != 'https' or not target.hostname or target.username is not None
            or target.password is not None or target.path != '/mcp' or target.query or target.fragment):
        raise ConnectionCheckError('invalid_address', 'Для dev нужен HTTPS-адрес MCP без параметров входа.')
    origin = 'https://' + target.netloc
    try:
        async with asyncio.timeout(20):
            async with httpx.AsyncClient(timeout=httpx.Timeout(5), follow_redirects=False,
                    headers={'Accept': 'application/json, text/event-stream'}) as client:
                health = await probe(client, origin + '/healthz', 200, parse_json=True)
                if health.get('service') != 'gateway-mcp' or health.get('ok') is not True:
                    raise ConnectionCheckError('gateway_unhealthy', 'Dev не подтвердил готовность шлюза.')
                if health.get('auth_enabled') is not True:
                    raise ConnectionCheckError('authentication_disabled',
                        'На dev отключена проверка входа. Администратору нужно включить авторизацию.')
                metadata = await probe(client, origin + '/.well-known/oauth-authorization-server',
                                       200, parse_json=True)
                endpoints = {'issuer': origin, 'authorization_endpoint': origin + '/oauth/authorize',
                             'token_endpoint': origin + '/oauth/token',
                             'registration_endpoint': origin + '/oauth/register'}
                if any(metadata.get(key) != value for key, value in endpoints.items()):
                    raise ConnectionCheckError('oauth_address_mismatch',
                        'Адреса авторизации не совпадают с dev. Администратору нужно проверить настройки OAuth.')
                if not supports(metadata, 'scopes_supported', scopes):
                    raise ConnectionCheckError('scopes_unavailable',
                        'Dev не объявляет необходимые права skills:read и knowledge:read.')
                if (not supports(metadata, 'code_challenge_methods_supported', {'S256'})
                        or not supports(metadata, 'response_types_supported', {'code'})
                        or not supports(metadata, 'token_endpoint_auth_methods_supported', {'none'})):
                    raise ConnectionCheckError('oauth_flow_unavailable',
                        'Dev не подтвердил поддержку входа с защитой PKCE.')
                await probe(client, origin + '/join', 200,
                    unavailable_message='Вход в dev пока закрыт. Администратору нужно завершить настройку приложения Битрикс24.')
                await probe(client, url, 401)
    except (TimeoutError, httpx.TimeoutException):
        raise ConnectionCheckError('timeout',
            'Dev не ответил за время проверки. Проверьте соединение и повторите попытку.') from None
    except httpx.RequestError as error:
        raise network_failure(error) from None
    return {'connection_ready': True, 'mcp_url': url, 'account_checked': False,
            'message': 'Стенд доступен. Следующий шаг — личный вход и проверка прав справочника.'}
