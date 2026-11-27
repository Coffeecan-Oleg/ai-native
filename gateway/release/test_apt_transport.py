"""Real APT transport, synthetic loopback HTTP only; never install packages."""
import os
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CONFIG = Path(__file__).resolve().parents[1]/'locks/apt-build.conf'
APT_HELPER = Path('/usr/lib/apt/apt-helper')


@unittest.skipUnless(os.name=='posix' and APT_HELPER.is_file(),
                     'Real APT transport requires the Linux CI runner')
class AptTransportTests(unittest.TestCase):
    def setUp(self):
        self.count = 0
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                owner.count += 1
                if self.path == '/busy':
                    self.send_response(503)
                    self.end_headers()
                    return
                if self.path == '/stall':
                    time.sleep(4)
                self.send_response(200)
                self.send_header('Content-Length','21')
                self.end_headers()
                try:
                    self.wfile.write(b'synthetic test bytes!')
                except (BrokenPipeError, ConnectionResetError):
                    pass
        self.server = ThreadingHTTPServer(('127.0.0.1',0), Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def download(self, path):
        url = 'http://127.0.0.1:'+str(self.server.server_port)+path
        target = self.directory/'result.bin'
        # Same policy; shorten only this transport timer to test stalls quickly.
        command = [str(APT_HELPER),'-c',str(CONFIG),'-o','Acquire::http::Timeout=1',
                   '-o','Acquire::http::Proxy=DIRECT',
                   'download-file',url,str(target)]
        started = time.monotonic()
        run = subprocess.run(command,capture_output=True,text=True,timeout=8)
        self.elapsed = time.monotonic()-started
        return run, target

    def test_policy_is_read_by_real_apt_library(self):
        run = subprocess.run(['apt-config','-c',str(CONFIG),'shell','HTTP','Acquire::http::Timeout',
              'HTTPS','Acquire::https::Timeout','RETRIES','Acquire::Retries',
              'MODE','APT::Update::Error-Mode','IPV4','Acquire::ForceIPv4'],
              capture_output=True,text=True,timeout=3)
        self.assertEqual(run.returncode,0)
        for expected in ["HTTP='15'","HTTPS='15'","RETRIES='1'","MODE='any'","IPV4='true'"]:
            self.assertIn(expected,run.stdout)

    def test_successful_download(self):
        run, target = self.download('/ok')
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertEqual(target.read_bytes(),b'synthetic test bytes!')

    def test_503_fails_after_bounded_retry(self):
        run, target = self.download('/busy')
        self.assertNotEqual(run.returncode,0)
        self.assertIn('503',run.stdout+run.stderr)
        self.assertGreaterEqual(self.count,1)
        self.assertLessEqual(self.count,2)
        self.assertLess(self.elapsed,7)

    def test_stalled_transport_times_out(self):
        run, target = self.download('/stall')
        self.assertNotEqual(run.returncode,0)
        self.assertGreaterEqual(self.count,1)
        # APT's HTTP worker also retries connections internally. Acquire::Retries
        # alone is not a cap on TCP attempts; the transport and phase clocks are.
        self.assertLess(self.elapsed,7)


if __name__ == '__main__':
    unittest.main()
