"""Read-only explanations and synthetic previews; no transport or storage calls."""
from datetime import datetime, timezone
from gateway_mcp.services.activity_events import CATEGORIES, format_batch, project


def readiness(settings, counts, *, transport, credential=None, now=None):
    now = now or datetime.now(timezone.utc)
    seen = settings.get('worker_seen_at')
    try:
        if isinstance(seen, str):
            seen = datetime.fromisoformat(seen.replace('Z', '+00:00'))
        fresh = isinstance(seen, datetime) and -5 <= (now-seen).total_seconds() < 30
    except (TypeError, ValueError, OverflowError):
        fresh = False
    status = settings.get('worker_status')
    checks = [
        ('Серверный канал', 'Подключён' if transport else 'Не подключён'),
        ('Защищённый вебхук', 'Доступен сервису' if credential is True else
         'Недоступен сервису' if credential is False else 'Не проверен'),
        ('Ответ отправителя', 'Свежий' if fresh else 'Нет свежего ответа'),
        ('Выбор владельца', 'Включено' if settings['enabled'] else 'Выключено'),
    ]
    if not transport:
        code, advice = 'channel_missing', 'Администратору нужно подключить канал на сервере. Включатель в этой форме его не создаёт.'
    elif credential is False or status == 'credential_unavailable':
        code, advice = 'credential_unavailable', 'Администратору нужно проверить защищённый файл и права сервисного пользователя. Значение вебхука в чат не присылайте.'
    elif not settings['enabled']:
        code, advice = 'disabled', 'Выберите категории и включите отправку, когда будете готовы. Старые события не рассылаются.'
    elif not fresh:
        code, advice = 'worker_stale', 'Обновите страницу. Если свежего ответа всё ещё нет, администратору нужно проверить процесс отправителя; повторно включать рассылку не требуется.'
    elif status == 'unknown':
        code, advice = 'delivery_unknown', 'Откройте служебный чат и проверьте сообщение по коду отправки. Автоматического повтора нет, чтобы не создать дубль.'
    elif status == 'rejected':
        code, advice = 'delivery_rejected', 'Битрикс24 отклонил отправку. Администратору нужно проверить права системной учётной записи и доступ к служебному чату.'
    elif status == 'rate_limited':
        code, advice = 'rate_limited', 'Битрикс24 ограничил частоту. Повтор выполняется автоматически с паузой не менее минуты, до пяти попыток. Нажимать включатель повторно не нужно.'
    elif status not in {'ready', 'sent'}:
        code, advice = 'worker_unknown', 'Состояние отправителя не подтверждено. Обновите страницу и передайте администратору время проверки.'
    elif counts.get('pending', 0):
        code, advice = 'waiting', 'События ждут своего интервала. В одном сообщении до четырёх событий; остальные остаются в очереди.'
    else:
        code, advice = 'ready', 'Отправитель отвечает. Очередь пуста: новое выбранное действие появится здесь после его выполнения.'
    label = ('Выключены' if not settings['enabled'] else
             'Готовы к отправке' if code in {'ready', 'waiting'} else 'Нужна проверка')
    return {'code':code, 'label':label, 'advice':advice, 'checks':checks,
            'history_needs_attention':sum(counts.get(k,0) for k in ('failed','unknown','expired'))}


def preview_message(category, *, now=None):
    if category not in CATEGORIES:
        raise ValueError('unknown_preview_category')
    samples = {
        'logins': {'event':'corporate_login'},
        'skill_changes': {'event':'skill_lifecycle_changed','arguments':{'action':'publish','skill_id':'shared-primer'}},
        'skill_reads': {'event':'tool_call','tool':'gateway_corporate_skill_read','arguments':{'skill':'shared-primer'}},
        'skill_runs': {'event':'assistant_skill_event','arguments':{'event_type':'completed','skill_id':'shared-primer'}},
        'agents': {'event':'assistant_agent_profile_updated','arguments':{'operation':'create','agent_key':'primer-agent'}},
        'access': {'event':'activity_notifications_changed'},
        'integrations': {'event':'managed_integration_change'},
        'mcp_calls': {'event':'tool_call','tool':'gateway_access_profile'},
        'errors': {'event':'tool_call','tool':'gateway_corporate_skill_read','status':'denied'},
    }
    subject = 'bitrix24:'+'a'*32+':1'
    payload = {'status':'ok','actor':{'subject':subject},
               'ts_ms':int((now or datetime.now(timezone.utc)).timestamp()*1000), **samples[category]}
    item = project(payload)
    return ('ПРИМЕР · вымышленные данные · ничего не отправлено\n\n' +
            format_batch([{'details':item}],{subject:'Иван Примеров'},'example-no-send'))


def delivery_hint(state, error_code):
    hints = {
        'bitrix_rejected':'Проверьте права отправителя и доступ к служебному чату.',
        'delivery_unconfirmed':'Проверьте чат по коду отправки. Повтор автоматически не выполняется.',
        'interrupted_delivery':'Процесс прервался во время отправки. Проверьте чат перед любым повтором.',
        'disabled_by_owner':'Категория или вся рассылка выключена; это событие не будет дослано.',
        'event_too_old':'Событие ждало больше суток и не будет отправлено.',
        'bitrix_rate_limited':('Пять попыток исчерпаны; проверьте ограничения Битрикс24.' if state=='failed'
                               else 'Ожидает повторной попытки после ограничения Битрикс24.'),
    }
    return hints.get(error_code,'Подробности доступны администратору.' if error_code else '')
