import hashlib,io,json,tempfile,tarfile,unittest
from pathlib import Path
import delivery,test_workflow

class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.archive,self.report,self.data=test_workflow.WorkflowTests().fixture(self.root)
        with tarfile.open(self.archive,'r:gz') as tar:self.files={m.name:tar.extractfile(m).read() for m in tar if m.isfile()}
    def tearDown(self):self.temp.cleanup()
    def check(self):
        with tarfile.open(self.archive,'w:gz') as tar:
            for name,raw in self.files.items():
                member=tarfile.TarInfo(name);member.size=len(raw);tar.addfile(member,io.BytesIO(raw))
        self.data.update(archive_sha256=hashlib.sha256(self.archive.read_bytes()).hexdigest(),archive_bytes=self.archive.stat().st_size)
        return delivery.verify_delivery(self.archive,self.data)
    def oci(self):
        manifest={'schemaVersion':2,'config':{'digest':self.data['image_id']},'layers':[]}
        raw=json.dumps(manifest).encode();digest=hashlib.sha256(raw).hexdigest()
        self.files['blobs/sha256/'+digest]=raw
        index={'schemaVersion':2,'manifests':[{'digest':'sha256:'+digest,'size':len(raw),'annotations':{
            'io.containerd.image.name':'docker.io/library/'+self.data['tag'],
            'org.opencontainers.image.ref.name':self.data['tag'].split(':',1)[1]}}]}
        self.files['index.json']=json.dumps(index).encode();self.files['oci-layout']=b'{"imageLayoutVersion":"1.0.0"}'
        return index
    def test_single_matching_oci_index_and_repository_are_accepted(self):
        self.oci();repo,tag=self.data['tag'].split(':',1);self.files['repositories']=json.dumps({repo:{tag:'a'*64}}).encode()
        self.assertTrue(self.check()['verified'])
    def test_secondary_repository_tag_cannot_be_loaded(self):
        self.files['repositories']=b'{"another":{"latest":"abc"}}'
        with self.assertRaisesRegex(ValueError,'repository_tags_mismatch'):self.check()
    def test_second_oci_image_is_refused(self):
        index=self.oci();index['manifests']*=2;self.files['index.json']=json.dumps(index).encode()
        with self.assertRaisesRegex(ValueError,'multiple_images'):self.check()
    def test_oci_tag_alias_is_refused(self):
        index=self.oci();index['manifests'][0]['annotations']['org.opencontainers.image.ref.name']='different'
        self.files['index.json']=json.dumps(index).encode()
        with self.assertRaisesRegex(ValueError,'tags_mismatch'):self.check()
    def test_oci_config_digest_must_match_docker_manifest(self):
        self.oci();name=next(n for n in self.files if n.startswith('blobs/'))
        body=json.loads(self.files.pop(name));body['config']['digest']='sha256:'+'f'*64;raw=json.dumps(body).encode()
        digest=hashlib.sha256(raw).hexdigest();self.files['blobs/sha256/'+digest]=raw
        index=json.loads(self.files['index.json']);index['manifests'][0].update(digest='sha256:'+digest,size=len(raw));self.files['index.json']=json.dumps(index).encode()
        with self.assertRaisesRegex(ValueError,'config_or_layers_mismatch'):self.check()
    def test_manifest_layer_must_exist_as_a_regular_file(self):
        manifest=json.loads(self.files['manifest.json']);manifest[0]['Layers']=['missing-layer'];self.files['manifest.json']=json.dumps(manifest).encode()
        with self.assertRaisesRegex(ValueError,'layers_invalid'):self.check()
    def test_alternate_path_spelling_is_not_an_alias(self):
        self.files['./manifest.json']=self.files['manifest.json']
        with self.assertRaisesRegex(ValueError,'structure_invalid'):self.check()

if __name__=='__main__':unittest.main()
