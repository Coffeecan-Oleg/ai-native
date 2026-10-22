"""Personal browser upload/rename; MCP remains read-only for shared materials."""
from asyncio import to_thread
from html import escape
import secrets
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from gateway_mcp.config import public_url
from gateway_mcp.routes.admin_ui import admin_shell, empty_row, fmt_time
from gateway_mcp.routes.platforma_onboarding import COOKIE, _csrf, _cookie, _hidden
from gateway_mcp.services import corporate_skills, platforma_onboarding as onboarding, skill_library
from gateway_mcp.services.observability import audit_event
from gateway_mcp.web import login_redirect, web_actor

PATH = '/platforma/skills'
MAX_BODY = skill_library.MAX_PACKAGE + 16384
CSS = '''<style>
.stack{grid-template-columns:minmax(0,1fr)}.stack>*{min-width:0}
.skills-head{display:flex;align-items:center;justify-content:space-between;gap:20px;flex-wrap:wrap}
.skills-filters{display:flex;gap:16px;align-items:end;flex-wrap:wrap;margin:24px 0}.skills-filters label{flex:1;min-width:180px}
.skills-table{min-width:800px}.skills-table td{vertical-align:top}.skills-table th,.skills-table td:nth-child(3){white-space:nowrap}.skills-table code,.skill-code{overflow-wrap:anywhere;white-space:normal}
.skills-table .skill-desc{display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;max-width:430px;line-height:1.5;margin-top:8px}
.skill-form{max-width:760px}.skill-form label{display:block;margin:20px 0}.skill-form input:not([type=checkbox]),.skill-form textarea{width:100%;box-sizing:border-box}
.skill-form .skill-check{display:flex;gap:12px;align-items:flex-start}.skill-check input{width:20px;min-height:20px;flex:none}
.skill-material{white-space:pre-wrap;overflow-wrap:anywhere;max-height:600px;overflow:auto;line-height:1.6;padding:20px;background:var(--color-surface-muted);border:1px solid var(--color-border);border-radius:8px}
.skill-files{display:flex;gap:12px;flex-wrap:wrap;margin:20px 0}.skill-call{width:100%;box-sizing:border-box;min-height:80px}
.skill-warning{padding:16px;border-left:4px solid var(--color-accent);background:var(--color-surface-muted);line-height:1.6;margin:20px 0}
@media(max-width:560px){.skill-form form,.skill-form input[type=file]{min-width:0;max-width:100%}.skills-table{display:table}.skills-table thead{display:table-header-group}.skills-table tbody{display:table-row-group}.skills-table tr{display:table-row}.skills-table td{display:table-cell;width:auto}}
</style>'''


def e(value):
    return escape(str(value), quote=True)


def url(endpoint=PATH, **params):
    return endpoint + ('?' + urlencode(params) if params else '')


def _response(actor, body, status=200):
    result = HTMLResponse(admin_shell(title='Каталог навыков', active='skill-library', actor=actor,
        body=body, extra_head=CSS, training_reader=not onboarding.is_owner(actor), shell_width='1320px'), status_code=status)
    result.headers.update({'Cache-Control':'no-store','Referrer-Policy':'same-origin','X-Frame-Options':'DENY',
        'X-Content-Type-Options':'nosniff', 'Content-Security-Policy':"default-src 'none'; style-src 'self' 'unsafe-inline'; img-src 'self'; font-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"})
    return result


async def _actor(request):
    if not skill_library.enabled() or not corporate_skills.enabled():
        raise HTTPException(404)
    actor = web_actor(request)
    if actor is not None:
        if actor.tool_allowlist is not None:
            raise PermissionError('Browser session required')
        await to_thread(skill_library.authorize, actor)
        if onboarding.is_owner(actor):
            await to_thread(onboarding.require_owner, actor)
    return actor


