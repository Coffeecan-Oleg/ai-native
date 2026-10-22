"""Read-only browser views over the same fictional snapshot/overlay as MCP."""
from asyncio import to_thread
from dataclasses import replace
from html import escape
from urllib.parse import urlencode

from starlette.exceptions import HTTPException
from starlette.responses import HTMLResponse, PlainTextResponse

from gateway_mcp.backends.common import BackendRouteError
from gateway_mcp.backends.platforma_demo import _active_store, _context, _validate_arguments, ROUTES
from gateway_mcp.backends.platforma_demo_store import QueryError, STATUS_LABELS
from gateway_mcp.config import read_json, tools_file
from gateway_mcp.routes.admin_ui import admin_shell, empty_row
from gateway_mcp.services import addon_guard, platforma_onboarding as onboarding
from gateway_mcp.services.access import require_resource_access
from gateway_mcp.services.observability import audit_event
from gateway_mcp.services.policy import has_scope
from gateway_mcp.web import login_redirect, web_actor

LIST_PATH = '/platforma/requests'
DETAIL_PATH = LIST_PATH + '/view'
FILTERS = {'query', 'unit_id', 'service_id', 'period', 'status', 'priority',
           'due_from', 'due_to', 'overdue_only', 'sort', 'limit', 'offset'}
CSS = '''<style>
.request-head{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.request-facts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 28px;margin:0}
.request-facts>div{padding:12px 0;border-bottom:1px solid var(--color-border);min-width:0}
.request-facts dt{font-size:12px;color:var(--color-muted);margin-bottom:6px}
.request-facts dd{margin:0;overflow-wrap:anywhere;white-space:pre-wrap}
.request-table{min-width:1160px;table-layout:auto}.request-table .request-title{display:block;margin-top:6px;max-width:340px}
.request-table th,.request-table td:nth-child(n+3){white-space:nowrap;overflow-wrap:normal}
.request-source{white-space:pre-wrap;overflow-wrap:anywhere}.request-filter-wide{grid-column:span 2}
.request-toggles{display:flex;align-items:center;gap:10px}.request-toggles input{width:20px;min-height:20px}
.request-filters{padding:20px;background:var(--color-surface);border:1px solid var(--color-border);border-radius:var(--radius-panel)}
.request-filter-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:16px}
.request-filters .audit-filter-actions{grid-column:span 2;flex-wrap:wrap}
.request-filters .audit-filter-actions>*{white-space:nowrap;flex-shrink:0}
.request-advanced{margin-top:20px}.request-advanced summary{cursor:pointer;font-size:13px;color:var(--color-ink-soft)}
.request-advanced[open] summary{margin-bottom:16px}
@media(max-width:1040px){.request-filter-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:760px){.request-facts{grid-template-columns:minmax(0,1fr)}}
@media(max-width:560px){.request-filter-grid{grid-template-columns:minmax(0,1fr)}.request-filter-wide,.request-filters .audit-filter-actions{grid-column:auto}
.request-table{display:table}.request-table thead{display:table-header-group}.request-table tbody{display:table-row-group}.request-table tr{display:table-row}.request-table th,.request-table td{display:table-cell;width:auto}}
</style>'''


def _escape(value):
    return escape(str(value if value is not None else '—'), quote=True)


def _date(value):
    return value[8:10] + '.' + value[5:7] + '.' + value[:4] if value else '—'


def _viewer(actor):
    if actor.tool_allowlist is not None:
        raise PermissionError('A browser session is required')
    if onboarding.is_owner(actor):
        onboarding.require_owner(actor)
        if not has_scope(actor, 'platforma:read'):
            raise PermissionError('Read scope is required')
        # A human admin session must still satisfy the sandbox read resource grant.
        return replace(actor, groups=(), scopes=('tools:call', 'platforma:read'),
                       scope_ceiling=('tools:call', 'platforma:read'),
                       tool_allowlist=tuple(sorted(set(ROUTES) | {'gateway_call_tool'})))
    actor = onboarding.oauth_reader_actor(actor)  # Rechecks approval and linked grants.
    if not has_scope(actor, 'platforma:read'):
        raise PermissionError('Read scope is required')
    return actor


