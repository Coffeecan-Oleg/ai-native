"""Owner-only settings for a fixed service-chat channel; no secrets in the browser."""
from asyncio import to_thread
from datetime import datetime, timezone, timedelta
from html import escape
import secrets
from urllib.parse import parse_qs, urlsplit
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from gateway_mcp.config import public_url
from gateway_mcp.routes.admin_ui import admin_shell
from gateway_mcp.routes.platforma_onboarding import COOKIE, _csrf, _cookie, _hidden
from gateway_mcp.services import corporate_access, activity_delivery, storage_activity_notifications as store
from gateway_mcp.services.activity_events import CATEGORIES
from gateway_mcp.services.observability import audit_event
from gateway_mcp.services.storage_admin_console import admin_audit_user_names
from gateway_mcp.web import web_actor, login_redirect

PATH='/admin/activity-notifications'
STATES={'pending':'В очереди','sending':'Отправляется','sent':'Доставлено','failed':'Не доставлено',
        'unknown':'Доставка не подтверждена','cancelled':'Отменено настройками','expired':'Истёк срок ожидания'}
CSS='''<style>
.content>.stack{grid-template-columns:minmax(0,1fr)}
.notice-settings,.notice-log,.page-head{min-width:0}
.notice-summary{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px;margin:24px 0}
.notice-summary article{padding:20px;border:1px solid var(--color-border);border-radius:8px;background:var(--color-surface)}
.notice-summary strong{display:block;font-size:24px;margin-bottom:8px}.notice-summary p{margin:0}
.notice-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;margin:20px 0}
.notice-choice{display:flex;align-items:flex-start;gap:12px;padding:16px;border:1px solid var(--color-border);border-radius:8px;cursor:pointer;min-height:72px}
.notice-choice input[type=checkbox]{width:22px;height:22px;min-height:22px;flex:none;margin-top:2px}
.notice-choice span{display:block}.notice-choice small{display:block;font-weight:400;line-height:1.5;margin-top:6px;color:var(--color-muted)}
.notice-choice:focus-within{outline:2px solid var(--color-focus);outline-offset:2px}
.notice-settings select{max-width:360px;min-height:44px}.notice-settings button{min-height:44px;margin-top:16px}
.notice-log{margin-top:24px}.notice-log td{vertical-align:top;overflow-wrap:anywhere}.notice-log table{min-width:620px}
@media(max-width:700px){.notice-grid,.notice-summary{grid-template-columns:1fr}}
</style>'''


def e(value):
    return escape(str(value or ''),quote=True)


def _check_csrf(request,actor,form):
    value=form.get('csrf')
    if not isinstance(value,str) or not value or not secrets.compare_digest(value,request.cookies.get(COOKIE,'')) or not secrets.compare_digest(value,_csrf(request,actor)):
        raise PermissionError('csrf_mismatch')


def stamp(value):
    if not value:return 'Нет данных'
    if isinstance(value,str):value=datetime.fromisoformat(value.replace('Z','+00:00'))
    return value.astimezone(timezone(timedelta(hours=3))).strftime('%d.%m %H:%M МСК')


