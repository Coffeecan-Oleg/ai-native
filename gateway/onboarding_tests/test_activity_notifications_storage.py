"""Disposable DB only; synthetic events and fake sender, never the corporate webhook."""
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from gateway_mcp.services.storage_core import _connect
from gateway_mcp.services.storage_audit import append_audit_event
from gateway_mcp.services import storage_activity_notifications as store, activity_delivery as worker
from gateway_mcp.services.activity_events import CATEGORIES

TEST_URL=os.getenv('ONBOARDING_TEST_DATABASE_URL','')
SUBJECT='bitrix24:'+'a'*32+':3318'


@unittest.skipUnless(TEST_URL,'Disposable PostgreSQL required')
class ActivityStorageTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ,{'GATEWAY_DATABASE_URL':TEST_URL,'GATEWAY_ACTIVITY_NOTIFICATIONS':'true'}))
        with _connect() as conn:
            self.assertEqual(conn.info.dbname,'platforma_onboarding_test')
            conn.execute('TRUNCATE platforma_activity_outbox RESTART IDENTITY')
            conn.execute("UPDATE platforma_activity_settings SET enabled=false,categories='[]',revision=1,interval_seconds=30,next_send_at=now(),overflow_count=0,worker_seen_at=NULL,worker_status='not_started'")

    def configure(self,enabled=True,categories=None,revision=1):
        return store.save_settings(actor_subject=SUBJECT,expected_revision=revision,enabled=enabled,categories=categories or list(CATEGORIES),interval_seconds=30)

    def add(self,name='corporate_login',args=None):
        append_audit_event({'ts_ms':1794314596000,'event':name,'actor':{'subject':SUBJECT},'status':'ok','arguments':args or {}})

    def due(self):
        with _connect() as conn:conn.execute("UPDATE platforma_activity_settings SET next_send_at=now()-interval '1 second'")

    def send(self,result=None):
        self.due()
        with patch('gateway_mcp.services.storage_admin_console.admin_audit_user_names',return_value={SUBJECT:'Иван Тестовый'}):
            return worker.run_once(sender=lambda _: result or {'outcome':'sent','message_id':123})

    def test_disabled_does_not_replay_history_on_enable(self):
        self.add();self.configure();self.assertEqual(store.snapshot()['rows'],[])
        self.add();self.assertEqual(len(store.snapshot()['rows']),1)

    def test_category_filters_and_disable_cancels_pending(self):
        rev=self.configure(categories=['logins']);self.add('skill_library_uploaded',{'skill_id':'shared-abc'});self.add()
        self.assertEqual(len(store.snapshot()['rows']),1)
        self.configure(enabled=False,revision=rev);self.assertEqual(store.snapshot()['rows'][0]['state'],'cancelled')

    def test_settings_stale_revision_rejected(self):
        self.configure()
        with self.assertRaises(ValueError):self.configure(revision=1)

    def test_confirmed_delivery_not_repeated(self):
        self.configure();self.add();self.assertEqual(self.send()['status'],'sent');self.assertEqual(self.send()['status'],'idle')
        self.assertEqual(store.snapshot()['rows'][0]['message_id'],123)

    def test_ambiguous_delivery_not_retried(self):
        self.configure();self.add();self.assertEqual(self.send({'outcome':'unknown'})['status'],'unknown');self.assertEqual(self.send()['status'],'idle')

    def test_restart_marks_abandoned_sending_unknown(self):
        self.configure();self.add()
        with _connect() as conn:conn.execute("UPDATE platforma_activity_outbox SET state='sending',attempted_at=now()-interval '3 minutes'")
        self.assertEqual(self.send()['status'],'idle');self.assertEqual(store.snapshot()['rows'][0]['state'],'unknown')

    def test_known_rate_limit_retries_and_attempt_limit(self):
        self.configure();self.add()
        for _ in range(5):self.send({'outcome':'rate_limited'})
        self.assertEqual(store.snapshot()['rows'][0]['state'],'failed')

    def test_atomic_audit_and_outbox(self):
        self.configure();self.add()
        row=store.snapshot()['rows'][0]
        with _connect() as conn:self.assertIsNotNone(conn.execute('SELECT id FROM audit_events WHERE id=%s',(row['audit_id'],)).fetchone())

    def test_queue_failure_does_not_lose_audit(self):
        self.configure()
        def sql_error(cur,*args):cur.execute('SELECT * FROM deliberately_absent_activity_fixture')
        with patch.object(store,'enqueue',side_effect=sql_error):self.add('notification_test_unique')
        with _connect() as conn:self.assertIsNotNone(conn.execute("SELECT id FROM audit_events WHERE event='notification_test_unique'").fetchone())

    def test_parallel_audit_events_have_no_duplicates(self):
        self.configure()
        with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(lambda _:self.add(),range(12)))
        self.assertEqual(len(store.snapshot()['rows']),12)
        self.assertEqual(self.send()['count'],4);self.assertEqual(store.snapshot()['counts']['pending'],8)

    def test_expired_pending_not_sent(self):
        self.configure();self.add()
        with _connect() as conn:conn.execute("UPDATE platforma_activity_outbox SET created_at=now()-interval '25 hours'")
        self.assertEqual(self.send()['status'],'idle');self.assertEqual(store.snapshot()['rows'][0]['state'],'expired')

    def test_worker_lock_prevents_parallel_delivery(self):
        self.configure();self.add()
        with _connect() as conn:
            conn.execute('SELECT pg_advisory_lock(%s)',(store.WORKER_LOCK,))
            self.assertEqual(self.send()['status'],'busy')

    def test_full_queue_keeps_audit_and_counts_overflow(self):
        self.configure()
        with _connect() as conn:
            conn.execute("INSERT INTO platforma_activity_outbox(audit_id,category,details) SELECT -n,'logins','{}'::jsonb FROM generate_series(1,5000) AS n")
        self.add()
        self.assertEqual(store.snapshot()['counts']['pending'],5000)
        self.assertEqual(store.snapshot()['settings']['overflow_count'],1)

    def test_disabling_one_category_preserves_the_other(self):
        rev=self.configure(categories=['logins','skill_changes'])
        self.add();self.add('skill_library_uploaded',{'skill_id':'shared-abc'})
        self.configure(categories=['logins'],revision=rev)
        self.assertEqual(store.snapshot()['counts'],{'pending':1,'cancelled':1})

    def test_pause_during_rate_limited_send_does_not_resurrect_queue(self):
        rev=self.configure();self.add();self.due()
        def rate_limited(_):
            self.configure(enabled=False,revision=rev)
            return {'outcome':'rate_limited'}
        with patch('gateway_mcp.services.storage_admin_console.admin_audit_user_names',return_value={}):
            worker.run_once(sender=rate_limited)
        self.assertEqual(store.snapshot()['rows'][0]['state'],'cancelled')
        self.configure(revision=rev+1)
        self.assertEqual(self.send()['status'],'idle')


if __name__=='__main__':unittest.main()
