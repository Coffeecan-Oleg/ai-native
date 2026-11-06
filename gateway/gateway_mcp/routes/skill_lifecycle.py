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
from gateway_mcp.services import skill_review,skill_comparison
from gateway_mcp.services.observability import audit_event

PATH=ui.PATH+'/manage'


def _url(**params):return ui.url(PATH,**params)


def _form(token,row,action,label,*,review=False):
    text='<form method="post" action="'+PATH+'/action">'+_hidden('csrf',token)
    text+=_hidden('skill_id',row['skill_id'])+_hidden('revision',row['revision'])+_hidden('action',action)
    if review:
        text+='<label class="skill-check"><input type="checkbox" name="review_ack" value="yes" required><span>Я проверил эту версию: назначение, разрешённые данные, инструменты и ссылки. Публикую её для сотрудников.</span></label>'
    return text+'<button>'+ui.e(label)+'</button></form>'


def _review_panel(report,skill_id):
    messages={
        'version_mismatch':'Версия в заголовке SKILL.md отличается от версии в карточке. Проверьте обе метки.',
        'header_invalid':'Не удалось разобрать заголовок SKILL.md. Проверьте YAML и поля версии.',
        'requirements_invalid':'Исправьте формат requirements.json: схема 1, от 1 до 10 операций. Содержимое файла можно открыть ниже.',
        'legacy_address':'Указан прежний адрес Платформы. Проверьте, нужна ли замена на platforma.akmetron.ru.',
        'reference_missing':'Для ссылки [текст][метка] не найдено определение метки в этом Markdown-файле.',
        'file_missing':'Ссылка указывает на файл, которого нет в этой редакции архива. Проверьте имя, регистр и комплект файлов.',
        'link_outside_package':'Ссылка выходит за пределы архива или содержит недопустимый путь. Добавьте ресурс в пакет и исправьте путь.',
        'link_invalid':'Не удалось разобрать адрес ссылки. Проверьте синтаксис.',
        'link_scheme_unchecked':'Это не обычная веб-ссылка. Её назначение нужно проверить вручную.',
        'server_path_unchecked':'Абсолютный серверный путь проверяется вручную, а не как файл архива.',
        'html_links_unchecked':'HTML-ссылки в этой строке не проверяются автоматически.',
        'scan_limit':'Достигнут лимит проверки. Просмотрите остальные материалы вручную.'}
    body='<section class="panel"><h2>Проверка файлов и ссылок</h2><p>Проверена сохранённая редакция '+ui.e(report['snapshot_revision'])+', версия '+ui.e(report['version'])+'. Файлов: '+str(report['files'])+'.</p>'
    body+='<p>Требуют внимания: '+str(report['attention_count'])+'. Всего замечаний и сведений: '+str(report['issue_count'])+'.</p>'
    if not report['scan_complete']:body+='<p role="status"><strong>Проверка выполнена частично.</strong> Достигнут лимит ссылок, строк или длины строки. Остальные материалы проверьте вручную.</p>'
    if not report['issue_count']:body+='<p>В пределах этой проверки замечаний не найдено. Это не автоматическое разрешение на публикацию.</p>'
    if report['issues']:
        body+='<div class="table-wrap"><table style="min-width:600px"><thead><tr><th>Файл / строка</th><th>Что проверить</th></tr></thead><tbody>'
        for item in report['issues']:
            link=_url(id=skill_id,version=report['snapshot_revision'],path=item['path'])
            body+='<tr><td><a href="'+ui.e(link)+'"><code style="overflow-wrap:anywhere">'+ui.e(item['path'])+'</code></a><br>Строка '+str(item['line'])+'</td><td>'+ui.e(messages[item['code']])+'</td></tr>'
        body+='</tbody></table></div>'
    if report['issues_truncated']:body+='<p>Показаны первые 50 замечаний. Общий счётчик учитывает остальные найденные замечания.</p>'
    if report['requirements']=='missing':body+='<p>Необязательный requirements.json не приложен. Заявленный набор операций здесь не определён.</p>'
    elif report['requirements']=='valid':
        kinds={'assistant':'Текстовая задача ассистента','route':'Серверный маршрут, заявленный автором','unavailable':'Автор отметил как неподключённую операцию'}
        body+='<h3>Декларация автора</h3><ul>'
        for op in report['operations']:
            body+='<li>'+ui.e(op['title'])+' — '+ui.e(kinds[op['kind']])+(' · <code>'+ui.e(op['route'])+'</code>' if 'route' in op else '')+'</li>'
        body+='</ul><p>Корректный формат декларации не подтверждает подключение сервиса или права пользователей.</p>'
    body+='<p>Проверено ссылок на файлы: '+str(report['local_file_links_checked'])+'. Внешних ссылок без проверки: '+str(report['external_links_unchecked'])+'. Якорей без проверки: '+str(report['anchors_unchecked'])+'.</p>'
    body+='<p class="small">'+ui.e(report['notice'])+'</p><p class="small">Отпечаток сохранённого материала: <code class="skill-code">'+ui.e(report['fingerprint'])+'</code>. После правок сохраните новую редакцию и откройте её снова.</p></section>'
    return body


