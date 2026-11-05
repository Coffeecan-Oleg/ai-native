"""Personal browser workflow. No new MCP write tools or executable resources."""
from asyncio import to_thread
from urllib.parse import parse_qs

from starlette.datastructures import UploadFile
from starlette.requests import Request
from starlette.responses import RedirectResponse

from gateway_mcp.routes import platforma_skills as ui
from gateway_mcp.routes.connection_diagnostics import _time
from gateway_mcp.routes.platforma_onboarding import _csrf,_hidden,_cookie
from gateway_mcp.services import skill_lifecycle as service
from gateway_mcp.services import platforma_onboarding as onboarding,skill_library
from gateway_mcp.services.observability import audit_event

PATH=ui.PATH+'/manage'


def _url(**params):return ui.url(PATH,**params)


def _form(actor,request,row,action,label,*,review=False):
    text='<form method="post" action="'+PATH+'/action">'+_hidden('csrf',_csrf(request,actor))
    text+=_hidden('skill_id',row['skill_id'])+_hidden('revision',row['revision'])+_hidden('action',action)
    if review:
        text+='<label class="skill-check"><input type="checkbox" name="review_ack" value="yes" required><span>Я проверил эту версию: назначение, разрешённые данные, инструменты и ссылки. Публикую её для сотрудников.</span></label>'
    return text+'<button>'+ui.e(label)+'</button></form>'


async def _dashboard(request,actor):
    if request.query_params:raise ValueError('Некорректный адрес.')
    rows=await to_thread(service.dashboard,actor)
    body='<h1>Мои черновики и версии</h1><p>Автор видит свои материалы; владелец — также очередь проверки. В MCP попадают только опубликованные версии.</p><p><a class="button" href="'+ui.PATH+'/upload">Новый черновик</a> <a href="'+ui.PATH+'">Общий каталог</a></p>'
    body+='<section class="panel"><div class="table-wrap"><table class="skills-table"><thead><tr><th>Название</th><th>Состояние</th><th>Версия</th><th>Автор</th><th>Действие</th></tr></thead><tbody>'
    for row in rows:
        state=service.LABELS[row['stage']]
        if row['published_revision'] is not None and row['stage'] in {'draft','review'}:
            state+=' · прежняя публикация доступна'
        body+='<tr><td>'+ui.e(row['title'])+'</td><td>'+ui.e(state)+'</td><td>'+ui.e(row['version'])+'</td><td>'+ui.e(row['author_label'])+'</td><td><a href="'+ui.e(_url(id=row['skill_id']))+'">Открыть</a></td></tr>'
    body+=('</tbody></table></div></section>' if rows else '</tbody></table></div><p>Черновиков пока нет. Загрузите свой навык или откройте принадлежащий вам опубликованный навык и выберите «Управлять версиями».</p></section>')
    return ui._response(actor,body)


async def _detail(request,actor):
    if set(request.query_params)-{'id','version','path'}:raise ValueError('Некорректный адрес версии.')
    skill_id=request.query_params.get('id','')
    rev=request.query_params.get('version')
    data=await to_thread(service.detail,actor,skill_id,int(rev) if rev else None)
    row=data['workflow'];version=data['payload'];path=request.query_params.get('path','SKILL.md')
    if path not in version['files']:raise ValueError('Файл версии не найден.')
    owner=onboarding.is_owner(actor)
    historical=version['revision']!=row['candidate_revision']
    body='<h1>'+ui.e(version['title'])+'</h1><p><a href="'+PATH+'">Черновики и проверка</a> · <a href="'+ui.PATH+'">Общий каталог</a></p>'
    body+='<section class="panel"><p>Состояние: <strong>'+ui.e(service.LABELS[row['stage']])+'</strong>. Версия материала: '+ui.e(version['version'])+'.</p><p>Постоянный ID: <code>'+ui.e(skill_id)+'</code></p>'
    if row['published_revision'] is not None and row['stage'] in {'draft','review'}:
        body+='<p>В MCP остаётся прежняя опубликованная версия. Этот черновик видят только автор и владелец.</p>'
    if historical:body+='<p>Вы читаете сохранённую версию. Она не изменяется при редактировании нового черновика.</p>'
    body+='<p>Проверка владельцем — редакционное согласование, а не гарантия безопасности или правильности ответа ИИ. Сервер не запускает файлы.</p></section>'
    if not historical and row['stage']!='archived':
        body+='<section class="panel skill-form"><h2>Новая редакция</h2><p>При сохранении публикация остаётся прежней до новой проверки. Для изменения опубликованного материала укажите новую версию, например 1.0.1.</p><form method="post" enctype="multipart/form-data" action="'+PATH+'/edit">'+_hidden('csrf',_csrf(request,actor))+_hidden('skill_id',skill_id)+_hidden('revision',row['revision'])
        body+='<label>Название<input name="title" value="'+ui.e(version['title'])+'" minlength="2" maxlength="120" required></label><label>Версия в каталоге<input name="version" value="'+ui.e(version['version'])+'" maxlength="64" required></label><label>Новый Markdown или ZIP, если меняются файлы<input type="file" name="file" accept=".md,.zip"></label><p>Без файла сохранятся прежние ресурсы. До 20 редакций, история не удаляется автоматически.</p><button>Сохранить черновик</button></form></section>'
    if not historical:
        body+='<section class="panel skill-form"><h2>Действия</h2>'
        if row['stage']=='draft':body+=_form(actor,request,row,'submit','Отправить на проверку владельцу')
        if row['stage']=='review' and owner:
            body+=_form(actor,request,row,'publish','Опубликовать проверенную версию',review=True)
            body+=_form(actor,request,row,'request_changes','Вернуть в черновик')
        if owner:
            body+=_form(actor,request,row,'restore' if row['stage']=='archived' else 'archive','Восстановить из архива' if row['stage']=='archived' else 'Перевести в архив')
        body+='</section>'
    body+='<section class="panel"><h2>Содержимое версии</h2><p>'
    for name in version['files']:
        body+='<a href="'+ui.e(_url(id=skill_id,version=version['revision'],path=name))+'">'+ui.e(name)+'</a> · '
    body+='</p><p>SHA256: <code>'+ui.e(version['manifest'][path]['sha256'])+'</code></p><pre class="skill-material">'+ui.e(version['files'][path])+'</pre></section>'
    body+='<section class="panel"><h2>История материалов</h2><ul>'
    for item in data['versions']:
        tag=' · опубликована' if item['revision']==row['published_revision'] else ''
        body+='<li><a href="'+ui.e(_url(id=skill_id,version=item['revision']))+'">'+ui.e(item['version'])+' · редакция '+str(item['revision'])+'</a>'+tag+'</li>'
    body+='</ul><h2>События</h2><ul>'
    labels={'draft_created':'Создан черновик','legacy_adopted':'Сохранена прежняя публикация','draft_updated':'Сохранена новая редакция','submitted':'Отправлен на проверку','changes_requested':'Возвращён в черновик','published':'Опубликован владельцем','archived':'Переведён в архив','restored':'Восстановлен из архива'}
    for item in data['events']:body+='<li>'+ui.e(labels[item['action']])+' · '+ui.e(_time(item['created_at']))+'</li>'
    body+='</ul></section>'
    return _cookie(ui._response(actor,body),_csrf(request,actor))


