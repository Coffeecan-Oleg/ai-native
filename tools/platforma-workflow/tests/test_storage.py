import copy,errno,json,os,subprocess,tempfile,unittest
from pathlib import Path
from unittest.mock import patch,Mock
from release_control import storage
from release_control.core import Refused,Store,Controller
from release_control.linux import Linux
import storage_preflight,workflow,development

class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.fsid='11111111-1111-1111-1111-111111111111';self.subvol=777
        self.groups=self.root/self.fsid/'qgroups';self.leaf=self.groups/'0_777';self.leaf.mkdir(parents=True)
        for key,value in {'enabled':1,'inconsistent':0,'mode':'qgroup'}.items():self.write(self.groups/key,value)
        self.values={'limit_flags':1,'referenced':800,'exclusive':400,'max_referenced':1000,
                     'max_exclusive':0,'rsv_data':10,'rsv_meta_pertrans':20,'rsv_meta_prealloc':30}
        self.save()
    def tearDown(self):self.temp.cleanup()
    def write(self,path,value):path.write_bytes((str(value)+'\n').encode())
    def save(self):
        for key,value in self.values.items():self.write(self.leaf/key,value)
    def probe(self):return storage.qgroup(Path('/copy'),self.fsid,self.subvol,self.root)
    def test_real_sysfs_fixture_uses_discovered_identity_and_reservations(self):
        result=self.probe();self.assertEqual(result['subvolume'],777)
        self.assertEqual(result['quota_available_bytes'],140)
    def test_unrelated_leaf_does_not_constrain_current_subvolume(self):
        (self.groups/'0_999').mkdir();self.assertEqual(self.probe()['quota_available_bytes'],140)
    def test_exclusive_and_referenced_limits_both_apply(self):
        self.values.update(limit_flags=3,max_exclusive=500);self.save()
        self.assertEqual(self.probe()['quota_available_bytes'],40)
    def test_referenced_exhaustion_is_zero_not_unsigned_wrap(self):
        self.values['referenced']=1001;self.save();self.assertEqual(self.probe()['quota_available_bytes'],0)
    def test_reservations_can_exhaust_capacity(self):
        self.values['rsv_data']=500;self.save();self.assertEqual(self.probe()['quota_available_bytes'],0)
    def test_stale_limits_without_flags_are_not_treated_as_active(self):
        self.values['limit_flags']=0;self.save();self.assertIsNone(self.probe()['quota_available_bytes'])
    def test_zero_active_limit_is_exhausted(self):
        self.values['max_referenced']=0;self.save();self.assertEqual(self.probe()['quota_available_bytes'],0)
    def test_parent_hierarchy_is_refused_not_silently_ignored(self):
        (self.groups/'1_100').mkdir()
        with self.assertRaisesRegex(Refused,'hierarchy_unverified'):self.probe()
    def test_inconsistent_accounting_is_unknown(self):
        self.write(self.groups/'inconsistent',1)
        with self.assertRaisesRegex(Refused,'inconsistent'):self.probe()
    def test_disabled_quota_is_not_assumed_unlimited(self):
        self.write(self.groups/'enabled',0)
        with self.assertRaisesRegex(Refused,'not_verified'):self.probe()
    def test_squota_mode_is_not_claimed_supported(self):
        self.write(self.groups/'mode','squota')
        with self.assertRaisesRegex(Refused,'mode_unsupported'):self.probe()
    def test_compressed_or_unknown_limit_flags_are_refused(self):
        for flag in [4,8,16]:
            self.values['limit_flags']=flag;self.save()
            with self.subTest(flag=flag),self.assertRaisesRegex(Refused,'flags_unsupported'):self.probe()
    def test_missing_current_leaf_is_refused(self):
        with self.assertRaisesRegex(Refused,'qgroup_missing'):
            storage.qgroup(Path('/copy'),self.fsid,123,self.root)
    def test_invalid_counter_values_are_refused(self):
        for value in ['-1','nan',str(2**64),'1\n2','7'*65]:
            self.write(self.leaf/'referenced',value)
            with self.subTest(value=value),self.assertRaisesRegex(Refused,'counter_invalid'):self.probe()
    def test_permission_error_returns_unknown_without_raw_path_or_diagnostic(self):
        with patch.object(storage,'inspect',side_effect=PermissionError('private-marker')):
            result=storage.check(Path('/copy'),1)
        self.assertEqual(result['status'],'unknown');self.assertNotIn('private-marker',str(result))
        self.assertFalse(result['sufficient']);self.assertFalse(result['writes_performed'])
    def test_unknown_quota_never_passes(self):
        with patch.object(storage,'inspect',side_effect=Refused('storage_qgroup_missing')):
            result=storage.check(Path('/copy'),1)
        self.assertEqual(result['reason'],'storage_qgroup_missing');self.assertFalse(result['sufficient'])
    def test_worse_of_two_counter_readings_is_used(self):
        original=storage.number;reads=0
        def changing(path):
            nonlocal reads
            if Path(path).name=='referenced':
                reads+=1;return 800 if reads==1 else 900
            return original(path)
        with patch.object(storage,'number',side_effect=changing):result=self.probe()
        self.assertEqual(result['quota_available_bytes'],40)
    def test_state_change_during_read_is_refused(self):
        original=storage.number;reads=0
        def changing(path):
            nonlocal reads
            if Path(path)==self.groups/'inconsistent':
                reads+=1;return 0 if reads==1 else 1
            return original(path)
        with patch.object(storage,'number',side_effect=changing):
            with self.assertRaisesRegex(Refused,'state_changed'):self.probe()
    @unittest.skipUnless(os.name=='posix','Linux file descriptor capacity check')
    def test_effective_capacity_uses_smaller_of_filesystem_and_quota(self):
        detail={'kind':'btrfs_flat_qgroup','quota_available_bytes':100}
        with patch.object(storage,'identity',return_value=(self.fsid,self.subvol)),\
             patch.object(storage,'qgroup',return_value=detail):
            result=storage.inspect(self.root.resolve())
        self.assertEqual(result['effective_available_bytes'],min(result['filesystem_available_bytes'],100))
    @unittest.skipUnless(os.name=='posix','Linux file descriptor capacity check')
    def test_unreadable_btrfs_identity_does_not_fall_back_to_df(self):
        with patch.object(storage,'identity',side_effect=OSError(errno.EPERM,'denied')),\
             patch.object(storage,'filesystem') as fallback:
            result=storage.check(self.root.resolve(),1)
        self.assertEqual(result['reason'],'storage_btrfs_identity_unavailable');fallback.assert_not_called()
    @unittest.skipUnless(os.name=='posix','Linux file descriptor capacity check')
    def test_ext4_project_quota_and_unknown_filesystems_do_not_pass(self):
        for fs,opts in [('ext4',{'rw','prjquota'}),('xfs',{'usrquota'}),('overlay',{'rw'})]:
            with patch.object(storage,'identity',side_effect=OSError(errno.ENOTTY,'other')),\
                 patch.object(storage,'filesystem',return_value=(fs,opts)):
                result=storage.check(self.root.resolve(),1)
            self.assertFalse(result['sufficient']);self.assertEqual(result['status'],'unknown')
    def test_exact_budget_plus_reserve_boundary(self):
        needed=300;reserve=storage.RESERVE_BYTES
        for free,expected in [(needed+reserve-1,False),(needed+reserve,True)]:
            with patch.object(storage,'inspect',return_value={'effective_available_bytes':free}):
                result=storage.check(Path('/copy'),needed)
            self.assertIs(result['sufficient'],expected);self.assertFalse(result['capacity_reserved'])
    def test_invalid_or_reduced_budgets_are_rejected(self):
        for needed,reserve in [(True,storage.RESERVE_BYTES),(0,storage.RESERVE_BYTES),(-1,storage.RESERVE_BYTES),
                (10**20,storage.RESERVE_BYTES),(1,0),(1,True)]:
            with self.subTest(needed=needed,reserve=reserve),self.assertRaisesRegex(Refused,'budget_invalid'):
                storage.check(Path('/copy'),needed,reserve)
    def test_require_stops_operation_on_low_capacity(self):
        with patch.object(storage,'inspect',return_value={'effective_available_bytes':0}):
            with self.assertRaisesRegex(Refused,'insufficient_capacity'):storage.require(Path('/copy'),1)
    def test_controller_gate_precedes_any_docker_or_window_action(self):
        backend=Linux.__new__(Linux);backend.base=Path('/copy/app');backend.state=Path('/copy/state')
        backend.c={'minimum_disk_bytes':512*storage.MIB};backend.container=Mock()
        with patch('release_control.linux.require_storage',side_effect=Refused('storage_insufficient_capacity')):
            with self.assertRaisesRegex(Refused,'insufficient_capacity'):backend.inspect('candidate')
        backend.container.assert_not_called()
    def test_bootstrap_copy_includes_cli_and_quota_module(self):
        files=development.source_files(Path(__file__).resolve().parents[1])
        self.assertIn('storage_preflight.py',files);self.assertIn('release_control/storage.py',files)
    def test_remote_budget_command_has_one_fixed_target(self):
        prefix,command=workflow.storage_command(123456789)
        self.assertEqual(prefix,['ssh','-T']);self.assertIn('/opt/platforma-dev/code/storage_preflight.py import 123456789',command)
        for value in ['1;id',True,-1,10**20]:
            with self.subTest(value=value),self.assertRaises(ValueError):workflow.storage_command(value)
    def response(self,**changes):
        value={'environment':'dev','operation':'image_import','docker_load_performed':False,'production_changed':False,
               'capacity':{'sufficient':True,'status':'sufficient','writes_performed':False,
                 'required_bytes':100,'reserve_bytes':128*1024**2,'path':'/var/lib/docker'}}
        value.update(changes);return value
    def test_client_performs_one_fixed_read_and_bounded_wait(self):
        result=self.response()
        with patch.object(workflow.subprocess,'run',return_value=Mock(stdout=json.dumps(result).encode(),returncode=0)) as run:
            value,code=workflow.read_storage(Path('/copy/config'),100)
        self.assertEqual(value,result);self.assertEqual(code,0)
        args=run.call_args.args[0];self.assertEqual(args[-2],'aiport');self.assertNotIn('load',args[-1])
        self.assertEqual(run.call_args.kwargs['timeout'],30)
    def test_client_hides_ssh_failure_and_has_no_retry(self):
        with patch.object(workflow.subprocess,'run',side_effect=subprocess.TimeoutExpired('private-marker',30)) as run:
            with self.assertRaisesRegex(ValueError,'^storage_report_unavailable$'):workflow.read_storage(Path('/copy/config'),100)
        self.assertEqual(run.call_count,1)
    def test_client_refuses_malformed_oversized_or_wrong_environment_report(self):
        for body in [b'not json',b'[]',b' '*16385,json.dumps(self.response(environment='production')).encode(),
                json.dumps(self.response(capacity={'sufficient':True,'status':'unknown','writes_performed':False})).encode()]:
            with patch.object(workflow.subprocess,'run',return_value=Mock(stdout=body,returncode=0)):
                with self.subTest(size=len(body)),self.assertRaises(ValueError):workflow.read_storage(Path('/copy/config'),100)
    def test_client_rejects_report_for_another_budget(self):
        result=self.response();result['capacity']['required_bytes']=99
        with patch.object(workflow.subprocess,'run',return_value=Mock(stdout=json.dumps(result).encode(),returncode=0)):
            with self.assertRaisesRegex(ValueError,'budget_mismatch'):workflow.read_storage(Path('/copy/config'),100)
    def test_cli_denies_other_commands_paths_and_production(self):
        with patch.object(storage_preflight.os,'geteuid',return_value=0,create=True),\
             patch.object(storage_preflight.os,'uname',return_value=type('U',(),{'nodename':'kromin'})(),create=True),\
             patch.object(storage_preflight,'check') as check:
            for args in [['load','1'],['import','1','/production'],['import','-1'],['import','1;id']]:
                with self.subTest(args=args),self.assertRaises(Refused):storage_preflight.report(args)
            check.assert_not_called()
    def test_cli_reports_capacity_without_loading(self):
        with patch.object(storage_preflight.os,'geteuid',return_value=0,create=True),\
             patch.object(storage_preflight.os,'uname',return_value=type('U',(),{'nodename':'kromin'})(),create=True),\
             patch.object(storage_preflight,'check',return_value={'sufficient':False}) as check:
            result=storage_preflight.report(['import','100'])
        check.assert_called_once_with(Path('/var/lib/docker'),100)
        self.assertFalse(result['docker_load_performed']);self.assertFalse(result['production_changed'])

