"""Manual dev knowledge guide using the dedicated Bitrix OAuth identity."""
import argparse
import asyncio
import json
import re

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from connection_check import ConnectionCheckError, check_connection

SPACE = 'space:organizational-awareness'
DEV_URL = 'https://platforma-dev.akmetron.ru/mcp'
SCOPES = {'knowledge:read', 'skills:read'}
TOOLS = {'gateway_access_profile', 'gateway_knowledge_search', 'gateway_knowledge_document_get'}
PASSPORT = 'awareness/project-profile.md'


class GuideError(Exception):
    pass


async def call(session, name, **arguments):
    result = await session.call_tool(name, arguments)
    if result.isError:
        raise GuideError(f'Шлюз отклонил вызов {name}.')
    raw = next((v.text for v in result.content if v.type == 'text'), None)
    if raw is None:
        raw = (result.structuredContent or {}).get('result')
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        raise GuideError('Ответ шлюза не удалось прочитать.') from None
    if not isinstance(data, dict) or data.get('ok') is not True:
        raise GuideError(f'Шлюз не подтвердил вызов {name}.')
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
        doc = (await call(session, 'gateway_knowledge_document_get', space=SPACE,
                          document=hit['path']))['document']
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
        return {'answer': result, 'title': doc['title'], 'document': doc['path'], 'space': SPACE}
    raise GuideError('В разрешённых документах ответ не найден.')


async def run(subject, keyword):
    from browser_login import authorized_client
    await check_connection(DEV_URL, SCOPES)
    async with authorized_client(DEV_URL, SCOPES) as http:
        async with streamable_http_client(DEV_URL, http_client=http) as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                return await answer(session, subject, keyword)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Справочник общих знаний Платформы (dev).')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--subject', help='Полный несекретный ID выделенной учётной записи.')
    mode.add_argument('--check', action='store_true', help='Проверить доступность dev до личного входа.')
    parser.add_argument('--find', help='Найти фрагмент по слову. По умолчанию — адрес MCP и портал входа.')
    args = parser.parse_args(argv)
    if args.check and args.find is not None:
        parser.error('Для поиска используйте --subject. Параметр --check проверяет подключение.')
    if args.subject is not None and not re.fullmatch(r'bitrix24:[a-f0-9]{32}:[1-9][0-9]{0,18}', args.subject):
        parser.error('Ожидается идентификатор Битрикс24.')
    try:
        output = asyncio.run(check_connection(DEV_URL, SCOPES) if args.check else run(args.subject, args.find))
    except Exception as error:
        def reason(exc):
            if isinstance(exc, (GuideError, ConnectionCheckError)):
                return str(exc)
            if isinstance(exc, BaseExceptionGroup):
                return next((r for sub in exc.exceptions if (r := reason(sub))), None)
        parser.exit(1, (reason(error) or 'Не удалось подключиться к dev или завершить OAuth.') + '\n')
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
