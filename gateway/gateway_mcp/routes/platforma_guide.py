"""Authenticated, read-only team guide; intake answers never leave the browser."""
from asyncio import to_thread
from html import escape
import json
from pathlib import Path
import secrets
from urllib.parse import quote

from starlette.exceptions import HTTPException
from starlette.responses import HTMLResponse, PlainTextResponse

from gateway_mcp.routes.admin_ui import admin_shell
from gateway_mcp.services import platforma_onboarding as onboarding
from gateway_mcp.services.observability import audit_event
from gateway_mcp.web import login_redirect, web_actor

PATH = '/platforma/how-it-works'
ASSETS = Path(__file__).resolve().parents[1] / 'help_assets'
DOCUMENTS = {'memo': 'Платформа-понятно-для-команды.txt',
             'card': 'Карточка-подключения-сервиса.txt', 'message': 'Сообщение-команде.txt'}
CSS = '''<style>
.stack{grid-template-columns:minmax(0,1fr)}.stack>*{min-width:0}
.guide-head{max-width:850px}.guide-head h1{margin:12px 0}.guide-head .lead{font-size:17px}
.guide-links,.guide-actions{display:flex;gap:12px;flex-wrap:wrap;margin:20px 0 0}
.guide-links a{font-size:14px}.guide-actions .button,.guide-actions button{white-space:normal;line-height:1.4}
.guide-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}
.guide-grid>*{min-width:0}.guide-choice{padding:24px;border:1px solid var(--color-border);border-radius:8px;background:var(--color-surface)}
.guide-choice h3{font-size:17px;margin:0}.guide-choice p{margin:12px 0 16px;color:var(--color-ink-soft)}
.guide-flow{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;list-style:none;padding:0;margin:24px 0}
.guide-flow li{padding:16px;border-radius:8px;background:var(--color-surface-muted);line-height:1.6}
.guide-flow strong{display:block;color:var(--color-accent)}
.guide-reading{max-width:850px}.guide-reading p,.guide-reading li{line-height:1.7}.guide-reading li+li{margin-top:10px}
.guide-note{padding:16px 20px;border-left:4px solid var(--color-accent);background:var(--color-surface-muted);line-height:1.7;margin:20px 0}
.guide-panel h2{margin-bottom:16px}.guide-panel h3{margin:24px 0 12px;font-size:16px}
.guide-panel summary{font-size:16px;font-weight:650;cursor:pointer;padding:12px 0;line-height:1.5}
.guide-memo{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.75;font-family:var(--font-sans);font-size:15px;max-width:850px}
.guide-prompt{width:100%;min-height:160px;box-sizing:border-box}
.guide-card-fields{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:24px;margin:24px 0}
.guide-card-fields label{min-width:0;line-height:1.5}.guide-card-fields small{font-weight:400;line-height:1.65;color:var(--color-ink-soft)}
.guide-card-fields textarea{width:100%;min-height:120px;resize:vertical}
.guide-panel textarea{font:inherit;font-size:14px;line-height:1.6;padding:12px;border:1px solid var(--color-border-strong);border-radius:6px;background:var(--color-surface);color:var(--color-ink)}
.guide-panel textarea:focus-visible{outline:2px solid var(--color-focus);outline-offset:2px}
.guide-card-output{width:100%;min-height:280px;resize:vertical}.guide-status{min-height:26px;line-height:1.6}
.guide-panel{scroll-margin-top:24px}.guide-panel a,.guide-head code{overflow-wrap:anywhere}
@media(max-width:700px){.guide-grid,.guide-card-fields{grid-template-columns:minmax(0,1fr)}.guide-flow{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:400px){.guide-flow{grid-template-columns:minmax(0,1fr)}.guide-actions>*{width:100%}}
</style>'''


def content():
    return json.loads((ASSETS/'platforma-guide.json').read_text(encoding='utf-8'))


def e(value):
    return escape(str(value), quote=True)


def _memo_sections(text):
    return ''.join('<p class="guide-memo">'+e(block)+'</p>' for block in text.split('\n\n'))


