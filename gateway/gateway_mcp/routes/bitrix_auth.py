"""Browser-bound Bitrix login and automatic corporate entry page."""
import asyncio
from html import escape
from urllib.parse import parse_qs, urlsplit, urlencode

from starlette.responses import RedirectResponse, HTMLResponse, JSONResponse, FileResponse, Response
from gateway_mcp.services import bitrix_identity as identity, corporate_access

HEADERS = {'Cache-Control':'no-store','Referrer-Policy':'no-referrer','X-Content-Type-Options':'nosniff',
    'X-Frame-Options':'DENY', 'Content-Security-Policy':"default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"}

LOGIN_FAILURE_REASONS = frozenset({
    'bitrix_state_invalid', 'bitrix_foreign_portal', 'bitrix_upstream_rejected',
    'bitrix_identity_unavailable', 'bitrix_unexpected_application_scopes',
    'bitrix_credential_invalid', 'bitrix_employee_inactive',
    'bitrix_internal_employee_required', 'bitrix_employee_department_required',
    'bitrix_secure_storage_required', 'bitrix_employee_invalid',
})


def bind_browser(response, url):
    if identity.enabled():
        state = parse_qs(urlsplit(url).query).get('state',[''])[0]
        response.set_cookie(identity.FLOW_COOKIE,state,max_age=600,httponly=True,secure=True,samesite='lax',path='/')
        response.headers.update(HEADERS)
    return response


async def entry(request):
    from gateway_mcp.web import web_actor
    actor = await asyncio.to_thread(web_actor,request)
    if actor is None:
        return HTMLResponse('<!doctype html><html lang="ru"><meta charset="utf-8"><title>Платформа Акметрон</title>'
            '<h1>Платформа Акметрон</h1><p>Войдите через корпоративный Битрикс24.</p>'
            '<p>Базовый доступ получают действующие внутренние сотрудники akmetron.bitrix24.ru.</p>'
            '<p><a href="/auth/bitrix24/login?next=/join">Войти через Битрикс24</a></p></html>',headers=HEADERS)
    from gateway_mcp.config import public_url
    endpoint = public_url().rstrip('/')+'/mcp'
    return HTMLResponse('<!doctype html><html lang="ru"><meta charset="utf-8"><title>Доступ открыт</title>'
        '<h1>Корпоративный доступ открыт</h1><p>'+escape(actor.display)+'</p>'
        '<p>Учётная запись внутреннего сотрудника проверена на akmetron.bitrix24.ru.</p>'
        '<p><a href="/admin">Открыть Платформу</a> · <a href="/platforma/skills">Навыки</a> · '
        '<a href="/platforma/how-it-works">Как это работает</a></p>'
        '<p>В вашем ИИ-приложении добавьте удалённый MCP-сервер с OAuth:</p><pre>'+escape(endpoint)+'</pre>'
        '<p>Пройдите личный вход через тот же корпоративный Битрикс24.</p>'
        '<p><a href="/auth/logout">Выйти</a></p></html>',headers=HEADERS)


