"""Stable local archive copy and one fixed dev SSH import command."""
import base64,hashlib,json,shlex,subprocess,tempfile
from delivery import verify_delivery

def command(ssh_config,report):
    from image_import import metadata
    value=metadata(report)
    token=base64.urlsafe_b64encode(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).decode()
    return ['ssh','-T','-F',str(ssh_config),'aiport',shlex.join(['sudo','-n','/usr/bin/python3','-B','-I',
                '/opt/platforma-dev/code/image_import.py','import',token])]

def import_image(archive,report,ssh_config):
    from workflow import read_storage
    from image_import import FIELDS
    data=json.loads(report.read_text(encoding='utf-8'))
    verified=verify_delivery(archive,report)
    fields={k:data[k] for k in FIELDS}
    argv=command(ssh_config,fields)
    # Freeze exactly the verified bytes before opening the network connection.
    with tempfile.TemporaryFile('w+b') as stable:
        digest=hashlib.sha256();size=0
        with archive.open('rb') as source:
            while chunk:=source.read(1024**2):
                size+=len(chunk)
                if size>verified['archive_bytes']:raise ValueError('import_local_archive_changed')
                stable.write(chunk);digest.update(chunk)
        if size!=verified['archive_bytes'] or digest.hexdigest()!=verified['archive_sha256']:
            raise ValueError('import_local_archive_changed')
        stable.flush();stable.seek(0)
        # Extra space covers the remote gzip staging file as well as import.
        preflight,code=read_storage(ssh_config,verified['import_required_bytes']+verified['archive_bytes'])
        if code!=0 or preflight['capacity']['sufficient'] is not True:raise ValueError('import_storage_refused')
        try:p=subprocess.run(argv,stdin=stable,capture_output=True,timeout=660)
        except (OSError,subprocess.TimeoutExpired):raise ValueError('import_outcome_unknown_do_not_retry') from None
    if len(p.stdout)>65536:raise ValueError('import_result_oversized_do_not_retry')
    try:result=json.loads(p.stdout)
    except (ValueError,UnicodeError):raise ValueError('import_outcome_unknown_do_not_retry') from None
    if not isinstance(result,dict):raise ValueError('import_outcome_unknown_do_not_retry')
    if p.returncode or result.get('verified') is not True or result.get('phase')!='accepted':
        reason=result.get('reason')
        safe=reason if isinstance(reason,str) and reason.startswith('import_') and len(reason)<96 and reason.replace('_','').isalnum() else 'import_not_accepted'
        raise ValueError(safe)
    if (result.get('environment')!='dev' or result.get('production_changed') is not False
            or result.get('deployment_performed') is not False or result.get('existing_runtime_unchanged') is not True
            or result.get('tag')!=verified['tag'] or result.get('image_id')!=verified['image_id']
            or result.get('archive_sha256')!=verified['archive_sha256']):raise ValueError('import_result_scope_mismatch')
    return result
