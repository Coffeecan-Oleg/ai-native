"""Read-only status page, with live administrator authorization in the service."""
from asyncio import to_thread
from html import escape
from urllib.parse import urlencode
from starlette.responses import HTMLResponse,PlainTextResponse
from gateway_mcp.routes.admin_ui import admin_shell
from gateway_mcp.web import web_actor,login_redirect

PATH='/admin/integrations/status'
CSS='''<style>
.protection-summary{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}
.protection-summary article,.protection-card{background:var(--color-surface);border:1px solid var(--color-border);border-radius:var(--radius-panel);padding:24px}
.protection-summary strong{display:block;font-size:26px}.protection-summary p{margin:0;color:var(--color-muted)}
.protection-card{margin-top:20px}.protection-card header{display:flex;justify-content:space-between;gap:12px;align-items:start;flex-wrap:wrap}
.protection-card h2{margin:0}.protection-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:24px;margin-top:18px}
.protection-grid h3{font-size:15px;margin:0 0 8px}.protection-grid p{margin:6px 0}
.protection-notice{padding:12px 16px;background:var(--color-surface-muted);border-radius:var(--radius-control)}
.protection-card code{overflow-wrap:anywhere}.protection-card li{margin:6px 0}
.protection-timing{margin-top:20px;overflow-x:auto}.protection-timing table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
.protection-timing th,.protection-timing td{padding:10px 12px;text-align:left;border-bottom:1px solid var(--color-border);white-space:nowrap}
@media(max-width:700px){.protection-summary,.protection-grid{grid-template-columns:1fr}.protection-card{padding:18px}}
</style>'''
PHASE={'ready':'Готова к допуску','not_enabled':'Защита сбоев не включена','paused':'Пауза после сбоев',
    'probing':'Выполняется проверочный запрос','awaiting_probe':'Ожидает проверочный запрос','busy':'Все общие места заняты'}
OUTCOME={'success':'Успешные','errors':'Ошибки','limited':'Ограничения','denied':'Отказы доступа','other':'Другие результаты'}


def _timing(data):
    if not data['timed_events']:
        return f'''<section class="protection-timing"><h3>Длительность обработки Gateway</h3>
            <p>Замеров нет. Рассмотрено завершённых событий: {data['events_considered']}; старые события без замера не считаются нулевой задержкой.</p>
            <p class="muted">Показатели появятся после новых обращений. Обновление страницы их не создаёт.</p></section>'''
    def ms(value):return 'Нет замеров' if value is None else str(value)+' мс'
    rows=''.join('<tr><th scope="row">'+OUTCOME[g['outcome']]+'</th><td>'+str(g['timed'])+' / '+str(g['observed'])+'</td>'+
        ''.join('<td>'+ms(g[k])+'</td>' for k in ['p50_ms','p95_ms','max_ms'])+'</tr>' for g in data['groups'] if g['observed'])
    return f'''<section class="protection-timing"><h3>Длительность обработки Gateway</h3>
        <p>Выборка: последние до {data['sample_limit']} завершённых событий этого адаптера за 24 часа; сейчас {data['events_considered']}, с замером {data['timed_events']}.</p>
        <table aria-label="Длительность обработки Gateway"><thead><tr><th scope="col">Результат</th><th scope="col">Замерено / событий</th>
        <th scope="col">p50</th><th scope="col">p95</th><th scope="col">Максимум</th></tr></thead><tbody>{rows}</tbody></table>
        <p class="muted">p50 и p95 — время, в которое уложились не менее 50% и 95% измеренных обращений этой группы. Группы не смешиваются: быстрый отказ не улучшает задержку успешных вызовов. При малой выборке это отдельные наблюдения, а не обещание скорости.</p>
        <p class="muted">Измеряется обработка инструмента до фиксации результата в аудите: включая проверки допуска и ожидание внешнего ответа, без последующей записи аудита, отправки ответа клиенту и заключительной очистки. Это не время только API и не полное время ожидания в приложении.</p></section>'''

def _time(value):
    # Only projection timestamps, never untrusted provider strings.
    from datetime import datetime,timezone
    try:return datetime.fromisoformat(str(value)).astimezone(timezone.utc).strftime('%d.%m.%Y %H:%M:%S UTC')
    except (ValueError,TypeError):return 'Нет за последние 24 часа'