class PreWindowTests(unittest.TestCase):
    def test_quota_shrink_after_plan_prevents_scheduling(self):
        with tempfile.TemporaryDirectory() as td:
            view={'current':{'healthy':True,'image_id':'old'},'candidate':{'image_id':'new'},'pending_before':[]}
            backend=Mock();backend.inspect.return_value=copy.deepcopy(view)
            controller=Controller(Store(Path(td)/'state'),backend)
            plan=controller.plan('candidate');backend.inspect.side_effect=Refused('storage_insufficient_capacity')
            with self.assertRaisesRegex(Refused,'insufficient_capacity'):
                controller.apply(plan['payload']['id'],plan['sha256'])
            backend.schedule.assert_not_called();backend.close_gate.assert_not_called()
            self.assertIsNone(controller.s.active())
    def test_quota_shrink_at_worker_aborts_before_window(self):
        with tempfile.TemporaryDirectory() as td:
            view={'current':{'healthy':True,'image_id':'old'},'candidate':{'image_id':'new'},'pending_before':[]}
            backend=Mock();backend.inspect.return_value=copy.deepcopy(view)
            controller=Controller(Store(Path(td)/'state'),backend)
            plan=controller.plan('candidate');op=controller.apply(plan['payload']['id'],plan['sha256'])
            backend.inspect.side_effect=Refused('storage_insufficient_capacity')
            result=controller.worker()
            self.assertEqual(result['phase'],'aborted');self.assertFalse(result['writes_possible'])
            self.assertEqual(result['error'],'storage_insufficient_capacity')
            backend.close_gate.assert_not_called();backend.stop_writers.assert_not_called()
            self.assertIsNone(controller.s.active())

if __name__=='__main__':unittest.main()
