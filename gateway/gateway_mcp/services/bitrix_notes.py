"""Read two employee instructions from Bitrix24 with the caller's own OAuth grant."""
import json
import re
import time

import httpx

from gateway_mcp.services import bitrix_identity as identity, external_limits
from gateway_mcp.services.knowledge import _snippet

PATHS = {'bitrix24/notes/489.md': 489, 'bitrix24/notes/531.md': 531}
ROUTE = {'transport': 'bitrix24-rest', 'name': 'note.document.get'}
DENIED = {'BITRIX_REST_V3_EXCEPTION_ACCESSDENIEDEXCEPTION',
          'BITRIX_REST_V3_EXCEPTION_ENTITYNOTFOUNDEXCEPTION'}
# Links verified against the published table of contents. If a heading changes,
# its text remains searchable, with a citation to the article instead.
SECTIONS = {
    (489, 'Что понадобится'): ('chto-ponadobitsya', ''),
    (489, 'Шаг 1. Откройте Платформу в Битрикс24'): ('shag-1-otkroyte-platformu-v-bitriks24', ''),
    (489, 'Если не получилось'): ('esli-ne-poluchilos', ''),
    (489, 'Что прислать для помощи'): ('chto-prislat-dlya-pomoshchi', ''),
    (489, 'Шаг 2. Подключите Платформу в своём ИИ-приложении'):
        ('shag-2-podklyuchite-platformu-v-svoyem-ii-prilozhenii',
         'Как подключить помощника к Платформе? Как подключить MCP в ИИ-приложении?'),
    (489, 'Шаг 3. Отправьте проверочный запрос'):
        ('shag-3-otpravte-proverochnyy-zapros', 'Как проверить подключение помощника?'),
    (489, 'Коворк/Код: после добавления MCP перезапустите приложение'):
        ('kovork-kod-posle-dobavleniya-mcp-perezapustite-prilozhenie', 'Как подключить Коворк/Код?'),
    (531, '1. Найдите подходящую инструкцию'):
        ('1-naydite-podkhodyashchuyu-instruktsiyu', 'Как найти подходящий навык?'),
    (531, '2. Скопируйте запрос из карточки'):
        ('2-skopiruyte-zapros-iz-kartochki', 'Как применить найденный навык? Как использовать готовый навык?'),
    (531, '4. Проверьте результат и журнал'):
        ('4-proverte-rezultat-i-zhurnal', 'Где посмотреть результат и журнал применения навыка?'),
    (531, 'Часть А. Использовать готовый навык'): ('chast-a-ispolzovat-gotovyy-navyk', ''),
    (531, '3. Пройдите простой пример'): ('3-proydite-prostoy-primer', ''),
    (531, 'Часть Б. Поделиться своим навыком'): ('chast-b-podelitsya-svoim-navykom', ''),
    (531, '1. Опишите повторяемую задачу'): ('1-opishite-povtoryaemuyu-zadachu', ''),
    (531, '2. Получите файл SKILL.md'): ('2-poluchite-fayl-skill-md', ''),
    (531, '3. Загрузите черновик'): ('3-zagruzite-chernovik', 'Как загрузить черновик навыка?'),
    (531, '4. Отправьте на проверку и дождитесь публикации'):
        ('4-otpravte-na-proverku-i-dozhdites-publikatsii', ''),
    (531, '5. Передайте коллегам ссылку и готовый запрос'):
        ('5-peredayte-kollegam-ssylku-i-gotovyy-zapros', ''),
    (531, 'Как обновлять и убирать устаревшее'): ('kak-obnovlyat-i-ubirat-ustarevshee', ''),
    (531, 'Если навык должен работать с другим сервисом'): ('esli-navyk-dolzhen-rabotat-s-drugim-servisom', ''),
}


class SourceUnavailable(RuntimeError):
    """A safe public reason, without request content, upstream errors or credentials."""


