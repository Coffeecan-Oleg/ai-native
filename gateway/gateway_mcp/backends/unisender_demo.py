"""One immutable fictional campaign. No imports, credentials or network I/O."""
CAMPAIGN_ID = 'LAB-UNISENDER-001'
ROUTE_NAME = 'platforma.unisender-demo.stats.get'


def campaign_stats(*, campaign_id):
    if not isinstance(campaign_id, str) or campaign_id != CAMPAIGN_ID:
        from gateway_mcp.backends.common import BackendRouteError
        raise BackendRouteError('Учебная кампания недоступна. Разрешён только ID LAB-UNISENDER-001.')
    # These are deliberately authored test numbers, not a snapshot of an account.
    return {'campaign_id': CAMPAIGN_ID, 'synthetic': True, 'read_only': True,
        'real_unisender_connected': False, 'upstream_contacted': False,
        'source': 'platforma-training:unisender-demo#'+CAMPAIGN_ID,
        'statistics': {'total':100, 'sent':100, 'delivered':98,
            'read_unique':25, 'read_all':30, 'clicked_unique':7, 'clicked_all':9,
            'unsubscribed':1, 'spam':0},
        'notice': 'Вымышленные учебные показатели. Это не данные аккаунта UniSender.',
        'unsupported_metrics': ['delivery_errors'],
        'metric_note': 'Отдельный счётчик ошибок здесь отсутствует. Разницу sent−delivered нельзя называть количеством ошибок.'}