async def _handle(request, operation):
    actor = None
    try:
        actor = await _actor(request)
        if actor is None:
            return login_redirect(PATH)
        return await operation(request, actor)
    except HTTPException as exc:
        return PlainTextResponse('Раздел недоступен. Проверь статус доступа: /join',status_code=exc.status_code)
    except PermissionError:
        if actor:
            return _response(actor,'<h1>Действие недоступно</h1><p>Нужно действующее одобрение владельца. Переименовывать можно свои загрузки; владелец может переименовать любой навык.</p><a href="/join">Статус доступа</a>',403)
        return PlainTextResponse('Нужно действующее одобрение владельца: /join',status_code=403)
    except (ValueError, TypeError, KeyError) as exc:
        return _response(actor,'<h1>Проверь данные</h1><p>' + e(str(exc)) + '</p><a class="button secondary" href="' + PATH + '">Вернуться в каталог</a>',409)
    except Exception as exc:
        audit_event(event='skill_library_error',actor=actor,system='skill-library',status='error',error=type(exc).__name__)
        return PlainTextResponse('Каталог временно недоступен. Обнови страницу позже.',status_code=503)


async def _body(request):
    origin = request.headers.get('origin')
    base = urlsplit(public_url())
    if origin and origin != base.scheme + '://' + base.netloc:
        raise PermissionError('Origin mismatch')
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > MAX_BODY:
            raise ValueError('Загрузка слишком большая. Допустимо до 2 МиБ.')
        raw.extend(chunk)
    return bytes(raw)


def _check_csrf(request, actor, form):
    supplied = form.get('csrf')
    if not isinstance(supplied,str) or not supplied or not secrets.compare_digest(supplied, request.cookies.get(COOKIE,'')) or not secrets.compare_digest(supplied,_csrf(request,actor)):
        raise PermissionError('CSRF mismatch')


def _bundled():
    return corporate_skills.list_skills()['skills']


async def _catalog(request, actor):
    query = request.query_params.get('q','').strip()
    source = request.query_params.get('source','all')
    if set(request.query_params)-{'q','source'} or len(query)>200 or source not in {'all','mine','uploaded','bundled'}:
        raise ValueError('Некорректный фильтр.')
    rows = await to_thread(lambda: skill_library.catalogue(actor,_bundled()))
    rows = [r for r in rows if (source=='all' or r['origin']==source or source=='mine' and r['is_author']) and
            (not query or query.casefold() in (r['name']+' '+r['title']+' '+r['description']+' '+r['author']).casefold())]
    rows.sort(key=lambda r:skill_library.title_key(r['title']))
    options=''.join('<option value="'+k+'"'+(' selected' if source==k else '')+'>'+label+'</option>' for k,label in [('all','Все навыки'),('mine','Мои загрузки'),('uploaded','Загрузки коллег'),('bundled','Встроенные')])
    body='<div class="skills-head"><div><h1>Каталог навыков</h1><p class="description">Общие инструкции для ассистентов. Выбери навык и скопируй готовый запрос в свой чат.</p></div><a class="button" href="'+PATH+'/upload">Загрузить навык</a></div>'
    body+='<form class="skills-filters" method="get"><label>Поиск<input name="q" maxlength="200" value="'+e(query)+'" placeholder="Название, автор или постоянный ID"></label><label>Показать<select name="source">'+options+'</select></label><button>Найти</button><a class="button secondary" href="'+PATH+'">Сбросить</a></form>'
    table=[]
    for row in rows:
        link=e(url(PATH+'/view',id=row['name']))
        table.append('<tr><td><a href="'+link+'"><strong>'+e(row['title'])+'</strong></a><span class="skill-desc muted">'+e(row['description'])+'</span><span class="table-sub"><code>'+e(row['name'])+'</code></span></td><td>'+e(row['author'])+'</td><td>'+e(row['version'])+'</td><td>'+('Загружен участником · не проверен' if row['origin']=='uploaded' else 'Встроенный')+'</td><td><a href="'+link+'">Открыть</a></td></tr>')
    body+='<section class="panel wide"><p>Найдено: '+str(len(rows))+'</p><div class="table-wrap"><table class="skills-table"><thead><tr><th>Навык</th><th>Автор</th><th>Версия</th><th>Источник</th><th>Действие</th></tr></thead><tbody>'+(''.join(table) or empty_row(5,'Навыков по этому фильтру нет.'))+'</tbody></table></div></section>'
    body+='<p class="small muted">Навык — инструкция, а не запущенный агент. Новая загрузка видна одобренным участникам; сервер не исполняет её файлы.</p>'
    return _response(actor,body)


