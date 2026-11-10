"""Transactional outbox and owner settings. Only projected audit facts are stored."""
import json
from uuid import uuid4
from gateway_mcp.services.storage_core import _connect, ensure_schema
from gateway_mcp.services.activity_events import CATEGORIES, project

WORKER_LOCK = 163728941


def enqueue(cur, audit_id, payload):
    item = project(payload)
    if item is None:
        return
    cur.execute('SELECT enabled,categories FROM platforma_activity_settings WHERE id=1')
    settings = cur.fetchone()
    if not settings or not settings['enabled'] or item['category'] not in settings['categories']:
        return
    cur.execute('SELECT pg_advisory_xact_lock(%s)', (WORKER_LOCK+1,))
    # Recheck under a row lock: a concurrent pause must not leave an old event
    # pending after the settings transaction cancelled its queue.
    cur.execute('SELECT enabled,categories FROM platforma_activity_settings WHERE id=1 FOR SHARE')
    settings = cur.fetchone()
    if not settings['enabled'] or item['category'] not in settings['categories']:
        return
    cur.execute("SELECT count(*) AS n FROM platforma_activity_outbox WHERE state IN ('pending','sending')")
    if cur.fetchone()['n'] >= 5000:
        cur.execute('UPDATE platforma_activity_settings SET overflow_count=overflow_count+1 WHERE id=1')
        return
    cur.execute('''INSERT INTO platforma_activity_outbox(audit_id,category,details)
                   VALUES(%s,%s,%s::jsonb) ON CONFLICT(audit_id) DO NOTHING''',
                (audit_id,item['category'],json.dumps(item,ensure_ascii=False)))


def snapshot():
    ensure_schema()
    with _connect() as conn:
        settings = dict(conn.execute('SELECT * FROM platforma_activity_settings WHERE id=1').fetchone())
        counts = conn.execute('SELECT state,count(*) AS n FROM platforma_activity_outbox GROUP BY state').fetchall()
        rows = conn.execute('''SELECT id,audit_id,category,details,state,created_at,attempted_at,completed_at,
                              attempts,batch_id,message_id,error_code FROM platforma_activity_outbox
                              ORDER BY id DESC LIMIT 40''').fetchall()
    return {'settings':settings,'counts':{r['state']:r['n'] for r in counts},'rows':[dict(r) for r in rows]}


def save_settings(*, actor_subject, expected_revision, enabled, categories, interval_seconds):
    if (type(enabled) is not bool or type(expected_revision) is not int or expected_revision < 1
            or not isinstance(categories,list) or any(not isinstance(c,str) or c not in CATEGORIES for c in categories)
            or len(set(categories)) != len(categories) or type(interval_seconds) is not int or interval_seconds not in {30,60,300,900}
            or enabled and not categories):
        raise ValueError('Проверьте выбранные события и интервал отправки.')
    ensure_schema()
    with _connect() as conn:
        row = conn.execute('''UPDATE platforma_activity_settings SET enabled=%s,categories=%s::jsonb,
                    interval_seconds=%s,revision=revision+1,updated_by=%s,updated_at=clock_timestamp(),
                    next_send_at=clock_timestamp()+make_interval(secs=>%s)
                    WHERE id=1 AND revision=%s RETURNING revision''',
                    (enabled,json.dumps(categories),interval_seconds,actor_subject,interval_seconds,expected_revision)).fetchone()
        if not row:
            raise ValueError('Настройки уже изменились. Обновите страницу и повторите выбор.')
        conn.execute("""UPDATE platforma_activity_outbox SET state='cancelled',completed_at=clock_timestamp(),
                        error_code='disabled_by_owner' WHERE state='pending' AND (NOT %s OR NOT(category=ANY(%s)))""",
                     (enabled,categories))
        return row['revision']