def register_bitrix_auth_routes(server):
    identity.install_safe_access_logging()

    @server.custom_route('/auth/bitrix24/frame-client.js',methods=['GET'],include_in_schema=False)
    async def frame_client(request):
        from pathlib import Path
        from gateway_mcp.services import bitrix_embed
        if not bitrix_embed.enabled():
            return Response(status_code=404)
        return FileResponse(Path(__file__).resolve().parents[1] / 'help_assets' / 'bitrix-frame.js',
                            media_type='application/javascript', headers={
                                'Cache-Control':'no-store', 'X-Content-Type-Options':'nosniff',
                                'Content-Security-Policy':"default-src 'none'"})

    @server.custom_route('/auth/bitrix24/frame-sdk.js',methods=['GET'],include_in_schema=False)
    async def frame_sdk(request):
        from pathlib import Path
        from gateway_mcp.services import bitrix_embed
        if not bitrix_embed.enabled():
            return Response(status_code=404)
        # Fixed reviewed public asset only: never proxy arbitrary URLs or credentials.
        return FileResponse(Path(__file__).resolve().parents[1] / 'help_assets' / 'bitrix24-sdk-v1.js',
                            media_type='application/javascript', headers={
                                'Cache-Control':'no-store', 'X-Content-Type-Options':'nosniff',
                                'Content-Security-Policy':"default-src 'none'"})

    @server.custom_route('/auth/bitrix24/frame-session',methods=['GET'],include_in_schema=False)
    async def frame_session(request):
        from gateway_mcp.services import bitrix_embed
        from gateway_mcp.web import web_actor
        if not bitrix_embed.enabled():
            return Response(status_code=404)
        actor = await asyncio.to_thread(web_actor, request)
        ok = actor is not None and bool(request.cookies.get(bitrix_embed.COOKIE))
        return JSONResponse({'ok':ok}, status_code=200 if ok else 401, headers=bitrix_embed.frame_headers())

    def app_landing():
        # The portal's installation/frame POST is not accepted as a personal login.
        # Do not read, retain, echo or use AUTH_ID/REFRESH_ID from that request.
        from gateway_mcp.config import public_url
        headers = {k:v for k,v in HEADERS.items() if k not in {'X-Frame-Options','Content-Security-Policy'}}
        headers['Content-Security-Policy'] = "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors https://akmetron.bitrix24.ru; base-uri 'none'"
        target=public_url().rstrip('/')+'/join'
        return HTMLResponse('<!doctype html><html lang="ru"><meta charset="utf-8"><h1>Платформа Акметрон</h1>'
            '<p>Для личного входа откройте Платформу в отдельном окне.</p><p><a target="_blank" rel="noopener noreferrer" href="'+escape(target,quote=True)+'">Открыть Платформу</a></p></html>',headers=headers)

    @server.custom_route('/auth/bitrix24/install',methods=['GET','POST'],include_in_schema=False)
    async def install(request):
        return app_landing()
    @server.custom_route('/auth/bitrix24/login',methods=['GET'],include_in_schema=False)
    async def login(request):
        from gateway_mcp.services.auth import create_oauth_state_payload
        try:
            identity.settings()
            state = create_oauth_state_payload({'flow':'bitrix24_browser','next':identity.safe_next(request.query_params.get('next','/admin'))},ttl_seconds=600)
            url = identity.authorize_url(state)
            return bind_browser(RedirectResponse(url,status_code=303),url)
        except Exception:
            return JSONResponse({'ok':False,'error':'bitrix_login_unavailable'},status_code=503,headers=HEADERS)

    @server.custom_route('/auth/bitrix24/callback',methods=['GET','POST'],include_in_schema=False)
    async def callback(request):
        if request.method=='POST':
            from gateway_mcp.services import bitrix_embed
            if bitrix_embed.enabled():
                from gateway_mcp.services.auth import mint_gateway_token
                from gateway_mcp.services.observability import audit_event
                from gateway_mcp.services.policy import GatewayActor
                try:
                    fields=await bitrix_embed.read_launch_form(request)
                    if len(request.query_params.getlist('DOMAIN'))!=1 or len(request.query_params.getlist('APP_SID'))!=1:
                        raise PermissionError('bitrix_frame_context_invalid')
                    employee=await asyncio.to_thread(bitrix_embed.complete_launch,fields,dict(request.query_params))
                    actor=await asyncio.to_thread(corporate_access.fresh_actor,employee['subject'])
                    token=mint_gateway_token(actor,ttl_seconds=bitrix_embed.SESSION_SECONDS,browser_channel=bitrix_embed.CHANNEL)
                    response=bitrix_embed.set_session(bitrix_embed.launch_page(actor),token)
                    audit_event(event='corporate_frame_login',actor=actor,tool='bitrix24_frame_login',system='identity',decision='allow',status='ok',arguments={})
                    return response
                except Exception as exc:
                    known={'bitrix_frame_origin_invalid','bitrix_frame_form_invalid','bitrix_frame_context_invalid',
                        'bitrix_frame_source_invalid','bitrix_frame_source_mismatch','bitrix_frame_portal_not_pinned',
                        'bitrix_frame_owner_initialization_required','bitrix_frame_application_invalid',
                        'bitrix_frame_credential_invalid'} | set(LOGIN_FAILURE_REASONS)
                    reason=str(exc) if isinstance(exc,PermissionError) and str(exc) in known else 'bitrix_frame_login_failed'
                    audit_event(event='corporate_frame_login',actor=GatewayActor(subject='anonymous'),tool='bitrix24_frame_login',system='identity',decision='deny',status='denied',arguments={},error=reason)
                    if reason=='bitrix_frame_owner_initialization_required':
                        return bitrix_embed.problem('Первый встроенный запуск должен выполнить владелец Платформы. После него сотрудники смогут открывать приложение автоматически.')
                    return bitrix_embed.problem()
            return app_landing()
        from gateway_mcp.services.auth import pop_oauth_state, mint_gateway_token, create_mcp_authorization_code, build_redirect_uri_with_params
        from gateway_mcp.web import with_gateway_cookie
        from gateway_mcp.services.observability import audit_event
        state = request.query_params.get('state','')
        try:
            if not identity.enabled() or not identity.browser_state_matches(state,request.cookies.get(identity.FLOW_COOKIE,'')):
                raise PermissionError('bitrix_state_invalid')
            stored = pop_oauth_state(state)
            if not stored or stored.get('flow') not in {'bitrix24_browser','mcp_oauth_authorize'}:
                raise PermissionError('bitrix_state_invalid')
            if request.query_params.get('domain') != identity.PORTAL:
                raise PermissionError('bitrix_foreign_portal')
            callback_member = request.query_params.get('member_id')
            employee = await asyncio.to_thread(identity.complete_login,request.query_params.get('code',''),callback_member)
            actor = await asyncio.to_thread(corporate_access.fresh_actor,employee['subject'])
            if stored['flow'] == 'mcp_oauth_authorize':
                code = create_mcp_authorization_code(actor,stored)
                target = build_redirect_uri_with_params(str(stored['redirect_uri']),{'code':code,'state':str(stored.get('client_state','')),'iss':__import__('gateway_mcp.services.auth',fromlist=['issuer_url']).issuer_url()})
                response = RedirectResponse(target,status_code=303)
            else:
                response = with_gateway_cookie(RedirectResponse(identity.safe_next(stored.get('next')),status_code=303),mint_gateway_token(actor))
            audit_event(event='corporate_login',actor=actor,tool='bitrix24_login',system='identity',decision='allow',scope='',status='ok',arguments={'channel':'mcp' if stored['flow']=='mcp_oauth_authorize' else 'web'})
            response.delete_cookie(identity.FLOW_COOKIE,path='/')
            response.headers.update(HEADERS)
            return response
        except Exception as exc:
            # Callback may contain credentials and private fields; never echo it.
            from gateway_mcp.services.policy import GatewayActor
            reason = str(exc) if isinstance(exc, PermissionError) and str(exc) in LOGIN_FAILURE_REASONS else 'bitrix_callback_failed'
            audit_event(event='corporate_login', actor=GatewayActor(subject='anonymous'),
                tool='bitrix24_login', system='identity', decision='deny', scope='',
                status='denied', arguments={}, error=reason)
            response = HTMLResponse('<!doctype html><html lang="ru"><meta charset="utf-8"><h1>Вход не выполнен</h1>'
                '<p>Нужна действующая внутренняя учётная запись akmetron.bitrix24.ru. '
                'Повторите вход или обратитесь к администратору.</p><a href="/join">Повторить вход</a></html>',status_code=403,headers=HEADERS)
            response.delete_cookie(identity.FLOW_COOKIE,path='/')
            return response
