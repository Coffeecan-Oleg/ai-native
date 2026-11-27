import importlib.util
from pathlib import Path
import unittest
spec=importlib.util.spec_from_file_location('export_image',Path(__file__).with_name('export-image.py'))
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


class ExportGuardTests(unittest.TestCase):
    def test_requires_exact_accepted_and_installed_source(self):
        sha='a'*40
        accepted={'successful':True,'source_sha':sha,'image':'sha256:'+'b'*64}
        installed={'verified':True,'source_revision':sha}
        module.validate(accepted,installed,sha)
        for a,i,s in [(accepted,installed,'bad'),(accepted|{'successful':False},installed,sha),
            (accepted|{'source_sha':'c'*40},installed,sha),(accepted,installed|{'source_revision':'c'*40},sha),
            (accepted|{'image':'other'},installed,sha)]:
            with self.assertRaises(ValueError):module.validate(a,i,s)
