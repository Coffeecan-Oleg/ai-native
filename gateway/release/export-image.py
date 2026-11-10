"""Export an accepted source-only image artifact. Never deploys to a server."""
import argparse,gzip,hashlib,json,os,re,shutil,subprocess,tarfile
from pathlib import Path


def validate(acceptance,installed,sha):
    if not re.fullmatch('[0-9a-f]{40}',sha):raise ValueError('invalid_source_revision')
    if acceptance.get('successful') is not True or acceptance.get('source_sha')!=sha:
        raise ValueError('unaccepted_source')
    if installed.get('verified') is not True or installed.get('source_revision')!=sha:
        raise ValueError('installed_source_mismatch')
    if not re.fullmatch('sha256:[0-9a-f]{64}',acceptance.get('image','')):
        raise ValueError('invalid_image_id')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--reports',type=Path,required=True);parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args();sha=os.getenv('SOURCE_GIT_SHA','')
    acceptance=json.loads((args.reports/'acceptance.json').read_text());installed=json.loads((args.reports/'installed-image.json').read_text())
    validate(acceptance,installed,sha)
    args.out.mkdir(parents=True,exist_ok=False)
    tag='platforma-gateway:evening-'+sha[:12]
    image=json.loads(subprocess.check_output(['docker','image','inspect','gateway-corporate-ci']))[0]
    if image['Id']!=acceptance['image'] or image['Config']['Labels'].get('org.opencontainers.image.revision')!=sha:
        raise ValueError('docker_source_mismatch')
    subprocess.run(['docker','tag',acceptance['image'],tag],check=True,timeout=15)
    raw=args.out/'image.tar';archive=args.out/'image.tar.gz'
    subprocess.run(['docker','save','--output',str(raw),tag],check=True,timeout=180)
    if raw.stat().st_size>2*1024**3:raise ValueError('image_archive_too_large')
    with tarfile.open(raw,'r:') as tf:
        manifest=json.load(tf.extractfile('manifest.json'))
        if len(manifest)!=1 or manifest[0].get('RepoTags')!=[tag]:raise ValueError('unexpected_archive_tags')
    with raw.open('rb') as src,archive.open('wb') as dest:
        with gzip.GzipFile(filename='',mode='wb',fileobj=dest,mtime=0) as gz:shutil.copyfileobj(src,gz,1024*1024)
    raw.unlink() # Exact owned intermediate, never a directory or production path.
    with archive.open('rb') as src:
        digest=hashlib.file_digest(src,'sha256').hexdigest()
    report={'source_sha':sha,'image_id':acceptance['image'],'tag':tag,'archive_sha256':digest,
        'archive_bytes':archive.stat().st_size,'schema_migrations':installed['migrations'],
        'runtime_uid':installed['uid'],'production_configuration_included':False,'database_included':False,
        'private_skill_mount_included':False,'retention_days':7,'deployment':False}
    (args.out/'delivery.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report))


if __name__=='__main__':main()
