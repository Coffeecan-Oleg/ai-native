"""Strict event projection: no prompts, responses, credentials or arbitrary error text."""
import re
from datetime import datetime, timezone, timedelta

CATEGORIES = {
    'logins': ('Входы сотрудников', 'Успешный вход в панель или личная авторизация MCP.'),
    'skill_changes': ('Изменения навыков', 'Загрузка, новая редакция, проверка, публикация и архив.'),
    'skill_reads': ('Чтение навыков', 'ИИ-приложение получило файл навыка. Это ещё не применение.'),
    'skill_runs': ('Применение навыков', 'Начало, завершение и ошибка по сообщению ИИ-приложения.'),
    'agents': ('Карточки агентов', 'Добавление и изменение карточки. Это не запуск агента.'),
    'access': ('Доступ и настройки', 'Изменения ролей, разрешений и настроек уведомлений.'),
    'integrations': ('Настройки интеграций', 'Сохранённые изменения подключений.'),
    'mcp_calls': ('Прочие обращения MCP', 'Факт вызова инструмента без запроса, результата и личной памяти.'),
    'errors': ('Ошибки и отказы', 'Неуспешные серверные действия без содержимого ошибки.'),
}
LIFECYCLE = {'submit':'отправил навык на проверку','publish':'опубликовал навык',
             'reject':'вернул навык на доработку','archive':'архивировал навык',
             'restore':'восстановил навык','adopt':'подключил управление версиями навыка',
             'withdraw':'отозвал навык с проверки'}
CHANGES = {'skill_library_uploaded':'загрузил навык','skill_library_renamed':'переименовал навык',
           'skill_draft_updated':'сохранил новую редакцию навыка'}
ACCESS = {'admin_role_granted','admin_access_request_decided','activity_notifications_changed'}
ACCESS_TOOLS = {'gateway_admin_grant_access_package','gateway_admin_revoke_access_package',
                'gateway_admin_grant_scope','gateway_admin_revoke_scope',
                'gateway_admin_grant_resource','gateway_admin_revoke_resource',
                'gateway_admin_access_request_decide'}


def identifier(value, limit=96):
    return value[:limit] if isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}',value) else ''


def project(payload):
    event = payload.get('event', '')
    tool = str(payload.get('tool', ''))
    args = payload.get('arguments')
    args = args if isinstance(args,dict) else {}
    actor = payload.get('actor')
    subject = actor.get('subject','') if isinstance(actor,dict) else ''
    # Personal memory/usage is deliberately absent from team messages, including errors.
    if payload.get('system') in {'memory','llm','llm-proxy'} or any(s in tool for s in ('memory','usage','llm')):
        return None
    category = action = object_id = ''
    if payload.get('status') in {'error','failed','denied','blocked','rate_limited'} and event != 'assistant_skill_event':
        if event == 'tool_call' or event.startswith(('corporate_','skill_','admin_','managed_')):
            category, action = 'errors', 'получил ошибку или отказ'
            object_id = identifier(tool)
    elif event in {'corporate_login','corporate_frame_login'} and payload.get('status') == 'ok':
        category, action = 'logins', 'вошёл в Платформу'
    elif event in CHANGES and payload.get('status') == 'ok' and not args.get('replayed'):
        category, action, object_id = 'skill_changes', CHANGES[event], identifier(args.get('skill_id'))
    elif event == 'skill_lifecycle_changed' and payload.get('status') == 'ok':
        category, action, object_id = 'skill_changes', LIFECYCLE.get(args.get('action'),'изменил состояние навыка'), identifier(args.get('skill_id'))
    elif event == 'assistant_skill_event' and args.get('event_type') in {'started','completed','failed'}:
        category = 'skill_runs'
        action = {'started':'начал применение навыка','completed':'завершил применение навыка','failed':'сообщил об ошибке применения навыка'}[args['event_type']]
        object_id = identifier(args.get('skill_id'))
    elif event == 'assistant_agent_profile_updated' and payload.get('status') == 'ok':
        category, action, object_id = 'agents', ('добавил карточку агента' if args.get('operation')=='create' else 'изменил карточку агента'), identifier(args.get('agent_key'))
    elif event in ACCESS and payload.get('status') == 'ok':
        category, action = 'access', ('изменил настройки уведомлений' if event=='activity_notifications_changed' else 'изменил права доступа')
    elif event == 'managed_integration_change' and payload.get('status') == 'ok':
        category, action = 'integrations', 'изменил настройки интеграции'
    elif event == 'tool_call' and payload.get('status') == 'ok':
        if tool in ACCESS_TOOLS and args.get('dry_run') is False:
            category, action, object_id = 'access', 'изменил права доступа', identifier(tool)
        elif tool == 'gateway_corporate_skill_read' and not args.get('check_only'):
            category, action, object_id = 'skill_reads', 'прочитал файл навыка', identifier(args.get('skill'))
        elif tool not in {'gateway_telemetry_skill_start','gateway_telemetry_skill_complete','gateway_telemetry_skill_fail'} and not tool.startswith('gateway_telemetry_skill'):
            category, action, object_id = 'mcp_calls', 'вызвал инструмент MCP', identifier(tool)
    if not category:
        return None
    subject = subject if isinstance(subject,str) and re.fullmatch(r'bitrix24:[a-f0-9]{32}:[1-9][0-9]{0,18}',subject) else ''
    ts = payload.get('ts_ms')
    return {'category':category,'action':action,'actor_subject':subject,'object_id':object_id,
            'reported':category=='skill_runs','ts_ms':ts if type(ts) is int and 0<ts<4102444800000 else 0}


def safe_name(value):
    # Prevent chat markup, mentions, links and line breaks in user-controlled names.
    value = str(value or '')
    value = ''.join(c for c in value if c.isalpha() or c in ' -').strip()
    return ' '.join(value.split())[:72]


def format_batch(rows, names, batch_id):
    lines = ['Платформа · действия сотрудников', '']
    for row in rows:
        item = row['details']
        subject = item.get('actor_subject','')
        name = safe_name(names.get(subject,'')) or ('Сотрудник №'+subject.rsplit(':',1)[-1] if subject else 'Пользователь не установлен')
        stamp = datetime.fromtimestamp(item.get('ts_ms',0)/1000,timezone(timedelta(hours=3))).strftime('%d.%m %H:%M') if item.get('ts_ms') else 'Время неизвестно'
        lines.append(f"{stamp} · {name} — {item['action']}" + (': '+item['object_id'] if item.get('object_id') else '') + '.')
    if any(r['details'].get('reported') for r in rows):
        lines.append('Применение навыка — по событию ИИ-приложения; качество результата не оценивалось.')
    lines.extend(['', 'Время МСК. Журнал: https://platforma.akmetron.ru/admin/audit', 'Отправка: '+identifier(batch_id)])
    return '\n'.join(lines)