def _read(actor, name, arguments):
    if name not in ROUTES or not has_scope(actor, 'platforma:read'):
        raise PermissionError('Read-only route required')
    if actor.tool_allowlist is not None and not {'gateway_call_tool', name} <= set(actor.tool_allowlist):
        raise PermissionError('Route is not allowed')
    registry = read_json(tools_file(), {'tools': []})
    route = next((r for r in registry['tools'] if r.get('name') == name), None)
    if not route or route.get('transport') != 'platforma-demo' or route.get('backend') != 'platforma' or route.get('scope') != 'platforma:read' or route.get('http_method') != 'GET':
        raise PermissionError('Read route is not configured')
    profile = addon_guard.check_call(actor, 'gateway_call_tool', {'tool_name': name})
    if profile is None:
        raise PermissionError('A training access profile is required')
    require_resource_access(actor=actor, route=route, arguments=arguments)
    _validate_arguments(arguments)
    result = _context() if name == 'platforma.context.get' else getattr(_active_store(), ROUTES[name])(**arguments)
    audit_event(event='platforma_browser_read', actor=actor, tool=name, system='platforma',
                decision='allow', status='ok', scope='platforma:read',
                arguments={'filter_names': sorted(arguments)})
    return result


def _filters(request, *, detail=False):
    allowed = FILTERS | ({'id'} if detail else set())
    if len(request.url.query) > 4096 or set(request.query_params) - allowed:
        raise QueryError('Неизвестные или слишком длинные параметры фильтра.')
    values = {}
    for key in request.query_params:
        entries = request.query_params.getlist(key)
        if len(entries) != 1 or len(entries[0]) > (200 if key == 'query' else 120):
            raise QueryError('Некорректные параметры фильтра.')
        if key != 'id' and entries[0].strip():
            values[key] = entries[0].strip()
    try:
        values['offset'] = int(values.get('offset', '0'))
        values['limit'] = int(values.get('limit', '25'))
    except ValueError:
        raise QueryError('Некорректный номер страницы.') from None
    if values['limit'] not in {25, 50, 100} or not 0 <= values['offset'] <= 100000:
        raise QueryError('Недопустимый размер или номер страницы.')
    if values.get('overdue_only', '0') not in {'0', '1'}:
        raise QueryError('Некорректный фильтр просрочки.')
    values['overdue_only'] = values.get('overdue_only') == '1'
    _validate_arguments(values)
    return values


def _url(path, values):
    params = {k: ('1' if v is True else v) for k, v in values.items() if v is not False and v is not None and v != ''}
    return path + ('?' + urlencode(params) if params else '')


def _select(label, name, options, filters, default=''):
    selected = str(filters.get(name, default))
    choices = ''.join('<option value="' + _escape(value) + '"' + (' selected' if str(value) == selected else '') + '>' + _escape(text) + '</option>' for value, text in options)
    return '<label>' + label + '<select name="' + name + '">' + choices + '</select></label>'


def _form(context, services, filters):
    controls = '<label class="request-filter-wide">Поиск<input name="query" maxlength="200" value="' + _escape(filters.get('query', '')) + '" placeholder="ID, тема, сервис или ответственный"></label>'
    controls += _select('Период', 'period', [('all', 'Все периоды')] + [(p, p) for p in reversed(context['periods'])], filters, context['default_period'])
    controls += _select('Подразделение', 'unit_id', [('', 'Все подразделения')] + [(r['id'], r['name']) for r in context['units']], filters)
    controls += _select('Статус', 'status', [('', 'Все статусы'), ('active', 'В работе'), ('completed', 'Все завершённые')] + [(s, t) for s, t in STATUS_LABELS.items() if s != 'active'], filters)
    controls += _select('Приоритет', 'priority', [('', 'Все приоритеты')] + [(p, p) for p in ('Обычный', 'Высокий', 'Критичный')], filters)
    controls += '<div class="audit-filter-actions"><button type="submit">Применить</button><a class="button secondary" href="' + LIST_PATH + '">Сбросить</a></div>'
    advanced = _select('Сервис', 'service_id', [('', 'Все сервисы')] + [(r['id'], r['name']) for r in services['records']], filters)
    advanced += _select('Сортировка', 'sort', [('due_asc', 'Срок: сначала ближайшие'), ('due_desc', 'Срок: сначала поздние'), ('created_desc', 'Создание: сначала новые'), ('id', 'По ID')], filters, 'due_asc')
    for key, label in [('due_from', 'Срок с'), ('due_to', 'Срок по')]:
        advanced += '<label>' + label + '<input type="date" name="' + key + '" value="' + _escape(filters.get(key, '')) + '"></label>'
    advanced += _select('На странице', 'limit', [(n, str(n)) for n in (25, 50, 100)], filters, '25')
    advanced += '<label class="request-toggles"><input type="checkbox" name="overdue_only" value="1"' + (' checked' if filters.get('overdue_only') else '') + '>Только просроченные в работе</label>'
    is_open = any(filters.get(k) for k in ('service_id','due_from','due_to','overdue_only')) or filters.get('sort','due_asc') != 'due_asc' or filters['limit'] != 25
    return '<form class="request-filters" action="' + LIST_PATH + '" method="get"><div class="request-filter-grid">' + controls + '</div><details class="request-advanced"' + (' open' if is_open else '') + '><summary>Дополнительные фильтры: сервис, сроки и просрочка</summary><div class="request-filter-grid">' + advanced + '</div></details></form>'


