"""Read only the corporate profile's exact shared document catalogue.

This capability never resolves a personal space, enumerates unrelated groups,
creates an index or inherits an old email/login membership.
"""
from gateway_mcp.services import corporate_access as access
from gateway_mcp.services import knowledge
from gateway_mcp.services.policy import has_scope


def _catalogue(actor, tool, space=access.SPACE):
    if not access.enabled() or space != access.SPACE:
        raise PermissionError('corporate_knowledge_space_denied')
    fresh = access.fresh_actor(actor.subject, ceiling=actor.scope_ceiling, tools=actor.tool_allowlist)
    if not has_scope(fresh, 'knowledge:read'):
        raise PermissionError('missing required scope: knowledge:read')
    if fresh.tool_allowlist is not None and tool not in fresh.tool_allowlist:
        raise PermissionError('corporate_knowledge_tool_denied')
    profile = access.profile(fresh)
    if space not in profile.get('spaces', []):
        raise PermissionError('corporate_knowledge_space_denied')
    root = knowledge.vault_root() / 'spaces' / 'organizational-awareness'
    # Resolve before reading, including a potentially substituted space symlink.
    expected = knowledge.vault_root().resolve() / 'spaces' / 'organizational-awareness'
    if root.resolve() != expected or not root.is_dir():
        raise PermissionError('corporate_knowledge_root_unavailable')
    return root.resolve(), profile['documents'][space]


def _document(root, documents, document):
    if document not in documents:
        raise PermissionError('corporate_knowledge_document_denied')
    path = knowledge._safe_child(root, document)
    if not path.is_file():
        raise FileNotFoundError('corporate_knowledge_document_unavailable')
    current = root
    for part in document.split('/'):
        current = current / part
        if current.is_symlink():
            raise PermissionError('corporate_knowledge_symlink_denied')
    payload = knowledge._document_payload(root_path=root, doc_path=path, include_content=True)
    payload['space'] = access.SPACE
    return payload


def _space(root, documents):
    # No members.yaml or unapproved README content is exposed as space metadata.
    return {'space': access.SPACE, 'type': 'space', 'slug': 'organizational-awareness',
            'title': 'Общие знания Платформы', 'role': 'reader',
            'documents': sum(knowledge._safe_child(root, name).is_file() for name in documents)}


def list_spaces(*, actor, include_personal=False):
    root, documents = _catalogue(actor, 'gateway_knowledge_space_list')
    return [_space(root, documents)]


def space_info(*, actor, space=''):
    root, documents = _catalogue(actor, 'gateway_knowledge_space_get', space)
    return _space(root, documents)


def get_document(*, actor, document, space=''):
    root, documents = _catalogue(actor, 'gateway_knowledge_document_get', space)
    return _document(root, documents, document)


def search_documents(*, actor, query='', space='', limit=10, include_personal=True, include_groups=True):
    root, documents = _catalogue(actor, 'gateway_knowledge_search', space)
    needle = query.casefold().strip()
    results = []
    # Filter at the read boundary, before ranking and limit. Never enumerate vault files.
    for name in sorted(documents):
        item = _document(root, documents, name)
        content = item.pop('content', '')
        haystack = '\n'.join([name, str(item.get('title', '')), content]).casefold()
        if needle and needle not in haystack:
            continue
        item['score'] = haystack.count(needle) if needle else 1
        item['snippet'] = knowledge._snippet(content, needle)
        results.append(item)
    results.sort(key=lambda item: (item['score'], str(item.get('updated_at', '')), item['path']), reverse=True)
    return results[:max(1, min(int(limit), 50))]
