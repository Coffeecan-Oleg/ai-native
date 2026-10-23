"""Read-only training views under /admin, without granting administrator scopes."""
from asyncio import to_thread
from datetime import timezone
from html import escape
from urllib.parse import urlencode, urlsplit

from starlette.responses import HTMLResponse

from gateway_mcp.config import public_url, read_json, tools_file
from gateway_mcp.routes.admin_ui import admin_shell, empty_row, fmt_time
from gateway_mcp.services import platforma_onboarding as onboarding
from gateway_mcp.services.storage import list_audit_events
from gateway_mcp.services.telemetry import skill_stats

HEADERS = {
    'Cache-Control': 'no-store', 'Referrer-Policy': 'same-origin',
    'X-Frame-Options': 'DENY', 'X-Content-Type-Options': 'nosniff',
    'Content-Security-Policy': "default-src 'none'; style-src 'self' 'unsafe-inline'; img-src 'self'; font-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
}
CSS = '''<style>
.training-flow{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;list-style:none;padding:0;margin:22px 0}
.training-flow li,.training-card{padding:20px;background:var(--color-surface-muted);border-radius:8px;min-width:0}
.training-flow strong{display:block;margin-bottom:8px;color:var(--color-accent)}
.training-cards{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px;margin:20px 0}
.training-note{border-left:4px solid var(--color-accent);padding:16px 20px;background:var(--color-surface-muted);margin:18px 0}
.training-reading{max-width:920px}.training-reading p,.training-reading li,.training-card p{line-height:1.7}
.training-code{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;padding:16px;background:var(--color-surface-muted);border-radius:8px}
.training-table td,.training-table th{overflow-wrap:anywhere}.training-table code{white-space:normal}
.training-links{display:flex;flex-wrap:wrap;gap:16px}.training-filters{display:flex;flex-wrap:wrap;align-items:end;gap:12px}
@media(max-width:850px){.training-flow,.training-cards{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:500px){.training-flow,.training-cards{grid-template-columns:minmax(0,1fr)}}
</style>'''

ROUTE_EXPLANATIONS = {
    'platforma.context.get': 'Посмотреть состав учебных данных, дату среза, подразделения и доступные периоды.',
    'platforma.services.list': 'Найти учебные сервисы и получить список с ограниченным числом строк.',
    'platforma.services.get': 'Открыть один учебный сервис по его идентификатору.',
    'platforma.requests.list': 'Найти учебные заявки по условиям. Просрочка считается относительно даты учебного среза.',
    'platforma.requests.get': 'Прочитать карточку одной учебной заявки.',
    'platforma.requests.create': 'Создать вымышленную заявку по учебному шаблону. Запись остаётся в песочнице.',
    'platforma.requests.update': 'Изменить разрешённые поля вымышленной заявки с проверкой её текущей версии.',
    'platforma.unisender-demo.stats.get': 'Прочитать сводные показатели одной вымышленной кампании LAB-UNISENDER-001.',
}


def e(value):
    return escape('' if value is None else str(value), quote=True)


def is_training_visitor(actor):
    return onboarding.enabled() and not onboarding.is_owner(actor)


def _page(actor, title, active, body, status=200):
    return HTMLResponse(admin_shell(title=title, active=active, actor=actor,
        body=body, training_reader=True, extra_head=CSS, shell_width='1320px'),
        status_code=status, headers=HEADERS)


async def _approved(actor, *, participant=False):
    # The browser cookie supplies identity only. Never trust cached admin groups
    # or scopes, and recheck live approvals/linked grants on each request.
    if actor.tool_allowlist is not None or not is_training_visitor(actor):
        raise PermissionError('Personal training browser session required')
    lookup = onboarding.active_participant if participant else onboarding.active_reader
    row = await to_thread(lookup, actor.subject)
    if row is None:
        raise PermissionError('Active training approval required')
    return row


def _failure(actor, exc):
    denied = isinstance(exc, PermissionError)
    return _page(actor, 'Обзор MCP', 'training-overview',
        '<h1>'+('Доступ к разделу недоступен' if denied else 'Раздел временно недоступен')+
        '</h1><p>Проверь <a href="/join">статус доступа</a> или повтори запрос позже.</p>',
        403 if denied else 503)


def _mcp_address():
    url = urlsplit(public_url())
    if url.scheme not in {'https', 'http'} or not url.hostname or url.username or url.password:
        return '/mcp'
    return url.scheme+'://'+url.netloc+'/mcp'


