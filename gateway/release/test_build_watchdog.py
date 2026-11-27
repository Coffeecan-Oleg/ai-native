"""Real subprocess regressions; Docker, network and credentials are never used."""
import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

HELPER = Path(__file__).with_name('build-image.py')
spec = importlib.util.spec_from_file_location('build_image', HELPER)
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)
FAKE = '''import json,os,signal,subprocess,sys,time
from pathlib import Path
mode=sys.argv[1]
print('synthetic build output',flush=True)
if mode=='ok':sys.exit(0)
if mode=='fail':sys.exit(7)
if mode=='stubborn':signal.signal(signal.SIGTERM,signal.SIG_IGN)
if mode in ('pipe','tree'):
    child=subprocess.Popen([sys.executable,'-c','import time; time.sleep('+('2' if mode=='pipe' else '30')+')'])
    Path(sys.argv[2]).write_text(str(child.pid))
    if mode=='pipe':sys.exit(0)
if mode=='tail':
    sys.stdout.buffer.write((b'x'*1000+b'\\n')*100+b'last marker\\n'+bytes([255]))
    sys.exit(0)
time.sleep(30)
'''


class BuildWatchdogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fake = self.root/'fake-build.py'
        self.fake.write_text(FAKE, encoding='utf-8')
        self.pidfile = self.root/'child.pid'
        self.reports = self.root/'reports'

    def run_build(self, mode='ok', seconds=5, command=None):
        started = time.monotonic()
        code = build.supervise(command or [sys.executable,str(self.fake),mode,str(self.pidfile)],
                               self.reports,'a'*40,seconds=seconds,heartbeat=.2,kill_grace=.2)
        elapsed = time.monotonic()-started
        report = json.loads((self.reports/'build.json').read_text(encoding='utf-8'))
        return code, report, elapsed

    def test_success_report_does_not_deploy(self):
        code, report, _ = self.run_build()
        self.assertEqual(code,0)
        self.assertTrue(report['successful'])
        self.assertFalse(report['deployment'])
        self.assertFalse(report['real_credentials_provided'])
        self.assertEqual(report['state'],'completed')
        self.assertEqual(report['source_sha'],'a'*40)
        self.assertIn('synthetic build output',report['build_tail'])

    def test_build_error_is_saved_and_returned(self):
        code, report, _ = self.run_build('fail')
        self.assertEqual(code,7)
        self.assertEqual(report['exit_code'],7)
        self.assertFalse(report['successful'])

    def test_hang_stops_before_outer_job_deadline(self):
        code, report, elapsed = self.run_build('hang',1)
        self.assertEqual(code,124)
        self.assertTrue(report['timeout_exit'])
        self.assertTrue(report['client_termination_confirmed'])
        self.assertFalse(report['successful'])
        self.assertLess(elapsed,3)

    def test_child_retaining_stdout_does_not_delay_report(self):
        try:
            code, report, elapsed = self.run_build('pipe',1)
            self.assertEqual(code,0)
            self.assertTrue(report['successful'])
            self.assertLess(elapsed,1)
        finally:
            # The only synthetic child exits after two seconds; leave no handles.
            time.sleep(2.2)

    def test_log_tail_is_bounded_and_accepts_non_utf8(self):
        code, report, _ = self.run_build('tail')
        self.assertEqual(code,0)
        self.assertLessEqual(len(report['build_tail']),60)
        self.assertTrue(all(len(line)<=500 for line in report['build_tail']))
        self.assertIn('last marker',report['build_tail'])

    def test_missing_executable_has_failed_report(self):
        code, report, _ = self.run_build(command=[str(self.root/'missing-docker')])
        self.assertEqual(code,127)
        self.assertEqual(report['error_type'],'FileNotFoundError')
        self.assertFalse(report['successful'])

    def test_already_exited_client_is_not_signalled_again(self):
        process = subprocess.Popen([sys.executable,'-c','pass'])
        process.wait(timeout=3)
        target = patch.object(build.os,'killpg') if os.name=='posix' else patch.object(process,'terminate')
        with target as send_signal:
            self.assertEqual(build.stop_client(process,.2),(False,True))
            send_signal.assert_not_called()

    def test_invalid_deadline_rejected_before_spawn(self):
        for value in [0,-1,1081,True,'1',1.1]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.run_build(seconds=value)
        self.assertFalse(self.reports.exists())

    def test_cli_invalid_inputs_do_not_invoke_docker(self):
        for seconds, sha in [('0','a'*40),('1081','a'*40),('1;echo injected','a'*40),('1','invalid')]:
            env=dict(os.environ,GATEWAY_CI_BUILD_SECONDS=seconds,SOURCE_GIT_SHA=sha,PATH='')
            result=subprocess.run([sys.executable,str(HELPER),'--reports',str(self.reports)],
                                  env=env,capture_output=True,timeout=5)
            self.assertEqual(result.returncode,64)
        self.assertFalse(self.reports.exists())

    @unittest.skipUnless(os.name=='posix','POSIX process-group termination requires Linux acceptance')
    def test_sigterm_ignoring_client_is_killed_within_grace(self):
        code, report, elapsed = self.run_build('stubborn',1)
        self.assertEqual(code,124)
        self.assertTrue(report['forced_kill_or_signal_exit'])
        self.assertTrue(report['client_termination_confirmed'])
        self.assertLess(elapsed,3)

    @unittest.skipUnless(os.name=='posix','POSIX process-group termination requires Linux acceptance')
    def test_timeout_terminates_own_client_process_group(self):
        try:
            code, report, _ = self.run_build('tree',1)
            self.assertEqual(code,124)
            pid=int(self.pidfile.read_text())
            status=Path('/proc')/str(pid)/'status'
            deadline=time.monotonic()+1
            while status.exists() and 'State:\tZ' not in status.read_text() and time.monotonic()<deadline:
                time.sleep(.05)
            self.assertTrue(not status.exists() or 'State:\tZ' in status.read_text())
        finally:
            if self.pidfile.exists():
                try:os.kill(int(self.pidfile.read_text()),signal.SIGKILL)
                except ProcessLookupError:pass

    @unittest.skipUnless(os.name=='posix','POSIX cancellation signal requires Linux acceptance')
    def test_cancellation_saves_failed_report_and_restores_signal_handling(self):
        runner=self.root/'runner.py'
        runner.write_text('import importlib.util,sys\nfrom pathlib import Path\ns=importlib.util.spec_from_file_location("build",sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)\nsys.exit(m.supervise([sys.executable,sys.argv[2],"hang"],Path(sys.argv[3]),"a"*40,seconds=8,heartbeat=.2,kill_grace=.2))\n',encoding='utf-8')
        process=subprocess.Popen([sys.executable,str(runner),str(HELPER),str(self.fake),str(self.reports)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            deadline=time.monotonic()+3
            report_file=self.reports/'build.json'
            while time.monotonic()<deadline:
                if report_file.is_file() and json.loads(report_file.read_text())['state']=='running':break
                time.sleep(.02)
            else:self.fail('child did not start')
            process.send_signal(signal.SIGTERM)
            self.assertEqual(process.wait(timeout=3),143)
            report=json.loads(report_file.read_text())
            self.assertEqual(report['signal'],signal.SIGTERM)
            self.assertFalse(report['successful'])
            self.assertTrue(report['client_termination_confirmed'])
        finally:
            if process.poll() is None:process.kill();process.wait(timeout=2)


if __name__=='__main__':unittest.main()
