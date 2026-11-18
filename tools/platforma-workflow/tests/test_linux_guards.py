"""Adapter guards without Docker; real lifecycle is covered in lab_integration."""
import io,json,os,tarfile,tempfile,unittest
from pathlib import Path
from release_control.core import Refused
from release_control.linux import Linux,write_bytes

class LinuxGuards(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.backend=Linux.__new__(Linux)
        self.backend.c={'project':'platforma','maximum_volume_bytes':1024}
    def tearDown(self):self.temp.cleanup()
    def archive(self,entries):
        path=self.root/'sample.tar.gz'
        with tarfile.open(path,'w:gz') as tar:
            for member,data in entries:tar.addfile(member,io.BytesIO(data) if member.isfile() else None)
        return path
    def member(self,name='safe.txt',data=b'safe'):
        m=tarfile.TarInfo(name);m.size=len(data);m.mode=0o600;m.uid=m.gid=10001
        return m,data
    def test_ordinary_snapshot_is_accepted(self):
        self.backend.validate_tar(self.archive([self.member()]))
    def test_unsafe_snapshot_entries_are_refused(self):
        for kind in ['traversal','absolute','symlink','hardlink','duplicate','mode','owner','size']:
            with self.subTest(kind=kind):
                m,data=self.member();items=[(m,data)]
                if kind=='traversal':m.name='../escape'
                if kind=='absolute':m.name='/escape'
                if kind=='symlink':m.type=tarfile.SYMTYPE;m.linkname='/etc/shadow';m.size=0
                if kind=='hardlink':m.type=tarfile.LNKTYPE;m.linkname='safe.txt';m.size=0
                if kind=='duplicate':items.append(self.member())
                if kind=='mode':m.mode=0o4755
                if kind=='owner':m.uid=12345
                if kind=='size':data=b'a'*1025;m.size=len(data);items=[(m,data)]
                with self.assertRaises(Refused):self.backend.validate_tar(self.archive(items))
    def test_inventory_has_no_working_env_mount_or_network(self):
        seen=[]
        def docker(*args,**kwargs):
            seen.extend(args);return json.dumps({'database_enabled':False,'pending':['0026_skill_lifecycle']}).encode()
        self.backend.docker=docker
        self.assertEqual(self.backend.inventory('sha256:'+'a'*64),['0026_skill_lifecycle'])
        self.assertEqual(seen[seen.index('--network')+1],'none')
        self.assertEqual(seen[seen.index('--user')+1],'10001:10001')
        self.assertTrue({'--read-only','--cap-drop','--memory','--pids-limit'}.issubset(seen))
        for denied in ['--env','--env-file','-e','-v','--volume','--mount','--privileged']:
            self.assertNotIn(denied,seen)
    def test_ordinary_service_excludes_concurrent_migration_job(self):
        normal='a'*64;job='b'*64
        self.backend.docker=lambda *args,**kw:(normal+'\n'+job+'\n').encode()
        def obj(cid):
            return {'Id':cid,'Config':{'Labels':{'com.docker.compose.project':'platforma',
                    'com.docker.compose.service':'gateway','com.docker.compose.oneoff':'False' if cid==normal else 'True'}}}
        self.backend.object=obj
        self.assertEqual(self.backend.container('gateway')['Id'],normal)
    def test_ambiguous_ordinary_service_is_refused(self):
        self.backend.docker=lambda *args,**kw:('a'*64+'\n'+'b'*64).encode()
        self.backend.object=lambda cid:{'Id':cid,'Config':{'Labels':{
            'com.docker.compose.project':'platforma','com.docker.compose.service':'gateway','com.docker.compose.oneoff':'False'}}}
        with self.assertRaisesRegex(Refused,'ambiguous'):self.backend.container('gateway')
    @unittest.skipUnless(os.name=='posix','POSIX metadata guard')
    def test_atomic_caddy_edit_preserves_existing_permissions(self):
        p=self.root/'platforma.caddy';p.write_bytes(b'before');p.chmod(0o640)
        before=p.stat();write_bytes(p,b'after');after=p.stat()
        self.assertEqual(p.read_bytes(),b'after')
        self.assertEqual((before.st_uid,before.st_gid,before.st_mode),(after.st_uid,after.st_gid,after.st_mode))
    @unittest.skipUnless(os.name=='posix','POSIX symlink guard')
    def test_config_edit_refuses_symlink(self):
        target=self.root/'real';target.write_bytes(b'unchanged')
        link=self.root/'link';link.symlink_to(target)
        with self.assertRaisesRegex(Refused,'symlink'):write_bytes(link,b'changed')
        self.assertEqual(target.read_bytes(),b'unchanged')
