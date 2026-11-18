"""Verified current-user Bitrix frame launch; no installer identity fallback."""
import hashlib
import hmac
import os
import re
from http.cookies import SimpleCookie
from urllib.parse import parse_qs

from starlette.responses import HTMLResponse
from gateway_mcp.services import bitrix_identity as identity

CHANNEL = 'bitrix24-frame'
COOKIE = '__Host-gateway_bitrix_frame'
PORTAL_ORIGIN = 'https://' + identity.PORTAL
PIN_PROVIDER = 'bitrix24-frame-source'
SESSION_SECONDS = 28800
MAX_BODY = 32768


def enabled():
    return identity.enabled() and os.getenv('GATEWAY_BITRIX_EMBED_ENABLED','').casefold() == 'true'


def frame_headers():
    return {'Cache-Control':'no-store', 'Referrer-Policy':'no-referrer',
            'X-Content-Type-Options':'nosniff',
            'Content-Security-Policy':"default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors "+PORTAL_ORIGIN}


def problem(message='Не удалось подтвердить личный вход. Закройте приложение в Битрикс24 и откройте заново.',status=403):
    from html import escape
    return HTMLResponse('<!doctype html><html lang="ru"><meta charset="utf-8"><title>Платформа Акметрон</title>'
        '<h1>Платформа Акметрон</h1><p>'+escape(message)+'</p></html>',status_code=status,headers=frame_headers())


async def read_launch_form(request):
    if request.headers.get('origin') != PORTAL_ORIGIN:
        raise PermissionError('bitrix_frame_origin_invalid')
    if request.headers.get('content-type','').split(';',1)[0].lower() != 'application/x-www-form-urlencoded':
        raise PermissionError('bitrix_frame_form_invalid')
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body)>MAX_BODY:raise PermissionError('bitrix_frame_form_invalid')
    try:
        fields=parse_qs(body.decode('utf-8'),keep_blank_values=True,max_num_fields=64,strict_parsing=True)
        if any(len(values)!=1 for values in fields.values()):raise ValueError()
        return {key:values[0] for key,values in fields.items()}
    except (ValueError,UnicodeError):
        raise PermissionError('bitrix_frame_form_invalid') from None


def _source_key():
    member=identity.settings()['member_id']
    if not member:raise PermissionError('bitrix_frame_portal_not_pinned')
    identity.require_member(member)
    return 'bitrix24-installation:'+member


def _source_record():
    from gateway_mcp.services.storage import get_user_oauth_token
    return get_user_oauth_token(PIN_PROVIDER,_source_key())


def _check_source(record,digest):
    if (record.get('scopes')!=['user_brief'] or
            not hmac.compare_digest(str(record.get('access_token','')),digest)):
        raise PermissionError('bitrix_frame_source_mismatch')


def _pin_source(employee,digest,app_id):
    # A first verified owner launch enrolls the source once. It cannot be
    # overwritten by reinstallation, another user, another app or installer data.
    from gateway_mcp.services.storage_core import _connect
    from gateway_mcp.services.storage import save_user_oauth_token
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("set local statement_timeout = '5s'")
        cur.execute('select pg_advisory_xact_lock(hashtextextended(%s,0))',(_source_key(),))
        record=_source_record()
        if record:
            _check_source(record,digest);return
        if employee['user_id']!=os.getenv('GATEWAY_BITRIX_OWNER_USER_ID',''):
            raise PermissionError('bitrix_frame_owner_initialization_required')
        if not save_user_oauth_token(provider=PIN_PROVIDER,actor_subject=_source_key(),yandex_id='',
            login='',email='',access_token=digest,token_type='BitrixFrameSourceHash',scopes=['user_brief'],
            metadata={'portal':identity.PORTAL,'application_id':str(app_id)},expires_at_epoch=None):
            raise PermissionError('bitrix_secure_storage_required')


