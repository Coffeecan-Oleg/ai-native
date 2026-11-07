import importlib.util,json,tempfile,unittest
from pathlib import Path
spec=importlib.util.spec_from_file_location('doc_check',Path(__file__).with_name('check-documentation.py'))
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

class DocumentationTests(unittest.TestCase):
    def test_current_source_manifest_is_consistent(self):
        result=module.check();self.assertTrue(result['successful'],result)
        self.assertFalse(result['network_calls']);self.assertFalse(result['published_pages_verified'])

    def test_stale_address_broken_link_and_wrong_version_are_detected(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'docs').mkdir();(root/'migrations').mkdir()
            (root/'docs/corporate-documentation.json').write_text(json.dumps({'release_status':'prepared_not_deployed','migration_count':26,
                'active_documents':['docs/start.md'],'legacy_documents':[]}))
            (root/'docs/start.md').write_text('https://kromin.axion-test.akmetron.ru/mcp\n[broken](missing.md)')
            result=module.check(root);self.assertFalse(result['successful'])
            self.assertEqual(len(result['errors']),3)
