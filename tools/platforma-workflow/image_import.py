#!/usr/bin/python3
"""Development-only, verified image import. Never starts or removes images."""
import base64,contextlib,hashlib,json,os,re,signal,stat,subprocess,sys,tempfile,uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from delivery import verify_delivery
from release_control.core import Refused,Store,atomic,read
from release_control.linux import ENV,trusted,validate_development_scope
from release_control.storage import require as require_storage

FIELDS={'source_sha','image_id','tag','archive_sha256','archive_bytes',
        'production_configuration_included','database_included','private_skill_mount_included'}

def metadata(value):
    if not isinstance(value,dict) or set(value)!=FIELDS:raise Refused('import_metadata_fields')
    for key,pattern in [('source_sha','[a-f0-9]{40}'),('image_id','sha256:[a-f0-9]{64}'),
                        ('archive_sha256','[a-f0-9]{64}')]:
        if not isinstance(value[key],str) or not re.fullmatch(pattern,value[key]):raise Refused('import_metadata_identity')
    if value['tag']!='platforma-gateway:evening-'+value['source_sha'][:12]:raise Refused('import_tag_mismatch')
    if type(value['archive_bytes']) is not int or not 0<value['archive_bytes']<=2*1024**3:
        raise Refused('import_archive_size')
    if any(value[key] is not False for key in FIELDS if key.endswith('_included')):
        raise Refused('import_corporate_data_declaration')
    return value

def decode(value):
    if not re.fullmatch('[A-Za-z0-9_=-]{1,4096}',value):raise Refused('import_metadata_encoding')
    try:return metadata(json.loads(base64.urlsafe_b64decode(value)))
    except (ValueError,UnicodeError,TypeError):raise Refused('import_metadata_invalid') from None

@contextlib.contextmanager
def host_lock(path):
    import fcntl
    fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode&0o077:raise Refused('import_lock_unsafe')
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise Refused('import_busy') from None
        yield
    finally:os.close(fd)

def receive(stream,target,expected):
    size=0;digest=hashlib.sha256()
    with target.open('xb') as output:
        os.chmod(target,0o600)
        while size<expected:
            chunk=stream.read(min(1024**2,expected-size))
            if not chunk:raise Refused('import_archive_truncated')
            output.write(chunk);digest.update(chunk);size+=len(chunk)
        if stream.read(1):raise Refused('import_archive_oversized')
        output.flush();os.fsync(output.fileno())
    return digest.hexdigest()

class DockerBackend:
    def __init__(self,base):self.base=base
    def run(self,args,timeout=30):
        try:p=subprocess.run(args,capture_output=True,timeout=timeout,env=ENV)
        except (OSError,subprocess.TimeoutExpired):raise Refused('import_command_unavailable_or_timeout') from None
        if p.returncode or len(p.stdout)>2*1024**2:raise Refused('import_command_failed')
        return p.stdout
    def docker(self,*args,**kw):return self.run(['/usr/bin/docker',*args],**kw)
    def guard(self):
        cfg=read(trusted(self.base/'config/release.json'));validate_development_scope(cfg)
        if Path(cfg['project_dir'])!=self.base/'app' or Path(cfg['state_dir'])!=self.base/'state':
            raise Refused('import_development_layout')
        if read(self.base/'state/active.json').get('operation_id') is not None:raise Refused('import_release_active')
        value=self.run(['/usr/bin/systemctl','show','--property=ActiveState','--value',cfg['worker_service']])
        if value.strip()!=b'inactive':raise Refused('import_worker_active')
    def capacity(self,path,required):return require_storage(path,required)
    def runtime(self):
        ids=self.docker('ps','-aq').decode().split()
        if len(ids)>128:raise Refused('import_container_count_limit')
        fmt='{"id":{{json .Id}},"image":{{json .Image}},"status":{{json .State.Status}},"started":{{json .State.StartedAt}},"restarts":{{json .RestartCount}}}'
        rows=self.docker('inspect','--format',fmt,*ids).splitlines() if ids else []
        return {'containers':sorted((json.loads(row) for row in rows),key=lambda row:row['id']),
                'volumes':sorted(self.docker('volume','ls','--quiet').decode().split()),
                'configuration':{name:hashlib.sha256(trusted(self.base/name).read_bytes()).hexdigest()
                    for name in ['config/release.json','app/compose.yaml','app/.env']}}
    def image(self,tag):
        found=self.docker('image','ls','--quiet','--no-trunc','--filter','reference='+tag).decode().split()
        if not found:return None
        value=json.loads(self.docker('image','inspect','--format',
            '{"id":{{json .Id}},"user":{{json .Config.User}},"revision":{{json (index .Config.Labels "org.opencontainers.image.revision")}}}',tag))
        return value
    def load(self,archive):
        # A timeout or lost SSH is an UNKNOWN result, never an automatic retry.
        self.docker('image','load','--quiet','--input',str(archive),timeout=480)