def _token(subject):
    from gateway_mcp.services.storage import get_user_oauth_token
    member, user = identity.parse_subject(subject)
    identity.require_member(member)
    try:
        try:
            record = get_user_oauth_token(identity.PROVIDER, subject)
        except Exception:
            raise SourceUnavailable('bitrix_notes_unavailable') from None
        if not record or set(record['scopes']) != {'user_brief', 'note'}:
            raise SourceUnavailable('bitrix_notes_reconnect_required')
        envelope = json.loads(record['access_token'])
        if (envelope['member_id'] != member or record['metadata']['user_id'] != user
                or record['metadata']['portal'] != identity.PORTAL or envelope['expires_at'] <= time.time()):
            raise SourceUnavailable('bitrix_notes_reconnect_required')
        return envelope['access_token']
    except (TypeError, KeyError, ValueError):
        raise SourceUnavailable('bitrix_notes_reconnect_required') from None


def _item(actor, doc_id):
    token = _token(actor.subject)
    try:
        permit = external_limits.require(ROUTE, actor)
        external_limits.consume(permit, ROUTE, actor)
        with httpx.Client(follow_redirects=False, trust_env=False, timeout=4) as client:
            with client.stream('POST', f'https://{identity.PORTAL}/rest/api/note.document.get',
                               json={'id': doc_id, 'auth': token}) as response:
                status = response.status_code
                if status in (429, 503):
                    external_limits.cooldown(ROUTE, external_limits.retry_after(response.headers.get('Retry-After')))
                    raise SourceUnavailable('bitrix_notes_unavailable')
                buf = bytearray()
                for chunk in response.iter_bytes():
                    if len(buf) + len(chunk) > 256 * 1024:
                        raise SourceUnavailable('bitrix_notes_unavailable')
                    buf.extend(chunk)
        payload = json.loads(buf)
        error = payload.get('error')
        code = error.get('code') if isinstance(error, dict) else error
        if code in DENIED:
            return None
        if code in ('expired_token', 'insufficient_scope', 'INVALID_CREDENTIALS'):
            raise SourceUnavailable('bitrix_notes_reconnect_required')
        if status != 200 or error:
            raise SourceUnavailable('bitrix_notes_unavailable')
        item = payload['result']['item']
        if item['id'] != doc_id or item['collectionId'] != 29 or item['state'] != 'live':
            return None
        if not isinstance(item['markdown'], str) or not isinstance(item['title'], str):
            raise SourceUnavailable('bitrix_notes_unavailable')
        return item
    except (httpx.HTTPError, external_limits.LimitError, ValueError, TypeError, AttributeError, KeyError):
        raise SourceUnavailable('bitrix_notes_unavailable') from None


def read(actor, space, path):
    if not identity.notes_enabled() or path not in PATHS:
        raise PermissionError('corporate_knowledge_document_denied')
    item = _item(actor, PATHS[path])
    if item is None:
        raise PermissionError('corporate_knowledge_document_denied')
    return {'space': space, 'path': path, 'id': f'bitrix24-note-{item["id"]}',
            'title': item['title'], 'content': item['markdown'], 'kind': 'note',
            'source_url': f'https://{identity.PORTAL}/note/document/{item["id"]}/',
            'updated_at': item.get('contentUpdatedAt') or ''}


def sections(markdown):
    heading, body, fence = '', [], ''
    for line in markdown.splitlines():
        marker = re.match(r'^\s{0,3}(\x60{3,}|~{3,})(.*)$', line)
        if marker:
            run, tail = marker.groups()
            if not fence:
                fence = run
            elif run.startswith(fence) and not tail.strip():
                fence = ''
        match = None if fence or marker else re.match(r'^ {0,3}#{1,6}\s+(.+?)\s*#*\s*$', line)
        if match:
            if body:
                yield heading, '\n'.join(body).strip()
            heading, body = match[1].strip(), []
        else:
            body.append(line)
    if body:
        yield heading, '\n'.join(body).strip()


def best_section(document, question):
    doc_id = PATHS[document['path']]
    best = None
    for heading, body in sections(document['content']):
        if not body:
            continue
        anchor, aliases = SECTIONS.get((doc_id, heading), ('', ''))
        score, word = question.match(heading + '\n' + aliases, body)
        if score and (best is None or score > best['score']):
            best = {k: v for k, v in document.items() if k != 'content'}
            best.update(score=score, section=heading, snippet=_snippet(body, word, size=1800))
            if anchor:
                best['source_url'] += '#' + anchor
    if best is None:
        score, _ = question.match(document['title'], '')
        if score:
            best = {k: v for k, v in document.items() if k != 'content'}
            best.update(score=score, snippet=_snippet(document['content'], ''))
    return best
