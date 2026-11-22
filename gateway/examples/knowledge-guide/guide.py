"""Manual corporate knowledge guide using the dedicated Bitrix OAuth identity."""
import argparse
import asyncio
import json
import re
import sys
from uuid import UUID

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from connection_check import ConnectionCheckError, check_connection

SPACE = 'space:organizational-awareness'
MCP_URL = 'https://platforma.akmetron.ru/mcp'
SCOPES = {'knowledge:read', 'skills:read'}
TOOLS = {'gateway_access_profile', 'gateway_knowledge_search', 'gateway_knowledge_document_get'}
PASSPORT = 'awareness/project-profile.md'


class GuideError(Exception):
    pass


async def call(session, name, **arguments):
    result = await session.call_tool(name, arguments)
    receipt = (result.meta or {}).get('ru.akmetron.platforma/request_id')
    if not isinstance(receipt, str) or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', receipt):
        receipt = None
    if result.isError:
        reference = f' ID запроса {receipt}.' if receipt else ''
        raise GuideError(f'Шлюз отклонил вызов {name}.{reference}')
    raw = next((v.text for v in result.content if v.type == 'text'), None)
    if raw is None:
        raw = (result.structuredContent or {}).get('result')
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        raise GuideError('Ответ шлюза не удалось прочитать.') from None
    if not isinstance(data, dict) or data.get('ok') is not True:
        raise GuideError(f'Шлюз не подтвердил вызов {name}.')
    if receipt:
        data['gateway_request_id'] = receipt
    return data


async def answer(session, subject, keyword=None):
    profile = (await call(session, 'gateway_access_profile'))['actor']
    if profile.get('subject') != subject or set(profile.get('scopes', [])) != SCOPES:
        raise GuideError('Учётная запись или права не совпадают с профилем справочника.')
    listing = await session.list_tools()
    if listing.nextCursor or {t.name for t in listing.tools} != TOOLS:
        raise GuideError('Набор доступных инструментов отличается от профиля справочника.')
    term = 'паспорт' if keyword is None else keyword.strip()
    if not 1 <= len(term) <= 120:
        raise GuideError('Введите короткое слово или фразу для поиска.')
    hits = (await call(session, 'gateway_knowledge_search', space=SPACE, query=term,
                       limit=10, include_personal=False, include_groups=False))['documents']
    for hit in hits:
        if hit.get('space') != SPACE or (keyword is None and hit.get('path') != PASSPORT):
            continue
        read = await call(session, 'gateway_knowledge_document_get', space=SPACE, document=hit['path'])
        doc = read['document']
        if doc.get('space') != SPACE or doc.get('path') != hit['path']:
            raise GuideError('Источник ответа не совпал с результатом поиска.')
        body = doc['content']
        if keyword is None:
            urls = set(re.findall(r'https://[a-z0-9.-]+/mcp\b', body, re.I))
            portals = set(re.findall(r'\b[a-z0-9-]+\.bitrix24\.ru\b', body, re.I))
            if len(urls) != 1 or len(portals) != 1:
                raise GuideError('В паспорте нет однозначного адреса MCP и портала входа.')
            result = f'Адрес подключения — {urls.pop()}. Вход — через {portals.pop()}.'
        else:
            lines = [line.strip() for line in body.splitlines() if term.casefold() in line.casefold()]
            if not lines:
                continue
            result = '\n'.join(lines[:3])[:900]
        response = {'answer': result, 'title': doc['title'], 'document': doc['path'], 'space': SPACE}
        if read.get('gateway_request_id'):
            response['gateway_request_id'] = read['gateway_request_id']
        return response
    raise GuideError('В разрешённых документах ответ не найден.')



CONTROL_PATH = 'acceptance/closed-control.md'
CONTROL_MARKER = 'AKMETRON-GUIDE-CLOSED-CONTROL'


def request_id(value):
    try:
        return str(UUID(str(value)))
    except ValueError:
        raise GuideError('Шлюз не выдал ID для сверки с журналом.') from None


