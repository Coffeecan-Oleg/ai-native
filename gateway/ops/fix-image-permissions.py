"""Build-time only: immutable code readable by the dedicated Gateway group."""
import os,stat
from pathlib import Path
GROUP=10001
for root in [Path('/app'),Path('/usr/local/lib/python3.12/site-packages/gateway_mcp')]:
    if not root.is_dir() or root.is_symlink():raise RuntimeError('image_source_missing')
    for directory,dirs,files in os.walk(root,followlinks=False):
        for path in [Path(directory),*(Path(directory)/n for n in files)]:
            info=path.lstat()
            if stat.S_ISLNK(info.st_mode):continue
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise RuntimeError('unexpected_image_file_type')
            mode=0o750 if path.is_dir() else (0o640 | (0o110 if info.st_mode & 0o111 else 0))
            os.chown(path,0,GROUP);os.chmod(path,mode)