def complete_launch(fields,query):
    if not enabled():raise PermissionError('bitrix_frame_disabled')
    if query.get('DOMAIN')!=identity.PORTAL or not re.fullmatch(r'[a-zA-Z0-9_-]{16,128}',query.get('APP_SID','')):
        raise PermissionError('bitrix_frame_context_invalid')
    # Never use request DOMAIN, SERVER_ENDPOINT or claimed USER_ID for API calls.
    member=identity.require_member(fields.get('member_id'))
    if member!=identity.settings()['member_id']:raise PermissionError('bitrix_foreign_portal')
    source=fields.get('APPLICATION_TOKEN','')
    if not re.fullmatch(r'[a-zA-Z0-9_-]{16,256}',source):raise PermissionError('bitrix_frame_source_invalid')
    digest=hashlib.sha256(source.encode()).hexdigest()
    pinned=_source_record()
    if pinned:_check_source(pinned,digest)
    ttl=fields.get('AUTH_EXPIRES','')
    if not re.fullmatch(r'[0-9]{1,5}',ttl) or not 1<=int(ttl)<=3600:
        raise PermissionError('bitrix_frame_credential_invalid')
    tokens=identity._validate_tokens({'member_id':member,'client_endpoint':'https://'+identity.PORTAL+'/rest/',
        'scope':fields.get('APPLICATION_SCOPE',''),'access_token':fields.get('AUTH_ID'),
        'refresh_token':fields.get('REFRESH_ID'),'expires_in':int(ttl)})
    info=identity._request('https://'+identity.PORTAL+'/rest/app.info.json',{'auth':tokens['access_token']}).get('result')
    if (not isinstance(info,dict) or info.get('CODE')!=identity.settings()['client_id']
            or info.get('STATUS')!='L' or info.get('INSTALLED') is not True):
        raise PermissionError('bitrix_frame_application_invalid')
    employee=identity.read_employee(tokens)
    if not pinned:_pin_source(employee,digest,info.get('ID'))
    identity._save(tokens,employee)
    with identity._LOCK:identity._CACHE[employee['subject']]=employee
    return employee


def set_session(response,token):
    if not re.fullmatch(r'[a-zA-Z0-9_.-]+',token):raise ValueError('invalid_browser_session')
    response.headers.append('Set-Cookie',COOKIE+'='+token+'; Path=/; Max-Age='+str(SESSION_SECONDS)+
        '; Secure; HttpOnly; SameSite=None; Partitioned')
    return response


def clear_session(response):
    response.headers.append('Set-Cookie',COOKIE+'=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=None; Partitioned')
    return response


def launch_page(actor):
    """Commit a real HTML document before any same-frame navigation.

    Keep DOMAIN/APP_SID on the launch URL for the official SDK handshake.
    The rendered page contains no upstream token and no Gateway JWT.
    """
    from gateway_mcp.routes.admin_ui import admin_shell
    from gateway_mcp.routes.corporate_ui import guide_body
    from gateway_mcp.services.corporate_access import is_owner
    body = ('<p id="bitrix-frame-status" role="status" data-state="starting">'
            'Проверяем связь с Битрикс24 и встроенную сессию…</p>'
            '<div id="bitrix-frame-problem" class="banner error" role="alert" hidden></div>'
            + guide_body())
    response = HTMLResponse(admin_shell(title='Платформа Акметрон', active='overview',
        actor=actor, body=body, training_reader=not is_owner(actor),
        extra_head='<script defer src="/auth/bitrix24/frame-client.js"></script>'))
    headers = frame_headers()
    headers['Content-Security-Policy'] = (
        "default-src 'none'; style-src 'self' 'unsafe-inline'; img-src 'self'; font-src 'self'; "
        "script-src 'self'; connect-src 'self'; "
        "form-action 'self'; base-uri 'none'; frame-ancestors " + PORTAL_ORIGIN)
    response.headers.update(headers)
    return response


def _panel_path(path):
    return path in {'/join','/auth/logout'} or any(path==root or path.startswith(root+'/')
        for root in ('/admin','/platforma','/credentials','/notifications','/files'))


