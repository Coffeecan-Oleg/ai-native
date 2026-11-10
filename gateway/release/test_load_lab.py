"""Host-side safety gates, without Docker, network or remote side effects."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch
import json

spec=importlib.util.spec_from_file_location('load_lab_host',Path(__file__).parent/'load_lab'/'host.py')
host=importlib.util.module_from_spec(spec);spec.loader.exec_module(host)
SHA='c3c05e2c6e1de5d5a61582c1386db49af6f579e0'
TAG='platforma-gateway:evening-'+SHA[:12]

class LoadLabGuardTests(unittest.TestCase):
    def test_only_disposable_host_accepted(self):
        for hostname in ['ai-srv','localhost','kromin.example','']:
            with self.subTest(host=hostname),self.assertRaisesRegex(ValueError,'host_refused'):
                host.guard(hostname,900,3000,TAG,SHA)

    def test_memory_and_disk_low_watermarks(self):
        for ram,disk in [(799,3000),(900,2047),(0,0)]:
            with self.subTest(ram=ram,disk=disk),self.assertRaisesRegex(ValueError,'headroom_refused'):
                host.guard('kromin',ram,disk,TAG,SHA)

    def test_boundary_has_room_for_all_container_limits(self):
        host.guard('kromin',800,2048,TAG,SHA)
        self.assertGreaterEqual(800-sum(host.MEMORY_MIB.values()),128)

    def test_wrong_tag_or_revision_refused(self):
        for tag,sha in [('platforma-gateway:latest',SHA),(TAG,SHA[:12]),(TAG,'a'*40),(TAG,SHA.upper())]:
            with self.subTest(tag=tag,sha=sha),self.assertRaisesRegex(ValueError,'image_refused'):
                host.guard('kromin',900,3000,tag,sha)

    def test_docker_memory_units_not_confused(self):
        self.assertEqual(host.mib('1GiB'),1024)
        self.assertEqual(host.mib('384MiB'),384)
        self.assertAlmostEqual(host.mib('1048576B'),1)
        self.assertAlmostEqual(host.mib('1000kB'),.9536743164)

    def test_exit_event_race_rechecked_before_retry(self):
        commands=[]
        def docker(*args):
            commands.append(args)
            if args[0]=='ps':return b'id'
            if args[0]=='inspect':return json.dumps([{'Config':{'Labels':{'platforma.load.lab':'own'}}}]).encode()
            if sum(c[0]=='rm' for c in commands)==1:raise RuntimeError('exit_event_race')
            return b''
        with patch.object(host.time,'sleep'):host.remove_container(docker,'own-container','own')
        self.assertEqual(sum(c[0]=='inspect' for c in commands),2)
        self.assertEqual(sum(c[0]=='rm' for c in commands),2)

    def test_cleanup_never_removes_foreign_container(self):
        removed=[]
        def docker(*args):
            if args[0]=='ps':return b'id'
            if args[0]=='inspect':return b'[{"Config":{"Labels":{"platforma.load.lab":"other"}}}]'
            removed.append(args)
        with self.assertRaisesRegex(ValueError,'foreign_container_refused'):
            host.remove_container(docker,'foreign','own')
        self.assertEqual(removed,[])

    def test_cleanup_retry_is_bounded(self):
        commands=[]
        def docker(*args):
            commands.append(args)
            if args[0]=='ps':return b'id'
            if args[0]=='inspect':return b'[{"Config":{"Labels":{"platforma.load.lab":"own"}}}]'
            raise RuntimeError('persistent_removal_failure')
        with patch.object(host.time,'sleep'),self.assertRaises(RuntimeError):
            host.remove_container(docker,'own-container','own')
        self.assertEqual(sum(c[0]=='rm' for c in commands),3)

if __name__=='__main__':unittest.main()
