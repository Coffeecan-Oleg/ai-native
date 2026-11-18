"""Bounded advisory checks of an already authorized saved skill snapshot.

No filesystem traversal, network, execution, grant or publication side effects.
Only basic Markdown file targets are checked; anchors/HTML are not validated.
"""
import io,json,posixpath,re
from urllib.parse import unquote,urlsplit

from gateway_mcp.services import skill_library as library,skill_lifecycle as lifecycle
from gateway_mcp.services.skill_capabilities import validate_operations

MAX_ISSUES=50
MAX_LINKS=500
MAX_LINES=20000
MAX_LINE=8192
LEGACY_HOST='kromin.axion-test.akmetron.ru'
INLINE=re.compile(r'!?\[([^\[\]\n]{0,512})\]\([ \t]*(<[^>\n]{1,1024}>|[^)\s]{1,1024})(?:[ \t]+["\'][^)\n]{0,512}["\'])?[ \t]*\)')
DEFINITION=re.compile(r'^ {0,3}\[([^\[\]\n]{1,200})\]:[ \t]*(<[^>\n]{1,1024}>|[^\s]{1,1024})')
REFERENCE=re.compile(r'!?\[([^\[\]\n]{1,512})\]\[([^\[\]\n]{0,200})\]')
SHORTCUT=re.compile(r'!?\[([^\[\]\n]{1,200})\](?![\[(])')
FENCE=re.compile(r'^ {0,3}(`{3,}|~{3,})(.*)$')


def _label(text):return ' '.join(text.split()).casefold()


def _without_code(line):
    tokens=list(re.finditer(r'`+',line));following={};next_same={}
    for index in range(len(tokens)-1,-1,-1):
        width=len(tokens[index][0]);following[index]=next_same.get(width);next_same[width]=index
    out=[];cursor=0;index=0
    while index<len(tokens):
        end=following[index]
        if end is None:index+=1;continue
        start=tokens[index].start();stop=tokens[end].end()
        out.extend([line[cursor:start],' '*(stop-start)]);cursor=stop;index=end+1
    return ''.join(out)+line[cursor:]


def _escaped(text,start):
    count=0;start-=1
    while start>=0 and text[start]=='\\':count+=1;start-=1
    return count%2==1


def _markdown_links(lines):
    definitions={};uses=[]
    for number,line in lines:
        match=DEFINITION.match(line)
        if match and not match[1].startswith('^'):
            definitions.setdefault(_label(match[1]),match[2]);continue
        uses.append((number,line))
    for number,line in uses:
        covered=[]
        for match in INLINE.finditer(line):
            if _escaped(line,match.start()):continue
            covered.append(match.span());yield number,match[2],False
        for match in REFERENCE.finditer(line):
            if _escaped(line,match.start()) or any(a<=match.start()<b for a,b in covered) or match[1].startswith('^'):continue
            covered.append(match.span());key=_label(match[2] or match[1])
            yield number,definitions.get(key),key not in definitions
        for match in SHORTCUT.finditer(line):
            if _escaped(line,match.start()) or any(a<=match.start()<b for a,b in covered) or match[1].startswith('^'):continue
            key=_label(match[1])
            if key in definitions:yield number,definitions[key],False