def _status(record):
    css = 'missing' if record['is_overdue'] else 'warning' if record['status'] == 'late' else 'neutral' if record['status'] == 'active' else 'ok'
    return '<span class="status ' + css + '">' + _escape(record['status_label']) + '</span>' + ('<span class="table-sub">Просрочена</span>' if record['is_overdue'] else '')


def _table(page, filters):
    rows = []
    for row in page['records']:
        link = _escape(_url(DETAIL_PATH, {**filters, 'id': row['id']}))
        rows.append('<tr><td><a href="' + link + '">' + _escape(row['id']) + '</a><span class="request-title">' + _escape(row['title']) + '</span></td>' +
                    '<td>' + _escape(row['unit_name']) + '<span class="table-sub">' + _escape(row['service_name']) + '</span></td>' +
                    '<td>' + _status(row) + '</td><td>' + _escape(row['priority']) + '</td><td>' + _escape(row['assignee_name']) + '</td>' +
                    '<td>' + _date(row['created_at']) + '</td><td>' + _date(row['due_at']) + '</td><td>' + str(row.get('revision', 0)) + '</td></tr>')
    pager = ''
    if page['offset']:
        pager += '<a class="button secondary" href="' + _escape(_url(LIST_PATH, {**filters, 'offset': max(0, page['offset'] - page['limit'])})) + '">Назад</a>'
    if page['has_more']:
        pager += '<a class="button secondary" href="' + _escape(_url(LIST_PATH, {**filters, 'offset': page['next_offset']})) + '">Далее</a>'
    count = str(page['total'])
    visible = str(page['offset'] + 1) + '–' + str(page['offset'] + page['returned']) if page['returned'] else '0'
    return '<section class="panel wide"><div class="panel-header"><h2>Найдено: ' + count + '</h2><span class="muted">Показано ' + visible + ' из ' + count + '</span></div><div class="table-wrap" role="region" aria-label="Таблица учебных заявок" tabindex="0"><table class="request-table"><thead><tr><th>Заявка</th><th>Подразделение / сервис</th><th>Статус</th><th>Приоритет</th><th>Ответственный</th><th>Создана</th><th>Срок</th><th>Версия</th></tr></thead><tbody>' + (''.join(rows) or empty_row(8, 'Заявок по этим фильтрам нет. Измени фильтры или нажми «Сбросить».')) + '</tbody></table></div><div class="audit-pager">' + pager + '</div></section>'


def _detail(data, filters):
    row = data['record']
    fields = [('Подразделение', row['unit_name']), ('Сервис', row['service_name']), ('Заказчик (учебный)', row['customer_name']),
              ('Ответственный (учебный)', row['assignee_name']), ('Приоритет', row['priority']), ('Создана', _date(row['created_at'])),
              ('Срок', _date(row['due_at'])), ('Завершена', _date(row['accepted_at'])), ('Версия', row.get('revision', 0)),
              ('Происхождение', 'Создана или изменена в песочнице' if row.get('sandbox_modified') else 'Исходный учебный снимок'),
              ('Трудозатраты, ч', row['effort_hours']), ('Внешний ID', row['external_id'] or '—'),
              ('Причина / комментарий', row['reason'] or '—'), ('Следующее действие', row['next_action'] or '—')]
    facts = ''.join('<div><dt>' + label + '</dt><dd>' + _escape(value) + '</dd></div>' for label, value in fields)
    return '<div class="page-head"><div><p class="muted">' + _escape(row['id']) + '</p><h1>' + _escape(row['title']) + '</h1></div><a class="button secondary" href="' + _escape(_url(LIST_PATH, filters)) + '">К списку с фильтрами</a></div><section class="panel"><div class="request-head">' + _status(row) + '</div><dl class="request-facts">' + facts + '</dl><p class="small request-source">Источник: <code>' + _escape(row['source']) + '</code></p></section>'


