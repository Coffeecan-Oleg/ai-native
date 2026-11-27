import base64,copy,hashlib,io,json,os,subprocess,tempfile,unittest
from pathlib import Path
from unittest.mock import patch,Mock
import image_import,import_client,workflow
from release_control.core import Refused,Store,atomic
from release_control.linux import Linux
import test_workflow

class Backend:
    def __init__(self,expected):self.expected=expected;self.loaded=False;self.current=None;self.calls=[];self.failure=None;self.runtime_value={'containers':['unchanged']}
    def guard(self):self.calls.append('guard')
    def capacity(self,path,required):
        self.calls.append(('capacity',str(path),required))
        if self.failure=='capacity':raise Refused('import_test_insufficient')
        return {'sufficient':True,'required_bytes':required}
    def runtime(self):return copy.deepcopy(self.runtime_value)
    def image(self,tag):return self.current
    def load(self,archive):
        self.calls.append('load')
        if self.failure=='load':raise Refused('import_command_failed')
        self.loaded=True
        self.current={'id':self.expected['image_id'],'user':'10001:10001','revision':self.expected['source_sha']}
        if self.failure=='identity':self.current['user']='0:0'
        if self.failure=='runtime':self.runtime_value['containers']=['changed']

class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name).resolve();(self.base/'state').mkdir(mode=0o700)
        self.archive,self.report,self.data=test_workflow.WorkflowTests().fixture(self.base)
        self.raw=self.archive.read_bytes();self.backend=Backend(self.data)
    def tearDown(self):self.temp.cleanup()
    def perform(self,raw=None):
        return image_import.perform(self.base,self.data,io.BytesIO(self.raw if raw is None else raw),self.backend)
    def marker(self):return self.base/'state/image-import-active.json'
    def journals(self):return list((self.base/'state/image-imports').glob('*/operation.json'))
    def test_metadata_rejects_extra_fields_wrong_types_or_real_data(self):
        for key,value in [('other','x'),('archive_bytes',True),('archive_bytes',0),('archive_bytes',2*1024**3+1),('tag','other'),('source_sha','z'*40),('database_included',True)]:
            data={**self.data,key:value}
            with self.subTest(key=key,value=value),self.assertRaises(Refused):image_import.metadata(data)
    def test_metadata_encoded_command_does_not_allow_paths_or_shell(self):
        argv=import_client.command(Path('local config'),self.data)
        self.assertEqual(argv[:5],['ssh','-T','-F','local config','aiport'])
        self.assertIn('/opt/platforma-dev/code/image_import.py import ',argv[5]);self.assertNotIn('production',argv[:5])
        for bad in ['x;id','../file','a'*5000]:
            with self.assertRaises(Refused):image_import.decode(bad)
    def test_shared_validator_accepts_metadata_without_report_file(self):
        self.assertEqual(workflow.verify_delivery(self.archive,self.report),workflow.verify_delivery(self.archive,self.data))
    @unittest.skipUnless(os.name=='posix' and os.geteuid()==0,'root Linux permission rehearsal')
    def test_success_revalidates_archive_loads_once_and_keeps_receipt(self):
        result=self.perform();self.assertTrue(result['verified']);self.assertTrue(result['docker_load_performed'])
        self.assertEqual(self.backend.calls.count('load'),1);self.assertEqual(result['phase'],'accepted')
        self.assertFalse(self.marker().exists());self.assertEqual(len(self.journals()),1)
        self.assertEqual(json.loads(self.journals()[0].read_text())['phase'],'accepted')
        self.assertEqual(list((self.base/'state').rglob('image.tar.gz')),[])
        self.assertFalse(result['production_changed']);self.assertFalse(result['deployment_performed'])
    @unittest.skipUnless(os.name=='posix' and os.geteuid()==0,'root Linux permission rehearsal')
    def test_matching_existing_image_is_noop_without_load(self):
        self.backend.current={'id':self.data['image_id'],'user':'10001:10001','revision':self.data['source_sha']}
        result=self.perform();self.assertTrue(result['already_present']);self.assertFalse(result['docker_load_performed']);self.assertNotIn('load',self.backend.calls)
    @unittest.skipUnless(os.name=='posix' and os.geteuid()==0,'root Linux permission rehearsal')
    def test_conflicting_tag_is_refused_before_archive_staging(self):
        self.backend.current={'id':'sha256:'+'b'*64}
        with self.assertRaisesRegex(Refused,'tag_conflict'):self.perform()
        self.assertFalse((self.base/'state/image-imports').exists());self.assertNotIn('load',self.backend.calls)
    @unittest.skipUnless(os.name=='posix' and os.geteuid()==0,'root Linux permission rehearsal')
    def test_truncated_oversized_and_corrupt_uploads_never_load(self):
        for raw in [self.raw[:-1],self.raw+b'!',bytes([self.raw[0]^1])+self.raw[1:]]:
            with self.subTest(length=len(raw)),self.assertRaises(Refused):self.perform(raw)
            self.assertNotIn('load',self.backend.calls);self.assertFalse(self.marker().exists())
    @unittest.skipUnless(os.name=='posix' and os.geteuid()==0,'root Linux permission rehearsal')
    def test_full_archive_validation_rejects_invalid_tar_before_load(self):
        raw=b'not-gzip';self.data.update(archive_bytes=len(raw),archive_sha256=hashlib.sha256(raw).hexdigest())
        with self.assertRaises(Refused):self.perform(raw)
        self.assertFalse(self.marker().exists());self.assertNotIn('load',self.backend.calls)
    @unittest.skipUnless(os.name=='posix' and os.geteuid()==0,'root Linux permission rehearsal')
    def test_space_refused_before_any_image_change(self):
        self.backend.failure='capacity'
        with self.assertRaisesRegex(Refused,'insufficient'):self.perform()
        self.assertNotIn('load',self.backend.calls);self.assertFalse(self.marker().exists())
    @unittest.skipUnless(os.name=='posix' and os.geteuid()==0,'root Linux permission rehearsal')
    def test_space_is_rechecked_after_staging_with_validated_expansion(self):
        self.perform();caps=[x for x in self.backend.calls if isinstance(x,tuple)]
        self.assertEqual(caps[0][1],str(self.base/'state'));self.assertEqual(caps[1][1],'/var/lib/docker')
        self.assertEqual(caps[1][2],workflow.verify_delivery(self.archive,self.report)['import_required_bytes'])
    @unittest.skipUnless(os.name=='posix' and os.geteuid()==0,'root Linux permission rehearsal')
    def test_space_loss_after_receiving_still_refuses_load(self):
        original=self.backend.capacity
        def reduced(path,required):
            if path==Path('/var/lib/docker'):raise Refused('import_capacity_shrank')
            return original(path,required)
        self.backend.capacity=reduced
        with self.assertRaisesRegex(Refused,'capacity_shrank'):self.perform()
        self.assertNotIn('load',self.backend.calls);self.assertFalse(self.marker().exists())
        self.assertEqual(list((self.base/'state').rglob('image.tar.gz')),[])
    @unittest.skipUnless(os.name=='posix' and os.geteuid()==0,'root Linux permission rehearsal')
    def test_load_failure_identity_mismatch_or_runtime_change_keep_unknown_marker(self):
        for failure in ['load','identity','runtime']:
            self.backend=Backend(self.data);self.backend.failure=failure
            with self.subTest(failure=failure),self.assertRaisesRegex(Refused,'outcome_unknown'):self.perform()
            self.assertEqual(json.loads(self.marker().read_text())['phase'],'unknown')
            with self.assertRaisesRegex(Refused,'unresolved'):self.perform()
            self.assertEqual(self.backend.calls.count('load'),1);self.marker().unlink()
    @unittest.skipUnless(os.name=='posix' and os.geteuid()==0,'root Linux permission rehearsal')
    def test_lost_process_marker_blocks_import_and_release_before_probe(self):
        atomic(self.marker(),{'id':'interrupted','phase':'receiving'})
        with self.assertRaisesRegex(Refused,'unresolved'):self.perform()
        engine=object.__new__(Linux);engine.state=self.base/'state';engine.c={'mode':'development'}
        with self.assertRaisesRegex(Refused,'unresolved'):engine.inspect('unused')
    @unittest.skipUnless(os.name=='posix' and os.geteuid()==0,'root Linux permission rehearsal')
    def test_shared_lock_conflict_fails_without_waiting(self):
        lock=self.base/'bootstrap.lock'
        with image_import.host_lock(lock):
            with self.assertRaisesRegex(Refused,'busy'):
                with image_import.host_lock(lock):self.fail('lock entered twice')
    def test_client_insufficient_storage_never_opens_import_ssh(self):
        with patch.object(workflow,'read_storage',return_value=({'capacity':{'sufficient':False}},1)),patch.object(import_client.subprocess,'run') as run:
            with self.assertRaisesRegex(ValueError,'storage_refused'):import_client.import_image(self.archive,self.report,Path('ssh.conf'))
            run.assert_not_called()
    def test_client_changed_local_copy_does_not_connect(self):
        with patch.object(import_client,'verify_delivery',return_value={**workflow.verify_delivery(self.archive,self.report),'archive_sha256':'0'*64}),patch.object(workflow,'read_storage') as probe:
            with self.assertRaisesRegex(ValueError,'local_archive_changed'):import_client.import_image(self.archive,self.report,Path('ssh.conf'))
            probe.assert_not_called()
    def test_client_timeout_never_retries_or_prints_diagnostic(self):
        with patch.object(workflow,'read_storage',return_value=({'capacity':{'sufficient':True}},0)),patch.object(import_client.subprocess,'run',side_effect=subprocess.TimeoutExpired('private-marker',1)) as run:
            with self.assertRaisesRegex(ValueError,'outcome_unknown_do_not_retry'):import_client.import_image(self.archive,self.report,Path('ssh.conf'))
            self.assertEqual(run.call_count,1)
    def test_client_rejects_untrusted_success_scope(self):
        result={'verified':True,'phase':'accepted','environment':'production'}
        with patch.object(workflow,'read_storage',return_value=({'capacity':{'sufficient':True}},0)),patch.object(import_client.subprocess,'run',return_value=Mock(returncode=0,stdout=json.dumps(result).encode())):
            with self.assertRaisesRegex(ValueError,'scope_mismatch'):import_client.import_image(self.archive,self.report,Path('ssh.conf'))

if __name__=='__main__':unittest.main()