def render_body(data):
    fields = ''.join('<label for="'+e(row['id'])+'"><span>'+str(i)+'. '+e(row['question'])+
        '</span><small>'+e(row['hint'])+'</small><textarea id="'+e(row['id'])+
        '" data-card-answer maxlength="2000" autocomplete="off" placeholder="Можно написать: не знаю"></textarea></label>'
        for i,row in enumerate(data['fields'],1))
    questions = '\n'.join(str(i)+'. '+row['question']+'\nОтвет: не указано\n'
                          for i,row in enumerate(data['fields'],1))
    blank = data['card_intro']+'\n\n'+questions+'\n'+data['card_footer']
    return '''<header class="guide-head">
<span class="status neutral">Учебная песочница · вымышленные данные</span>
<h1>Как это работает</h1>
<p class="lead">Общие навыки, данные и инструменты для ваших ИИ-ассистентов. Здесь — что уже можно делать и с чего начать подключение своего решения.</p>
<nav class="guide-links" aria-label="На этой странице"><a href="#first-step">С чего начать</a><a href="#basics">Как устроена Платформа</a><a href="#service-card">Карточка сервиса</a><a href="#full-guide">Полная памятка</a></nav>
</header>
<section id="first-step" class="guide-panel"><h2>Что вы хотите сделать?</h2>
<div class="guide-grid">
<article class="guide-choice"><h3>Поделиться навыком или инструкцией</h3><p>Загрузите текст с правилами работы. Коллега сможет прочитать его и применить в своём ассистенте.</p><a class="button" href="/platforma/skills">Открыть каталог навыков</a><p class="small">В каталоге нажмите «Загрузить навык», дайте понятное название и выберите .md или ZIP с SKILL.md. Откройте карточку и скопируйте готовый запрос своему ИИ. После переименования ID сохраняется.</p></article>
<article class="guide-choice"><h3>Подключить сервис или свой код</h3><p>UniSender, Битрикс24 или отдельный MCP-сервер подключаются вместе с администратором. Сначала опишите, что уже есть.</p><a class="button secondary" href="#service-card">Заполнить карточку сервиса</a><p class="small">Где нет ответа, напишите «не знаю». Не нужно сначала изучать всю архитектуру.</p></article>
<article class="guide-choice"><h3>Дать коллегам данные для примера</h3><p>К навыку можно приложить вымышленные текстовые справочники. Например, CSV в папке references внутри ZIP.</p><a href="#data-and-agents">Как делиться данными</a></article>
<article class="guide-choice"><h3>Поделиться работающим агентом</h3><p>Для процесса с кодом, моделью и расписанием понадобятся место запуска, права и способ остановки.</p><a href="#data-and-agents">Что подготовить для агента</a></article>
</div></section>
<section id="basics" class="panel guide-panel">
<h2>От вашего запроса до результата</h2>
<ol class="guide-flow"><li><strong>1. Вы</strong>Ставите задачу в своём ИИ-приложении.</li><li><strong>2. Ассистент</strong>Читает выбранный навык и выбирает доступный инструмент.</li><li><strong>3. MCP и Платформа</strong>Передают вызов; сервер проверяет личный доступ и выполняет разрешённую операцию.</li><li><strong>4. Результат</strong>Ассистент показывает ответ и источник. Вы проверяете, что получилось.</li></ol>
<div class="guide-reading"><p><strong>MCP</strong> — стандарт связи ИИ-приложения с сервером. <strong>Навык</strong> объясняет, как выполнить задачу. <strong>Инструмент</strong> — функция, которая действительно умеет читать или менять сведения в подключённой системе. <strong>Данные</strong> — факты, с которыми она работает. <strong>Плагин</strong> может содержать настройки подключения и инструкции.</p>
<p>Например, навык поможет написать черновик письма. Для получения статистики UniSender нужна отдельная интеграция. Для отправки нужна отдельно разрешённая функция отправки. Название «MCP рассылок» и роль администратора такую функцию не создают.</p>
<div class="guide-note"><strong>Сейчас доступно:</strong> учебные сервисы и заявки, карта знаний, общая библиотека текстовых навыков и собственный учёт их применения поддерживаемыми клиентами. <strong>UniSender пока не подключён.</strong> Автоматическая загрузка произвольных MCP-серверов и запуск загруженного кода не реализованы.</div>
<p>Яндекс подтверждает вашу личность для Платформы. Доступ к другому сервису настраивается отдельно. Прочитанные материалы попадут в контекст вашего ИИ-приложения: используйте только разрешённые для него сведения.</p></div>
</section>
<section id="data-and-agents" class="panel guide-panel guide-reading">
<h2>Как делиться данными и агентами</h2>
<p><strong>Текстовый учебный пример:</strong> добавьте вымышленные материалы к SKILL.md и попросите ассистента явно прочитать нужный файл. Каталог принимает текстовые файлы; PDF и изображения не поддерживаются. Общая загрузка документов и автоматическая синхронизация баз здесь не реализованы.</p>
<p><strong>Актуальные рабочие данные:</strong> оставляем их в исходной системе. Определяем разрешённые сведения, сотрудников и действия, затем подключаем серверный инструмент. Для начала выбираем одну операцию только для чтения.</p>
<p><strong>Работающий агент:</strong> кроме инструкции нужны код, среда запуска, владелец, права, журнал и способ остановки. Один SKILL.md не переносит модель, расписание и доступы. Если отдельный MCP-сервер уже работает, его можно подключить к поддерживающему приложению отдельно; включение в общий Gateway требует проверки и настройки.</p>
<p><strong>Как проверить результат:</strong> коллега читает навык под своим аккаунтом; интеграция выполняет нужную разрешённую операцию; для агента дополнительно проверены запуск, учёт и остановка. Ваш чат и токены при обмене навыком не передаются.</p>
</section>
<section id="service-card" class="panel guide-panel">
<h2>Карточка подключения сервиса</h2>
<p class="guide-reading">Заполните то, что знаете, скопируйте или скачайте карточку и передайте Олегу привычным рабочим способом. По ней решим, можно ли уже поделиться навыком, проверить готовый сервер или нужна разработка.</p>
<div class="guide-note"><strong>Ответы остаются в этом окне браузера.</strong> Они не отправляются на Платформу и не сохраняются в аккаунте. Скопируйте или скачайте их перед закрытием или обновлением страницы. Не вводите пароли, API-ключи, токены, полные OAuth-ссылки и реальные контакты клиентов.</div>
<noscript><p>Для заполнения прямо на странице включите JavaScript или скачайте пустую карточку и заполните её в текстовом редакторе.</p></noscript>
<div class="guide-card-fields">'''+fields+'''</div>
<label for="card-output">Готовый текст для передачи</label>
<textarea id="card-output" class="guide-card-output" readonly data-intro="'''+e(data['card_intro'])+'''" data-footer="'''+e(data['card_footer'])+'''">'''+e(blank)+'''</textarea>
<div class="guide-actions"><button type="button" id="card-copy" disabled>Скопировать карточку</button><button type="button" id="card-download" class="secondary" disabled>Скачать заполненную .txt</button><a class="button secondary" href="'''+PATH+'''/download?document=card">Скачать пустую .txt</a></div>
<p id="card-status" class="guide-status small" role="status" aria-live="polite"></p>
<details><summary>Не знаете, что уже готово? Запросите описание у своего ИИ</summary><p>Скопируйте этот запрос в приложение, где настраивали решение. Ответ проверьте и перенесите в поля выше.</p><textarea readonly class="guide-prompt" aria-label="Запрос своему ИИ для описания решения">'''+e(data['assistant_prompt'])+'''</textarea></details>
<h3>Что будет после карточки</h3><ol class="guide-reading"><li>Олег и разработчик проверят готовые компоненты и выберут одну операцию для пилота.</li><li>Владелец данных согласует состав данных, доступ и действия. Реквизиты сервиса вводятся отдельно защищённым способом.</li><li>Подключение проверим под разными сотрудниками: нужная операция работает, запрещённые действия недоступны, обращение видно в учёте.</li></ol>
<p class="small muted">Карточка — описание задачи. Она не подключает сервис автоматически и не разрешает рассылку или изменения рабочих данных.</p>
</section>
<section id="full-guide" class="panel guide-panel">
<h2>Полная памятка для команды</h2><p>Подробные шаги, пример с рассылками, роли и признаки готовности. Обновлено: '''+e(data['updated'])+'''.</p>
<details><summary>Читать памятку здесь</summary>'''+_memo_sections(data['documents']['memo'])+'''</details>
<div class="guide-actions"><a class="button secondary" href="'''+PATH+'''/download?document=memo">Скачать памятку .txt</a><a class="button secondary" href="'''+PATH+'''/download?document=message">Скачать краткое сообщение команде</a></div>
<p class="small">Для справки: <a href="https://modelcontextprotocol.io/docs/learn/architecture" rel="noreferrer">официальная архитектура MCP</a> · <a href="https://www.unisender.com/ru/support/api/common/api-key/" rel="noreferrer">API-ключ UniSender</a>.</p>
</section>'''