def recover_and_prune(conn):
    conn.execute("""UPDATE platforma_activity_outbox SET state='unknown',completed_at=clock_timestamp(),
                    error_code='interrupted_delivery' WHERE state='sending' AND attempted_at < now()-interval '2 minutes'""")
    conn.execute("""UPDATE platforma_activity_outbox SET state='expired',completed_at=clock_timestamp(),
                    error_code='event_too_old' WHERE state='pending' AND created_at < now()-interval '24 hours'""")
    conn.execute("DELETE FROM platforma_activity_outbox WHERE state NOT IN ('pending','sending') AND completed_at < now()-interval '30 days'")


def claim(conn):
    # Serialize with settings changes until claimed rows are committed as sending.
    settings = conn.execute('SELECT *,next_send_at <= clock_timestamp() AS due FROM platforma_activity_settings WHERE id=1 FOR UPDATE').fetchone()
    recover_and_prune(conn)
    conn.execute("UPDATE platforma_activity_settings SET worker_seen_at=clock_timestamp(),worker_status='ready' WHERE id=1")
    if not settings['enabled'] or not settings['due']:
        conn.commit()
        return [], ''
    conn.execute("""UPDATE platforma_activity_outbox SET state='cancelled',completed_at=clock_timestamp(),
                    error_code='disabled_by_owner' WHERE state='pending' AND NOT(category=ANY(%s))""",(settings['categories'],))
    # Four bounded lines keep the fixed message under the transport limit, even with long names/IDs.
    rows = conn.execute("SELECT id,details FROM platforma_activity_outbox WHERE state='pending' ORDER BY id LIMIT 4 FOR UPDATE SKIP LOCKED").fetchall()
    batch = 'act-'+uuid4().hex[:20]
    if rows:
        conn.execute("""UPDATE platforma_activity_outbox SET state='sending',attempts=attempts+1,
                        attempted_at=clock_timestamp(),batch_id=%s WHERE id=ANY(%s)""",(batch,[r['id'] for r in rows]))
        conn.execute('UPDATE platforma_activity_settings SET next_send_at=clock_timestamp()+make_interval(secs=>interval_seconds) WHERE id=1')
    conn.commit()  # Durable before POST; a terminated worker will never silently resend.
    return [dict(r) for r in rows], batch


def finish(conn, rows, result):
    settings = conn.execute('SELECT enabled,categories FROM platforma_activity_settings WHERE id=1 FOR UPDATE').fetchone()
    outcome = result.get('outcome')
    state = {'sent':'sent','rejected':'failed','unknown':'unknown','rate_limited':'pending'}.get(outcome,'unknown')
    message_id = result.get('message_id') if outcome == 'sent' else None
    if state == 'sent' and (type(message_id) is not int or message_id <= 0):
        state, message_id = 'unknown', None
    error = {'sent':'','rejected':'bitrix_rejected','unknown':'delivery_unconfirmed','rate_limited':'bitrix_rate_limited'}.get(outcome,'delivery_unconfirmed')
    conn.execute("""UPDATE platforma_activity_outbox SET state=CASE WHEN %s='pending' AND attempts>=5 THEN 'failed' ELSE %s END,
                    completed_at=CASE WHEN %s='pending' AND attempts<5 THEN NULL ELSE clock_timestamp() END,
                    message_id=%s,error_code=%s WHERE id=ANY(%s) AND state='sending'""",
                 (state,state,state,message_id,error,[r['id'] for r in rows]))
    if state == 'pending':
        conn.execute("""UPDATE platforma_activity_outbox SET state='cancelled',completed_at=clock_timestamp(),
                        error_code='disabled_by_owner' WHERE id=ANY(%s) AND state='pending'
                        AND (NOT %s OR NOT(category=ANY(%s)))""",
                     ([r['id'] for r in rows],settings['enabled'],settings['categories']))
        conn.execute("UPDATE platforma_activity_settings SET next_send_at=greatest(next_send_at,clock_timestamp()+interval '60 seconds') WHERE id=1")
    conn.execute('UPDATE platforma_activity_settings SET worker_status=%s,worker_seen_at=clock_timestamp() WHERE id=1',(outcome if outcome in {'sent','rejected','unknown','rate_limited'} else 'unknown',))
    conn.commit()