def _card(row):
    title=escape(row['label']);r=row['rate_policy'];s=row['safety_policy'];totals=row['totals']
    phase=row['phase'];badge='warning' if row['pauses'] or phase in {'paused','busy','probing','awaiting_probe'} else 'neutral'
    feature='<p>Маршруты чтения выключены настройкой. Наличие ключа и доступность сервиса этим экраном не проверяются.</p>' if row['route_enabled'] is False else ''
    rate=(f"На сотрудника: {r['USER_PER_MINUTE']}/мин, серия {r['USER_BURST']}. Общая: {r['SHARED_PER_MINUTE']}/мин, серия {r['SHARED_BURST']}." if r else 'Не включены для этого режима.')
    safety=(f"Одновременно: {s['USER_CONCURRENCY']} на сотрудника, {s['SHARED_CONCURRENCY']} общих. Занято общих мест: {row['active_calls']}. Бюджет: {s['TIMEOUT_SECONDS']} с." if s else 'Для этого адаптера защита сбоев не включена.')
    pauses=''.join('<li>'+escape(p['label'])+': '+str(p['seconds'])+' с, до '+escape(_time(p['until']))+'</li>' for p in row['pauses'])
    pause_text='<ul>'+pauses+'</ul>' if pauses else '<p>Действующая пауза не зарегистрирована.</p>'
    budget=(f"Доступно в общей короткой серии: {row['shared_burst_available']}." if row['shared_burst_available'] is not None else 'Общий бюджет не измеряется: лимитер выключен.')
    problem=row['latest_problem'];last='<p>Ошибок и отказов за последние 24 часа не зарегистрировано.</p>'
    if problem:
        last='<p>'+escape(problem['label'])+' · '+escape(_time(problem['at']))+'</p>'
        if problem['code']:last+='<p><code>'+escape(problem['code'])+'</code></p>'
        if problem['request_id']:
            url='/admin/audit?'+urlencode({'days':1,'gateway_request_id':problem['request_id']})
            last+='<p><a href="'+escape(url,quote=True)+'">Запрос '+escape(problem['request_id'])+' в журнале</a></p>'
    return f'''<article class="protection-card" aria-label="{title}"><header><div><h2>{title}</h2>
      <p class="muted">Маршрутов в текущем каталоге: {row['route_count']}</p></div>
      <span class="status {badge}">{escape(PHASE[phase])}</span></header>
      {feature}<div class="protection-grid"><section><h3>Сейчас: правила допуска</h3><p>{escape(rate)}</p><p>{escape(safety)}</p>
      <p>{escape(budget)} Ожидание по общим ограничениям: {row['shared_next_seconds']} с.</p>{pause_text}
      <p class="muted">Личная квота и права каждого сотрудника проверяются при вызове. Этот экран их не заменяет.</p></section>
      <section><h3>Наблюдения Gateway за 24 часа</h3><p>Всего: {totals['total']}. Успешно: {totals['success']}. Ошибки: {totals['errors']}.
      Ограничения: {totals['limited']}. Отказы доступа: {totals['denied']}.</p>
      <p>Последний успешный вызов Gateway: {escape(_time(row['last_success_at']))}.</p>
      <p class="muted">Это записи журнала за период, а не проверка доступности сервиса прямо сейчас.</p><h3>Последняя причина отказа или ошибки</h3>{last}</section></div>{_timing(row['timing'])}</article>'''

def render(actor,data=None,unavailable=False):
    if unavailable:
        body='<div class="page-head"><h1>Состояние интеграций</h1></div><div class="banner error">Не удалось прочитать состояние. Доступность сервисов неизвестна. Попробуйте обновить страницу позже.</div>'
    else:
        rows=data['rows'];pauses=sum(bool(r['pauses']) for r in rows);problems=sum(r['totals']['errors']+r['totals']['limited'] for r in rows)
        body=f'''<div class="page-head"><div><h1>Состояние интеграций</h1><p class="lead">Правила допуска, сохранённые паузы и наблюдения Gateway.</p></div>
          <a class="button secondary" href="{PATH}">Обновить состояние</a></div>
          <p>Снимок: {escape(_time(data['observed_at']))}. Показаны внешние адаптеры текущего каталога.</p>
          <p class="protection-notice">Страница не проверяет связь с UniSender и не снимает паузы. «Готова к допуску» означает состояние защиты; доступность сервиса и права сотрудника этим не подтверждены. Штатная проверка корпоративной учётной записи при входе сохраняется.</p>
          <section class="protection-summary" aria-label="Сводка интеграций"><article><strong>{len(rows)}</strong><p>Адаптеров в каталоге</p></article>
          <article><strong>{pauses}</strong><p>С действующей паузой</p></article><article><strong>{problems}</strong><p>Ошибок и ограничений за 24 часа</p></article></section>
          {''.join(_card(r) for r in rows) if rows else '<p class="empty-note">Внешних маршрутов в текущем каталоге нет.</p>'}
          <p class="muted">Ключи, параметры запросов и персональные записи здесь не отображаются. Обновление не изменяет лимиты и настройки; автоматического обновления нет.</p>'''
    return admin_shell(title='Состояние интеграций',active='integration-status',actor=actor,body=body,shell_width='1180px',extra_head=CSS)

def register_integration_status_routes(mcp):
    @mcp.custom_route(PATH,methods=['GET'],include_in_schema=False)
    async def page(request):
        actor=web_actor(request)
        if actor is None:return login_redirect(PATH)
        from gateway_mcp.services import external_status
        try:data=await to_thread(external_status.snapshot,actor)
        except PermissionError:return PlainTextResponse('Current administrator authority is required.',status_code=403)
        except Exception:
            return HTMLResponse(render(actor,unavailable=True),status_code=503,headers={'Cache-Control':'no-store','Referrer-Policy':'same-origin'})
        return HTMLResponse(render(actor,data),headers={'Cache-Control':'no-store','Referrer-Policy':'same-origin'})