def render(actor,data,token,*,saved=False,error='',transport=False,names=None):
    settings=data['settings']; counts=data['counts']; names=names or {}
    seen=settings.get('worker_seen_at')
    if isinstance(seen,str):seen=datetime.fromisoformat(seen)
    fresh=bool(seen and datetime.now(timezone.utc)-seen < timedelta(seconds=30))
    if not settings['enabled']:mode='Выключены'
    elif not transport:mode='Канал не подключён'
    elif not fresh:mode='Нет свежего ответа отправителя'
    elif settings.get('worker_status') in {'credential_unavailable','unknown','rejected'}:mode='Нужна проверка доставки'
    else:mode='Включены'
    choices=''.join(f'<label class="notice-choice"><input type="checkbox" name="category" value="{key}"'+(' checked' if key in settings['categories'] else '')+f'><span>{e(title)}<small>{e(description)}</small></span></label>' for key,(title,description) in CATEGORIES.items())
    rows=''
    for row in data['rows']:
        item=row['details'];subject=item.get('actor_subject','')
        who=names.get(subject) or ('Сотрудник №'+subject.rsplit(':',1)[-1] if subject else 'Не установлен')
        rows+=f'<tr><td>{e(stamp(row["created_at"]))}</td><td>{e(who)}</td><td>{e(item["action"])}<br><small>{e(item.get("object_id"))}</small></td><td>{e(STATES[row["state"]])}<br><small>{e(row.get("error_code"))}</small></td><td>{e(row.get("message_id") or "—")}</td></tr>'
    if not rows:rows='<tr><td colspan="5">Отправок пока нет. После включения сюда попадут только новые выбранные события.</td></tr>'
    options=''.join(f'<option value="{n}"'+(' selected' if n==settings['interval_seconds'] else '')+f'>{label}</option>' for n,label in [(30,'Раз в 30 секунд'),(60,'Раз в минуту'),(300,'Раз в 5 минут'),(900,'Раз в 15 минут')])
    message = '<div class="banner" role="status">Настройки сохранены.</div>' if saved else ''
    if error:message+='<div class="banner error" role="alert">'+e(error)+'</div>'
    body=f'''<div class="page-head"><div><h1>Уведомления</h1><p class="lead">Кто вошёл, что изменил и какие навыки применял. Управление служебным чатом.</p></div><a class="button secondary" href="{PATH}">Обновить</a></div>
    {message}<section class="notice-summary" aria-label="Состояние рассылки"><article><strong>{e(mode)}</strong><p>Уведомления о действиях</p></article><article><strong>{counts.get('pending',0)}</strong><p>Ожидают отправки</p></article><article><strong>{sum(counts.get(k,0) for k in ('failed','unknown','expired'))}</strong><p>Требуют внимания</p></article></section>
    <section class="panel notice-settings"><h2>Что отправлять</h2><p>Получатель: <a href="https://akmetron.bitrix24.ru/online/?IM_DIALOG=chat68282">служебный чат 68282</a>. Отправитель — системная учётная запись. Вебхук здесь не отображается.</p>
    <form method="post" action="{PATH}/settings">{_hidden('csrf',token)}{_hidden('revision',settings['revision'])}
    <label class="notice-choice"><input type="checkbox" name="enabled" value="yes"{' checked' if settings['enabled'] else ''}><span>Отправлять уведомления в чат<small>Общий выключатель для действий сотрудников. Журнал Платформы продолжает работать.</small></span></label>
    <div class="notice-grid">{choices}</div><label>Как часто отправлять накопленные события<select name="interval">{options}</select></label>
    <p class="small muted">До четырёх событий в сообщении; остальные ждут следующего интервала. Отключённые события из очереди отменяются и после включения не досылаются. Уже начатая отправка может завершиться.</p><button type="submit">Сохранить настройки</button></form></section>
    <section class="panel notice-log"><h2>Последние события доставки</h2><p>Ответ отправителя: {e(stamp(seen))}. Настройки изменены: {e(stamp(settings.get('updated_at')))}.</p>
    <p class="small">Доставлено — Битрикс24 вернул ID сообщения. «Доставка не подтверждена» требует проверки чата; автоматического повтора нет, чтобы не создать дубликат. Ожидание ограничено 24 часами, история доставки — 30 днями.</p>
    <div class="table-wrap"><table><thead><tr><th>Время</th><th>Сотрудник</th><th>Действие</th><th>Доставка</th><th>ID сообщения</th></tr></thead><tbody>{rows}</tbody></table></div>
    <p>Не помещено в очередь из-за лимита: {settings.get('overflow_count',0)}. Исходные действия остаются в <a href="/admin/audit">журнале событий</a>.</p></section>
    <section class="panel notice-log"><h2>Границы наблюдения</h2><p>Чтение файла навыка не доказывает его применение. Запуск и результат известны только по событию ИИ-приложения. Добавление карточки агента не запускает его.</p><p>Тексты запросов, ответы ИИ, файлы навыков, личная память, статистика расхода и секреты в чат не отправляются. Включение не рассылает старый журнал.</p><p>Эта страница управляет действиями сотрудников. Независимая проверка доступности сервера — отдельный канал; она пока не включена из-за сетевого доступа.</p></section>'''
    response=HTMLResponse(admin_shell(title='Уведомления',active='activity-notifications',actor=actor,body=body,extra_head=CSS,shell_width='1240px'))
    response.headers.update({'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'same-origin','Content-Security-Policy':"default-src 'none'; style-src 'self' 'unsafe-inline'; font-src 'self'; img-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"})
    return _cookie(response,token)


async def owner(request):
    if not corporate_access.enabled():raise PermissionError()
    actor=web_actor(request)
    if actor is not None:await to_thread(corporate_access.require_owner,actor)
    return actor


def register_activity_notification_routes(mcp):
    @mcp.custom_route(PATH,methods=['GET'],include_in_schema=False)
    async def page(request):
        try:
            actor=await owner(request)
            if actor is None:return login_redirect(PATH)
            data=await to_thread(store.snapshot)
            names=await to_thread(admin_audit_user_names,[r['details'].get('actor_subject','') for r in data['rows']])
            return render(actor,data,_csrf(request,actor),saved=request.query_params.get('saved')=='1',transport=activity_delivery.configured(),names=names)
        except PermissionError:return PlainTextResponse('Настройки доступны только владельцу Платформы.',status_code=403)
        except Exception:return PlainTextResponse('Не удалось прочитать настройки уведомлений. Повторите позже.',status_code=503)

    @mcp.custom_route(PATH+'/settings',methods=['POST'],include_in_schema=False)
    async def save(request):
        try:
            actor=await owner(request)
            if actor is None:return PlainTextResponse('Нужен корпоративный вход.',status_code=403)
            base=urlsplit(public_url())
            if request.headers.get('origin') not in {None,base.scheme+'://'+base.netloc}:raise PermissionError()
            if request.headers.get('content-type','').split(';')[0]!='application/x-www-form-urlencoded':raise ValueError('Неверный формат формы.')
            raw=bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw)>4096:raise ValueError('Форма слишком большая.')
            form=parse_qs(raw.decode('utf-8'),keep_blank_values=True,max_num_fields=16)
            if set(form)-{'csrf','revision','enabled','category','interval'} or any(len(v)!=1 for k,v in form.items() if k!='category'):raise ValueError('Неверные поля формы.')
            _check_csrf(request,actor,{'csrf':form.get('csrf',[''])[0]})
            if form.get('enabled') not in (None,['yes']):raise ValueError('Неверное состояние выключателя.')
            enabled=form.get('enabled')==['yes']
            if enabled:
                if not activity_delivery.configured():raise ValueError('Канал отправки ещё не подключён на сервере.')
                try:await to_thread(activity_delivery.credential_url)
                except Exception:raise ValueError('Защищённый вебхук недоступен отправителю.') from None
            revision=await to_thread(store.save_settings,actor_subject=actor.subject,expected_revision=int(form['revision'][0]),enabled=enabled,categories=form.get('category',[]),interval_seconds=int(form['interval'][0]))
            audit_event(event='activity_notifications_changed',actor=actor,system='activity-notifications',status='ok',arguments={'revision':revision})
            return RedirectResponse(PATH+'?saved=1',status_code=303)
        except PermissionError:return PlainTextResponse('Нет доступа или устарела форма. Обновите страницу.',status_code=403)
        except (ValueError,KeyError,UnicodeError):return PlainTextResponse('Настройки не сохранены. Проверьте форму, доступность канала и обновите страницу.',status_code=409)
        except Exception:return PlainTextResponse('Не удалось сохранить настройки. Повторите позже.',status_code=503)
