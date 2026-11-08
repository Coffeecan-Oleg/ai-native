#!/usr/bin/env python3
"""Render an existing operations_check report in Russian; offline, no mutations."""
import argparse
import datetime as dt
import json
import re
from pathlib import Path

LIMIT = 65536
CHECKS = {
    'gateway_healthy': ('Приложение Gateway', 'Проверить status и локальный журнал Gateway.'),
    'postgres_healthy': ('База PostgreSQL', 'Проверить status и состояние базы; не восстанавливать старый дамп автоматически.'),
    'ssh_auth_enabled': ('Авторизация на сервере', 'Сверить включение авторизации. Не расширять доступ для обхода ошибки.'),
    'ssh_revision': ('Версия на сервере', 'Сверить установленную версию с принятым выпуском.'),
    'image_tag': ('Тег Docker-образа', 'Сверить version и принятый выпуск; не запускать deploy автоматически.'),
    'schema': ('Версия схемы БД', 'Сверить counts и план миграций принятого выпуска.'),
    'https_health': ('Доступность сайта и базы по HTTPS', 'Проверить DNS, TLS, сеть и /healthz; причина требует уточнения.'),
    'https_auth_enabled': ('Авторизация по HTTPS', 'Сверить состояние Gateway и настройки авторизации.'),
    'https_revision': ('Версия по HTTPS', 'Сравнить публичную версию с version по SSH и принятым выпуском.'),
    'anonymous_mcp_denied': ('MCP защищён от входа без токена', 'Проверить HTTP-ответ и защиту /mcp. Ожидается 401 без токена.'),
    'anonymous_metrics_closed': ('Метрики закрыты от анонимного доступа', 'Проверить защиту /metrics. Ожидается 401, 403 или 404.'),
    'oauth_resource': ('Адрес подключения OAuth', 'Сверить metadata с текущим адресом https://platforma.akmetron.ru/mcp.'),
}
GROUPS = {
    'ssh_status': ('Состояние по SSH', tuple(list(CHECKS)[:4])),
    'ssh_version': ('Версия по SSH', ('image_tag',)),
    'ssh_counts': ('Схема по SSH', ('schema',)),
    '/healthz': ('Состояние по HTTPS', ('https_health', 'https_auth_enabled', 'https_revision')),
    '/mcp': ('Анонимный доступ MCP', ('anonymous_mcp_denied',)),
    '/metrics': ('Анонимный доступ к метрикам', ('anonymous_metrics_closed',)),
    '/.well-known/oauth-protected-resource/mcp': ('Настройки OAuth', ('oauth_resource',)),
}
LABELS = {'pass': 'ПРОЙДЕНО', 'fail': 'НЕ ПРОЙДЕНО', 'unknown': 'НЕ ПРОВЕРЕНО'}


def summarize(report):
    if not isinstance(report, dict) or type(report.get('schema')) is not int or report['schema'] != 1:
        raise ValueError('unsupported_report_schema')
    revision = report.get('expected_revision')
    if not isinstance(revision, str) or not re.fullmatch('[a-f0-9]{40}', revision):
        raise ValueError('invalid_revision')
    try:
        stamp = dt.datetime.fromisoformat(report['checked_at'])
        if stamp.tzinfo is None:
            raise ValueError
        stamp = stamp.astimezone(dt.timezone(dt.timedelta(hours=3)))
    except (TypeError, ValueError, KeyError, OverflowError):
        raise ValueError('invalid_check_time') from None
    if report.get('writes_performed') is not False or report.get('notifications_sent') is not False:
        raise ValueError('not_a_read_only_report')
    rows = report.get('checks')
    if not isinstance(rows, list) or not 1 <= len(rows) <= len(CHECKS):
        raise ValueError('invalid_checks')
    statuses = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('invalid_check')
        name, state = row.get('name'), row.get('status')
        if not isinstance(name, str) or not isinstance(state, str) or state not in LABELS:
            raise ValueError('invalid_check')
        if name in CHECKS:
            expanded = (name,)
        elif name in GROUPS and state == 'unknown':
            expanded = GROUPS[name][1]
        else:
            raise ValueError('unrecognized_check')
        for item in expanded:
            if item in statuses:
                raise ValueError('duplicate_check')
            statuses[item] = state
    # A truncated or edited report must never be summarized as fully healthy.
    if set(statuses) != set(CHECKS):
        raise ValueError('incomplete_report')
    passed = all(state == 'pass' for state in statuses.values())
    if type(report.get('technical_checks_passed')) is not bool or report['technical_checks_passed'] != passed:
        raise ValueError('inconsistent_report')
    counts = {key: sum(value == key for value in statuses.values()) for key in LABELS}
    lines = ['ПЛАТФОРМА — РАЗОВАЯ ТЕХНИЧЕСКАЯ ПРОВЕРКА',
             'Время проверки: ' + stamp.strftime('%d.%m.%Y %H:%M:%S') + ' МСК',
             'Ожидаемый выпуск: ' + revision,
             'Итог: ' + ('все 12 технических проверок пройдены.' if passed else 'есть отклонения или неполученные результаты.'),
             f"Пройдено: {counts['pass']}; не пройдено: {counts['fail']}; не проверено: {counts['unknown']}.", '']
    actions = []
    for name, (label, action) in CHECKS.items():
        state = statuses[name]
        lines.append(LABELS[state] + ' — ' + label)
        if state == 'fail':
            actions.append(action)
        elif state == 'unknown':
            actions.append('Повторить недоступную проверку из разрешённой сети и сверить личный SSH-доступ. Отсутствие ответа не доказывает поломку Платформы.')
    lines += ['', 'Следующий шаг:']
    if passed:
        actions = ['Если проблема осталась у сотрудника, проверить его личное подключение в конкретном ИИ-приложении и чате. Успех под владельцем не подтверждает доступ коллеги.']
    lines += ['- ' + action for action in dict.fromkeys(actions)]
    lines += ['', 'Границы результата:',
              'Это снимок на указанное время, а не непрерывное наблюдение.',
              'Не проверены: вход обычного сотрудника, его приложение, бизнес-операции, квота диска, внешняя резервная копия, восстановление production, доставка уведомлений и production-контроллер выпуска.',
              'Программа диагностики не меняла сервер и не отправляла уведомлений.',
              'В сводку не включаются исходные ответы, журналы, ключи, заголовки и персональные данные.',
              'Сводка предназначена для внутренней технической поддержки. Она не является подтверждением завершения всего проекта.']
    return '\n'.join(lines) + '\n'


def load_report(path):
    with path.open('rb') as stream:
        raw = stream.read(LIMIT + 1)
    if len(raw) > LIMIT:
        raise ValueError('report_too_large')
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError):
        raise ValueError('invalid_json_report') from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.report.resolve() == args.output.resolve():
            raise ValueError('input_and_output_must_differ')
        rendered = summarize(load_report(args.report))
        # Exclusive create prevents overwriting an existing result or a symlink.
        with args.output.open('x', encoding='utf-8', newline='\n') as stream:
            stream.write(rendered)
    except (OSError, ValueError):
        print('Сводка не создана: проверьте формат отчёта и новый путь результата. Существующие файлы не заменяются.')
        return 2
    print('Сводка создана. Это преобразование отчёта, а не новая проверка сервера.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
