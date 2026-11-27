import copy,importlib.util,os,tempfile,unittest
from pathlib import Path
from release_control.core import Store,Controller,Refused,atomic
from main import dispatch
spec=importlib.util.spec_from_file_location('integration',Path(__file__).parents[1]/'dispatcher-integration.py')
integration=importlib.util.module_from_spec(spec);spec.loader.exec_module(integration)
class Fake:
    def __init__(self):
        self.calls=[];self.fail=None;self.fail_close=False;self.open=False
        self.view={'current':{'healthy':True,'image_id':'old','release_sha':'old'},
                   'candidate':{'image_id':'new','revision':'a'*40},'pending_before':[]}
    def do(self,n):
        self.calls.append(n)
        if self.fail==n:raise Refused(n+'_failed')
    def inspect(self,tag):return copy.deepcopy(self.view)
    def schedule(self):self.do('schedule')
    def close_gate(self,op):self.do('close');self.open=False
    def assert_closed(self,op):
        if self.open:raise Refused('maintenance_unconfirmed')
    def ensure_closed(self,op):
        self.do('ensure_closed')
        if self.fail_close:raise Refused('cannot_close')
        self.open=False
    def stop_writers(self,op):self.do('stop')
    def snapshot(self,op):self.do('snapshot');return {'id':'synthetic'}
    def migrate(self,op):self.do('migrate')
    def start_candidate(self,op):self.do('start')
    def check_candidate(self,op):self.do('check')
    def open_gate(self,op,previous=False):self.do('open');self.open=True
    def snapshot_failed_state(self,op):self.do('failed_snapshot')
    def restore(self,op):self.do('restore')
    def start_previous(self,op):self.do('start_old')
    def check_previous(self,op):self.do('check_old')
class Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.s=Store(Path(self.temp.name)/'state')
        self.b=Fake();self.now=1000;self.e=Controller(self.s,self.b,lambda:self.now)
    def tearDown(self):self.temp.cleanup()
    def queued(self):
        p=self.e.plan('candidate');r=self.e.apply(p['payload']['id'],p['sha256']);return p,r['id']
    def ready(self):
        p,oid=self.queued();self.e.worker();return p,oid
    def test_plan_is_not_apply(self):
        self.e.plan('candidate');self.assertIsNone(self.s.active());self.assertEqual(self.b.calls,[])
    def test_exact_order_and_separate_open(self):
        _,oid=self.ready();self.assertEqual(self.b.calls,['schedule','close','stop','snapshot','migrate','start','check'])
        self.assertFalse(self.b.open);self.assertEqual(self.e.status(oid)['phase'],'awaiting_accept')
        self.e.accept(oid);self.assertTrue(self.b.open);self.assertTrue(self.e.status(oid)['writes_possible'])
    def test_changed_plan_hash_refused(self):
        p=self.e.plan('candidate');p['payload']['tag']='other';atomic(self.s.path('plans',p['payload']['id']),p)
        with self.assertRaisesRegex(Refused,'plan_hash'):self.e.apply(p['payload']['id'],p['sha256'])
    def test_wrong_hash_refused(self):
        p=self.e.plan('candidate')
        with self.assertRaises(Refused):self.e.apply(p['payload']['id'],'0'*64)
    def test_expired_plan(self):
        p=self.e.plan('candidate');self.now+=1801
        with self.assertRaisesRegex(Refused,'expired'):self.e.apply(p['payload']['id'],p['sha256'])
    def test_state_drift_before_apply(self):
        p=self.e.plan('candidate');self.b.view['current']['image_id']='changed'
        with self.assertRaisesRegex(Refused,'baseline_changed'):self.e.apply(p['payload']['id'],p['sha256'])
    def test_drift_before_worker_has_no_window(self):
        _,oid=self.queued();self.b.view['current']['image_id']='changed';self.e.worker()
        self.assertIsNone(self.s.active());self.assertEqual(self.b.calls,['schedule'])
        self.assertEqual(self.e.status(oid)['phase'],'aborted')
    def test_repeat_apply_does_not_schedule_twice(self):
        p,oid=self.queued()
        for _ in range(3):self.assertEqual(self.e.apply(p['payload']['id'],p['sha256'])['id'],oid)
        self.assertEqual(self.b.calls,['schedule'])
    def test_second_release_refused(self):
        self.queued()
        with self.assertRaisesRegex(Refused,'active'):self.e.plan('other')
    def test_parallel_lock_refused(self):
        with self.s.lock():
            with self.assertRaisesRegex(Refused,'busy'):self.e.plan('other')
    def test_migration_failure_gate_closed(self):
        _,oid=self.queued();self.b.fail='migrate';self.e.worker()
        self.assertFalse(self.b.open);self.assertNotIn('start',self.b.calls);self.assertNotIn('restore',self.b.calls)
        self.assertEqual(self.e.status(oid)['phase'],'failed_closed')
    def test_failed_recovery_never_opens(self):
        _,oid=self.ready();self.b.fail='restore'
        self.assertEqual(self.e.recover(oid)['phase'],'recovery_failed');self.assertFalse(self.b.open)
    def test_recovery_has_its_own_explicit_open(self):
        _,oid=self.ready();self.assertEqual(self.e.recover(oid)['phase'],'recovered_awaiting_open')
        self.assertFalse(self.b.open);self.e.reopen_previous(oid);self.assertTrue(self.b.open)
    def test_forensic_snapshot_precedes_restore(self):
        _,oid=self.ready();self.e.recover(oid)
        self.assertLess(self.b.calls.index('failed_snapshot'),self.b.calls.index('restore'))
    def test_snapshot_failure_never_migrates(self):
        self.queued();self.b.fail='snapshot';self.e.worker()
        self.assertNotIn('migrate',self.b.calls);self.assertFalse(self.b.open)
    def test_accepted_release_cannot_restore(self):
        _,oid=self.ready();self.e.accept(oid)
        with self.assertRaises(Refused):self.e.recover(oid)
        self.assertNotIn('restore',self.b.calls)
    def test_write_marker_precedes_open(self):
        _,oid=self.ready()
        def open_gate(op,previous=False):
            disk=self.s.load_op(oid);self.assertTrue(disk['writes_possible'])
            self.assertEqual(disk['phase'],'opening');self.b.open=True
        self.b.open_gate=open_gate;self.e.accept(oid)
    def test_uncertain_open_prevents_restore(self):
        _,oid=self.ready();self.b.fail='open';self.e.accept(oid)
        self.assertTrue(self.e.status(oid)['writes_possible']);self.assertFalse(self.b.open)
        with self.assertRaisesRegex(Refused,'write_boundary'):self.e.recover(oid)
    def test_lost_gate_before_accept_disables_snapshot_restore(self):
        _,oid=self.ready()
        def lost(op):raise Refused('maintenance_unconfirmed')
        self.b.check_candidate=lost
        with self.assertRaisesRegex(Refused,'maintenance'):self.e.accept(oid)
        self.assertTrue(self.e.status(oid)['writes_possible'])
        with self.assertRaisesRegex(Refused,'write_boundary'):self.e.recover(oid)
    def test_unconfirmed_reclosure_after_snapshot_disables_restore(self):
        _,oid=self.queued();self.b.fail='check';self.b.fail_close=True;self.e.worker()
        self.assertEqual(self.e.status(oid)['phase'],'failed_unconfirmed')
        self.assertFalse(self.e.status(oid)['recovery_allowed'])
        with self.assertRaisesRegex(Refused,'write_boundary'):self.e.recover(oid)
    def test_recover_refuses_open_gate_before_touching_snapshot(self):
        _,oid=self.ready();self.b.open=True;self.b.calls=[]
        with self.assertRaisesRegex(Refused,'confirmed_closed_gate'):self.e.recover(oid)
        self.assertTrue(self.e.status(oid)['writes_possible'])
        self.assertNotIn('restore',self.b.calls)
    def test_interrupted_worker_is_not_replayed(self):
        _,oid=self.queued();op=self.s.load_op(oid);op['phase']='migrating';self.s.save(op,'simulated_death')
        with self.assertRaisesRegex(Refused,'already_started'):self.e.worker()
        self.assertNotIn('migrate',self.b.calls)
    def test_status_survives_new_process(self):
        _,oid=self.ready();self.assertEqual(Controller(Store(self.s.root),self.b).status(oid)['phase'],'awaiting_accept')
    def test_queued_operation_can_be_resumed_without_replay(self):
        _,oid=self.queued();self.assertEqual(self.e.current()['id'],oid)
        self.e.resume(oid);self.assertEqual(self.b.calls,['schedule','schedule'])
        self.e.worker()
        with self.assertRaisesRegex(Refused,'interrupted_phase'):self.e.resume(oid)
    def test_accept_retry_is_read_only(self):
        _,oid=self.ready();self.e.accept(oid);before=list(self.b.calls)
        self.assertEqual(self.e.accept(oid)['phase'],'accepted');self.assertEqual(self.b.calls,before)
    def test_recover_retry_does_not_restore_twice(self):
        _,oid=self.ready();self.e.recover(oid);before=list(self.b.calls)
        self.e.recover(oid);self.assertEqual(self.b.calls,before)
    def test_missing_plan_link_recovers_existing_operation(self):
        p,oid=self.queued();entry=p.copy()
        atomic(self.s.path('plans',p['payload']['id']),entry)
        self.assertEqual(self.e.apply(p['payload']['id'],p['sha256'])['id'],oid)
    def test_pending_baseline_refused(self):
        self.b.view['pending_before']=['0025']
        with self.assertRaisesRegex(Refused,'baseline'):self.e.plan('candidate')
    def test_bad_tag_never_reaches_backend(self):
        for bad in ['-x','a;reboot','../../x','x y','x\nstatus','$(whoami)']:
            with self.assertRaises(Refused):self.e.plan(bad)
    def test_path_traversal_ids(self):
        with self.assertRaises(Refused):self.e.status('../../etc/passwd')
    def test_private_worker_not_public(self):
        with self.assertRaisesRegex(Refused,'not_allowed'):dispatch(['_worker'],self.e)
    def test_unknown_args_refused(self):
        with self.assertRaises(Refused):dispatch(['plan','x','--config','/tmp/evil'],self.e)
    def test_legacy_mutations_blocked(self):
        for a in [['deploy','x'],['rollback'],['restart'],['db','apply','x']]:
            with self.assertRaises(Refused):integration.guard_legacy(a,self.s)
        self.queued()
        with self.assertRaises(Refused):integration.guard_legacy(['files','apply','x'],self.s)
        integration.guard_legacy(['skills','list'],self.s)
    @unittest.skipUnless(os.name=='posix','POSIX-only symlink permission check')
    def test_symbolic_state_path_refused(self):
        link=Path(self.temp.name)/'link';link.symlink_to(self.s.root,target_is_directory=True)
        with self.assertRaises(Refused):Store(link)
if __name__=='__main__':unittest.main(verbosity=2)