def _comparison_panel(report,skill_id):
    body='<section class="panel"><h2>Что изменилось в этой редакции</h2>'
    if report['previous_revision'] is None:
        return body+'<p>Это первая сохранённая редакция: сравнивать пока не с чем. Перед публикацией проверьте все файлы.</p></section>'
    basis='Опубликованная редакция' if report['basis']=='published' else 'Предыдущая сохранённая редакция (могла быть черновиком)'
    body+='<p>'+basis+': '+str(report['previous_revision'])+' · версия '+ui.e(report['previous_version'])+'. Сравнивается с редакцией '+str(report['current_revision'])+' · версия '+ui.e(report['current_version'])+'.</p>'
    counts=report['counts']
    body+='<p>Добавлено файлов: '+str(counts['added'])+'. Изменено: '+str(counts['changed'])+'. Удалено: '+str(counts['removed'])+'. Без изменений: '+str(counts['unchanged'])+'.</p>'
    if report['omitted_content']:body+='<p role="status"><strong>Показаны не все строки.</strong> Для больших файлов или при достижении лимита откройте обе сохранённые редакции по ссылкам ниже. Список изменённых файлов и их SHA256 показан полностью.</p>'
    labels={'title':'Название','description':'Описание','version':'Версия в каталоге'}
    if report['metadata']:
        body+='<h3>Изменения карточки</h3><div class="table-wrap"><table style="min-width:600px"><thead><tr><th>Поле</th><th>Было</th><th>Стало</th></tr></thead><tbody>'
        for item in report['metadata']:body+='<tr><td>'+labels[item['field']]+'</td><td style="overflow-wrap:anywhere">'+ui.e(item['before'])+'</td><td style="overflow-wrap:anywhere">'+ui.e(item['after'])+'</td></tr>'
        body+='</tbody></table></div>'
    if not report['files']:body+='<p>Содержимое файлов не изменилось.</p>'
    kinds={'added':'Добавлен','removed':'Удалён','changed':'Изменён'}
    colors={'added':'background:#e6f5ed;color:#14573b','removed':'background:#fdeceb;color:#842927','hunk':'color:#445b80','context':''}
    for item in report['files']:
        body+='<h3>'+kinds[item['change']]+': <code class="skill-code">'+ui.e(item['path'])+'</code></h3><p>'
        for key,label,revision in [('before_sha256','Было',report['previous_revision']),('after_sha256','Стало',report['current_revision'])]:
            if item[key]:
                url=_url(id=skill_id,version=revision,path=item['path'])
                body+='<a href="'+ui.e(url)+'">'+label+' · редакция '+str(revision)+'</a> · SHA256: <code class="skill-code">'+ui.e(item[key])+'</code><br>'
        body+='</p>'
        if item['diff']:
            body+='<details><summary>Показать строки: − удалено, + добавлено</summary><pre class="skill-material">'
            for line in item['diff']:body+='<span style="display:block;'+colors[line['kind']]+'">'+ui.e(line['text'])+'</span>'
            body+='</pre></details>'
        if item['diff_status']=='input_limit':body+='<p>Файл превышает лимит построчного сравнения. Откройте материалы по ссылкам.</p>'
        elif item['diff_status']=='output_limit':body+='<p>Вывод строк сокращён по общему лимиту. Откройте материалы по ссылкам.</p>'
        elif item['diff_status']=='line_endings_only':body+='<p>Текст строк совпадает; изменились разделители строк или завершающий перенос.</p>'
    body+='<p class="small">Показаны различия сохранённых материалов. Это не оценка безопасности инструкции, прав или качества ответа ИИ. Сравнение не изменяет публикацию.</p></section>'
    return body


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
    # A new or another actor's cookie causes _csrf() to mint a nonce. Reuse
    # that one value for every form and the response cookie on this page.
    token=_csrf(request,actor)
    historical=version['revision']!=row['candidate_revision']
    body='<h1>'+ui.e(version['title'])+'</h1><p><a href="'+PATH+'">Черновики и проверка</a> · <a href="'+ui.PATH+'">Общий каталог</a></p>'
    body+='<section class="panel"><p>Состояние: <strong>'+ui.e(service.LABELS[row['stage']])+'</strong>. Версия материала: '+ui.e(version['version'])+'.</p><p>Постоянный ID: <code>'+ui.e(skill_id)+'</code></p>'
    if row['published_revision'] is not None and row['stage'] in {'draft','review'}:
        body+='<p>В MCP остаётся прежняя опубликованная версия. Этот черновик видят только автор и владелец.</p>'
    if historical:body+='<p>Вы читаете сохранённую версию. Она не изменяется при редактировании нового черновика.</p>'
    body+='<p>Проверка владельцем — редакционное согласование, а не гарантия безопасности или правильности ответа ИИ. Сервер не запускает файлы.</p></section>'
    report=await to_thread(skill_review.inspect_snapshot,version)
    body+=_review_panel(report,skill_id)
    comparison=await to_thread(skill_comparison.compare,version,data['comparison_base'],basis=data['comparison_kind'])
    body+=_comparison_panel(comparison,skill_id)
    if not historical and row['stage']!='archived':
        body+='<section class="panel skill-form"><h2>Новая редакция</h2><p>При сохранении публикация остаётся прежней до новой проверки. Для изменения опубликованного материала укажите новую версию, например 1.0.1.</p><form method="post" enctype="multipart/form-data" action="'+PATH+'/edit">'+_hidden('csrf',token)+_hidden('skill_id',skill_id)+_hidden('revision',row['revision'])
        body+='<label>Название<input name="title" value="'+ui.e(version['title'])+'" minlength="2" maxlength="120" required></label><label>Версия в каталоге<input name="version" value="'+ui.e(version['version'])+'" maxlength="64" required></label><label>Новый Markdown или ZIP, если меняются файлы<input type="file" name="file" accept=".md,.zip"></label><p>Без файла сохранятся прежние ресурсы. До 20 редакций, история не удаляется автоматически.</p><button>Сохранить черновик</button></form></section>'
    if not historical:
        body+='<section class="panel skill-form"><h2>Действия</h2>'
        if row['stage']=='draft':body+=_form(token,row,'submit','Отправить на проверку владельцу')
        if row['stage']=='review' and owner:
            body+=_form(token,row,'publish','Опубликовать проверенную версию',review=True)
            body+=_form(token,row,'request_changes','Вернуть в черновик')
        if owner:
            body+=_form(token,row,'restore' if row['stage']=='archived' else 'archive','Восстановить из архива' if row['stage']=='archived' else 'Перевести в архив')
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
    return _cookie(ui._response(actor,body),token)


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
