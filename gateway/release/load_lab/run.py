"""Explicit, bounded SSH entrypoint for a disposable synthetic load lab."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import shlex
import subprocess
import tarfile


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ssh-config',type=Path,required=True)
    parser.add_argument('--host',default='aiport')
    parser.add_argument('--ssh',default='ssh')
    parser.add_argument('--image',required=True)
    parser.add_argument('--revision',required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--candidate-root',type=Path)
    parser.add_argument('--full-suite',action='store_true')
    args=parser.parse_args()
    # Local early guard supplements the independent remote host/image guard.
    from host import guard
    guard('kromin',800,2048,args.image,args.revision)
    if args.host.startswith('-'):parser.error('SSH alias cannot be an option')
    args.output.mkdir(parents=True,exist_ok=True)
    receipt=args.output/'load-receipt.json'
    if receipt.exists():parser.error('Choose a fresh output directory; preserve previous evidence')
    base=Path(__file__).resolve().parent;buf=io.BytesIO()
    with tarfile.open(fileobj=buf,mode='w:gz') as tar:
        for name in ['app.py','driver.py']:
            raw=(base/name).read_bytes();item=tarfile.TarInfo(name);item.size=len(raw);item.mode=0o644
            tar.addfile(item,io.BytesIO(raw))
        if args.candidate_root:
            for name in ['authorization_reader.py','storage_access.py','storage_oauth.py']:
                raw=(args.candidate_root/'gateway_mcp/services'/name).read_bytes()
                item=tarfile.TarInfo('overlay/'+name);item.size=len(raw);item.mode=0o644
                tar.addfile(item,io.BytesIO(raw))
    payload=buf.getvalue();digest=hashlib.sha256(payload).hexdigest()
    (args.output/'load-payload.tar.gz').write_bytes(payload)
    code=(base/'host.py').read_text(encoding='utf-8')
    command='sudo -n '+shlex.join(['python3','-c',code,args.image,args.revision,digest,'full' if args.full_suite else 'bursts'])
    try:
        process=subprocess.run([args.ssh,'-T','-o','BatchMode=yes','-F',str(args.ssh_config.resolve()),args.host,command],
            input=payload,capture_output=True,timeout=360)
    except subprocess.TimeoutExpired:
        raise SystemExit('SSH deadline exceeded: inspect labelled lab remotely; do not assume cleanup')
    (args.output/'transport-diagnostic.log').write_bytes(process.stderr)
    try:result=json.loads(process.stdout)
    except (ValueError,UnicodeDecodeError):
        (args.output/'setup-error.log').write_bytes(process.stdout+process.stderr)
        raise SystemExit('No receipt; inspect setup-error.log and remote labelled lab')
    receipt.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k not in ['samples','load']}))
    raise SystemExit(0 if process.returncode==0 and result.get('successful') else 1)

if __name__=='__main__':main()
