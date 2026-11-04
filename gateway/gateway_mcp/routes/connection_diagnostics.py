"""Browser identity is not proof of an MCP call in another application."""
from asyncio import to_thread
from datetime import datetime, timedelta, timezone
from html import escape
from urllib.parse import urlencode

from starlette.responses import HTMLResponse, PlainTextResponse
from gateway_mcp.routes.admin_ui import admin_shell
from gateway_mcp.services import corporate_access
from gateway_mcp.services import connection_diagnostics as service
from gateway_mcp.services.policy import has_scope
from gateway_mcp.web import login_redirect, web_actor

PATH = '/platforma/connection'


def _time(value):
    try:
        stamp = value if isinstance(value,datetime) else datetime.fromisoformat(str(value).replace('Z','+00:00'))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.astimezone(timezone(timedelta(hours=3))).strftime('%d.%m.%Y %H:%M МСК')
    except (ValueError,TypeError):
        return '—'


def _response(actor, body, status=200):
    return HTMLResponse(admin_shell(title='Проверить подключение',active='connection-check',actor=actor,body=body),
                        status_code=status,headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer',
                        'X-Content-Type-Options':'nosniff'})


def _render(actor, mark, result):
    prompt = ('Через MCP Платформы вызови gateway_access_profile с параметром diagnostic_id="'+mark+'". '
              'Покажи connection_check, мои scopes и имя вызванного инструмента. '
              'Ничего не меняй в рабочих данных. Если инструмент недоступен в этом чате, '
              'прямо сообщи, что запрос на сервер не выполнялся. Не заменяй вызов предположениями.')
    heading = '<h1>Проверить подключение</h1><p>Вход в панель подтверждён. Теперь проверим отдельный вызов из вашего ИИ-приложения.</p>'
    instructions = ('<section class="panel"><h2>1. Отправьте запрос своему ассистенту</h2>'
        '<p>Скопируйте весь текст ниже в тот чат, где хотите использовать Платформу. '
        'Нужен личный вход через корпоративный Битрикс24. Пароли и API-ключи не вводите.</p>'
        '<label for="connection-prompt">Готовый запрос</label><textarea id="connection-prompt" readonly rows="7" '
        'style="box-sizing:border-box;width:100%;font:inherit;line-height:1.5">'+escape(prompt)+'</textarea>'
        '<p>Метка проверки: <code>'+escape(mark)+'</code>. Она действует 15 минут и не даёт прав доступа.</p>'
        '<p>Проверка запишет техническое событие в журнал, рабочие данные не изменит.</p></section>')
    refresh = PATH+'?'+urlencode({'check':mark})
    body = heading+instructions+'<section class="panel"><h2>2. Обновите результат</h2>'
    if result is None:
        body += ('<p role="status"><strong>Запрос с этой меткой пока не получен от вашей учётной записи.</strong></p>'
            '<p>Открытие этой страницы не проверяет MCP. Сначала дождитесь ответа ассистента, затем обновите результат.</p>')
    else:
        client = result['client']
        label = client['name'] if client['name'] != 'unknown' else 'Не определено'
        if client['version']:
            label += ' '+client['version']
        tools = ''.join('<li><code>'+escape(t)+'</code></li>' for t in result['allowed_tools'])
        body += ('<p role="status"><strong>Запрос дошёл через MCP. Личность совпадает с вашим входом в панель.</strong></p>'
            '<dl><dt>Время проверки</dt><dd>'+escape(_time(result['checked_at']))+'</dd>'
            '<dt>Приложение</dt><dd>'+escape(label)+'</dd><dt>Идентификатор серверного запроса</dt><dd><code>'
            +escape(result['gateway_request_id'])+'</code></dd><dt>Права при этом вызове</dt><dd><code>'
            +escape(', '.join(result['effective_scopes']) or '—')+'</code></dd></dl>'
            '<p>Название приложения сообщает сам клиент. Место выполнения чата сервер не определяет.</p>'
            '<details><summary>Какие инструменты разрешены этим набором прав</summary><ul>'+tools+'</ul>'
            '<p>Это серверные разрешения. Отображение инструментов в другом чате и доступ к конкретным данным проверяются отдельно.</p></details>')
    body += ('<p><a class="button" href="'+escape(refresh,quote=True)+'">Обновить результат</a> '
             '<a href="'+PATH+'">Начать новую проверку</a></p></section>'
             '<section class="panel"><h2>3. Если вызова нет</h2><ol>'
             '<li>Проверьте, что в вашем ИИ-приложении включено MCP-подключение Платформы.</li>'
             '<li>Пройдите вход через свой корпоративный Битрикс24. Веб-панель и MCP требуют отдельных сессий.</li>'
             '<li>Если ассистент пишет, что инструментов нет, проверьте выбранное подключение и среду этого чата.</li>'
             '<li>Передайте администратору метку и точный текст ошибки без токенов и паролей. '
             'При успехе приложите идентификатор серверного запроса.</li></ol>'
             '<p>Отсутствие записи не доказывает неисправность сервера: запрос мог не отправиться, '
             'быть отклонён до вызова инструмента или прийти под другой учётной записью.</p></section>')
    return body


def register_connection_diagnostics_routes(mcp):
    @mcp.custom_route(PATH,methods=['GET'],include_in_schema=False)
    async def connection_page(request):
        if not corporate_access.enabled():
            return PlainTextResponse('Not found',status_code=404)
        actor = web_actor(request)
        if actor is None:
            return login_redirect(PATH)
        if not has_scope(actor,'skills:read'):
            return PlainTextResponse('Access denied',status_code=403)
        items = list(request.query_params.multi_items())
        if len(items)>1 or any(key!='check' for key,_ in items):
            return _response(actor,'<h1>Некорректная ссылка проверки</h1>',400)
        try:
            mark = service.validate_mark(items[0][1]) if items else service.new_mark()
        except ValueError:
            return _response(actor,'<h1>Метка проверки недействительна или устарела</h1><p><a href="'+PATH+'">Начать новую проверку</a></p>',400)
        try:
            result = await to_thread(service.read_probe,actor,mark) if items else None
        except Exception:
            return _response(actor,'<h1>Журнал проверок временно недоступен</h1><p>Повторите проверку позже. '
                'Содержимое внутренних ошибок не показывается.</p>',503)
        return _response(actor,_render(actor,mark,result))