def matches(image,expected):
    return image=={'id':expected['image_id'],'user':'10001:10001','revision':expected['source_sha']}

def perform(base,value,stream,backend):
    """Caller holds the shared bootstrap and release Store locks throughout."""
    expected=metadata(value);state=base/'state';marker=state/'image-import-active.json'
    if marker.exists() or marker.is_symlink():raise Refused('image_import_unresolved')
    backend.guard();before=backend.runtime()
    existing=backend.image(expected['tag'])
    if existing is not None and not matches(existing,expected):raise Refused('import_existing_tag_conflict')
    # Bound gzip staging first; after validation separately recheck import space.
    backend.capacity(state,expected['archive_bytes']+8*1024**2)
    parent=state/'image-imports';parent.mkdir(mode=0o700,exist_ok=True);trusted(parent,True)
    opdir=parent/('import-'+uuid.uuid4().hex);opdir.mkdir(mode=0o700)
    archive=opdir/'image.tar.gz';journal=opdir/'operation.json'
    result={'id':opdir.name,'environment':'dev','phase':'receiving','tag':expected['tag'],
            'image_id':expected['image_id'],'archive_sha256':expected['archive_sha256'],
            'production_changed':False,'deployment_performed':False,'docker_load_performed':False}
    atomic(journal,result);atomic(marker,{'id':result['id'],'phase':'receiving'})
    try:
        if receive(stream,archive,expected['archive_bytes'])!=expected['archive_sha256']:
            raise Refused('import_archive_changed')
        try:verified=verify_delivery(archive,expected)
        except (ValueError,KeyError,TypeError,UnicodeError):raise Refused('import_archive_validation_failed') from None
        result['delivery']=verified;result['phase']='verified';atomic(journal,result)
        backend.guard()
        current=backend.image(expected['tag'])
        if current is not None and not matches(current,expected):raise Refused('import_existing_tag_conflict')
        if current is None:
            result['capacity']=backend.capacity(Path('/var/lib/docker'),verified['import_required_bytes'])
            if backend.runtime()!=before:raise Refused('import_runtime_changed_before_load')
            result['phase']='loading';result['docker_load_started']=True
            atomic(journal,result);atomic(marker,{'id':result['id'],'phase':'loading'})
            backend.load(archive);result['docker_load_performed']=True
        if not matches(backend.image(expected['tag']),expected):raise Refused('import_image_identity_mismatch')
        if backend.runtime()!=before:raise Refused('import_runtime_changed_after_load')
        result.update(phase='accepted',verified=True,already_present=current is not None,existing_runtime_unchanged=True)
        atomic(journal,result);marker.unlink()
        return result
    except BaseException as ex:
        uncertain=result['phase']=='loading'
        result.update(phase='unknown' if uncertain else 'refused',verified=False,
                      reason=str(ex) if isinstance(ex,Refused) else 'import_internal_error')
        atomic(journal,result)
        if uncertain:atomic(marker,{'id':result['id'],'phase':'unknown'})
        else:marker.unlink()
        raise Refused('import_outcome_unknown' if uncertain else result['reason']) from None
    finally:
        if archive.exists():archive.unlink()

def deadline(signum,frame):raise Refused('import_interrupted_or_timeout')

def main(args):
    if os.geteuid()!=0 or os.uname().nodename!='kromin':raise Refused('development_host_or_operator_required')
    if len(args)!=2 or args[0]!='import':raise Refused('import_command_not_allowed')
    expected=decode(args[1]);base=trusted(Path('/opt/platforma-dev'),True)
    trusted(base/'state',True)
    signal.signal(signal.SIGALRM,deadline);signal.signal(signal.SIGTERM,deadline);signal.signal(signal.SIGHUP,deadline)
    signal.alarm(600)
    try:
        with host_lock(Path('/var/tmp/platforma-dev-bootstrap.lock')),Store(base/'state').lock():
            return perform(base,expected,sys.stdin.buffer,DockerBackend(base))
    finally:signal.alarm(0)

if __name__=='__main__':
    try:print(json.dumps(main(sys.argv[1:])))
    except Exception as ex:
        print(json.dumps({'ok':False,'reason':str(ex) if isinstance(ex,Refused) else 'import_internal_error',
                          'production_changed':False,'deployment_performed':False}));raise SystemExit(1)