def render_overview(actor, approval, profile, registry, *, participant):
    resources = profile.get('resources', [])
    reads = {r['id'] for r in resources if r.get('kind')=='backend_route' and r.get('operations')==['read']}
    writes = {r['id'] for r in resources if r.get('kind')=='backend_route' and r.get('operations')==['write']}
    tools = sorted(r['id'].removeprefix('mcp:') for r in resources if r.get('kind')=='tool')
    routes = []
    for row in registry.get('tools', []):
        if row.get('name') not in reads | writes:
            continue
        name = row['name']
        explanation = ROUTE_EXPLANATIONS.get(name, row.get('description', ''))
        technical = '<details><summary>Техническое описание</summary>'+e(row.get('description'))+'</details>'
        routes.append('<tr><td><code>'+e(name)+'</code></td><td>'+e(explanation)+technical+
            '</td><td>'+('Создание / изменение учебных данных' if name in writes else 'Чтение')+'</td></tr>')
    tools_html = ''.join('<li><code>'+e(name)+'</code></li>' for name in tools)
    telemetry_link = '<a href="/admin/telemetry/skills">Мои запуски навыков</a>' if participant else ''
    return '''<header class="page-head"><div><span class="status neutral">Учебная песочница · режим наблюдения</span>
<h1>Обзор MCP</h1><p class="lead">Как ваше ИИ-приложение получает доступ к навыкам, данным и инструментам Платформы.</p></div></header>
<section class="panel training-reading"><h2>От запроса до результата</h2>
<ol class="training-flow"><li><strong>1. Ваше приложение</strong>Вы ставите задачу в чате. Ассистент выбирает навык и инструмент.</li>
<li><strong>2. Подключение MCP</strong>Передаёт разрешённый вызов от вашего имени после личного входа.</li>
<li><strong>3. Gateway Платформы</strong>Проверяет одобрение, срок, права и параметры; фиксирует результат в журнале.</li>
<li><strong>4. Инструмент и ответ</strong>Выполняет конкретную операцию. Ассистент показывает полученные сведения и источник.</li></ol>
<p><strong>Навык</strong> — инструкция для ассистента. <strong>Инструмент</strong> — серверная функция. <strong>Данные</strong> — сведения, с которыми она работает. Чтение навыка само по себе не подключает сервис и не запускает фоновую работу.</p>
<p>Адрес MCP этого стенда: <code>'''+e(_mcp_address())+'''</code>.</p>
<div class="training-note"><strong>Ваш учебный доступ действует до '''+e(approval['approved_until'].astimezone(timezone.utc).strftime('%d.%m.%Y %H:%M'))+''' UTC.</strong>
На этой странице показаны разрешения Платформы для вашего аккаунта. Ранее выданное подключение MCP может хранить более узкий список. Если инструмент найден, но отклонён сообщением OAuth allowlist, заново пройдите личный вход в подключение; обычное обновление токена сохраняет старые ограничения.</div></section>
<section class="training-cards"><article class="training-card"><h2>1. Прочитать навык</h2><p>В каталоге выберите понятное название, откройте карточку и скопируйте готовый запрос своему ИИ. Попросите прочитать инструкцию и применить её к вымышленной задаче.</p><a href="/platforma/skills">Каталог навыков</a></article>
<article class="training-card"><h2>2. Проверить инструмент</h2><p>Попросите ИИ через MCP прочитать учебную заявку или сводку кампании LAB-UNISENDER-001. Реальный UniSender пока не подключён.</p><a href="/platforma/requests">Учебные заявки</a></article>
<article class="training-card"><h2>3. Найти результат</h2><p>В журнале проверьте время, результат и ID своего вызова. Учёт навыка появится, если клиент передал события его начала и завершения. Чтение файла не считается автоматически успешным запуском навыка.</p><a href="/admin/audit">Мои обращения к MCP</a><p>'''+telemetry_link+'''</p></article></section>
<section class="panel"><h2>Операции, разрешённые вашим учебным профилем</h2><p>Только сведения из общего учебного профиля. Перечень не запускает операции и не проверяет настройки вашего приложения.</p>
<div class="table-wrap"><table class="training-table"><thead><tr><th>Имя операции</th><th>Что делает</th><th>Действие</th></tr></thead><tbody>'''+(''.join(routes) or empty_row(3))+'''</tbody></table></div>
<details><summary>Имена инструментов MCP для вашего ассистента</summary><ul>'''+tools_html+'''</ul></details></section>
<section class="panel training-reading"><h2>Первый самостоятельный тест</h2><p>Отправьте в своё ИИ-приложение:</p>
<pre class="training-code">Через MCP Платформы покажи доступные мне навыки: названия, ID и назначение. Используй серверный каталог. Пока ничего не запускай. При отказе покажи точную ошибку.</pre>
<p>Затем выберите один навык, попросите прочитать его инструкцию и применить к учебному примеру. Проверьте результат и собственный журнал.</p>
<div class="training-links"><a href="/platforma/how-it-works">Памятка и подключение своего сервиса</a><a href="/join">Статус моего доступа</a></div>
<p>Настройки пользователей, секреты подключений и общая статистика доступны владельцу стенда. Учебный обзор показывает устройство системы и ваш собственный результат работы.</p></section>'''


async def overview_response(actor):
    try:
        approval = await _approved(actor)
        profile = await to_thread(onboarding.reader_profile, actor)
        participant = await to_thread(onboarding.active_participant, actor.subject) is not None
        registry = await to_thread(read_json, tools_file(), {'tools': []})
        body = render_overview(actor, approval, profile, registry, participant=participant)
        return _page(actor, 'Обзор MCP', 'training-overview', body)
    except Exception as exc:
        return _failure(actor, exc)


