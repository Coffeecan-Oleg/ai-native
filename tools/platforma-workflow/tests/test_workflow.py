import copy,gzip,hashlib,io,json,tempfile,tarfile,unittest
from pathlib import Path
import workflow

class WorkflowTests(unittest.TestCase):
    def test_environments_have_distinct_fixed_targets(self):
        dev=workflow.remote_command('dev',['plan','evening-123abc'])
        prod=workflow.remote_command('production',['plan','evening-123abc'])
        self.assertEqual(dev[0],'aiport');self.assertEqual(prod[0],'platforma-production')
        self.assertIn('/opt/platforma-dev/',dev[1]);self.assertTrue(prod[1].startswith('release plan '))
    def test_injection_and_private_worker_are_rejected(self):
        for args in [['_worker'],['plan','x;id'],['plan','-x'],['plan','x','--config','/tmp/other'],['apply','../../x','0'*64]]:
            with self.subTest(args=args),self.assertRaises(ValueError):workflow.remote_command('dev',args)
    def fixture(self,root):
        source='a'*40;tag='platforma-gateway:evening-'+source[:12]
        config=json.dumps({'config':{'User':'10001:10001','Labels':{'org.opencontainers.image.revision':source}}}).encode()
        digest=hashlib.sha256(config).hexdigest()
        archive=root/'image.tar.gz'
        with tarfile.open(archive,'w:gz') as tar:
            for name,data in [('manifest.json',json.dumps([{'RepoTags':[tag],'Config':digest+'.json','Layers':[]}]).encode()),(digest+'.json',config)]:
                info=tarfile.TarInfo(name);info.size=len(data);tar.addfile(info,io.BytesIO(data))
        report={'source_sha':source,'tag':tag,'image_id':'sha256:'+digest,'archive_sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),
          'archive_bytes':archive.stat().st_size,'production_configuration_included':False,'database_included':False,'private_skill_mount_included':False}
        path=root/'delivery.json';path.write_text(json.dumps(report));return archive,path,report
    def test_delivery_checks_config_digest_not_oci_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            a,p,d=self.fixture(Path(td));self.assertTrue(workflow.verify_delivery(a,p)['verified'])
            d['image_id']='sha256:'+'b'*64;p.write_text(json.dumps(d))
            with self.assertRaisesRegex(ValueError,'config_digest'):workflow.verify_delivery(a,p)
    def test_changed_archive_is_not_loaded(self):
        with tempfile.TemporaryDirectory() as td:
            a,p,d=self.fixture(Path(td));a.write_bytes(a.read_bytes()+b'changed')
            with self.assertRaisesRegex(ValueError,'size_invalid'):workflow.verify_delivery(a,p)
    def test_corporate_data_declaration_is_required(self):
        with tempfile.TemporaryDirectory() as td:
            a,p,d=self.fixture(Path(td));d['database_included']=True;p.write_text(json.dumps(d))
            with self.assertRaisesRegex(ValueError,'isolation'):workflow.verify_delivery(a,p)

if __name__=='__main__':unittest.main()
