"""Personal Yandex entry, waiting room and owner-only reader decisions."""
import hashlib
import hmac
import json
import secrets
import time
from asyncio import to_thread
from datetime import timedelta, timezone
from functools import wraps
from html import escape
from urllib.parse import parse_qs, urlsplit

from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from gateway_mcp.config import public_url
from gateway_mcp.services import branding
from gateway_mcp.services import platforma_onboarding as service
from gateway_mcp.services.auth import jwt_secret
from gateway_mcp.services.observability import audit_event
from gateway_mcp.web import login_redirect, web_actor

COOKIE = 'platforma_reader_csrf'
LABELS = {'pending': 'Ожидает твоего решения', 'approved': 'Доступ открыт',
          'rejected': 'Заявка отклонена', 'revoked': 'Доступ отозван', 'expired': 'Срок доступа истёк'}
CSS = '''<style>
:root{font-family:system-ui,sans-serif;color:#172a27;background:#f5f7f5}
body{margin:0}main{max-width:820px;margin:48px auto;padding:0 24px}h1{font-size:30px;line-height:1.2}
.card{padding:24px;border:1px solid #d9e2dd;border-radius:12px;background:white;margin:20px 0}
.muted{color:#536660}p,li{line-height:1.6}a{color:#14583e}button,.button{display:inline-block;
background:#14583e;color:white;border:0;border-radius:7px;padding:12px 18px;font:inherit;
text-decoration:none;cursor:pointer}.secondary{background:#edf2ee;color:#172a27}.danger{background:#923b34}
label{display:block;margin:16px 0}input[type=text],textarea{box-sizing:border-box;width:100%;padding:10px;
font:inherit;border:1px solid #a7b8ad;border-radius:5px}pre{white-space:pre-wrap;overflow-wrap:anywhere;
background:#f1f4f1;padding:12px}td,th{text-align:left;padding:12px;border-bottom:1px solid #ddd}
table{width:100%;border-collapse:collapse}code{overflow-wrap:anywhere}a:focus-visible,button:focus-visible,
input:focus-visible,textarea:focus-visible{outline:3px solid #799e27;outline-offset:3px}
@media(max-width:650px){main{margin:24px auto}table,tbody,tr,td{display:block}thead{display:none}}
</style>'''


def _time(value):
    return value.astimezone(timezone(timedelta(hours=3))).strftime('%d.%m.%Y %H:%M МСК') if value else '—'


