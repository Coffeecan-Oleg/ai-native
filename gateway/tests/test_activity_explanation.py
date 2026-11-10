import unittest
from datetime import datetime, timezone, timedelta
from gateway_mcp.services.activity_explanation import readiness, preview_message, delivery_hint
from gateway_mcp.services.activity_events import CATEGORIES

NOW=datetime(2026,11,10,18,tzinfo=timezone.utc)

class ExplanationTests(unittest.TestCase):
    def settings(self,**kw):
        return {'enabled':True,'worker_seen_at':NOW,'worker_status':'ready',**kw}

    def test_missing_channel_and_secret_have_actionable_explanations(self):
        r=readiness(self.settings(),{},transport=False,now=NOW)
        self.assertEqual(r['code'],'channel_missing');self.assertIn('Администратору',r['advice'])
        r=readiness(self.settings(),{},transport=True,credential=False,now=NOW)
        self.assertEqual(r['code'],'credential_unavailable');self.assertIn('в чат не присылайте',r['advice'])

    def test_disabled_is_not_reported_as_live_delivery(self):
        r=readiness(self.settings(enabled=False),{},transport=True,credential=True,now=NOW)
        self.assertEqual(r['code'],'disabled');self.assertEqual(r['label'],'Выключены')

    def test_stale_malformed_and_future_heartbeat_are_not_healthy(self):
        for stamp in [NOW-timedelta(seconds=31),NOW+timedelta(minutes=1),None,'bad',datetime(2026,1,1)]:
            with self.subTest(stamp=stamp):
                r=readiness(self.settings(worker_seen_at=stamp),{},transport=True,credential=True,now=NOW)
                self.assertEqual(r['code'],'worker_stale')

    def test_delivery_outcomes_have_distinct_next_steps(self):
        for status,code,word in [('unknown','delivery_unknown','дубль'),('rejected','delivery_rejected','права'),
                                 ('rate_limited','rate_limited','автоматически'),('new_status','worker_unknown','не подтверждено')]:
            with self.subTest(status=status):
                r=readiness(self.settings(worker_status=status),{},transport=True,credential=True,now=NOW)
                self.assertEqual(r['code'],code);self.assertIn(word,r['advice'])

    def test_old_failures_are_separate_from_current_ready_status(self):
        r=readiness(self.settings(),{'failed':2,'unknown':1},transport=True,credential=True,now=NOW)
        self.assertEqual(r['code'],'ready');self.assertEqual(r['history_needs_attention'],3)

    def test_pending_queue_is_not_an_error(self):
        r=readiness(self.settings(),{'pending':5},transport=True,credential=True,now=NOW)
        self.assertEqual(r['code'],'waiting');self.assertIn('интервала',r['advice'])

    def test_all_previews_are_synthetic_and_use_current_formatter(self):
        for category in CATEGORIES:
            with self.subTest(category=category):
                text=preview_message(category,now=NOW)
                self.assertIn('ничего не отправлено',text);self.assertIn('Иван Примеров',text)
                self.assertIn('Отправка: example-no-send',text)
                self.assertLess(len(text),1800)
        self.assertIn('по событию ИИ-приложения',preview_message('skill_runs',now=NOW))

    def test_unknown_preview_is_not_reflected(self):
        with self.assertRaises(ValueError):preview_message('<script>SECRET</script>')

    def test_unknown_delivery_error_never_exposes_raw_text(self):
        self.assertNotIn('SECRET',delivery_hint('failed','SECRET webhook url'))
        self.assertIn('Пять попыток',delivery_hint('failed','bitrix_rate_limited'))
        self.assertIn('Ожидает',delivery_hint('pending','bitrix_rate_limited'))

if __name__=='__main__':unittest.main()