def _partition_cookie(value):
    # Preserve cookie values, expiry, HttpOnly and path; change only frame transport.
    parts=[p.strip() for p in value.split(';') if p.strip()]
    parts=[p for p in parts if p.lower().split('=',1)[0] not in {'samesite','partitioned','secure'}]
    return '; '.join(parts+['Secure','SameSite=None','Partitioned'])


def _frame_claims(headers):
    from gateway_mcp.services.auth import decode_gateway_token
    try:
        cookies=SimpleCookie();cookies.load(headers.get('cookie',''))
        token=cookies[COOKIE].value
        claims=decode_gateway_token(token,verify=True)
        if claims.get('browser_channel')!=CHANNEL:return False
        member,_=identity.parse_subject(claims.get('sub',''));identity.require_member(member)
        return True
    except Exception:return False


class BitrixFrameMiddleware:
    """Keep ordinary OAuth/MCP and first-party cookie behavior unchanged."""
    def __init__(self,app):self.app=app
    async def __call__(self,scope,receive,send):
        if scope['type']!='http' or not enabled():
            return await self.app(scope,receive,send)
        headers={k.decode('latin-1').lower():v.decode('latin-1') for k,v in scope.get('headers',[])}
        signed=_frame_claims(headers)
        path=scope.get('path','')
        frame=signed or headers.get('sec-fetch-dest')=='iframe'
        # The parent portal posts a fresh current-user launch on every reopen.
        # Its origin and credentials are checked by read_launch_form/complete_launch.
        if path=='/auth/bitrix24/callback' and scope['method']=='POST':
            return await self.app(scope,receive,send)
        if signed and scope['method'] not in {'GET','HEAD','OPTIONS'}:
            from gateway_mcp.config import public_url
            if headers.get('origin')!=public_url().rstrip('/'):
                return await problem(status=403)(scope,receive,send)
        if not frame or not _panel_path(path):return await self.app(scope,receive,send)
        if path=='/join' and not signed:
            return await problem('Откройте Платформу из меню Битрикс24. Если браузер блокирует встроенную сессию, разрешите данные этого приложения в настройках браузера.',401)(scope,receive,send)
        replacement=None
        async def framed_send(message):
            nonlocal replacement
            if message['type']=='http.response.start':
                pairs=list(message.get('headers',[]))
                location=next((v.decode('latin-1') for k,v in pairs if k.lower()==b'location'),'')
                if message['status'] in {302,303,307,308} and location.startswith(('/auth/','/join')):
                    response=problem('Сессия Платформы не открылась или завершилась. Закройте приложение в Битрикс24 и откройте заново. Если это повторяется, проверьте разрешение браузера на встроенную сессию.',401)
                    replacement=response.body
                    cookies=[(k,_partition_cookie(v.decode('latin-1')).encode('latin-1')) for k,v in pairs if k.lower()==b'set-cookie']
                    message={**message,'status':response.status_code,'headers':list(response.raw_headers)+cookies}
                else:
                    converted=[];policies=[]
                    for key,value in pairs:
                        low=key.lower()
                        if low==b'x-frame-options':continue
                        if low==b'content-security-policy':policies.append(value.decode('latin-1'));continue
                        if low==b'set-cookie':value=_partition_cookie(value.decode('latin-1')).encode('latin-1')
                        converted.append((key,value))
                    for policy in policies or ['']:
                        directives=[p.strip() for p in policy.split(';') if p.strip() and not p.strip().lower().startswith('frame-ancestors')]
                        directives.append('frame-ancestors '+PORTAL_ORIGIN)
                        converted.append((b'content-security-policy','; '.join(directives).encode('latin-1')))
                    message={**message,'headers':converted}
            elif message['type']=='http.response.body' and replacement is not None:
                message={**message,'body':replacement,'more_body':False};replacement=b''
            await send(message)
        await self.app(scope,receive,framed_send)
