"""Shared CI archive validation; never extracts or loads an archive."""
import hashlib,json,re,tarfile
from pathlib import PurePosixPath

def catalogs(tar,manifest,data,seen):
    layers=manifest.get('Layers')
    if not isinstance(layers,list) or any(not isinstance(n,str) or n not in seen or not tar.getmember(n).isfile() for n in layers):
        raise ValueError('delivery_layers_invalid')
    def small(name):
        member=tar.getmember(name)
        if not member.isfile() or member.size>2*1024**2:raise ValueError('delivery_catalog_oversized')
        raw=tar.extractfile(member).read();return raw,json.loads(raw)
    if 'repositories' in seen:
        _,repos=small('repositories');repo,tag=data['tag'].split(':',1)
        if not isinstance(repos,dict) or set(repos)!={repo} or not isinstance(repos[repo],dict) or set(repos[repo])!={tag}:
            raise ValueError('delivery_repository_tags_mismatch')
    if 'index.json' in seen or 'oci-layout' in seen:
        if not {'index.json','oci-layout'}<=seen:raise ValueError('delivery_oci_incomplete')
        _,layout=small('oci-layout');_,index=small('index.json')
        entries=index.get('manifests')
        if layout!={'imageLayoutVersion':'1.0.0'} or not isinstance(entries,list) or len(entries)!=1:
            raise ValueError('delivery_oci_multiple_images')
        entry=entries[0];digest=entry.get('digest','');annotations=entry.get('annotations',{})
        if (not re.fullmatch('sha256:[a-f0-9]{64}',digest)
                or annotations.get('org.opencontainers.image.ref.name')!=data['tag'].split(':',1)[1]
                or annotations.get('io.containerd.image.name') not in [data['tag'],'docker.io/library/'+data['tag']]):
            raise ValueError('delivery_oci_tags_mismatch')
        raw,oci=small('blobs/sha256/'+digest[7:])
        if 'sha256:'+hashlib.sha256(raw).hexdigest()!=digest or len(raw)!=entry.get('size'):
            raise ValueError('delivery_oci_manifest_digest')
        oci_layers=oci.get('layers')
        if (oci.get('config',{}).get('digest')!=data['image_id'] or not isinstance(oci_layers,list)
                or ['blobs/sha256/'+item.get('digest','')[7:] for item in oci_layers]!=layers):
            raise ValueError('delivery_oci_config_or_layers_mismatch')

def verify_delivery(archive,report):
    data=dict(report) if isinstance(report,dict) else json.loads(report.read_text(encoding='utf-8'))
    required=['source_sha','image_id','tag','archive_sha256','archive_bytes']
    if any(k not in data for k in required):raise ValueError('delivery_manifest_missing_fields')
    if not re.fullmatch('[a-f0-9]{40}',data['source_sha']) or not re.fullmatch('sha256:[a-f0-9]{64}',data['image_id']):raise ValueError('delivery_identity_invalid')
    if data['tag']!='platforma-gateway:evening-'+data['source_sha'][:12]:raise ValueError('delivery_tag_mismatch')
    if archive.stat().st_size!=data['archive_bytes'] or archive.stat().st_size>2*1024**3:raise ValueError('delivery_size_invalid')
    with archive.open('rb') as f:digest=hashlib.file_digest(f,'sha256').hexdigest()
    if digest!=data['archive_sha256']:raise ValueError('delivery_archive_changed')
    if any(data.get(k) is not False for k in ['production_configuration_included','database_included','private_skill_mount_included']):
        raise ValueError('delivery_isolation_not_declared')
    with tarfile.open(archive,'r:gz') as tar:
        total=10240;seen=set()
        for item in tar:
            p=PurePosixPath(item.name);total+=512+((item.size+511)//512)*512
            if (p.is_absolute() or p.as_posix()!=item.name or '..' in p.parts or '\\' in item.name or item.name in seen or item.issparse()
                    or not(item.isfile() or item.isdir()) or item.size<0 or total>4*1024**3 or len(seen)>=4096):
                raise ValueError('delivery_archive_structure_invalid')
            seen.add(item.name)
        member=tar.getmember('manifest.json')
        if member.size>65536:raise ValueError('delivery_manifest_oversized')
        value=json.load(tar.extractfile(member))
        if len(value)!=1 or value[0].get('RepoTags')!=[data['tag']]:raise ValueError('delivery_unexpected_tags')
        member=tar.getmember(value[0]['Config'])
        if member.size>2*1024**2:raise ValueError('delivery_config_oversized')
        raw=tar.extractfile(member).read();config=json.loads(raw)
        if 'sha256:'+hashlib.sha256(raw).hexdigest()!=data['image_id']:raise ValueError('delivery_config_digest_mismatch')
        if config['config'].get('User')!='10001:10001' or config['config'].get('Labels',{}).get('org.opencontainers.image.revision')!=data['source_sha']:
            raise ValueError('delivery_installed_source_mismatch')
        catalogs(tar,value[0],data,seen)
    return {'verified':True,**{k:data[k] for k in required},'deployment_performed':False,
            'expanded_archive_bytes':total,'import_required_bytes':2*total+64*1024**2,
            'import_estimate':'two_uncompressed_copies_plus_metadata; reserve checked on host'}
