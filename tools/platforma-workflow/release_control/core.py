"""Durable, fail-closed release state machine. No infrastructure on import."""
import contextlib, hashlib, json, os, re, secrets, time
from pathlib import Path

class Refused(RuntimeError):
    """Safe reason code only; never include command output or credentials."""

def encoded(value):
    return (json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))+'\n').encode()

def sha(value):
    return hashlib.sha256(value).hexdigest()

def atomic(path,value):
    path=Path(path)
    if path.is_symlink():raise Refused('state_symlink')
    temp=path.with_name(path.name+'.tmp-'+secrets.token_hex(8))
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(fd,'wb') as f:
            f.write(encoded(value));f.flush();os.fsync(f.fileno())
        os.replace(temp,path)
        if os.name=='posix':
            d=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(d)
            finally:os.close(d)
    finally:
        if temp.exists():temp.unlink()

def read(path):
    p=Path(path)
    if p.is_symlink() or not p.is_file() or p.stat().st_size>2*1024**2:raise Refused('unsafe_state')
    return json.loads(p.read_text(encoding='utf-8'))

class Store:
    def __init__(self,root):
        self.root=Path(root)
        if self.root.is_symlink():raise Refused('state_symlink')
        self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        if os.name=='posix':
            s=self.root.stat()
            if s.st_uid!=os.geteuid() or s.st_mode&0o077:raise Refused('state_permissions')
        for name in ['plans','operations']:
            p=self.root/name
            if p.is_symlink():raise Refused('state_symlink')
            p.mkdir(mode=0o700,exist_ok=True)
    def path(self,kind,key):
        if kind not in ['plans','operations'] or not re.fullmatch(r'(plan|op)-[a-f0-9]{32}',key):raise Refused('invalid_id')
        return self.root/kind/(key+'.json')
    @contextlib.contextmanager
    def lock(self,wait=False):
        p=self.root/'lock'
        if p.is_symlink():raise Refused('lock_symlink')
        fd=os.open(p,os.O_RDWR|os.O_CREAT|getattr(os,'O_NOFOLLOW',0),0o600)
        try:
            if os.name=='posix':
                import fcntl
                try:fcntl.flock(fd,fcntl.LOCK_EX|(0 if wait else fcntl.LOCK_NB))
                except BlockingIOError:raise Refused('operation_busy')
            else:
                import msvcrt
                try:
                    if os.fstat(fd).st_size==0:os.write(fd,b'0')
                    os.lseek(fd,0,0);msvcrt.locking(fd,msvcrt.LK_LOCK if wait else msvcrt.LK_NBLCK,1)
                except OSError:raise Refused('operation_busy')
            yield
        finally:os.close(fd)
    def active(self):
        p=self.root/'active.json'
        return read(p)['operation_id'] if p.exists() else None
    def save(self,op,event):
        op['events'].append({'at':time.time(),'phase':op['phase'],'event':event})
        atomic(self.path('operations',op['id']),op)
    def load_op(self,key):return read(self.path('operations',key))