def _load(actor, filters, request_id):
    actor = _viewer(actor)
    if request_id is not None:
        return actor, _read(actor, 'platforma.requests.get', {'request_id': request_id}), None, None
    context = _read(actor, 'platforma.context.get', {})
    services = _read(actor, 'platforma.services.list', {'unit_id': filters.get('unit_id'), 'limit': 100})
    page = _read(actor, 'platforma.requests.list', filters)
    return actor, page, context, services


def _response(actor, body, status=200, *, reader=False):
    response = HTMLResponse(admin_shell(title='Учебные заявки', active='training-requests', actor=actor,
                            body=body, shell_width='1440px', extra_head=CSS, training_reader=reader), status_code=status)
    response.headers.update({'Cache-Control': 'no-store', 'Referrer-Policy': 'same-origin', 'X-Frame-Options': 'DENY',
        'X-Content-Type-Options': 'nosniff', 'Content-Security-Policy': "default-src 'none'; style-src 'self' 'unsafe-inline'; img-src 'self'; font-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"})
    return response


def register_platforma_request_routes(mcp):
    async def page(request):
        if not onboarding.enabled():
            return PlainTextResponse('Not found', status_code=404)
        try:
            actor = web_actor(request)
        except HTTPException:
            return PlainTextResponse('Доступ к учебным заявкам закрыт. Статус доступа: /join', status_code=403)
        if actor is None:
            return login_redirect(DETAIL_PATH + '?' + urlencode({'id': request.query_params.get('id', '')}) if request.url.path == DETAIL_PATH else LIST_PATH)
        reader = not onboarding.is_owner(actor)
        try:
            detail = request.url.path == DETAIL_PATH
            filters = _filters(request, detail=detail)
            identifier = request.query_params.get('id') if detail else None
            if detail and (not identifier or len(identifier) > 120):
                raise QueryError('Нужен корректный ID заявки.')
            viewer, data, context, services = await to_thread(_load, actor, filters, identifier)
            body = _detail(data, filters) if detail else '<div class="page-head"><div><h1>Учебные заявки</h1><p class="lead">Просмотр вымышленных сервисных обращений.</p></div><a class="button secondary" href="' + _escape(_url(LIST_PATH, {'query': 'ТЕСТ', 'period': 'all', 'sort': 'created_desc'})) + '">Найти «ТЕСТ»</a></div>'
            notice = '<div class="banner">Вымышленные данные. Учебный снимок: <strong>' + _date(data['snapshot_as_of']) + '</strong>. Просрочка считается на эту дату. Только просмотр; создание и изменение — через MCP. Обнови страницу, чтобы увидеть последние правки.</div>'
            body += notice
            if not detail:
                body += _form(context, services, filters) + _table(data, filters)
            return _response(viewer if reader else actor, body, reader=reader)
        except PermissionError:
            audit_event(event='platforma_browser_read', actor=actor, system='platforma', decision='deny', status='denied')
            return _response(actor, '<h1>Доступ закрыт</h1><p>Проверь аккаунт и <a href="/join">статус доступа</a>. Данные не показаны.</p>', 403, reader=reader)
        except (QueryError, BackendRouteError) as error:
            missing = str(error).startswith('REQUEST_UNAVAILABLE')
            body = '<h1>' + ('Заявка не найдена' if missing else 'Проверь фильтры') + '</h1><p>' + _escape(str(error)) + '</p><a class="button secondary" href="' + LIST_PATH + '">К списку заявок</a>'
            return _response(actor, body, 404 if missing else 400, reader=reader)
        except Exception as error:
            audit_event(event='platforma_browser_error', actor=actor, system='platforma', status='error', error=type(error).__name__)
            return _response(actor, '<h1>Временно недоступно</h1><p>Не удалось безопасно загрузить учебные заявки. Попробуй позже.</p>', 503, reader=reader)
    for path in (LIST_PATH, DETAIL_PATH):
        mcp.custom_route(path, methods=['GET'], include_in_schema=False)(page)