def _headers(nonce):
    return {'Cache-Control':'no-store', 'Referrer-Policy':'same-origin', 'X-Frame-Options':'DENY',
        'X-Content-Type-Options':'nosniff', 'Content-Security-Policy':
        "default-src 'none'; style-src 'self' 'unsafe-inline'; img-src 'self'; font-src 'self'; "
        "script-src 'nonce-"+nonce+"'; connect-src 'none'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'"}


def page_response(actor):
    nonce = secrets.token_urlsafe(24)
    script = (ASSETS/'platforma-guide.js').read_text(encoding='utf-8')
    body = render_body(content())+'<script nonce="'+nonce+'">'+script+'</script>'
    return HTMLResponse(admin_shell(title='Как это работает', active='how-it-works', actor=actor,
        body=body, extra_head=CSS, training_reader=not onboarding.is_owner(actor), shell_width='1320px'),
        headers=_headers(nonce))


async def _handle(request, download=False):
    actor = None
    try:
        if not onboarding.enabled():
            raise HTTPException(404)
        actor = web_actor(request)
        if actor is None:
            response = login_redirect(PATH)
            response.headers['Cache-Control'] = 'no-store'
            return response
        # Re-evaluate current approval/expiry for every page and attachment.
        if not onboarding.is_owner(actor) and not await to_thread(onboarding.active_reader,actor.subject):
            raise HTTPException(403)
        if download:
            if set(request.query_params) != {'document'} or len(request.query_params.getlist('document')) != 1:
                raise HTTPException(400)
            key = request.query_params['document']
            if key not in DOCUMENTS:
                raise HTTPException(400)
            response = PlainTextResponse(content()['documents'][key],headers=_headers(secrets.token_urlsafe(24)))
            response.headers['Content-Disposition'] = "attachment; filename=platforma-"+key+".txt; filename*=UTF-8''"+quote(DOCUMENTS[key])
            return response
        if request.query_params:
            raise HTTPException(400)
        return page_response(actor)
    except HTTPException as exc:
        message = 'Раздел недоступен. Проверь статус доступа: /join' if exc.status_code in {403,404} else 'Некорректный адрес. Открой раздел «Как это работает» из меню.'
        return PlainTextResponse(message,status_code=exc.status_code,headers=_headers(secrets.token_urlsafe(24)))
    except Exception as exc:
        audit_event(event='platforma_guide_error',actor=actor,system='platforma-guide',status='error',error=type(exc).__name__)
        return PlainTextResponse('Памятка временно недоступна. Попробуй позже.',status_code=503,
                                 headers=_headers(secrets.token_urlsafe(24)))


def register_platforma_guide_routes(mcp):
    @mcp.custom_route(PATH,methods=['GET'],include_in_schema=False)
    async def platforma_guide(request):
        return await _handle(request)

    @mcp.custom_route(PATH+'/download',methods=['GET'],include_in_schema=False)
    async def platforma_guide_download(request):
        return await _handle(request,download=True)