def inspect_snapshot(payload):
    """Call only after lifecycle.detail authorized the actor and selected a snapshot."""
    lifecycle._verify(payload)
    files=payload['files'];issues=[];issue_count=0;attention=0;limited=False
    counts={'lines_checked':0,'links_seen':0,'local_file_links_checked':0,'external_links_unchecked':0,'anchors_unchecked':0}
    def issue(level,code,path='SKILL.md',line=1):
        nonlocal issue_count,attention
        issue_count+=1;attention+=level=='attention'
        if len(issues)<MAX_ISSUES:issues.append({'level':level,'code':code,'path':path,'line':line})
    # A missing optional frontmatter is valid. Only an explicitly declared version
    # can differ from the catalogue's canonical saved version.
    root=files['SKILL.md'].lstrip('\ufeff').replace('\r\n','\n')
    if root.startswith('---\n'):
        try:
            parsed=library.parse_upload('SKILL.md',files['SKILL.md'].encode('utf-8'))
            end=root.find('\n---\n',4,16384)
            if end>=0 and re.search(r'^version[ \t]*:',root[4:end],re.M) and parsed['version']!=payload['version']:
                issue('attention','version_mismatch')
        except ValueError:issue('attention','header_invalid')
    operations=[];requirements='missing'
    if 'requirements.json' in files:
        try:
            if len(files['requirements.json'].encode('utf-8'))>8192:raise ValueError('requirements_too_large')
            operations=validate_operations(json.loads(files['requirements.json']));requirements='valid'
        except (ValueError,TypeError,KeyError,RecursionError):
            requirements='invalid';issue('attention','requirements_invalid','requirements.json')
    for path,text in sorted(files.items()):
        visible=[];fence=None
        for number,line in enumerate(io.StringIO(text,newline=None),1):
            if counts['lines_checked']>=MAX_LINES:limited=True;break
            counts['lines_checked']+=1
            if LEGACY_HOST in line.casefold():issue('info','legacy_address',path,number)
            if not path.casefold().endswith('.md'):continue
            if len(line)>MAX_LINE:limited=True;continue
            mark=FENCE.match(line)
            if fence:
                if mark and mark[1][0]==fence[0] and len(mark[1])>=fence[1] and not mark[2].strip():fence=None
                continue
            if mark:fence=(mark[1][0],len(mark[1]));continue
            if line.startswith(('    ','\t')):continue
            line=_without_code(line)
            html=INLINE.sub(lambda match:' '*len(match[0]),line)
            if re.search(r'<(?:a|img)(?=[\s/>])',html,re.I):issue('info','html_links_unchecked',path,number)
            visible.append((number,line))
        for number,target,missing_reference in _markdown_links(visible):
            if counts['links_seen']>=MAX_LINKS:limited=True;break
            counts['links_seen']+=1
            if missing_reference:issue('attention','reference_missing',path,number);continue
            if target.startswith('<') and target.endswith('>'):target=target[1:-1]
            try:parts=urlsplit(target)
            except ValueError:issue('attention','link_invalid',path,number);continue
            if parts.scheme or parts.netloc:
                counts['external_links_unchecked']+=1
                if parts.scheme and parts.scheme.casefold() not in {'http','https','mailto'}:issue('attention','link_scheme_unchecked',path,number)
                continue
            if not parts.path:counts['anchors_unchecked']+=1;continue
            target=unquote(parts.path)
            if target.startswith('/'):
                issue('info','server_path_unchecked',path,number);continue
            normalized=posixpath.normpath(posixpath.join(posixpath.dirname(path),target))
            try:library.safe_path(normalized)
            except ValueError:issue('attention','link_outside_package',path,number);continue
            counts['local_file_links_checked']+=1
            if normalized not in files:issue('attention','file_missing',path,number)
            if parts.fragment:counts['anchors_unchecked']+=1
    if limited:issue('attention','scan_limit')
    return {'snapshot_revision':payload.get('revision'),'version':payload['version'],'fingerprint':payload['fingerprint'],
        'files':len(files),'bytes':sum(spec['bytes'] for spec in payload['manifest'].values()),
        'issues':issues,'issue_count':issue_count,'attention_count':attention,'issues_truncated':issue_count>len(issues),
        'scan_complete':not limited,'requirements':requirements,'operations':operations,**counts,
        'network_calls':False,'permissions_checked':False,'publication_decision':'owner_review',
        'notice':'Проверяются базовые Markdown-ссылки на файлы, версия заголовка и формат декларации. Внешние страницы, якоря, права, безопасность смысла инструкции и качество ответа ИИ этим отчётом не подтверждаются.'}