def _response(title, body, status=200, *, refresh=False):
    meta = '<meta http-equiv="refresh" content="10">' if refresh else ''
    html = ('<!doctype html><html lang="ru"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">' + meta +
            '<title>' + escape(title) + '</title>' + CSS + branding.head() + '</head><body>' + branding.header() + '<main><h1>' +
            escape(title) + '</h1>' + body + '</main></body></html>')
    response = HTMLResponse(html, status_code=status)
    # Native HTML POSTs under no-referrer carry Origin: null in browsers.
    # Keep external referrers private while allowing our strict same-origin forms.
    response.headers.update({'Cache-Control': 'no-store', 'Referrer-Policy': 'same-origin',
                             'X-Frame-Options': 'DENY', 'X-Content-Type-Options': 'nosniff',
                             'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"})
    if branding.enabled():
        response.headers['Content-Security-Policy'] = "default-src 'none'; style-src 'self' 'unsafe-inline'; img-src 'self'; font-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    return response


def _csrf_mac(actor, nonce):
    return hmac.new(jwt_secret().encode(), ('platforma-reader-csrf|' + actor.subject + '|' + nonce).encode(), hashlib.sha256).hexdigest()


def _csrf(request, actor):
    token = request.cookies.get(COOKIE, '')
    nonce, _, signature = token.partition('.')
    if len(nonce) != 43 or not secrets.compare_digest(signature, _csrf_mac(actor, nonce)):
        nonce = secrets.token_urlsafe(32)
        token = nonce + '.' + _csrf_mac(actor, nonce)
    return token


def _cookie(response, token):
    response.set_cookie(COOKIE, token, max_age=3600, httponly=True, secure=public_url().startswith('https://'), samesite='lax', path='/')
    return response


def _hidden(name, value):
    return '<input type="hidden" name="' + escape(name, quote=True) + '" value="' + escape(str(value), quote=True) + '">'


async def _form(request, actor):
    origin = request.headers.get('origin')
    base = urlsplit(public_url())
    if origin and origin != base.scheme + '://' + base.netloc:
        raise PermissionError('Origin mismatch')
    raw = await request.body()
    if len(raw) > 16000:
        raise ValueError('Form too large')
    values = parse_qs(raw.decode(), keep_blank_values=True, max_num_fields=10)
    if any(len(v) != 1 for v in values.values()) or set(values) - {'csrf','decision','revision','reason','confirmation','confirmed'}:
        raise ValueError('Invalid form fields')
    form = {k: v[0] for k, v in values.items()}
    supplied = form.get('csrf', '')
    if not supplied or not secrets.compare_digest(supplied, request.cookies.get(COOKIE, '')) or not secrets.compare_digest(supplied, _csrf(request, actor)):
        raise PermissionError('CSRF mismatch')
    return form


def _signature(actor, preview, csrf, expires):
    payload = json.dumps({'purpose': 'APP11-reader-decision-v1', 'actor': actor.subject,
                          'preview': preview, 'csrf': csrf, 'expires': expires}, sort_keys=True, separators=(',', ':'))
    return str(expires) + '.' + hmac.new(jwt_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()


def _guard(owner=False):
    def decorate(handler):
        @wraps(handler)
        async def guarded(request):
            if not service.enabled():
                return PlainTextResponse('Not found', status_code=404)
            actor = web_actor(request)
            if actor is None:
                return login_redirect('/admin/platforma-readers' if owner else '/join')
            try:
                if owner:
                    await to_thread(service.require_owner, actor)
                return await handler(request, actor)
            except PermissionError:
                return _response('Действие недоступно', '<p>Проверь аккаунт и обнови страницу. <a href="/join">Статус доступа</a></p>', 403)
            except (ValueError, KeyError, TypeError):
                return _response('Состояние изменилось', '<p>Открой страницу заново перед повторным решением.</p>', 409)
            except Exception as exc:
                audit_event(event='platforma_onboarding_error', actor=actor, system='platforma-onboarding', status='error', error=type(exc).__name__)
                return _response('Временно недоступно', '<p>Попробуй позже. Данные и права не открываются при ошибке проверки.</p>', 503)
        return guarded
    return decorate


def _permissions():
    return ('<ul><li>Чтение общей карты знаний Платформы.</li><li>Чтение вымышленных сервисов и заявок.</li></ul>'
            '<p class="muted">Без изменения данных, администрирования, личной памяти и статистики владельца.</p>')


def register_platforma_onboarding_routes(mcp):
    @mcp.custom_route('/join', methods=['GET'], include_in_schema=False)
    async def entry(request):
        if not service.enabled():
            return PlainTextResponse('Not found', status_code=404)
        actor = web_actor(request)
        if actor is None:
            return _response('Платформа — доступ для коллег', '<div class="card"><p>Войди своим аккаунтом Яндекса. Заявка появится у владельца; данные откроются после его одобрения.</p><a class="button" href="/auth/yandex/login?next=/join">Войти через Яндекс</a></div>' + _permissions())
        return await _entry_authenticated(request)

    @_guard()
    async def _entry_authenticated(request, actor):
        if service.is_owner(actor):
            return _response('Управление доступом коллег', '<p>Ты вошёл как владелец.</p><a class="button" href="/admin/platforma-readers">Заявки и доступ коллег</a>')
        row = await to_thread(service.own_request, actor, create=True)
        status = service.effective_status(row)
        # Also surface broken/revoked linked grants as closed, never as an open connection.
        if status == 'approved' and await to_thread(service.active_reader, actor.subject) is None:
            status = 'revoked'
        token = _csrf(request, actor)
        body = '<p>Аккаунт: <strong>' + escape(actor.email) + '</strong></p>'
        if status == 'pending':
            body += '<div class="card"><p>Заявка отправлена владельцу. Пока он её не одобрит, карта знаний, сервисы и заявки закрыты.</p><p class="muted">Страница проверяет решение каждые 10 секунд. Можно закрыть её и вернуться позже.</p></div>'
        elif status == 'approved':
            endpoint = public_url().rstrip('/') + '/mcp'
            body += '<div class="card"><p>Роль: <strong>Читатель Платформы</strong>. Действует до ' + escape(_time(row['approved_until'])) + '.</p>' + _permissions() + '</div>'
            body += '<h2>Подключить своего ИИ-ассистента</h2><p>Добавь удалённый MCP-сервер с OAuth и войди тем же аккаунтом Яндекса:</p><pre>' + escape(endpoint) + '</pre>'
            body += '<details><summary>Команды для Codex CLI</summary><pre>' + escape('codex mcp add platforma --url ' + endpoint + '\ncodex mcp login platforma') + '</pre></details>'
            body += '<p>Первый запрос ассистенту:</p><pre>Прочитай awareness/start.md и awareness/project-profile.md в space:organizational-awareness. Покажи доступную карту и учебные сервисы.</pre>'
        else:
            body += '<div class="card"><p>Данные закрыты. Можно обратиться к владельцу или повторно подать заявку.</p><form method="post" action="/join/request">' + _hidden('csrf', token) + '<button>Подать заявку повторно</button></form></div>'
        body += '<p><a href="/auth/logout">Выйти</a></p>'
        return _cookie(_response(LABELS[status], body, refresh=status == 'pending'), token)

    @mcp.custom_route('/join/request', methods=['POST'], include_in_schema=False)
    @_guard()
    async def resubmit(request, actor):
        form = await _form(request, actor)
        if set(form) != {'csrf'}:
            raise ValueError('Unexpected request fields')
        await to_thread(service.own_request, actor, create=True, resubmit=True)
        return RedirectResponse('/join', status_code=303)

    @mcp.custom_route('/admin/platforma-readers', methods=['GET'], include_in_schema=False)
    @_guard(owner=True)
    async def admin_page(request, actor):
        page = max(0, min(int(request.query_params.get('page', '0')), 2000))
        listing = await to_thread(service.list_requests, actor, page * 50)
        token = _csrf(request, actor)
        body = '<p>Общая ссылка для коллег: <a href="/join">' + escape(public_url().rstrip('/') + '/join') + '</a></p>'
        body += '<p>Одобрение выдаёт только роль «Читатель Платформы».</p>' + _permissions()
        body += '<table><thead><tr><th>Аккаунт Яндекса</th><th>Состояние</th><th>Решение</th></tr></thead><tbody>'
        for row in listing['rows']:
            status = service.effective_status(row)
            body += '<tr><td><strong>' + escape(row['email']) + '</strong><br>' + escape(row['login']) + '<br><span class="muted">Заявка: ' + escape(_time(row['requested_at'])) + '</span></td><td>' + LABELS[status]
            if row['approved_until']:
                body += '<br><span class="muted">До ' + escape(_time(row['approved_until'])) + '</span>'
            body += '</td><td>'
            if row['status'] in {'pending', 'approved'}:
                body += '<form method="post" action="/admin/platforma-readers/' + str(row['id']) + '/preview">' + _hidden('csrf', token) + _hidden('revision', row['revision'])
                body += '<label>Комментарий<input type="text" name="reason" maxlength="2000" required></label>'
                if row['status'] == 'pending':
                    body += '<button name="decision" value="approved">Одобрить</button> <button class="secondary" name="decision" value="rejected">Отклонить</button>'
                else:
                    body += '<button class="danger" name="decision" value="revoked">Отозвать доступ</button>'
                body += '</form>'
            body += '</td></tr>'
        if not listing['rows']:
            body += '<tr><td colspan="3">Заявок пока нет.</td></tr>'
        body += '</tbody></table><p>Всего заявителей: ' + str(listing['count']) + '.</p>'
        if page:
            body += '<a href="?page=' + str(page-1) + '">Назад</a> '
        if (page + 1) * 50 < listing['count']:
            body += '<a href="?page=' + str(page+1) + '">Далее</a>'
        from gateway_mcp.routes.admin_ui import admin_shell
        response = _response('Доступ коллег', body)
        response.body = admin_shell(title='Доступ коллег', active='platforma-readers', actor=actor, body='<div class="akm-heading"><span class="eyebrow">Управление доступом</span><h1>Доступ коллег</h1></div><section class="panel">' + body + '</section>' if branding.enabled() else '<h1>Доступ коллег</h1>' + body, extra_head=CSS).encode()
        response.headers['content-length'] = str(len(response.body))
        return _cookie(response, token)

    async def decision_preview(request, actor, form):
        return await to_thread(service.preview, actor, request.path_params['request_id'],
                               form.get('decision', ''), int(form.get('revision', '0')), form.get('reason', ''))

    @mcp.custom_route('/admin/platforma-readers/{request_id}/preview', methods=['POST'], include_in_schema=False)
    @_guard(owner=True)
    async def preview_page(request, actor):
        form = await _form(request, actor)
        preview = await decision_preview(request, actor, form)
        signature = _signature(actor, preview, form['csrf'], int(time.time()) + 600)
        label = {'approved': 'Подтвердить одобрение', 'rejected': 'Подтвердить отказ', 'revoked': 'Подтвердить отзыв'}[preview['decision']]
        body = '<div class="card"><p>Аккаунт: <strong>' + escape(preview['email']) + '</strong></p>'
        if preview['decision'] == 'approved':
            body += '<p>Откроется роль «Читатель Платформы» до ' + escape(_time(service._utc(preview['until']))) + '.</p>' + _permissions()
        else:
            body += '<p>Доступ к данным будет закрыт. Скачанные ранее копии это не удаляет.</p>'
        body += '<p>Комментарий: ' + escape(preview['reason']) + '</p></div><form method="post" action="/admin/platforma-readers/' + preview['request_id'] + '/confirm">'
        for key in ('csrf', 'revision', 'decision', 'reason'):
            body += _hidden(key, form[key])
        body += _hidden('confirmation', signature) + '<label><input type="checkbox" name="confirmed" value="yes" required> Подтверждаю это решение</label><button>' + label + '</button> <a href="/admin/platforma-readers">Отмена</a></form>'
        return _response('Проверка решения', body)

    @mcp.custom_route('/admin/platforma-readers/{request_id}/confirm', methods=['POST'], include_in_schema=False)
    @_guard(owner=True)
    async def confirm_page(request, actor):
        form = await _form(request, actor)
        preview = await decision_preview(request, actor, form)
        if form.get('confirmed') != 'yes':
            raise PermissionError('Confirmation required')
        supplied = form.get('confirmation', '')
        expires = int(supplied.split('.', 1)[0])
        if not 0 <= expires - int(time.time()) <= 600 or not secrets.compare_digest(supplied, _signature(actor, preview, form['csrf'], expires)):
            raise PermissionError('Fresh matching preview required')
        await to_thread(service.decide, actor, preview['request_id'], preview['decision'], preview['revision'], preview['reason'])
        audit_event(event='platforma_reader_decision', actor=actor, system='platforma-onboarding', decision=preview['decision'], status='ok', arguments={'request_id': preview['request_id']})
        return RedirectResponse('/admin/platforma-readers', status_code=303)