async def confirm_revocation():
    print('Оставьте это окно открытым. Запретите knowledge:read только справочнику. '
          'После сохранения запрета нажмите Enter.', file=sys.stderr, flush=True)
    await asyncio.to_thread(input)


async def denied_read(session, path, permitted_codes):
    result = await session.call_tool('gateway_knowledge_document_get', {'space': SPACE, 'document': path})
    if not result.isError:
        raise GuideError('Контрольное чтение вернуло документ. Остановите приёмку.')
    try:
        error = json.loads(result.content[0].text)['error']
    except (ValueError, TypeError, IndexError, KeyError, AttributeError):
        raise GuideError('Причину отказа подтвердить не удалось.') from None
    if error not in permitted_codes:
        raise GuideError('Получена другая ошибка вместо ожидаемого отказа.')
    return {'error': error, 'request_id': request_id((result.meta or {}).get('ru.akmetron.platforma/request_id'))}


async def check_journey(session, subject, pause=confirm_revocation):
    passport = await answer(session, subject)
    request_id(passport.get('gateway_request_id'))
    if passport['answer'] != 'Адрес подключения — https://platforma.akmetron.ru/mcp. Вход — через akmetron.bitrix24.ru.':
        raise GuideError('Паспорт содержит другие реквизиты основной Платформы.')
    results = await call(session, 'gateway_knowledge_search', query=CONTROL_MARKER, space=SPACE,
                         limit=50, include_personal=False, include_groups=False)
    if results.get('documents') != [] or results.get('count') != 0:
        raise GuideError('Контрольная метка появилась в поиске. Остановите приёмку.')
    closed = await denied_read(session, CONTROL_PATH, {'addon_document_denied'})
    await pause()
    revoked = await denied_read(session, PASSPORT, {'addon_resource_or_operation_denied', 'knowledge_scope_required'})
    return {'passport': passport, 'control_matches': 0, 'closed_read': closed, 'after_revocation': revoked}


async def run(subject, keyword, acceptance=False):
    from browser_login import authorized_client
    await check_connection(MCP_URL, SCOPES)
    async with authorized_client(MCP_URL, SCOPES) as http:
        async with streamable_http_client(MCP_URL, http_client=http) as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                if acceptance:
                    return await check_journey(session, subject)
                return await answer(session, subject, keyword)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Справочник общих знаний Платформы.')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--subject', help='Полный несекретный ID выделенной учётной записи.')
    mode.add_argument('--check', action='store_true', help='Проверить доступность Платформы до личного входа.')
    parser.add_argument('--find', help='Найти фрагмент по слову. По умолчанию — адрес MCP и портал входа.')
    parser.add_argument('--acceptance', action='store_true', help='Приёмка с администратором в одной сессии.')
    args = parser.parse_args(argv)
    if args.acceptance and (args.check or args.find is not None):
        parser.error('Для приёмки укажите только --subject и --acceptance.')
    if args.check and args.find is not None:
        parser.error('Для поиска используйте --subject. Параметр --check проверяет подключение.')
    if args.subject is not None and not re.fullmatch(r'bitrix24:[a-f0-9]{32}:[1-9][0-9]{0,18}', args.subject):
        parser.error('Ожидается идентификатор Битрикс24.')
    try:
        if args.check:
            operation = check_connection(MCP_URL, SCOPES)
        elif args.acceptance:
            operation = run(args.subject, None, acceptance=True)
        else:
            operation = run(args.subject, args.find)
        output = asyncio.run(operation)
    except Exception as error:
        def reason(exc):
            if isinstance(exc, (GuideError, ConnectionCheckError)):
                return str(exc)
            if isinstance(exc, BaseExceptionGroup):
                return next((r for sub in exc.exceptions if (r := reason(sub))), None)
        parser.exit(1, (reason(error) or 'Не удалось подключиться к Платформе или завершить OAuth.') + '\n')
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
