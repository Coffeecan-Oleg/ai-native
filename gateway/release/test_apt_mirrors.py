"""Authenticated synthetic APT repository; only loopback, never installation."""
import hashlib
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LOCKS = Path(__file__).resolve().parents[1]/'locks'
READY = os.name == 'posix' and all(shutil.which(x) for x in ['apt-get','gpg','gpgconf'])


def command(args, **kwargs):
    return subprocess.run(args,capture_output=True,text=True,timeout=12,
                          env=dict(os.environ,LC_ALL='C',LANG='C'),**kwargs)


@unittest.skipUnless(READY, 'Linux APT/GPG required for signed mirror tests')
class AptMirrorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key_temp = tempfile.TemporaryDirectory(prefix='gateway-test-key-')
        cls.key_home = Path(cls.key_temp.name)
        cls.gpg = ['gpg','--homedir',str(cls.key_home),'--batch','--pinentry-mode','loopback','--passphrase','']
        run = command(cls.gpg+['--quick-generate-key','Gateway synthetic mirror tests','ed25519','sign','1d'])
        if run.returncode:
            cls.key_temp.cleanup()
            raise RuntimeError('Cannot generate synthetic test signing key: '+run.stderr)
        exported = subprocess.run(cls.gpg+['--export'],capture_output=True,timeout=5)
        if exported.returncode:
            raise RuntimeError('Cannot export synthetic public key')
        cls.public_key = exported.stdout

    @classmethod
    def tearDownClass(cls):
        command(['gpgconf','--homedir',str(cls.key_home),'--kill','gpg-agent'])
        cls.key_temp.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='gateway-mirror-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.payload = b'!<arch>\nsynthetic test package bytes only\n'
        self.sha = hashlib.sha256(self.payload).hexdigest()
        self.package_path = 'pool/main/g/gateway-fixture/gateway-fixture_1.0_all.deb'
        self.version = '1.0'
        self.pool_mode = {'cdn':'ok','snapshot':'ok'}
        self.requests = {'cdn':[],'snapshot':[]}
        self.servers = {}
        self.files = {}
        owner = self
        for role in ['cdn','snapshot']:
            def handler(role=role):
                class Handler(BaseHTTPRequestHandler):
                    def log_message(self,*args):
                        pass
                    def do_GET(self):
                        path = self.path.lstrip('/')
                        owner.requests[role].append(path)
                        if path == owner.package_path:
                            mode = owner.pool_mode[role]
                            if mode == '503':
                                self.send_response(503);self.end_headers();return
                            data = (b'bad'+owner.payload[3:]) if mode == 'corrupt' else owner.payload
                        elif role=='snapshot' and path in owner.files:
                            data = owner.files[path]
                        else:
                            self.send_response(404);self.end_headers();return
                        self.send_response(200)
                        self.send_header('Content-Length',str(len(data)))
                        self.end_headers()
                        self.wfile.write(data)
                return Handler
            server = ThreadingHTTPServer(('127.0.0.1',0),handler())
            threading.Thread(target=server.serve_forever,daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            self.servers[role]=server
        self.pubkey = self.root/'test-public.gpg'
        self.pubkey.write_bytes(self.public_key)
        self.mirrors = self.root/'mirror.list'
        rows=[];seen=set()
        # Retain the actual production priorities and type restrictions.
        for line in (LOCKS/'debian.mirrors').read_text().splitlines():
            if not line or line.startswith('#'):continue
            uri,metadata = line.split('\t',1)
            role = 'snapshot' if '/archive/' in uri else 'cdn'
            if role in seen:continue
            seen.add(role)
            rows.append('http://127.0.0.1:'+str(self.servers[role].server_port)+'/\t'+metadata)
        self.mirrors.write_text('\n'.join(rows)+'\n')
        sources = self.root/'sources.sources'
        sources.write_text('Types: deb\nURIs: mirror+file:'+str(self.mirrors)+
            '\nSuites: synthetic\nComponents: main\nArchitectures: amd64\nSigned-By: '+str(self.pubkey)+'\n')
        for part in ['lists/partial','archives/partial','debs']:
            (self.root/part).mkdir(parents=True,exist_ok=True)
        (self.root/'status').write_text('')
        self.config = self.root/'apt.conf'
        self.config.write_text((LOCKS/'apt-build.conf').read_text()+f'''
Dir::Etc::sourcelist "{sources}";
Dir::Etc::sourceparts "-";
Dir::State::lists "{self.root/'lists'}";
Dir::State::status "{self.root/'status'}";
Dir::Cache::archives "{self.root/'archives'}";
Dir::Cache::pkgcache "";
Dir::Cache::srcpkgcache "";
Acquire::http::Proxy "DIRECT";
Acquire::http::Timeout "2";
Acquire::Retries "0";
APT::Get::AllowUnauthenticated "false";
Acquire::AllowInsecureRepositories "false";
Acquire::AllowDowngradeToInsecureRepositories "false";
''')
        self.make_repository()

    def make_repository(self, tamper=False):
        packages = ('Package: gateway-fixture\nVersion: '+self.version+
            '\nArchitecture: all\nMaintainer: Synthetic Test <test@example.invalid>\n'+
            'Filename: '+self.package_path+'\nSize: '+str(len(self.payload))+
            '\nSHA256: '+self.sha+'\nDescription: synthetic download only\n\n').encode()
        relative = 'main/binary-amd64/Packages'
        release = ('Origin: Gateway synthetic test\nLabel: Gateway synthetic test\nSuite: synthetic\n'+
            'Codename: synthetic\nDate: '+format_datetime(datetime.now(timezone.utc))+
            '\nArchitectures: amd64 all\nComponents: main\nSHA256:\n '+
            hashlib.sha256(packages).hexdigest()+' '+str(len(packages))+' '+relative+'\n')
        (self.root/'Release').write_text(release)
        run = command(self.gpg+['--yes','--output',str(self.root/'InRelease'),'--clearsign',str(self.root/'Release')])
        self.assertEqual(run.returncode,0,run.stderr)
        signed = (self.root/'InRelease').read_bytes()
        if tamper:signed=signed.replace(b'Origin: Gateway',b'Origin: Changed')
        self.files={'dists/synthetic/InRelease':signed,
                    'dists/synthetic/'+relative:packages}

    def update(self):
        return command(['apt-get','-c',str(self.config),'update'],cwd=self.root)

    def download(self):
        return command(['apt-get','-c',str(self.config),'download','gateway-fixture=1.0'],cwd=self.root/'debs')

    def test_cdn_never_supplies_indexes_and_pinned_bytes_match(self):
        updated=self.update();self.assertEqual(updated.returncode,0,updated.stderr)
        fetched=self.download();self.assertEqual(fetched.returncode,0,fetched.stderr)
        self.assertFalse(any(path.startswith('dists/') for path in self.requests['cdn']))
        self.assertIn(self.package_path,self.requests['cdn'])
        self.assertNotIn(self.package_path,self.requests['snapshot'])
        self.assertEqual(next((self.root/'debs').glob('*.deb')).read_bytes(),self.payload)

    def test_cdn_503_falls_back_to_frozen_snapshot(self):
        self.assertEqual(self.update().returncode,0)
        self.pool_mode['cdn']='503'
        fetched=self.download();self.assertEqual(fetched.returncode,0,fetched.stderr)
        self.assertIn(self.package_path,self.requests['cdn'])
        self.assertIn(self.package_path,self.requests['snapshot'])

    def test_all_failed_mirrors_refuse_download(self):
        self.assertEqual(self.update().returncode,0)
        self.pool_mode={'cdn':'503','snapshot':'503'}
        fetched=self.download()
        self.assertNotEqual(fetched.returncode,0)
        self.assertFalse(list((self.root/'debs').glob('*.deb')))

    def test_same_length_corruption_is_rejected_by_sha256(self):
        self.assertEqual(self.update().returncode,0)
        self.pool_mode={'cdn':'corrupt','snapshot':'corrupt'}
        fetched=self.download()
        self.assertNotEqual(fetched.returncode,0)
        self.assertIn('Hash Sum mismatch',fetched.stdout+fetched.stderr)

    def test_bad_signature_refuses_index(self):
        self.make_repository(tamper=True)
        updated=self.update()
        self.assertNotEqual(updated.returncode,0)
        self.assertTrue('signature' in updated.stderr.lower() or 'BADSIG' in updated.stderr)

    def test_missing_pinned_version_does_not_upgrade(self):
        self.version='1.1';self.make_repository()
        self.assertEqual(self.update().returncode,0)
        fetched=self.download()
        self.assertNotEqual(fetched.returncode,0)
        self.assertNotIn(self.package_path,self.requests['cdn'])
        self.assertNotIn(self.package_path,self.requests['snapshot'])

    def test_both_production_lists_keep_indexes_frozen(self):
        for name in ['debian.mirrors','debian-security.mirrors']:
            with self.subTest(archive=name):
                for line in (LOCKS/name).read_text().splitlines():
                    if not line or line.startswith('#'):continue
                    uri,metadata=line.split('\t',1)
                    self.assertTrue(uri.startswith('https://'))
                    if 'type:deb' not in metadata:
                        self.assertIn('/20261021T124057Z/',uri)
                        self.assertTrue(uri.startswith(('https://snapshot.debian.org/','https://snapshot-cloudflare.debian.org/')))


if __name__ == '__main__':
    unittest.main()