def _days(request):
    return int(request.query_params.get('days', '7')) if request.query_params.get('days', '7') in {'1', '7', '30'} else 7


def _filters(path, days):
    options = ''.join('<option value="'+str(n)+'"'+(' selected' if n==days else '')+'>'+label+'</option>'
        for n,label in ((1,'24 часа'),(7,'7 дней'),(30,'30 дней')))
    return '<form class="training-filters" method="get" action="'+path+'"><label>Период<select name="days">'+options+'</select></label><button type="submit">Показать</button></form>'


def _audit_result(row):
    payload = row.get('payload') or {}
    reason = str(payload.get('error') or '') if isinstance(payload, dict) else ''
    if reason.startswith('tool is outside delegated OAuth allowlist:'):
        return 'Подключение OAuth не разрешает эту операцию. Пройдите новый личный вход.'
    return {'ok':'Выполнено','allowed':'Разрешено','denied':'Отказ в доступе','error':'Ошибка выполнения',
            'failed':'Не выполнено','blocked':'Заблокировано'}.get(str(row.get('status')), 'Событие зарегистрировано')


async def audit_response(request, actor):
    try:
        await _approved(actor)
        days = _days(request)
        # User-supplied actor, project, event, request ID and payload options are
        # never forwarded. This query always selects this exact authenticated subject.
        rows = await to_thread(list_audit_events, days=days, actor_subject=actor.subject, limit=51, include_payload=True)
        rendered = []
        for row in rows[:50]:
            payload = row.get('payload') or {}
            arguments = payload.get('arguments') or {} if isinstance(payload, dict) else {}
            route = arguments.get('tool_name', '') if isinstance(arguments, dict) else ''
            rendered.append('<tr><td>'+e(fmt_time(row.get('created_at')))+' UTC</td><td>'+e(row.get('tool') or row.get('event') or 'Служебное событие')+
                ('<br><code>'+e(route)+'</code>' if route else '')+'</td><td>'+e(_audit_result(row))+
                '</td><td><code>'+e(row.get('gateway_request_id'))+'</code></td></tr>')
        body = '<h1>Мои обращения к MCP</h1><p class="lead">Только события вашего аккаунта. По ним видно, дошёл ли вызов до сервера и был ли разрешён.</p>'+_filters('/admin/audit', days)+\
            '<section class="panel"><div class="table-wrap"><table class="training-table"><thead><tr><th>Время</th><th>Инструмент / операция</th><th>Результат</th><th>ID запроса</th></tr></thead><tbody>'+(''.join(rendered) or empty_row(4, 'Вызовы за этот период не найдены'))+'</tbody></table></div></section>'
        if len(rows)>50:body+='<p>Показаны 50 последних событий. Выберите меньший период, чтобы разобрать недавнюю проверку.</p>'
        body+='<p>Тексты заданий, аргументы, ответы и токены в этой таблице не показываются. События других пользователей недоступны.</p>'
        return _page(actor, 'Мои обращения к MCP', 'training-audit', body)
    except Exception as exc:
        return _failure(actor, exc)


async def skill_response(request, actor):
    try:
        await _approved(actor, participant=True)
        days = _days(request)
        profile = await to_thread(onboarding.reader_profile, actor)
        project = profile.get('skill_telemetry', {}).get('project')
        if project != 'platforma-training':
            raise PermissionError('Training telemetry project is not configured')
        result = await to_thread(skill_stats, days=days, limit=100, actor_subject=actor.subject, project=project)
        rendered = []
        for row in result.get('skills', []):
            number = lambda key: e(max(0, int(row.get(key) or 0)))
            rendered.append('<tr><td>'+e(row.get('skill_id'))+'</td><td>'+e(row.get('agent'))+'</td><td>'+number('started_count')+
                '</td><td>'+number('completed_count')+'</td><td>'+number('failed_count')+'</td><td>'+number('running_count')+'</td></tr>')
        body = '<h1>Мои запуски навыков</h1><p class="lead">Ваш собственный учёт в учебном проекте platforma-training.</p>'+_filters('/admin/telemetry/skills', days)+\
            '<section class="panel"><div class="table-wrap"><table class="training-table"><thead><tr><th>Навык</th><th>Приложение</th><th>Начато</th><th>Завершено</th><th>Ошибки</th><th>В работе</th></tr></thead><tbody>'+(''.join(rendered) or empty_row(6, 'Запуски пока не зарегистрированы'))+'</tbody></table></div></section>'
        body+='<div class="training-note">Чтение навыка и применение навыка — разные действия. Для этой таблицы приложение должно явно передать события started и completed либо failed через доступные инструменты учёта. Таблица не измеряет стоимость или токены и не подтверждает качество результата.</div>'
        return _page(actor, 'Мои запуски навыков', 'training-telemetry', body)
    except Exception as exc:
        return _failure(actor, exc)
