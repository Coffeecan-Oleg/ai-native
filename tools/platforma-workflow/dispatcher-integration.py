"""Merge into existing ROOT-OWNED dispatcher; this is NOT its replacement.

Apply guard_legacy before existing mutators. _worker is systemd-only.
"""
from contextlib import contextmanager
from release_control.core import Refused
READ_ONLY={'status','version','logs','counts','env-check','images','help'}
def guard_legacy(tokens,store):
    if not tokens:raise Refused('command_required')
    if tokens[0] in ['deploy','rollback','restart']:raise Refused('use_release_workflow')
    if tokens[0]=='db' and tokens[1:] and tokens[1] in ['apply','rollback','restore']:
        raise Refused('manual_database_recovery_requires_operator')
    if store.active():
        if tokens[0] in READ_ONLY:return
        if tokens in [['skills','list'],['skills','history'],['files'],['files','list']]:return
        if tokens[:2] in [['skills','show'],['files','show'],['files','get']]:return
        raise Refused('active_release_blocks_legacy_mutation')
def release_tokens(tokens):
    sizes={'plan':2,'apply':3,'current':1,'resume':2,'status':2,'report':2,'accept':2,'recover':2,'reopen-previous':2}
    if not tokens or tokens[0] not in sizes or len(tokens)!=sizes[tokens[0]]:
        raise Refused('release_command_not_allowed')
    return ['/usr/bin/python3','-I','/usr/local/libexec/platforma-release/main.py',*tokens]

@contextmanager
def legacy_guard(tokens,store):
    """Wrap the WHOLE original handler to avoid a check/use race with apply."""
    read_only=(tokens and tokens[0] in READ_ONLY) or tokens in [
        ['skills','list'],['skills','history'],['files'],['files','list']]
    if read_only:
        guard_legacy(tokens,store);yield;return
    with store.lock():
        guard_legacy(tokens,store);yield