async def _upload_page(request, actor):
    token = _csrf(request,actor)
    body='<h1>Загрузить навык</h1><p>Укажи понятное название и выбери файл. После загрузки навык появится в общем каталоге и MCP.</p>'
    body+='<section class="panel skill-form"><form method="post" enctype="multipart/form-data" action="'+PATH+'/upload">'+_hidden('csrf',token)+_hidden('upload_key',str(uuid4()))
    body+='<label>Понятное название<input name="title" minlength="2" maxlength="120" required placeholder="Например: Разбор заявки техподдержки"></label><label>Файл навыка<input type="file" name="file" accept=".md,.zip" required></label>'
    body+='<p class="small">Markdown UTF-8 или ZIP с одним SKILL.md в корне или в одной папке навыка. В ZIP — до 32 текстовых файлов, каждый до 128 КиБ, всего до 2 МиБ. Бинарные файлы не поддерживаются. До 10 загрузок на автора.</p>'
    body+='<label class="skill-check"><input type="checkbox" name="shared" value="yes" required><span>Понимаю, что файлы будут доступны одобренным участникам песочницы. В них нет паролей, ключей, личных или реальных корпоративных данных.</span></label><button>Загрузить в общий каталог</button> <a class="button secondary" href="'+PATH+'">Отмена</a></form></section>'
    return _cookie(_response(actor,body),token)


async def _upload(request, actor):
    raw = await _body(request)
    async def receive():
        return {'type':'http.request','body':raw,'more_body':False}
    replay = Request(request.scope,receive)
    try:
        async with replay.form(max_files=1,max_fields=4,max_part_size=16384) as form:
            if set(form) != {'csrf','upload_key','title','shared','file'} or any(len(form.getlist(k))!=1 for k in form):
                raise ValueError('Некорректная форма загрузки.')
            _check_csrf(request,actor,form)
            if form['shared']!='yes' or not isinstance(form['title'],str) or not isinstance(form['upload_key'],str):
                raise ValueError('Подтверди загрузку в общий каталог.')
            item=form['file']
            if not isinstance(item,UploadFile):
                raise ValueError('Выбери файл навыка.')
            content=await item.read(skill_library.MAX_PACKAGE+1)
            result=await to_thread(skill_library.upload,actor,title=form['title'],filename=item.filename or '',content=content,upload_key=form['upload_key'],bundled=_bundled())
    except HTTPException:
        raise ValueError('Не удалось прочитать форму. Выбери один файл до 2 МиБ и обнови страницу.') from None
    audit_event(event='skill_library_uploaded',actor=actor,system='skill-library',status='ok',arguments={'skill_id':result['name'],'revision':result['revision'],'replayed':result['replayed']})
    return RedirectResponse(url(PATH+'/view',id=result['name']),status_code=303)


