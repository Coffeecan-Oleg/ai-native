"""Exercise the actual workflow shell block with a fake Docker executable."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
WORKFLOW=ROOT/'.github/workflows/corporate-release.yml'
BASH=shutil.which('bash')
if os.name=='nt' and Path('C:/Program Files/Git/bin/bash.exe').is_file():
    BASH='C:/Program Files/Git/bin/bash.exe'

def block():
    text=WORKFLOW.read_text(encoding='utf-8')
    value=text.split('          # BEGIN BUILD WATCHDOG\n',1)[1].split('          # END BUILD WATCHDOG',1)[0]
    return '\n'.join(line[10:] if line.startswith(' '*10) else line for line in value.splitlines())+'\n'

@unittest.skipUnless(BASH,'Bash is required')
class BuildWatchdogTests(unittest.TestCase):
    def run_build(self,mode='ok',seconds='5'):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);commands=root/'bin';commands.mkdir()
            (commands/'docker').write_text('#!/usr/bin/env bash\nprintf "synthetic-build-line\\n"\ncase "$FAKE_BUILD_MODE" in ok) exit 0;; fail) exit 7;; hang) sleep 10;; esac\n')
            # The shell command uses the same local Python as the test harness.
            (commands/'python3').write_text('#!/usr/bin/env bash\nexec "$PYTHON_FOR_BUILD_TEST" "$@"\n')
            for command in commands.iterdir():command.chmod(0o700)
            script=root/'build.sh';script.write_text(block(),encoding='utf-8')
            env=dict(os.environ,RUNNER_TEMP=root.as_posix(),SOURCE_GIT_SHA='a'*40,
                     GATEWAY_CI_BUILD_SECONDS=seconds,FAKE_BUILD_MODE=mode,
                     PYTHON_FOR_BUILD_TEST=Path(sys.executable).as_posix(),
                     BUILD_TEST_BIN=commands.as_posix(),BUILD_TEST_SCRIPT=script.as_posix())
            # MSYS needs POSIX PATH syntax; never replace the host environment.
            if os.name=='nt':
                launcher='test_bin=$(/usr/bin/cygpath -u "$BUILD_TEST_BIN"); export PATH="$test_bin:/usr/bin:/bin"; exec /usr/bin/bash "$BUILD_TEST_SCRIPT"'
                args=[BASH,'-c',launcher]
            else:
                env['PATH']=str(commands)+os.pathsep+env['PATH'];args=[BASH,str(script)]
            result=subprocess.run(args,env=env,cwd=ROOT,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=20)
            report=root/'gateway-ci-report/build.json'
            return result, json.loads(report.read_text()) if report.is_file() else None

    def test_success_report_does_not_deploy(self):
        result,report=self.run_build()
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue(report['successful']);self.assertFalse(report['deployment'])
        self.assertEqual(report['source_sha'],'a'*40)
        self.assertIn('synthetic-build-line',report['build_tail'])

    def test_build_error_is_saved_and_returned(self):
        result,report=self.run_build('fail')
        self.assertEqual(result.returncode,7,result.stderr)
        self.assertEqual(report['exit_code'],7);self.assertFalse(report['successful'])

    def test_hang_is_terminated_and_reported_before_job_timeout(self):
        result,report=self.run_build('hang','1')
        self.assertEqual(result.returncode,124,result.stderr)
        self.assertTrue(report['timeout_exit']);self.assertFalse(report['successful'])

    def test_invalid_or_extended_deadline_is_rejected(self):
        for seconds in ['0','1081','9999','x','1;echo injected']:
            with self.subTest(seconds=seconds):
                result,report=self.run_build(seconds=seconds)
                self.assertEqual(result.returncode,64,result.stderr)
                self.assertIsNone(report)

if __name__=='__main__':unittest.main()
