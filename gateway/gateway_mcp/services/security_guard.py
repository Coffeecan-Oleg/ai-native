"""Server-side policy core. Adapter must supply authenticated and resolved facts."""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json

@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str

def fingerprint(subject, project, resource, operation, payload):
    body = json.dumps([subject, project, resource, operation, payload], ensure_ascii=False,
                      sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(body.encode()).hexdigest()

def authorize(profile, *, subject, project, resource, operation, payload=None,
              resource_kind=None, skill_impact=None, approval_check=None, now=None):
    """Call before effects; default deny. Inputs are server-resolved, never client claims.

    approval_check receives a bound digest and approver subject, and must atomically
    validate/consume a server-stored approval. A bool from the request is not approval.
    profile must be loaded afresh from an owner-controlled authoritative store.
    """
    if profile.get('status') != 'active': return Decision(False, 'inactive_profile')
    if not subject or subject != profile.get('subject'): return Decision(False, 'subject_mismatch')
    if not project or project != profile.get('project'): return Decision(False, 'outside_project')
    now = now or datetime.now(timezone.utc)
    expires = profile.get('expires_at')
    if expires:
        try:
            expiry = datetime.fromisoformat(expires.replace('Z', '+00:00'))
            if expiry.tzinfo is None or now >= expiry: return Decision(False, 'expired')
        except (ValueError, TypeError): return Decision(False, 'invalid_expiry')
    if operation in profile.get('forbidden_operations', []): return Decision(False, 'forbidden_operation')
    grants = [g for g in profile.get('resources', []) if g.get('id') == resource
              and g.get('kind') == resource_kind and operation in g.get('operations', [])]
    if not resource or resource == '*' or not grants: return Decision(False, 'resource_or_operation_denied')
    if resource_kind == 'skill' and operation in ('update', 'delete') and profile.get('protect_existing_skills', True):
        return Decision(False, 'existing_skill_protected')
    needs_approval = operation in profile.get('approval_operations', [])
    if resource_kind == 'crm' and operation in ('create', 'update', 'delete'):
        if skill_impact not in ('none', 'affects_skills'): return Decision(False, 'impact_not_verified')
        needs_approval = needs_approval or skill_impact == 'affects_skills'
    if resource_kind == 'mail':
        if resource != profile.get('sending_mailbox'): return Decision(False, 'mailbox_denied')
    if needs_approval:
        approver = profile.get('approver_subject')
        if not approver or not approval_check: return Decision(False, 'approval_required')
        digest = fingerprint(subject, project, resource, operation, payload)
        if not approval_check(digest, approver): return Decision(False, 'approval_required')
    return Decision(True, 'allowed')
