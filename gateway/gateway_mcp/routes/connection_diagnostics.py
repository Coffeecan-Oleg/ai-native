"""Browser identity is not proof of an MCP call in another application."""
from asyncio import to_thread
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path as FilePath
import re
from urllib.parse import urlencode, urlsplit

from starlette.responses import FileResponse, HTMLResponse, PlainTextResponse
from gateway_mcp.config import public_url, release_sha
from gateway_mcp.routes.admin_ui import admin_shell
from gateway_mcp.services import corporate_access
from gateway_mcp.services import connection_diagnostics as service
from gateway_mcp.services.policy import has_scope
from gateway_mcp.web import login_redirect, web_actor

PATH = '/platforma/connection'
REPORT_PATH = PATH + '/report'
CLIENT_PATH = PATH + '/download.js'
REPORT_HEADERS = {'Cache-Control':'no-store', 'Referrer-Policy':'no-referrer',
                  'X-Content-Type-Options':'nosniff'}


def _time(value):
    try:
        stamp = value if isinstance(value,datetime) else datetime.fromisoformat(str(value).replace('Z','+00:00'))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.astimezone(timezone(timedelta(hours=3))).strftime('%d.%m.%Y %H:%M МСК')
    except (ValueError,TypeError):
        return '—'


def _response(actor, body, status=200):
    return HTMLResponse(admin_shell(title='Проверить подключение',active='connection-check',actor=actor,body=body,
                        extra_head='<script defer src="'+CLIENT_PATH+'"></script>'),
                        status_code=status,headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer',
                        'X-Content-Type-Options':'nosniff'})


def _report_context():
    # Configuration only, never Host/Forwarded headers, callback URLs or env dumps.
    origin = 'Не указан'
    try:
        parsed = urlsplit(public_url())
        if (parsed.scheme in ('http', 'https') and parsed.hostname
                and parsed.username is None and parsed.password is None
                and re.fullmatch(r'[A-Za-z0-9.:-]+', parsed.hostname)):
            host = '['+parsed.hostname+']' if ':' in parsed.hostname else parsed.hostname
            origin = parsed.scheme+'://'+host+(':'+str(parsed.port) if parsed.port else '')
    except ValueError:
        pass
    revision = release_sha()
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        revision = 'Локальная разработка' if revision == 'dev' else 'Не определён'
    return origin, revision


