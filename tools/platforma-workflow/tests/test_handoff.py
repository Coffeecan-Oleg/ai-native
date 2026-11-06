from datetime import datetime,timezone
import copy,json,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import handoff

NOW=datetime(2026,11,6,17,0,tzinfo=timezone.utc)
DIGEST='a'*64

def fixture():
    return {'schema':1,'status':'ready','package_sha256':DIGEST,'recorded_at':'2026-11-06T17:00:00Z',
      'development':{'public_url':'https://platforma-dev.akmetron.ru','https_route_verified':True},
      'production':{k:True for k in handoff.PRODUCTION_FLAGS},
      'source_copies':[{'path':p,'sha256':'b'*64,'bytes':123} for p in sorted(handoff.SOURCE_PATHS)],'notes':[]}

class HandoffTests(unittest.TestCase):
    def parse(self,data):return handoff.parse_receipt(json.dumps(data).encode(),DIGEST,now=NOW)
    def test_complete_receipt_remains_report_not_deploy_permission(self):
        r=self.parse(fixture())
        self.assertTrue(r['operator_report_complete']);self.assertFalse(r['deployment_authorized'])
        self.assertFalse(r['server_changes_performed']);self.assertIn('not_independently_verified',r['evidence_level'])
    def test_missing_timestamp_is_compatible_but_not_ready(self):
        d=fixture();del d['recorded_at'];r=self.parse(d)
        self.assertFalse(r['fresh']);self.assertFalse(r['operator_report_complete'])
    def test_old_and_future_receipts_are_not_fresh(self):
        for stamp in ['2026-11-04T17:00:00Z','2026-11-06T17:10:00Z']:
            d=fixture();d['recorded_at']=stamp
            with self.subTest(stamp=stamp):self.assertFalse(self.parse(d)['fresh'])
    def test_invalid_date_rejected_without_echo(self):
        for stamp in ['2026-02-30T17:00:00Z','now','2026-11-06T17:00:00+00:00']:
            d=fixture();d['recorded_at']=stamp
            with self.subTest(stamp=stamp),self.assertRaisesRegex(handoff.ReceiptError,'timestamp_invalid'):self.parse(d)
    def test_wrong_package_cannot_be_complete(self):
        d=fixture();d['package_sha256']='c'*64;r=self.parse(d)
        self.assertFalse(r['package_matches']);self.assertIn('package_identity_mismatch',r['remaining'])
    def test_unknown_fields_and_credentials_are_rejected(self):
        for target in ['root','development','production']:
            d=fixture();obj=d if target=='root' else d[target];obj['client_secret']='synthetic-never-print'
            with self.subTest(target=target),self.assertRaisesRegex(handoff.ReceiptError,'invalid_fields'):self.parse(d)
    def test_misleading_urls_and_commands_are_not_followed(self):
        for url in ['https://platforma-dev.akmetron.ru.evil.test','https://user:pass@platforma-dev.akmetron.ru','https://platforma-dev.akmetron.ru/?token=x','http://platforma-dev.akmetron.ru','https://platforma-dev.akmetron.ru;deploy']:
            d=fixture();d['development']['public_url']=url
            with self.subTest(url=url),self.assertRaisesRegex(handoff.ReceiptError,'url_invalid'):self.parse(d)
    def test_source_copy_paths_are_fixed_and_unique(self):
        for path in ['config/.env','runtime/../../x','runtime/other.py','runtime/workflow-dispatcher-source.py;id']:
            d=fixture();d['source_copies'][0]['path']=path
            with self.subTest(path=path),self.assertRaisesRegex(handoff.ReceiptError,'path_invalid'):self.parse(d)
        d=fixture();d['source_copies'][1]=copy.deepcopy(d['source_copies'][0])
        with self.assertRaisesRegex(handoff.ReceiptError,'path_invalid'):self.parse(d)
    def test_boolean_strings_and_integer_flags_rejected(self):
        for value in [1,'true',None,[],{}]:
            d=fixture();d['production']['legacy_mutators_guarded']=value
            with self.subTest(value=value),self.assertRaisesRegex(handoff.ReceiptError,'boolean_required'):self.parse(d)
    def test_source_size_and_hash_validation(self):
        for field,value in [('bytes',True),('bytes',262145),('sha256','not-a-digest')]:
            d=fixture();d['source_copies'][0][field]=value
            with self.subTest(field=field,value=value),self.assertRaisesRegex(handoff.ReceiptError,'identity_invalid'):self.parse(d)
    def test_notes_are_not_echoed_to_output(self):
        d=fixture();d['notes']=['synthetic-secret-in-note'];r=self.parse(d)
        self.assertNotIn('synthetic-secret-in-note',json.dumps(r));self.assertEqual(r['notes_count'],1)
    def test_duplicate_fields_invalid_json_and_oversize(self):
        for raw in [b'{"schema":1,"schema":1}',b'{"schema":NaN}',b'\xff',b'[]',b' '*16385]:
            with self.subTest(raw=raw[:30]),self.assertRaises(handoff.ReceiptError):handoff.parse_receipt(raw,DIGEST,now=NOW)
    def test_expected_digest_required(self):
        with self.assertRaisesRegex(handoff.ReceiptError,'expected_digest_invalid'):handoff.parse_receipt(b'{}','anything')
    def test_partial_and_missing_sources_report_concrete_pending_items(self):
        d=fixture();d['status']='partial';d['source_copies']=[];d['production']['legacy_mutators_guarded']=False
        r=self.parse(d);self.assertFalse(r['operator_report_complete'])
        self.assertIn('legacy_mutators_guarded',r['remaining']);self.assertEqual(sum(s.startswith('missing_source:') for s in r['remaining']),2)
    def test_remote_command_is_one_fixed_read_and_no_shell(self):
        args=handoff.ssh_argv(Path('my config'))
        self.assertEqual(args[-2:],['platforma-production','files get runtime/workflow-handoff.json'])
        self.assertIn('BatchMode=yes',args);self.assertIn('StrictHostKeyChecking=yes',args)
        for command in ['deploy','apply','restore','load']:self.assertNotIn(command,args)
    def test_cli_roundtrip_and_secret_error_does_not_echo_input(self):
        script=Path(handoff.__file__).with_name('workflow.py')
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'receipt.json';path.write_text(json.dumps(fixture()),encoding='utf-8')
            args=[sys.executable,str(script),'handoff','--receipt',str(path),'--expected-package-sha256',DIGEST]
            p=subprocess.run(args,capture_output=True,text=True,timeout=10)
            self.assertEqual(p.returncode,0,p.stderr);self.assertTrue(json.loads(p.stdout)['ok'])
            d=fixture();d['secret']='synthetic-secret-never-print';path.write_text(json.dumps(d))
            p=subprocess.run(args,capture_output=True,text=True,timeout=10)
            self.assertEqual(p.returncode,1);self.assertNotIn('synthetic-secret',p.stdout+p.stderr)
            self.assertEqual(json.loads(p.stdout)['reason'],'handoff_invalid_fields')
    def test_remote_missing_receipt_does_not_expose_error_text(self):
        class Result:
            stdout=None
            def __enter__(self):
                import io
                self.stdout=io.BytesIO(b'synthetic-server-error-do-not-print');return self
            def __exit__(self,*args):pass
            def wait(self,timeout=None):return 1
        with patch('handoff.subprocess.Popen',return_value=Result()):
            with self.assertRaisesRegex(handoff.ReceiptError,'^handoff_receipt_unavailable$'):handoff.read_remote('cfg')
    def test_remote_timeout_terminates_local_ssh(self):
        from threading import Event
        done=Event()
        class Reader:
            def read(self,size):done.wait(2);return b''
        class Result:
            stdout=Reader()
            killed=False
            def __enter__(self):return self
            def __exit__(self,*args):done.set()
            def kill(self):self.killed=True;done.set()
            def wait(self,timeout=None):return 0
        proc=Result()
        with patch('handoff.subprocess.Popen',return_value=proc):
            with self.assertRaisesRegex(handoff.ReceiptError,'ssh_timeout'):handoff.read_remote('cfg',timeout=.01)
        self.assertTrue(proc.killed)
    def test_remote_oversize_terminates_reader(self):
        import io
        class Result:
            stdout=io.BytesIO(b'x'*(handoff.MAX_BYTES+100))
            killed=False
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def kill(self):self.killed=True
            def wait(self,timeout=None):return 0
        proc=Result()
        with patch('handoff.subprocess.Popen',return_value=proc):
            with self.assertRaisesRegex(handoff.ReceiptError,'size_invalid'):handoff.read_remote('cfg')
        self.assertTrue(proc.killed)

if __name__=='__main__':unittest.main()
