"""Read only the corporate profile's exact shared document catalogue.

This capability never resolves a personal space, enumerates unrelated groups,
creates an index or inherits an old email/login membership.
"""
from gateway_mcp.services import corporate_access as access
from gateway_mcp.services import knowledge
from gateway_mcp.services import bitrix_notes
from gateway_mcp.services.policy import has_scope
from gateway_mcp.services.text_search import SearchQuery
import re


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
            'documents': sum(knowledge._safe_child(root, name).is_file()
                             for name in documents if name not in bitrix_notes.PATHS),
            **({'document_count_scope': 'local_files'} if documents.keys() & bitrix_notes.PATHS.keys() else {})}


def list_spaces(*, actor, include_personal=False):
    root, documents = _catalogue(actor, 'gateway_knowledge_space_list')
    return [_space(root, documents)]


def space_info(*, actor, space=''):
    root, documents = _catalogue(actor, 'gateway_knowledge_space_get', space)
    return _space(root, documents)


def get_document(*, actor, document, space=''):
    root, documents = _catalogue(actor, 'gateway_knowledge_document_get', space)
    if document in documents and document in bitrix_notes.PATHS:
        return bitrix_notes.read(actor, space, document)
    return _document(root, documents, document)


def search_documents(*, actor, query='', space='', limit=10, include_personal=True, include_groups=True):
    root, documents = _catalogue(actor, 'gateway_knowledge_search', space)
    question = SearchQuery(query)
    results = []
    warning = None
    # Filter at the read boundary, before ranking and limit. Never enumerate vault files.
    for name in sorted(documents):
        if name in bitrix_notes.PATHS:
            if warning is None:
                try:
                    doc = bitrix_notes.read(actor, space, name)
                except PermissionError:
                    continue
                except bitrix_notes.SourceUnavailable as error:
                    warning = str(error)
                    continue
                hit = bitrix_notes.best_section(doc, question)
                if hit:
                    results.append(hit)
            continue
        try:
            item = _document(root, documents, name)
        except FileNotFoundError:
            # An approved path may not yet have a file on a new installation.
            # Permission errors are still handled by the normal denial path.
            continue
        content = item.pop('content', '')
        score, anchor = question.match(name + '\n' + str(item.get('title', '')), content)
        if not score:
            continue
        # The employee guides quote common questions. The passport is the source
        # for the actual registered identifiers, so rank a populated one first.
        if (name == 'awareness/project-profile.md'
                and question.terms & {'реквизит', 'инн', 'огрн', 'кпп'}
                and re.search(r'\b(?:ИНН|КПП|ОГРН)\s*[:—-]?\s*\d{9,15}\b', content, re.I)):
            score += 200
        item['score'] = score
        item['snippet'] = knowledge._snippet(content, anchor)
        results.append(item)
    results.sort(key=lambda item: (item['score'], str(item.get('updated_at', '')), item['path']), reverse=True)
    hits = results[:max(1, min(int(limit), 50))]
    payload = {'ok': True, 'count': len(hits), 'documents': hits}
    if warning:
        payload['partial'] = True
        payload['source_warning'] = warning
    return payload