def _report_text(mark, result):
    # Only the subject-bound, sanitized read_probe result belongs in this file.
    origin, revision = _report_context()
    lines = ['Платформа Акметрон — проверка подключения',
             'Файл сформирован: '+_time(datetime.now(timezone.utc)),
             'Сервер Платформы: '+origin,
             'Версия выпуска при подготовке отчёта: '+revision,
             'Метка проверки: '+mark, '']
    if result is None:
        lines += ['Результат: вызов MCP с этой меткой от вашей учётной записи пока не найден.',
                  'Это не подтверждает неисправность сервера. Запрос мог не отправиться,',
                  'быть отклонён до вызова инструмента или прийти под другой учётной записью.',
                  'Дождитесь ответа ассистента, обновите результат и скачайте файл ещё раз.']
    else:
        client = result['client']
        label = client['name'] if client['name'] != 'unknown' else 'Не определено'
        if client['version']:
            label += ' '+client['version']
        lines += ['Результат: вызов MCP получен от вашей учётной записи.',
                  'Время вызова: '+_time(result['checked_at']),
                  'ID серверного запроса: '+(result['gateway_request_id'] or 'Не указан'),
                  'Приложение: '+label,
                  'Права при вызове: '+(', '.join(result['effective_scopes']) or 'Не указаны'),
                  'Разрешённые инструменты: '+(', '.join(result['allowed_tools']) or 'Не указаны'),
                  '', 'Название и версию приложения сообщает сам клиент.',
                  'Место выполнения чата сервер не определяет.',
                  'Эта проверка подтверждает только вызов gateway_access_profile.',
                  'Доступ к конкретным данным и результат применения навыка проверяются отдельно.']
    lines += ['', 'Что передать в поддержку',
              'Приложите этот файл. Отдельно напишите, что хотели сделать, на каком шаге',
              'возникла проблема и какой текст ошибки показало ваше приложение.',
              'Не добавляйте пароли, токены, ключи и полные ссылки авторизации.',
              'Файл не отправляется автоматически. Это снимок результата на указанное время.']
    return '\n'.join(lines)+'\n'


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
    download = REPORT_PATH+'?'+urlencode({'check':mark})
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
             '<a href="'+PATH+'">Начать новую проверку</a></p>'
             '<p><button type="button" id="connection-report-prepare" data-report-url="'
             +escape(download,quote=True)+'" aria-describedby="connection-report-status">Подготовить файл (.txt)</button></p>'
             '<p id="connection-report-status" role="status" aria-live="polite" aria-atomic="true">'
             'Сначала подготовьте файл, затем нажмите появившуюся ссылку скачивания.</p>'
             '<p><a id="connection-report-download" hidden download="platforma-connection.txt">'
             'Скачать готовый файл (.txt)</a></p>'
             '<details id="connection-report-preview" hidden><summary>Показать текст отчёта</summary>'
             '<p id="connection-report-copy-help">Если файл не скачивается, нажмите на текст ниже, '
             'выделите его целиком и скопируйте в обращение в поддержку. '
             'Текст совпадает с подготовленным файлом.</p>'
             '<label for="connection-report-text">Текст вашей проверки</label>'
             '<textarea id="connection-report-text" readonly rows="12" '
             'aria-describedby="connection-report-copy-help" '
             'style="box-sizing:border-box;width:100%;font:inherit;line-height:1.5"></textarea></details>'
             '<noscript><p>Для подготовки файла включите JavaScript в браузере. '
             'При обращении в поддержку можно скопировать результат с этой страницы.</p></noscript>'
             '<p>Если нужна помощь, приложите файл к обращению в поддержку. Он содержит результат только вашей проверки '
             'и не отправляется автоматически. Перед скачиванием обновите результат.</p></section>'
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
    @mcp.custom_route(CLIENT_PATH,methods=['GET'],include_in_schema=False)
    async def connection_download_client(request):
        # Fixed public script only. No session, report, user data or proxy URL.
        if not corporate_access.enabled():
            return PlainTextResponse('Not found',status_code=404,headers=REPORT_HEADERS)
        return FileResponse(FilePath(__file__).resolve().parents[1]/'help_assets'/'connection-download.js',
            media_type='application/javascript',headers=REPORT_HEADERS)

    @mcp.custom_route(REPORT_PATH,methods=['GET'],include_in_schema=False)
    async def connection_report(request):
        def problem(message,status):
            return PlainTextResponse(message,status_code=status,headers=REPORT_HEADERS)
        if not corporate_access.enabled():
            return problem('Not found',404)
        actor = web_actor(request)
        if actor is None:
            response = login_redirect(PATH)
            response.headers.update(REPORT_HEADERS)
            return response
        if not has_scope(actor,'skills:read'):
            return problem('Access denied',403)
        items = list(request.query_params.multi_items())
        if len(items)!=1 or items[0][0]!='check':
            return problem('Некорректная ссылка проверки. Начните новую проверку в панели.',400)
        try:
            mark = service.validate_mark(items[0][1])
        except ValueError:
            return problem('Метка проверки недействительна или устарела. Начните новую проверку в панели.',400)
        try:
            result = await to_thread(service.read_probe,actor,mark)
            body = _report_text(mark,result)
        except Exception:
            return problem('Журнал проверок временно недоступен. Повторите позже.',503)
        return PlainTextResponse(body,headers={**REPORT_HEADERS,
            'Content-Disposition':'attachment; filename="platforma-connection.txt"'})

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
