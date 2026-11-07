"""Read-only Linux capacity gate. No shell, Docker, sync, cleanup or quota writes.

Btrfs ABI: Linux include/uapi/linux/btrfs.h and fs/btrfs/sysfs.c.
MVP supports flat, consistent full qgroups. A hierarchy is refused, not ignored.
"""
import errno,os,platform,re,struct,uuid
from pathlib import Path
from .core import Refused

MIB=1024**2
RESERVE_BYTES=128*MIB
MAX_REQUIRED_BYTES=9*1024**3
FS_INFO=0x8400941f
INO_LOOKUP=0xd0009412

def number(path):
    with Path(path).open('rb') as stream:raw=stream.read(65)
    if len(raw)>64 or not re.fullmatch(rb'[0-9]+\n?',raw):raise Refused('storage_counter_invalid')
    value=int(raw)
    if value>2**64-1:raise Refused('storage_counter_invalid')
    return value

def identity(fd):
    import fcntl
    fs=bytearray(1024);fcntl.ioctl(fd,FS_INFO,fs,True)
    lookup=bytearray(4096);struct.pack_into('=QQ',lookup,0,0,256)
    fcntl.ioctl(fd,INO_LOOKUP,lookup,True)
    subvolume=struct.unpack_from('=Q',lookup)[0]
    if subvolume<5:raise Refused('storage_subvolume_invalid')
    return str(uuid.UUID(bytes=bytes(fs[16:32]))),subvolume

def filesystem(path):
    """Longest matching mount; escaped mount points remain paths, never code."""
    def unescape(value):
        return re.sub(r'\\(040|011|012|134)',lambda m:chr(int(m[1],8)),value)
    rows=[]
    with Path('/proc/self/mountinfo').open() as stream:
        for index,line in enumerate(stream):
            if index>=8192 or len(line)>65536:raise Refused('storage_mount_table_limit')
            before,sep,after=line.partition(' - ')
            fields=before.split();tail=after.split()
            if not sep or len(fields)<6 or len(tail)<3:raise Refused('storage_mount_table_invalid')
            mount=Path(unescape(fields[4]))
            if path==mount or mount in path.parents:
                rows.append((len(mount.parts),index,tail[0],set(fields[5].split(','))|set(tail[2].split(','))))
    if not rows:raise Refused('storage_mount_unresolved')
    _,_,kind,options=max(rows,key=lambda row:(row[0],row[1]))
    return kind,options

def qgroup(path,fsid,subvolume,sysfs=Path('/sys/fs/btrfs')):
    groups=sysfs/fsid/'qgroups'
    # Missing/inaccessible quota state is not evidence of unlimited capacity.
    enabled=number(groups/'enabled')
    if enabled!=1:raise Refused('storage_btrfs_quota_not_verified')
    if number(groups/'inconsistent')!=0:raise Refused('storage_btrfs_inconsistent')
    if (groups/'mode').read_text().strip()!='qgroup':raise Refused('storage_btrfs_mode_unsupported')
    entries=[]
    for index,item in enumerate(groups.iterdir()):
        if index>=4096:raise Refused('storage_qgroup_scan_limit')
        if item.is_dir():
            if not re.fullmatch(r'0_[0-9]+',item.name):raise Refused('storage_btrfs_hierarchy_unverified')
            entries.append(item.name)
    leaf=groups/('0_'+str(subvolume))
    if leaf.name not in entries:raise Refused('storage_qgroup_missing')
    def counters():
        return {key:number(leaf/key) for key in ['limit_flags','referenced','exclusive','max_referenced',
          'max_exclusive','rsv_data','rsv_meta_pertrans','rsv_meta_prealloc']}
    readings=[counters(),counters()]
    available=[]
    for data in readings:
        flags=data['limit_flags']
        if flags&~3:raise Refused('storage_btrfs_limit_flags_unsupported')
        reserved=sum(data[key] for key in ['rsv_data','rsv_meta_pertrans','rsv_meta_prealloc'])
        for flag,used,limit in [(1,'referenced','max_referenced'),(2,'exclusive','max_exclusive')]:
            if flags&flag:
                available.append(max(0,data[limit]-data[used]-reserved))
    if (number(groups/'inconsistent')!=0 or number(groups/'enabled')!=1
            or (groups/'mode').read_text().strip()!='qgroup'):
        raise Refused('storage_btrfs_state_changed')
    if sorted(entries)!=sorted(x.name for x in groups.iterdir() if x.is_dir()):
        raise Refused('storage_btrfs_state_changed')
    return {'kind':'btrfs_flat_qgroup','fsid':fsid,'subvolume':subvolume,
      'quota_available_bytes':min(available) if available else None,'readings':readings,
      'hierarchy_verified':'no_parent_groups_present','reservations_subtracted':True}

def inspect(path):
    if os.name!='posix' or platform.system()!='Linux' or platform.machine() not in ['x86_64','aarch64']:
        raise Refused('storage_linux_abi_required')
    path=Path(path)
    if not path.is_absolute() or path.resolve(strict=True)!=path or not path.is_dir():
        raise Refused('storage_path_invalid')
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        stat=os.fstatvfs(fd);physical=stat.f_bavail*stat.f_frsize
        try:
            fsid,subvolume=identity(fd)
        except OSError as ex:
            if ex.errno!=errno.ENOTTY:raise Refused('storage_btrfs_identity_unavailable') from None
            kind,options=filesystem(path)
            if kind not in ['ext4','xfs'] or any('quota' in opt and opt not in ['noquota'] for opt in options):
                raise Refused('storage_filesystem_quota_unverified')
            detail={'kind':'filesystem_only','filesystem':kind,'quota_available_bytes':None,
                    'quota_scope':'no_quota_mount_option_observed'}
        else:
            detail=qgroup(path,fsid,subvolume)
            if identity(fd)!=(fsid,subvolume):raise Refused('storage_identity_changed')
        cap=detail['quota_available_bytes']
        return {'path':str(path),'filesystem_available_bytes':physical,
                'effective_available_bytes':min(physical,cap) if cap is not None else physical,**detail}
    finally:os.close(fd)

def check(path,required_bytes,reserve_bytes=RESERVE_BYTES):
    if (type(required_bytes) is not int or not 0<required_bytes<=MAX_REQUIRED_BYTES or
            type(reserve_bytes) is not int or not RESERVE_BYTES<=reserve_bytes<=1024*MIB):
        raise Refused('storage_budget_invalid')
    try:
        result=inspect(path)
    except Refused as ex:
        return {'sufficient':False,'status':'unknown','reason':str(ex),'writes_performed':False}
    except (OSError,ValueError):
        return {'sufficient':False,'status':'unknown','reason':'storage_read_unavailable','writes_performed':False}
    sufficient=result['effective_available_bytes']>=required_bytes+reserve_bytes
    return {**result,'sufficient':sufficient,'status':'sufficient' if sufficient else 'insufficient',
      'required_bytes':required_bytes,'reserve_bytes':reserve_bytes,
      'reason':None if sufficient else 'storage_insufficient_capacity','writes_performed':False,
      'capacity_reserved':False}

def require(path,required_bytes):
    result=check(path,required_bytes)
    if not result['sufficient']:raise Refused(result['reason'])
    return result