async def _action(request,actor):
    raw=await ui._body(request)
    if len(raw)>4096 or request.headers.get('content-type','').split(';')[0]!='application/x-www-form-urlencoded':raise ValueError('Неверная форма действия.')
    values=parse_qs(raw.decode('utf-8'),keep_blank_values=True,max_num_fields=6)
    if set(values)-{'csrf','skill_id','revision','action','review_ack'} or not {'csrf','skill_id','revision','action'}<=set(values) or any(len(v)!=1 for v in values.values()):raise ValueError('Неверная форма действия.')
    form={k:v[0] for k,v in values.items()};ui._check_csrf(request,actor,form)
    if form['action']=='adopt':
        row=await to_thread(service.adopt_published,actor,skill_id=form['skill_id'],expected_revision=int(form['revision']))
    else:
        row=await to_thread(service.transition,actor,skill_id=form['skill_id'],expected_revision=int(form['revision']),action=form['action'],bundled=ui._bundled(),review_ack=form.get('review_ack')=='yes')
    audit_event(event='skill_lifecycle_changed',actor=actor,system='skill-library',status='ok',arguments={'skill_id':row['skill_id'],'revision':row['revision'],'action':form['action']})
    return RedirectResponse(_url(id=row['skill_id']),status_code=303)


async def _edit(request,actor):
    raw=await ui._body(request)
    async def receive():return {'type':'http.request','body':raw,'more_body':False}
    replay=Request(request.scope,receive)
    async with replay.form(max_files=1,max_fields=5,max_part_size=16384) as form:
        if set(form)!={'csrf','skill_id','revision','title','version','file'} or any(len(form.getlist(k))!=1 for k in form):raise ValueError('Неверная форма версии.')
        ui._check_csrf(request,actor,form)
        if not all(isinstance(form[k],str) for k in ('skill_id','revision','title','version')):raise ValueError('Неверные поля версии.')
        item=form['file']
        if not isinstance(item,UploadFile):raise ValueError('Неверный файл версии.')
        content=await item.read(skill_library.MAX_PACKAGE+1)
        if not item.filename and content:raise ValueError('Файл должен иметь имя.')
        row=await to_thread(service.update_draft,actor,skill_id=form['skill_id'],expected_revision=int(form['revision']),title=form['title'],version=form['version'],filename=item.filename,content=content if item.filename else None,bundled=ui._bundled())
    audit_event(event='skill_draft_updated',actor=actor,system='skill-library',status='ok',arguments={'skill_id':row['skill_id'],'revision':row['revision']})
    return RedirectResponse(_url(id=row['skill_id']),status_code=303)


def register_skill_lifecycle_routes(mcp):
    async def handle(request,operation):
        if not service.enabled():
            from starlette.responses import PlainTextResponse
            return PlainTextResponse('Not found',status_code=404)
        return await ui._handle(request,operation)

    @mcp.custom_route(PATH,methods=['GET'],include_in_schema=False)
    async def skill_management(request):
        return await handle(request,_detail if 'id' in request.query_params else _dashboard)

    @mcp.custom_route(PATH+'/action',methods=['POST'],include_in_schema=False)
    async def skill_action(request):return await handle(request,_action)

    @mcp.custom_route(PATH+'/edit',methods=['POST'],include_in_schema=False)
    async def skill_edit(request):return await handle(request,_edit)
