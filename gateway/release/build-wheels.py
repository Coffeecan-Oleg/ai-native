"""Build hash-checked inputs offline and record resulting wheel artifacts."""
import hashlib,json,subprocess,sys
from pathlib import Path
dest=Path('/wheels');dest.mkdir(exist_ok=True)
if sys.argv[1]=='dependencies':
    inputs=sorted(Path('/inputs').iterdir())
    assert len(inputs)==73, 'Unexpected dependency input set'
    subprocess.run([sys.executable,'-m','pip','wheel','--no-index','--no-deps','--no-build-isolation','--wheel-dir',str(dest),*(str(p) for p in inputs)],check=True)
else:
    assert sys.argv[1]=='application'
    subprocess.run([sys.executable,'-m','pip','wheel','--no-index','--no-deps','--no-build-isolation','--wheel-dir',str(dest),'.'],check=True)
    wheels=sorted(dest.glob('*.whl'));assert len(wheels)==74
    manifest={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in wheels}
    (dest/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    (dest/'requirements.txt').write_text(''.join(f'/wheels/{name} --hash=sha256:{digest}\n' for name,digest in manifest.items()))
