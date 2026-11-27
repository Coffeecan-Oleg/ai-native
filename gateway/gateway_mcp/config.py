import json
import os
import re
from pathlib import Path
from typing import Any


def root() -> Path:
    return Path(__file__).resolve().parents[1]


def repo_root() -> Path:
    return root()


def public_url() -> str:
    return os.getenv(
        "GATEWAY_PUBLIC_URL", os.getenv("GATEWAY_ISSUER_URL", "http://localhost:8000")
    ).rstrip("/")


IMAGE_REVISION_FILE = Path('/usr/local/share/gateway-release/source-revision')


def release_sha() -> str:
    """New images identify themselves; an old compose value cannot mask rollout."""
    if IMAGE_REVISION_FILE.exists():
        try:
            with IMAGE_REVISION_FILE.open(encoding='ascii') as handle:
                value=handle.read(81).strip()
        except (OSError,UnicodeError):
            return 'unknown'
        return value if re.fullmatch('[0-9a-f]{40}',value) else 'unknown'
    value=os.getenv('GATEWAY_RELEASE_SHA','dev')
    return value if re.fullmatch(r'[A-Za-z0-9+._-]{1,512}',value) else 'dev'


def resolve_configured_path(env_name: str, default: str) -> Path:
    raw = os.getenv(env_name, default)
    path = Path(raw)
    if not path.is_absolute():
        cwd_path = (Path.cwd() / path).resolve()
        if cwd_path.exists():
            return cwd_path
        path = (root() / path).resolve()
    return path


def tools_file() -> Path:
    return resolve_configured_path("GATEWAY_TOOLS_FILE", "gateway-tools.json")


def factory_projects_file() -> Path:
    return resolve_configured_path(
        "GATEWAY_FACTORY_PROJECTS_FILE",
        "gateway-factory-projects.json",
    )


def read_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    return json.loads(path.read_text(encoding="utf-8"))
