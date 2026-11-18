import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import operations_check as ops
import operations_summary as summary
from test_operations_check import fixture, SHA


class SummaryTests(unittest.TestCase):
    def good(self):
        report = ops.assess(fixture(), SHA)
        report['checked_at'] = '2026-11-08T15:00:00+00:00'
        return report

    def test_success_time_and_limits(self):
        text = summary.summarize(self.good())
        self.assertIn('08.11.2026 18:00:00 МСК', text)
        self.assertIn('все 12 технических проверок пройдены', text)
        self.assertIn('Успех под владельцем не подтверждает доступ коллеги', text)
        self.assertIn('а не непрерывное наблюдение', text)

    def test_actual_missing_channel_contract(self):
        report = ops.assess({}, SHA)
        text = summary.summarize(report)
        self.assertIn('не проверено: 12', text)
        self.assertNotIn('все 12 технических проверок пройдены', text)
        self.assertEqual(text.count('Отсутствие ответа не доказывает'), 1)

    def test_each_channel_can_be_unknown_alone(self):
        for group in ('ssh', 'http'):
            for channel in fixture()[group]:
                with self.subTest(group=group, channel=channel):
                    data = fixture()
                    data[group][channel] = {'error': 'unavailable'}
                    self.assertIn('неполученные результаты', summary.summarize(ops.assess(data, SHA)))

    def test_failure_includes_matching_next_action(self):
        data = fixture()
        data['http']['/metrics']['status'] = 200
        text = summary.summarize(ops.assess(data, SHA))
        self.assertIn('НЕ ПРОЙДЕНО — Метрики', text)
        self.assertIn('Проверить защиту /metrics', text)

    def test_input_text_and_personal_data_never_copied(self):
        report = self.good()
        secret = 'synthetic-secret@example.invalid'
        report['extra'] = secret
        report['not_verified'] = [secret]
        for row in report['checks']:
            row['observed'] = secret
            row['expected'] = secret
        self.assertNotIn(secret, summary.summarize(report))

    def test_truncated_and_duplicate_report_rejected(self):
        for alter in (lambda r: r['checks'].pop(), lambda r: r['checks'].__setitem__(0, r['checks'][1])):
            report = self.good()
            alter(report)
            with self.assertRaises(ValueError):
                summary.summarize(report)

    def test_false_pass_unknown_names_malformed_flags_rejected(self):
        for name, value in [('schema', True), ('writes_performed', True), ('notifications_sent', True),
                            ('technical_checks_passed', 1), ('technical_checks_passed', False),
                            ('expected_revision', 'x\nsecret'), ('checked_at', '2026-11-08T18:00:00')]:
            report = self.good()
            report[name] = value
            with self.subTest(name=name), self.assertRaises(ValueError):
                summary.summarize(report)
        for field in ('name', 'status'):
            report = self.good()
            report['checks'][0][field] = 'secret'
            with self.assertRaises(ValueError):
                summary.summarize(report)

    def test_cli_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder)/'report.json', Path(folder)/'summary.txt'
            source.write_text(json.dumps(self.good()), encoding='utf-8')
            with patch('builtins.print'):
                self.assertEqual(summary.main(['--report', str(source), '--output', str(target)]), 0)
                before = target.read_bytes()
                self.assertEqual(summary.main(['--report', str(source), '--output', str(target)]), 2)
                self.assertEqual(target.read_bytes(), before)
                self.assertEqual(summary.main(['--report', str(source), '--output', str(source)]), 2)
            self.assertEqual(json.loads(source.read_text('utf-8')), self.good())

    def test_invalid_and_oversized_json(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)/'report.json'
            for data in (b'\xff', b'{', b' '*(summary.LIMIT+1)):
                source.write_bytes(data)
                with self.assertRaises(ValueError):
                    summary.load_report(source)

    def test_no_network_or_process_launch_for_render(self):
        with (patch('subprocess.run', side_effect=AssertionError('no subprocess')),
              patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('no network'))):
            self.assertIn('ПЛАТФОРМА', summary.summarize(self.good()))