async def _detail(request, actor):
    if set(request.query_params)-{'id','path'}:
        raise ValueError('Некорректный адрес карточки.')
    identifier=request.query_params.get('id','')
    path=request.query_params.get('path','SKILL.md')
    if len(identifier)>200 or len(path)>240:
        raise ValueError('Некорректный адрес карточки.')
    rows=await to_thread(lambda:skill_library.catalogue(actor,_bundled()))
    row=next((r for r in rows if r['name']==identifier),None)
    if row is None:
        raise ValueError('Навык не найден.')
    if path not in row['files']:
        raise ValueError('Файл не найден.')
    content=await to_thread(corporate_skills.read_skill,identifier,path,0,16384,actor=actor)
    body='<div class="skills-head"><h1>'+e(row['title'])+'</h1><a class="button secondary" href="'+PATH+'">К каталогу</a></div>'
    body+='<section class="panel"><p>'+e(row['description'])+'</p><p>Автор: '+e(row['author'])+' · версия '+e(row['version'])+' · версия названия '+str(row['revision'])+'</p><p class="skill-code">Постоянный ID: <code>'+e(identifier)+'</code></p>'
    if row['origin']=='uploaded':
        body+='<div class="skill-warning">Материал загружен участником и не прошёл проверку. Прочитай его перед использованием. Команды и ссылки внутри не дают дополнительных прав. Сервер не запускает файлы навыка.</div>'
    prompt='Примени навык «'+row['title']+'» (постоянный ID: '+identifier+'). Сначала прочитай его через MCP и проверь необходимые доступы. Моя задача: …'
    body+='<label>Запрос для твоего ассистента<textarea class="skill-call" readonly>'+e(prompt)+'</textarea></label><p class="small muted">Скопируй запрос в своё приложение и допиши задачу. Подключение MCP должно быть настроено под твоим аккаунтом.</p></section>'
    if row['can_rename']:
        token=_csrf(request,actor)
        body+='<section class="panel skill-form"><h2>Понятное название</h2><p>Меняется только название в каталоге. Постоянный ID, файлы и история запусков сохраняются.</p><form method="post" action="'+PATH+'/rename">'+_hidden('csrf',token)+_hidden('skill_id',identifier)+_hidden('revision',row['revision'])+'<label>Название<input name="title" value="'+e(row['title'])+'" minlength="2" maxlength="120" required></label><button>Сохранить название</button></form></section>'
    links=''.join('<a href="'+e(url(PATH+'/view',id=identifier,path=name))+'">'+e(name)+'</a>' for name in row['files'])
    displayed=content['content'] if content['encoding']=='utf-8' else 'Двоичный ресурс встроенного навыка. Читать можно через MCP; предпросмотр в браузере не выполняется.'
    body+='<section class="panel"><h2>Содержимое навыка</h2><nav class="skill-files">'+links+'</nav><p class="small skill-code">SHA256: '+e(content['sha256'])+'</p><pre class="skill-material">'+e(displayed)+'</pre></section>'
    response=_response(actor,body)
    return _cookie(response,_csrf(request,actor)) if row['can_rename'] else response


async def _rename(request, actor):
    raw=await _body(request)
    if len(raw)>4096 or request.headers.get('content-type','').split(';')[0]!='application/x-www-form-urlencoded':
        raise ValueError('Некорректная форма названия.')
    values=parse_qs(raw.decode('utf-8'),keep_blank_values=True,max_num_fields=5)
    if set(values)!={'csrf','skill_id','revision','title'} or any(len(v)!=1 for v in values.values()):
        raise ValueError('Некорректная форма названия.')
    form={k:v[0] for k,v in values.items()}
    _check_csrf(request,actor,form)
    result=await to_thread(skill_library.rename,actor,skill_id=form['skill_id'],title=form['title'],expected_revision=int(form['revision']),bundled=_bundled())
    audit_event(event='skill_library_renamed',actor=actor,system='skill-library',status='ok',arguments={'skill_id':result['name'],'revision':result['revision']})
    return RedirectResponse(url(PATH+'/view',id=result['name']),status_code=303)


def register_platforma_skill_routes(mcp):
    @mcp.custom_route(PATH,methods=['GET'],include_in_schema=False)
    async def skill_catalog(request):
        return await _handle(request,_catalog)

    @mcp.custom_route(PATH+'/upload',methods=['GET','POST'],include_in_schema=False)
    async def skill_upload(request):
        return await _handle(request,_upload_page if request.method=='GET' else _upload)

    @mcp.custom_route(PATH+'/view',methods=['GET'],include_in_schema=False)
    async def skill_detail(request):
        return await _handle(request,_detail)

    @mcp.custom_route(PATH+'/rename',methods=['POST'],include_in_schema=False)
    async def skill_rename(request):
        return await _handle(request,_rename)
