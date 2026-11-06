"""Personal read/application timeline; no cross-employee filters or writes."""
from asyncio import to_thread
from starlette.responses import PlainTextResponse
from gateway_mcp.routes import platforma_skills as ui
from gateway_mcp.routes.connection_diagnostics import _time
from gateway_mcp.services import skill_activity,skill_capabilities

PATH=ui.PATH+'/activity'
LABELS={'server_read':'Сервер: прочитан SKILL.md','client_started':'Клиент: сообщил о начале',
    'client_completed':'Клиент: сообщил о завершении','client_failed':'Клиент: сообщил об ошибке'}


def capability_panel(report):
    sources={'reviewed_registry':'Проверенный список для этой редакции',
        'author_declaration':'Список операций, заявленный автором','registry_stale':'Содержимое изменилось: прежний список требует пересмотра',
        'requirements_missing':'Для навыка ещё не указан список операций','requirements_invalid':'Список операций некорректен'}
    labels={'assistant_task':'Работа ассистента','route_allowed':'Маршрут разрешён','missing_scope':'Не хватает права',
        'token_ceiling':'Ограничение подключения','not_connected':'Не подключено','access_denied':'Недоступно',
        'unavailable':'Проверка недоступна'}
    body='<section class="panel"><h2>Что доступно с этим навыком</h2><p>'+ui.e(sources[report['requirements_source']])+'</p>'
    for op in report['operations']:
        body+='<p><strong>'+ui.e(op['title'])+'</strong> — '+ui.e(labels[op['status']])+'.<br>'+ui.e(op['reason'])+'</p>'
    if not report['operations']:body+='<p>Это не подтверждение готовности. Автор может добавить requirements.json, затем отправить редакцию на проверку владельцу.</p>'
    return body+'<p class="muted">'+ui.e(report['notice'])+'</p></section>'


async def page(request,actor):
    if set(request.query_params)-{'skill'} or len(request.query_params.multi_items())>1:raise ValueError('Неверный фильтр.')
    skill=request.query_params.get('skill','')
    report=await to_thread(skill_activity.own_activity,actor,skill=skill)
    body='<h1>Мои чтения и запуски навыков</h1><p>Последние 50 событий вашей учётной записи за 30 дней.</p><p><a href="'+ui.PATH+'">Каталог навыков</a></p>'
    body+='<section class="panel"><p>'+ui.e(report['notice'])+'</p><form method="get"><label>Постоянный ID навыка<input name="skill" value="'+ui.e(skill)+'" maxlength="101"></label> <button>Показать</button> <a href="'+PATH+'">Все мои события</a></form></section>'
    body+='<section class="panel"><div class="table-wrap"><table class="skills-table"><thead><tr><th>Время</th><th>Событие</th><th>Навык / версия</th><th>Связь с чтением</th></tr></thead><tbody>'
    for row in report['items']:
        proof=('Чтение подтверждено сервером' if row['kind']=='server_read' else 'Совпадение версии с чтением подтверждено' if row['read_link_verified'] else 'Серверное чтение не связано')
        body+='<tr><td>'+ui.e(_time(row['created_at']))+'</td><td>'+ui.e(LABELS[row['kind']])+'</td><td><code>'+ui.e(row['skill'])+'</code><br>'+ui.e(row['version'] or 'Не указана')+'</td><td>'+ui.e(proof)
        if row['sha256']:body+='<br><code>SHA256 '+ui.e(row['sha256'])+'</code>'
        body+='</td></tr>'
    if not report['items']:body+='<tr><td colspan="4">Событий пока нет. Чтение из MCP и сообщения клиента появятся здесь после реальных вызовов.</td></tr>'
    body+='</tbody></table></div></section>'
    return ui._response(actor,body)


def register_skill_activity_routes(mcp):
    @mcp.custom_route(PATH,methods=['GET'],include_in_schema=False)
    async def activity(request):
        if not skill_activity.enabled():return PlainTextResponse('Not found',status_code=404)
        return await ui._handle(request,page)