class Controller:
    def __init__(self,store,backend,clock=time.time):
        self.s=store;self.b=backend;self.clock=clock
    def plan(self,tag):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}',tag):raise Refused('invalid_tag')
        with self.s.lock():
            if self.s.active():raise Refused('active_operation')
            view=self.b.inspect(tag)
            if not view['current']['healthy'] or view['pending_before']:raise Refused('baseline_not_ready')
            if view['candidate']['image_id']==view['current']['image_id']:raise Refused('already_running')
            key='plan-'+secrets.token_hex(16)
            payload={'schema':1,'id':key,'created':self.clock(),'expires':self.clock()+1800,
                     'tag':tag,'view':view,'acceptance':'technical_then_explicit_open',
                     'rollback':'closed_before_writes_only','configuration_changes':['gateway.image only']}
            result={'payload':payload,'sha256':sha(encoded(payload))}
            atomic(self.s.path('plans',key),result)
            return result
    def apply(self,key,digest):
        entry=read(self.s.path('plans',key))
        if not re.fullmatch('[a-f0-9]{64}',digest) or sha(encoded(entry['payload']))!=digest or entry['sha256']!=digest:
            raise Refused('plan_hash_mismatch')
        # Idempotent retry: even while the worker holds the lock, return its op.
        if entry.get('operation_id'):return self.status(entry['operation_id'])
        with self.s.lock():
            entry=read(self.s.path('plans',key))
            if entry.get('operation_id'):return self.status(entry['operation_id'])
            if self.s.active():
                active=self.s.load_op(self.s.active())
                if active['plan_id']==key and active['plan_sha256']==digest:
                    entry['operation_id']=active['id'];atomic(self.s.path('plans',key),entry)
                    return self.public(active)
                raise Refused('active_operation')
            if self.clock()>entry['payload']['expires']:raise Refused('plan_expired')
            if self.b.inspect(entry['payload']['tag'])!=entry['payload']['view']:raise Refused('baseline_changed')
            oid='op-'+secrets.token_hex(16)
            op={'schema':1,'id':oid,'plan_id':key,'plan_sha256':digest,'plan':entry['payload'],
                'phase':'queued','writes_possible':False,'snapshot':None,'error':None,'events':[]}
            self.s.save(op,'approved_plan_queued')
            atomic(self.s.root/'active.json',{'operation_id':oid})
            entry['operation_id']=oid;atomic(self.s.path('plans',key),entry)
            try:self.b.schedule()
            except Exception:
                op['phase']='failed_closed';op['error']='worker_schedule_failed';self.s.save(op,'not_started')
            return self.public(op)
    def current(self):
        oid=self.s.active()
        return self.status(oid) if oid else {'active':False}
    def resume(self,oid):
        with self.s.lock():
            op=self._active(oid)
            if op['phase']!='queued':raise Refused('interrupted_phase_requires_recovery')
            self.b.schedule();op['error']=None;self.s.save(op,'queued_worker_rescheduled')
            return self.public(op)
    def worker(self):
        # systemd may start before apply has returned and released its lock.
        with self.s.lock(wait=True):
            oid=self.s.active()
            if not oid:raise Refused('no_active_operation')
            op=self.s.load_op(oid)
            if op['phase']!='queued':raise Refused('worker_already_started')
            view=op['plan']['view']
            if self.b.inspect(op['plan']['tag'])!=view:
                op['phase']='aborted';op['error']='baseline_changed';self.s.save(op,'no_window_started')
                atomic(self.s.root/'active.json',{'operation_id':None})
                return self.public(op)
            try:
                for phase,action in [
                    ('closing',lambda:self.b.close_gate(op)),
                    ('stopping',lambda:self.b.stop_writers(op)),
                    ('snapshotting',lambda:self._snapshot(op)),
                    ('migrating',lambda:self.b.migrate(op)),
                    ('starting',lambda:self.b.start_candidate(op)),
                    ('checking',lambda:self.b.check_candidate(op))]:
                    op['phase']=phase;self.s.save(op,'before_'+phase)
                    action();self.s.save(op,'after_'+phase)
                op['phase']='awaiting_accept';self.s.save(op,'healthy_gate_closed')
            except Exception as ex:
                self._lost_gate(op,ex)
                op['phase']='failed_closed'
                op['error']=str(ex) if isinstance(ex,Refused) else 'backend_failure'
                # Best effort reclosure; NEVER open or restore automatically.
                try:self.b.ensure_closed(op)
                except Exception:
                    if op['snapshot']:op['writes_possible']=True
                    op['error']='closure_unconfirmed';op['phase']='failed_unconfirmed'
                self.s.save(op,'operator_decision_required')
            return self.public(op)
    def _snapshot(self,op):
        op['snapshot']=self.b.snapshot(op)
        self.s.save(op,'snapshot_verified')
    def accept(self,oid):
        with self.s.lock():
            previous=self.s.load_op(oid)
            if previous['phase']=='accepted':return self.public(previous)
            op=self._active(oid)
            if op['phase']=='accepted':return self.public(op)
            if op['phase']!='awaiting_accept':raise Refused('not_ready_for_accept')
            try:self.b.check_candidate(op)
            except Exception as ex:
                self._lost_gate(op,ex);raise
            # Durable BEFORE Caddy can admit any request. Never clear this flag.
            op['writes_possible']=True;op['phase']='opening';self.s.save(op,'write_boundary_crossed')
            try:
                self.b.open_gate(op)
                op['phase']='accepted';self.s.save(op,'access_opened')
                atomic(self.s.root/'active.json',{'operation_id':None})
            except Exception:
                try:self.b.ensure_closed(op)
                except Exception:pass
                op['phase']='opening_failed';op['error']='open_result_uncertain';self.s.save(op,'no_snapshot_rollback')
            return self.public(op)
    def recover(self,oid):
        with self.s.lock():
            op=self._active(oid)
            if op['writes_possible']:raise Refused('restore_after_write_boundary_denied')
            if op['phase']=='recovered_awaiting_open':return self.public(op)
            if op['phase'] in ['queued','accepted','recovered']:raise Refused('recovery_not_applicable')
            if op['snapshot']:
                try:self.b.assert_closed(op)
                except Exception as ex:
                    op['writes_possible']=True;self.s.save(op,'recovery_gate_unconfirmed_no_restore')
                    raise Refused('restore_requires_confirmed_closed_gate') from ex
            try:
                self.b.ensure_closed(op);self.b.stop_writers(op)
                op['phase']='recovering';self.s.save(op,'recovery_started')
                if op['snapshot']:
                    self.b.snapshot_failed_state(op)
                    self.b.restore(op)
                else:self.b.start_previous(op)
                self.b.check_previous(op)
                op['phase']='recovered_awaiting_open';self.s.save(op,'previous_healthy_gate_closed')
            except Exception as ex:
                self._lost_gate(op,ex)
                op['phase']='recovery_failed';op['error']=str(ex) if isinstance(ex,Refused) else 'recovery_failure'
                self.s.save(op,'gate_must_remain_closed')
            return self.public(op)
    def _lost_gate(self,op,ex):
        if op['snapshot'] and isinstance(ex,Refused) and str(ex) in ['maintenance_unconfirmed','neighbour_changed','closure_unconfirmed']:
            # A candidate may already have served writes. Reclosing cannot undo
            # that exposure, so the old snapshot must no longer be restored.
            op['writes_possible']=True;self.s.save(op,'gate_proof_lost_no_snapshot_restore')
    def reopen_previous(self,oid):
        with self.s.lock():
            previous=self.s.load_op(oid)
            if previous['phase']=='recovered':return self.public(previous)
            op=self._active(oid)
            if op['phase']!='recovered_awaiting_open':raise Refused('previous_not_ready')
            self.b.check_previous(op)
            op['writes_possible']=True;op['phase']='opening_previous';self.s.save(op,'write_boundary_crossed')
            try:
                self.b.open_gate(op,previous=True)
                op['phase']='recovered';self.s.save(op,'previous_access_opened')
                atomic(self.s.root/'active.json',{'operation_id':None})
            except Exception:
                try:self.b.ensure_closed(op)
                except Exception:pass
                op['phase']='opening_failed';op['error']='open_result_uncertain';self.s.save(op,'no_snapshot_rollback')
            return self.public(op)
    def _active(self,oid):
        if self.s.active()!=oid:raise Refused('not_active_operation')
        return self.s.load_op(oid)
    def status(self,oid):
        return self.public(self.s.load_op(oid))
    @staticmethod
    def public(op):
        return {k:op[k] for k in ['id','plan_id','plan_sha256','phase','writes_possible','error','events']}| {
            'candidate':op['plan']['view']['candidate'],
            'snapshot_verified':bool(op['snapshot']),
            'recovery_allowed':not op['writes_possible'] and op['phase'] not in ['queued','recovered','aborted']}
